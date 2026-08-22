"""群聊记录存储插件主入口。

监听 QQ 群消息，将文本和图片分别存入 MySQL，提供管理指令和 Web 管理后台。
本文件仅保留框架交互（指令注册 / 事件监听 / 生命周期）与薄 handler：

- v0.6.0 起消息保存逻辑（解析/缓冲/落库/补录）全部委托给 core.saver.MessageSaver
- v0.8.1 起实例装配 / 后台 MySQL 初始化 / 生命周期停机全部委托给
  core.bootstrap.PluginBootstrap（各服务经 self.* 透出保持访问路径不变）
- v0.8.1 起历史管理类指令体（history_*/补库）委托给 core.commands.GroupCommands

注册性内容（@register 类装饰、@filter.* 指令/事件装饰与签名、terminate 直接
定义于类体）一律不迁移——加载器在 main.py 指定读取。
"""

import asyncio

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.star.filter.command import GreedyStr

from .core.bootstrap import PluginBootstrap
from .core.parsing import stats_fallback_text
from .core.profile.capture import extract_at_targets
from .core.stats import StatsBuildError
from .core.stats.models import StatsQuery
from .core.stats.parser import USAGE_TEXT, StatsParseError, parse_stats_args


@register(
    "astrbot_plugin_group_history_save_mysql",
    "AnteriorTAg127",
    "将 QQ 群聊天记录保存到 MySQL，支持 Web 管理后台与群聊历史自动总结（MySQL 优先 + 协议端补齐）；人物分析支持群成员发言习惯与画像分析（@ 或 QQ 触发，Web 可跨群）；数据分析支持 Web 实时统计面板与 /群统计 指令报告卡（定时日报/周报推送 + 分段快照统计）",
    "0.8.0",
)
class GroupHistoryPlugin(Star):
    """群聊记录存储插件。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config
        # v0.8.1 实例装配全部下沉 core.bootstrap；本类仅保存 bootstrap 并把
        # 各服务透出到同名属性（self.mysql_mgr 等），保持既有 handler 与
        # 外部访问路径不变
        self.bootstrap = PluginBootstrap(self, context, config)
        self.mysql_mgr = self.bootstrap.mysql_mgr
        self.config_mgr = self.bootstrap.config_mgr
        self.cleaner = self.bootstrap.cleaner
        self.summary_service = self.bootstrap.summary_service
        self.profile_service = self.bootstrap.profile_service
        self.stats_service = self.bootstrap.stats_service
        self.web_api = self.bootstrap.web_api
        self.saver = self.bootstrap.saver
        self.backfill = self.bootstrap.backfill
        self.commands = self.bootstrap.commands

        self._init_task: asyncio.Task | None = None

    async def initialize(self):
        """异步初始化：连接数据库、启动定时任务（委托 bootstrap 后台执行）。"""
        await self.bootstrap.initialize()
        self._init_task = self.bootstrap._init_task

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def on_group_message(self, event: AstrMessageEvent):
        """监听群消息：按群触发自动补库（重启后首条消息）并保存到 MySQL。

        先交给 ReloadBackfill.maybe_trigger（每群一次、幂等；若该群补库进行中，
        其消息经 saver 门控缓冲、补库完成后带去重 flush），再委托 MessageSaver
        处理实时消息。
        """
        try:
            await self.backfill.maybe_trigger(event)
            await self.saver.handle_group_message(event)
        except Exception as e:
            logger.error(f"[HistorySave] 处理群消息时出错: {e}", exc_info=True)

    @filter.command("history_start")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def history_start(self, event: AstrMessageEvent, group_id: str = ""):
        """开启指定群的聊天记录保存。

        用法: /history_start [群号]
        不填群号则默认为当前群。
        """
        yield event.plain_result(await self.commands.group_start(event, group_id))

    @filter.command("history_stop")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def history_stop(self, event: AstrMessageEvent, group_id: str = ""):
        """关闭指定群的聊天记录保存。

        用法: /history_stop [群号]
        不填群号则默认为当前群。
        """
        yield event.plain_result(await self.commands.group_stop(event, group_id))

    @filter.command("history_status")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def history_status(self, event: AstrMessageEvent):
        """查询聊天记录保存的状态。"""
        yield event.plain_result(await self.commands.group_status(event))

    @filter.command("history_clean")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def history_clean(self, event: AstrMessageEvent, days: str = ""):
        """手动清理过期的图片记录。

        用法: /history_clean [天数]
        不填天数则使用配置的默认值。
        """
        yield event.plain_result(await self.commands.group_clean(event, days))

    @filter.command("补库")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def force_backfill(
        self, event: AstrMessageEvent, group_id: str = "", hours: str = ""
    ):
        """强制对指定群补库（管理员，可随时触发一次，绕过「每群每重启一次」限制）。

        用法: /补库 [群号] [小时数]
        不填群号默认当前群；不填小时数按「该群最后记录时间 − 5 分钟」窗口补，
        填小时数则强制回补最近 N 小时（如 /补库 123456 24）。
        """
        yield event.plain_result(
            await self.commands.group_backfill(event, group_id, hours)
        )

    @filter.command("消息总结", alias={"总结"})
    async def summary_count(self, event: AstrMessageEvent, arg: str = ""):
        """按条数总结群聊记录。用法: /消息总结 <数量>，如 /消息总结 512"""
        await self.summary_service.handle_count_command(event, arg)

    @filter.command("消息总结时间", alias={"总结时间"})
    async def summary_window(self, event: AstrMessageEvent, arg: str = ""):
        """按时间范围总结群聊记录。用法: /消息总结时间 <时长>，如 /消息总结时间 24h 或 1d"""
        await self.summary_service.handle_window_command(event, arg)

    @filter.command("人物分析", alias={"人物画像", "分析TA"})
    async def profile_analyze(self, event: AstrMessageEvent, arg: str = ""):
        """分析群成员发言习惯与人物画像。用法: /人物分析 [@成员 或 QQ号]"""
        await self.profile_service.handle_command(event, arg)

    @filter.command("群统计", alias={"群数据", "统计"})
    async def group_stats(self, event: AstrMessageEvent, arg: GreedyStr):
        """群/个人数据统计图片报告卡。

        用法: /群统计 [@某人 | QQ号] [时间范围]（参数顺序自由，均可省略）
        时间范围: 今日（默认）/ 昨日 / 7天 / 30天 / 全部 / 单日或区间日期

        参数注解用 ``GreedyStr``（框架内置指令同款写法）接收指令名之后的
        **全部剩余文本**：普通 ``arg: str`` 注入只给第一个空格分词，会把
        「2026-08-01 2026-08-04」空格分隔日期区间截断成单日。
        """
        try:
            # 仅群环境有效：私聊回用法提示
            group_id = event.get_group_id()
            if not group_id:
                yield event.plain_result(f"请在群内使用。\n{USAGE_TEXT}")
                return

            service = self.stats_service
            if service is None:
                yield event.plain_result("统计模块尚未就绪")
                return

            # 每群冷却（async，时长读 stats_cooldown）：冷却期内静默忽略——
            # 不回复，仅经 finally 的 stop_event 阻止指令文本流入 LLM
            if not await service.check_cooldown(str(group_id)):
                return

            # @ 目标：消息链提取（已剔除 "all"）后再剔除 bot 自身（写法同 profile）
            at_targets = extract_at_targets(event.get_messages())
            try:
                self_id = str(event.get_self_id() or "")
            except Exception:
                self_id = ""
            if self_id:
                at_targets = [qq for qq in at_targets if qq != self_id]

            # 解析指令参数（成员 + 时间范围）；失败回附带的用法文案
            try:
                member_id, time_range = parse_stats_args(arg or "", at_targets)
            except StatsParseError as e:
                yield event.plain_result(getattr(e, "usage", "") or USAGE_TEXT)
                return

            # 组装查询：top_n 读 stats_top_n 配置（typed 读取自带默认回退，
            # build_stats 内部再夹住 1–50 防御）
            query = StatsQuery(
                group_id=str(group_id),
                member_id=member_id,
                time_range=time_range,
                top_n=await self.config_mgr.get_stats_setting_typed("stats_top_n"),
            )
            try:
                data = await service.build_stats(query)
            except StatsBuildError as e:
                yield event.plain_result(str(e))
                return

            # 渲染报告卡：成功发图片；失败降级纯文本摘要
            image = await service.render(data, f"{group_id} 群聊统计")
            if image:
                yield event.image_result(image)
            else:
                yield event.plain_result(stats_fallback_text(data, time_range.label))
        except Exception as e:
            # 最外层兜底：任何异常不冒泡出 handler
            logger.error(f"[Stats] 群统计指令处理异常: {e}", exc_info=True)
            yield event.plain_result("统计失败，请稍后重试")
        finally:
            # 指令消息一律终止传播（含冷却静默路径），避免指令文本继续流入 LLM
            event.stop_event()

    async def terminate(self):
        """插件卸载/停用时清理资源（委托 bootstrap，LIFO 停机）。"""
        # 注意：terminate 必须直接定义于本类体（加载器按 star_cls_type.__dict__
        # 查找）；函数体委托 bootstrap.shutdown
        await self.bootstrap.shutdown()
