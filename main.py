"""群聊记录存储插件主入口。

监听 QQ 群消息，将文本和图片分别存入 MySQL，提供管理指令和 Web 管理后台。
v0.6.0 起本文件仅保留框架交互（指令注册 / 事件监听 / 生命周期），
消息保存逻辑（解析/缓冲/落库/补录）全部委托给 core.saver.MessageSaver。
"""

import asyncio

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.star.filter.command import GreedyStr

from .core.backfill import ReloadBackfill
from .core.cleaner import ImageCleaner
from .core.db_config import ConfigManager
from .core.db_mysql import MySQLManager
from .core.parsing import stats_fallback_text
from .core.profile.capture import extract_at_targets
from .core.profile.service import ProfileService
from .core.saver import MessageSaver
from .core.stats import StatsBuildError, StatsService
from .core.stats.models import StatsQuery
from .core.stats.parser import USAGE_TEXT, StatsParseError, parse_stats_args
from .core.summary import SummaryService
from .core.webapi import WebAPI

# 后台 MySQL 初始化的最大连续失败次数：超过后放弃重试并停用存储功能，
# 避免数据库长期不可用时无限刷日志。恢复方式：修正配置后在插件管理重启插件。
MAX_INIT_ATTEMPTS = 5


@register(
    "astrbot_plugin_group_history_save_mysql",
    "AnteriorTAg127",
    "将 QQ 群聊天记录保存到 MySQL，支持 Web 管理后台与群聊历史自动总结（MySQL 优先 + 协议端补齐）；人物分析支持群成员发言习惯与画像分析（@ 或 QQ 触发，Web 可跨群）；数据分析支持 Web 实时统计面板与 /群统计 指令报告卡（定时日报/周报推送 + 分段快照统计）",
    "0.6.0",
)
class GroupHistoryPlugin(Star):
    """群聊记录存储插件。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config

        # 初始化 MySQL 管理器（动态连接池）
        # config 可能为 None（框架在 _conf_schema.json 缺失时以 config=None 实例化插件），
        # 统一用空 dict 兜底，保证默认值生效
        cfg = config or {}
        self.mysql_mgr = MySQLManager(
            host=cfg.get("mysql_host", "127.0.0.1"),
            port=cfg.get("mysql_port", 3306),
            user=cfg.get("mysql_user", "root"),
            password=cfg.get("mysql_password", ""),
            database=cfg.get("mysql_database", "astrbot_history"),
            pool_min_size=cfg.get("pool_min_size", 1),
            pool_max_size=cfg.get("pool_max_size", 10),
            pool_idle_timeout=cfg.get("pool_idle_timeout", 120),
            pool_timeout=cfg.get("pool_timeout", 30),
            pool_ping_cooldown=cfg.get("pool_ping_cooldown", 5),
        )

        # 初始化本地配置管理器
        self.config_mgr = ConfigManager()

        # 初始化图片清理器
        self.cleaner = ImageCleaner(self.mysql_mgr, self.config_mgr)

        # 初始化总结服务（v0.3，须在 WebAPI 之前构造，以便注入其存储实例）
        self.summary_service = SummaryService(
            context, self.config_mgr, self.mysql_mgr, self
        )

        # 初始化人物分析服务（v0.4，须在 WebAPI 之前构造，以便注入服务与其存储实例）
        self.profile_service = ProfileService(
            context, self.config_mgr, self.mysql_mgr, self
        )

        # 初始化数据分析服务（v0.5.0，须在 WebAPI 之前构造，以便注入服务实例；
        # 构造仅组装上游模块引用无 I/O，调度器在 MySQL 初始化成功后才 start）
        self.stats_service = StatsService(
            context, self.mysql_mgr, self.config_mgr, self
        )

        # 初始化 Web API（注入总结存储实例供总结历史端点使用，注入人物分析服务与存储实例）
        self.web_api = WebAPI(
            context,
            self.mysql_mgr,
            self.config_mgr,
            self.cleaner,
            summary_storage=self.summary_service.storage,
            summary_renderer=self.summary_service.renderer,
            profile_service=self.profile_service,
            profile_storage=self.profile_service.storage,
            profile_renderer=self.profile_service.renderer,
            stats_service=self.stats_service,
        )

        # v0.6.0 消息保存逻辑委托（解析/缓冲/落库/补录全部在 core.saver）
        self.saver = MessageSaver(self.mysql_mgr, self.config_mgr, self.stats_service)
        # v0.6.0 重载自动补库服务（MySQL 初始化成功后从 OneBot 拉取历史补齐窗口缺口）
        self.backfill = ReloadBackfill(context, self.mysql_mgr, self.config_mgr)

        self._init_task: asyncio.Task | None = None

    async def initialize(self):
        """异步初始化：连接数据库、启动定时任务。

        MySQL 连接放入后台任务执行，避免数据库不可达时阻塞 AstrBot 启动。
        """
        # 初始化本地配置（aiosqlite 本地文件，极快，不会阻塞）
        config_ok = await self.config_mgr.initialize()
        if not config_ok:
            logger.error("[HistorySave] 本地配置初始化失败，插件功能受限")

        # MySQL 初始化放入后台，不阻塞框架启动
        self._init_task = asyncio.create_task(self._background_mysql_init())
        logger.info("[HistorySave] 插件已加载，MySQL 连接在后台初始化中")

    async def _background_mysql_init(self):
        """后台初始化 MySQL，失败时每 60 秒重试一次。

        连续失败 MAX_INIT_ATTEMPTS 次后放弃重试：关闭连接池、停用存储功能，
        避免数据库长期不可用时无限刷日志。每次尝试用 120 秒强制兜底。
        """
        retry_interval = 60
        # initialize() 内含 schema 迁移 DDL（大表 ADD INDEX 可能远超 10s），
        # 原 10s 超时会把 DDL 中途 cancel，导致连续 5 次失败后永久放弃存储；
        # 本任务为后台任务，放长超时不阻塞框架启动（F8）
        init_timeout = 120
        attempt = 0
        while not self.saver.is_initialized:
            attempt += 1
            try:
                mysql_ok = await asyncio.wait_for(
                    self.mysql_mgr.initialize(), timeout=init_timeout
                )
                if mysql_ok:
                    self.saver.set_initialized()
                    # 先补录初始化窗口内缓冲的消息，再启动定时清理
                    await self.saver.flush_pending()
                    await self.cleaner.start()
                    # 启动总结清理调度器（失败仅记日志，不阻断插件启动）
                    try:
                        await self.summary_service.start()
                    except Exception as e:
                        logger.error(f"[HistorySummary] 启动总结服务失败: {e}")
                    # 启动人物分析清理调度器（失败仅记日志，不阻断插件启动）
                    try:
                        await self.profile_service.start()
                    except Exception as e:
                        logger.error(f"[Profile] 启动人物分析服务失败: {e}")
                    # 启动数据分析调度器（失败仅记日志，不阻断插件启动）
                    try:
                        await self.stats_service.start()
                    except Exception as e:
                        logger.error(f"[Stats] 启动数据分析服务失败: {e}")
                    # v0.5.5 快照启动回填：MySQL 可用且 stats 服务已启动后发起后台
                    # 批量回填（服务层 create_task 自持句柄、terminate 自行取消，
                    # 幂等可重入）；失败仅记日志，不阻断插件加载
                    try:
                        await self.stats_service.startup_backfill()
                    except Exception as e:
                        logger.error(f"[HistorySave] 快照启动回填发起失败: {e}")
                    # v0.6.0 重载自动补库：MySQL 就绪后后台拉取历史补齐（失败仅记日志）
                    try:
                        await self.backfill.start()
                    except Exception as e:
                        logger.error(f"[HistorySave] 重载自动补库启动失败: {e}")
                    logger.info(
                        "[HistorySave] MySQL 连接成功，插件初始化完成，开始监听群消息"
                    )
                    return
                else:
                    logger.warning(
                        f"[HistorySave] MySQL 连接失败（第 {attempt} 次尝试），"
                        f"{retry_interval}s 后重试..."
                    )
            except asyncio.TimeoutError:
                logger.warning(
                    f"[HistorySave] MySQL 初始化超时（第 {attempt} 次尝试），"
                    f"{retry_interval}s 后重试..."
                )
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning(
                    f"[HistorySave] MySQL 初始化异常（第 {attempt} 次尝试）: {e}，"
                    f"{retry_interval}s 后重试..."
                )
            # 重试耗尽：放弃并停用存储功能（关闭连接池使后续操作快速失败）
            if attempt >= MAX_INIT_ATTEMPTS:
                # 明确记录被丢弃的缓冲消息条数，避免静默丢失无迹可查（F6）
                dropped = self.saver.mark_gave_up()
                logger.error(
                    f"[HistorySave] MySQL 连续 {attempt} 次连接失败，停止重试，"
                    f"消息存储功能已停用，同时丢弃启动窗口缓冲消息 {dropped} 条。"
                    f"请检查数据库配置与连通性，"
                    f"然后在插件管理中重启本插件以恢复。"
                )
                try:
                    await self.mysql_mgr.close()
                except Exception:
                    pass
                return
            # 等待后重试
            try:
                await asyncio.sleep(retry_interval)
            except asyncio.CancelledError:
                return

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def on_group_message(self, event: AstrMessageEvent):
        """监听群消息并保存到 MySQL（委托给 core.saver.MessageSaver）。"""
        try:
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
        target_group = self._resolve_group_id(event, group_id)
        if target_group is None:
            yield event.plain_result("请提供有效的群号，或在群内使用此指令。")
            return

        success = await self.config_mgr.add_group(target_group)
        if success:
            yield event.plain_result(f"已开启群 {target_group} 的聊天记录保存。")
        else:
            yield event.plain_result(f"开启群 {target_group} 记录失败，请检查日志。")

    @filter.command("history_stop")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def history_stop(self, event: AstrMessageEvent, group_id: str = ""):
        """关闭指定群的聊天记录保存。

        用法: /history_stop [群号]
        不填群号则默认为当前群。
        """
        target_group = self._resolve_group_id(event, group_id)
        if target_group is None:
            yield event.plain_result("请提供有效的群号，或在群内使用此指令。")
            return

        success = await self.config_mgr.remove_group(target_group)
        if success:
            yield event.plain_result(f"已关闭群 {target_group} 的聊天记录保存。")
        else:
            yield event.plain_result(f"关闭群 {target_group} 记录失败，请检查日志。")

    @filter.command("history_status")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def history_status(self, event: AstrMessageEvent):
        """查询聊天记录保存的状态。"""
        # 数据库连接状态
        ping = await self.mysql_mgr.ping()
        db_status = "✅ 已连接" if ping["connected"] else "❌ 未连接"
        latency = f"{ping['latency_ms']}ms" if ping["connected"] else "-"
        pool_info = ping.get("pool", {})
        pool_str = (
            f"{pool_info.get('used', 0)}活跃/"
            f"{pool_info.get('current_size', 0)}总计 "
            f"(范围 {pool_info.get('min_size', 1)}~{pool_info.get('max_size', 10)})"
        )

        # 统计信息
        stats = await self.mysql_mgr.get_stats()

        # 群列表
        groups = await self.config_mgr.get_groups()
        settings = await self.config_mgr.get_all_settings()
        all_mode = settings.get("all_mode", "false") == "true"

        enabled_groups = [g for g in groups if g["enabled"]]
        group_list = ", ".join(str(g["group_id"]) for g in enabled_groups) or "无"

        text = (
            f"📊 群聊记录存储状态\n"
            f"━━━━━━━━━━━━━━\n"
            f"数据库: {db_status} ({latency})\n"
            f"连接池: {pool_str}\n"
            f"ALL 模式: {'开启' if all_mode else '关闭'}\n"
            f"记录中的群: {group_list}\n"
            f"━━━━━━━━━━━━━━\n"
            f"今日消息: {stats.get('today_messages', 0)} 条\n"
            f"今日图片: {stats.get('today_images', 0)} 条\n"
            f"总消息: {stats.get('total_messages', 0)} 条\n"
            f"总图片: {stats.get('total_images', 0)} 条\n"
            f"━━━━━━━━━━━━━━\n"
            f"图片保留: {settings.get('image_retention_days', '3')} 天"
        )
        yield event.plain_result(text)

    @filter.command("history_clean")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def history_clean(self, event: AstrMessageEvent, days: str = ""):
        """手动清理过期的图片记录。

        用法: /history_clean [天数]
        不填天数则使用配置的默认值。
        """
        clean_days = None
        if days:
            try:
                clean_days = int(days)
                if clean_days < 1:
                    yield event.plain_result("天数不能小于 1。")
                    return
                if clean_days > 36500:
                    yield event.plain_result("天数过大（上限 36500 天）。")
                    return
            except ValueError:
                yield event.plain_result("请提供有效的天数（正整数）。")
                return

        deleted = await self.cleaner.manual_clean(clean_days)
        if deleted >= 0:
            if clean_days is not None:
                actual_days = clean_days
            else:
                # 配置值可能被篡改为非数字，兜底默认 3 天，避免指令抛异常
                try:
                    actual_days = int(
                        await self.config_mgr.get_setting("image_retention_days", "3")
                    )
                except (ValueError, TypeError):
                    actual_days = 3
            yield event.plain_result(
                f"清理完成：删除了 {deleted} 条 {actual_days} 天前的图片记录。"
            )
        else:
            yield event.plain_result("清理失败，请检查数据库连接。")

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

    def _resolve_group_id(
        self, event: AstrMessageEvent, group_id_str: str
    ) -> int | None:
        """解析目标群号：优先使用参数，否则使用当前群。"""
        if group_id_str:
            try:
                return int(group_id_str)
            except ValueError:
                return None
        # 尝试获取当前群号
        try:
            gid = event.get_group_id()
            if gid:
                return int(gid)
        except (ValueError, TypeError):
            pass
        return None

    async def terminate(self):
        """插件卸载/停用时清理资源。"""
        # 取消后台初始化任务（若仍在重试中）
        if self._init_task and not self._init_task.done():
            self._init_task.cancel()
            try:
                await self._init_task
            except asyncio.CancelledError:
                pass
        # v0.6.0 停止重载自动补库任务（吞 CancelledError 不阻塞后续清理）
        try:
            await self.backfill.stop()
        except asyncio.CancelledError:
            pass
        # 停止数据分析调度器（LIFO：最后启动的最先停止；吞 CancelledError 不阻塞后续清理）
        try:
            await self.stats_service.stop()
        except asyncio.CancelledError:
            pass
        # 停止总结清理调度器（与 cleaner 停止并列，吞 CancelledError 不阻塞后续清理）
        try:
            await self.summary_service.stop()
        except asyncio.CancelledError:
            pass
        # 停止人物分析清理调度器（与总结停止并列，吞 CancelledError 不阻塞后续清理）
        try:
            await self.profile_service.stop()
        except asyncio.CancelledError:
            pass
        await self.cleaner.stop()
        await self.mysql_mgr.close()
        await self.config_mgr.close()
        logger.info("[HistorySave] 插件已安全停止")
