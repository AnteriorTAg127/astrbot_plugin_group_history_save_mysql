"""总结编排层（模块 H）：指令入口 + 校验链 + 全流程串联 + 错误兜底。

承接 main.py（模块 K）委托的 ``/消息总结``（数量模式）与 ``/消息总结时间``
（时间模式）两条指令，串联上游四模块：

- fetcher（模块 D）混合数据获取
- summarizer（模块 E）统计 + LLM 总结
- formatter（模块 F）消息链渲染（内部三级降级，绝不向上抛）
- storage + scheduler（模块 G）持久化与每日过期清理

校验链（两个 handler 共用，严格按序）：

1. 总开关 ``summary_enabled``
2. 群环境（``event.get_group_id()`` 非空）
3. 白名单（``summary_whitelist_mode == "whitelist"`` 且群不在列表 → 拒绝；
   mode=all 跳过）
4. 限流**检查**（用户/群双冷却，内存 dict + ``time.monotonic()``；只检查不盖章）
5. 参数解析校验（count ``^\\d+$`` / window ``^\\d+[hd]$``、>0、≤ 上限，
   超限拒绝并提示上限）
6. 限流**盖章**（参数校验通过、真正开始执行前才写时间戳，避免无效指令白耗冷却），
   盖章后立即发**触发反馈**（``summary_feedback_mode``：reaction 在触发消息贴 👍 /
   text 文字提示 / none 关闭；reaction 失败自动降级文字，配置项见
   ``summary_feedback_text``）

错误兜底（分级，任何异常不冒泡到 main.py handler）：

- :class:`SummaryProviderError` → 提示配置总结模型
- LLM / 其他异常 → ``logger.error(..., exc_info=True)`` + 「总结生成失败，请稍后重试」
- 提示性消息发送失败仅 warning，避免二次崩溃
- 触发反馈失败仅 warning，不影响主流程（绝不冒泡、绝不阻断总结）

框架 API 核实记录（AstrBot 主项目 ``astrbot/core/platform/astr_message_event.py``）：

- ``event.get_group_id() -> str``，非群组消息返回空字符串
- ``event.get_sender_id() -> str``（取 ``message_obj.sender.user_id``）
- ``event.plain_result(text) -> MessageEventResult``，而 ``MessageEventResult``
  **继承自** ``MessageChain``，故可直接传给签名为
  ``async def send(self, message: MessageChain)`` 的 ``event.send()``；
  全程 ``await send``，**绝不 yield**（保证可脱离事件钩子调用）
- ``StarTools.get_data_dir(plugin_name) -> Path``，导入路径与 db_config.py 一致

契约见 开发/v0.3/分工.md「接口约定 → SummaryService」，不得私改。
"""

from __future__ import annotations

import asyncio
import math
import re
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import StarTools

from .fetcher import HistoryFetcher
from .formatter import SummaryFormatter
from .scheduler import CleanupScheduler
from .storage import SummaryStorage
from .summarizer import Summarizer, SummaryProviderError

if TYPE_CHECKING:
    from astrbot.api.star import Context, Star

    from ..db_config import ConfigManager
    from ..db_mysql import MySQLManager
    from .models import FetchOutcome

# 插件名（与 db_config.py / web_api.py 一致），供 StarTools.get_data_dir 定位数据目录
PLUGIN_NAME = "astrbot_plugin_group_history_save_mysql"

# 参数校验正则：count 模式纯数字；window 模式数字 + h(小时)/d(天)
_COUNT_RE = re.compile(r"^\d+$")
_WINDOW_RE = re.compile(r"^(\d+)([hd])$")

# ---------- 提示性文案（统一经 plain_result + await send 发送） ----------
_MSG_DISABLED = "总结功能未启用，请在 Web 管理后台开启"
_MSG_NOT_GROUP = "请在群内使用"
_MSG_NOT_WHITELISTED = "本群未开启总结功能"
_MSG_EMPTY_MATERIAL = "该范围内没有可总结的消息"
_MSG_PROVIDER_ERROR = "未配置总结模型且无法获取会话模型，请在 Web 管理后台配置"
_MSG_GENERATION_FAILED = "总结生成失败，请稍后重试"
_MSG_TOO_FREQUENT = "操作太频繁，请 {remain} 秒后再试"
_COUNT_USAGE = "用法：/消息总结 <数量>，如 /消息总结 512"
_WINDOW_USAGE = "用法：/消息总结时间 <时长>，如 /消息总结时间 24h 或 1d"

# 冷却/上限配置的最终兜底值（配置层已含回退，此处仅防御异常类型）
_DEFAULT_USER_COOLDOWN = 60
_DEFAULT_GROUP_COOLDOWN = 120
_DEFAULT_MAX_COUNT = 1000
_DEFAULT_MAX_HOURS = 168

# ---------- 触发反馈（指令生效后即时确认，见 SummaryService._send_feedback） ----------
_DEFAULT_FEEDBACK_TEXT = "📝 收到！正在总结中，请稍候…"
_REACTION_ACTION = "set_msg_emoji_like"
# 协议端要求参数名为 emoji_id；值取 👍 的 Unicode 码点（十进制字符串），
# 非 CQ 小表情 id（NapCat 按 ID 长度区分：>3 位走 type-2 Unicode 表情路径）
_REACTION_EMOJI_ID = "128077"


class SummaryService:
    """总结编排服务：指令入口、校验链、全流程串联与错误兜底。

    Attributes:
        storage: 总结持久化器。公开以便 main.py（模块 K）复用同一实例注入
            WebAPI（模块 I）；base_dir 为
            ``StarTools.get_data_dir(PLUGIN_NAME) / "summaries"``。
    """

    def __init__(
        self,
        context: Context,
        config_mgr: ConfigManager,
        mysql_mgr: MySQLManager,
        star: Star,
    ) -> None:
        """构造服务并在内部组装上游四模块与清理调度器。"""
        self._context = context
        self._config_mgr = config_mgr
        self._star = star
        # 上游四模块实例（构造签名以各模块实际交付版本为准）
        self._fetcher = HistoryFetcher(mysql_mgr, config_mgr)
        self._summarizer = Summarizer(context, config_mgr)
        self._formatter = SummaryFormatter(star, config_mgr)
        # Web 导出图片用的 T2I 渲染器（与 formatter 内部同一构造参数；局部导入
        # 避免与 formatter 的循环导入——formatter 模块内部同样局部导入本类）
        from .t2i_render import T2IRenderer

        self.renderer = T2IRenderer(star, config_mgr)
        self.storage = SummaryStorage(StarTools.get_data_dir(PLUGIN_NAME) / "summaries")
        self._scheduler = CleanupScheduler(self.storage, config_mgr)
        # 限流时间戳表（monotonic 时钟，仅内存不持久化）：
        # 检查与盖章分离——_precheck 只检查；_stamp_cooldown 在参数校验通过后盖章
        self._user_last: dict[str, float] = {}
        self._group_last: dict[str, float] = {}

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """启动过期总结清理调度器（storage 与 scheduler 已在 __init__ 内部构造）。"""
        try:
            await self._scheduler.start()
        except Exception:
            logger.error("[HistorySummary] 启动清理调度器失败", exc_info=True)

    async def stop(self) -> None:
        """停止清理调度器：cancel + await，吞 CancelledError。"""
        try:
            await self._scheduler.stop()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.warning("[HistorySummary] 停止清理调度器异常", exc_info=True)

    # ------------------------------------------------------------------
    # 指令入口（main.py 薄 handler 委托于此）
    # ------------------------------------------------------------------

    async def handle_count_command(self, event: AstrMessageEvent, arg: str) -> None:
        """数量模式：/消息总结 <数量>。

        校验链（总开关 → 群环境 → 白名单 → 限流检查 → 参数校验 → 限流盖章）
        通过后执行 fetcher → summarizer → sources 注入 → storage.save →
        formatter.render → ``await event.send``。异常分级兜底，绝不冒泡。
        """
        try:
            prechecked = await self._precheck(event)
            if prechecked is None:
                return
            group_id, sender_id = prechecked

            count = await self._parse_count_arg(event, arg)
            if count is None:
                return
            self._stamp_cooldown(sender_id, group_id)
            await self._send_feedback(event)

            outcome = await self._fetcher.fetch_by_count(
                group_id=group_id, event=event, count=count
            )
            await self._run_summary(event, group_id, outcome, f"最近 {count} 条消息")
        except SummaryProviderError:
            await self._reply(event, _MSG_PROVIDER_ERROR)
        except Exception:
            # 最外层兜底：任何异常不得冒泡到 main.py handler 之外
            logger.error("[HistorySummary] 数量总结指令处理异常", exc_info=True)
            await self._reply(event, _MSG_GENERATION_FAILED)

    async def handle_window_command(self, event: AstrMessageEvent, arg: str) -> None:
        """时间模式：/消息总结时间 <时长>（h=小时 / d=天）。校验链同数量模式。"""
        try:
            prechecked = await self._precheck(event)
            if prechecked is None:
                return
            group_id, sender_id = prechecked

            parsed = await self._parse_window_arg(event, arg)
            if parsed is None:
                return
            delta, scope_desc = parsed
            self._stamp_cooldown(sender_id, group_id)
            await self._send_feedback(event)

            window_end = datetime.now()
            outcome = await self._fetcher.fetch_by_window(
                group_id=group_id,
                event=event,
                window_start=window_end - delta,
                window_end=window_end,
            )
            await self._run_summary(event, group_id, outcome, scope_desc)
        except SummaryProviderError:
            await self._reply(event, _MSG_PROVIDER_ERROR)
        except Exception:
            logger.error("[HistorySummary] 时间总结指令处理异常", exc_info=True)
            await self._reply(event, _MSG_GENERATION_FAILED)

    # ------------------------------------------------------------------
    # 校验链（步骤 1-4，两个 handler 共用）
    # ------------------------------------------------------------------

    async def _precheck(self, event: AstrMessageEvent) -> tuple[str, str] | None:
        """执行校验链步骤 1-4，全部通过返回 ``(group_id, sender_id)``，否则 None。

        任一失败分支直接发送提示消息并返回 None。步骤 4 限流**只检查不盖章**，
        盖章在参数校验通过后由 _stamp_cooldown 完成。
        """
        # 1. 总开关（typed 读取已含类型回退，非 True 一律视为未启用）
        if not await self._config_mgr.get_summary_setting_typed("summary_enabled"):
            await self._reply(event, _MSG_DISABLED)
            return None

        # 2. 群环境（get_group_id 对非群消息返回空串）
        group_id = event.get_group_id()
        if not group_id:
            await self._reply(event, _MSG_NOT_GROUP)
            return None

        # 3. 白名单（仅 mode == "whitelist" 校验列表；mode=all 及其他值跳过）
        mode = await self._config_mgr.get_summary_setting("summary_whitelist_mode")
        if mode.strip().lower() == "whitelist":
            whitelist = await self._config_mgr.get_summary_setting_typed(
                "summary_group_whitelist"
            )
            allowed = (
                {str(item).strip() for item in whitelist}
                if isinstance(whitelist, list)
                else set()
            )
            if group_id not in allowed:
                await self._reply(event, _MSG_NOT_WHITELISTED)
                return None

        # 4. 限流检查（只检查，不盖章）
        # sender_id 兜底：get_sender_id 在 user_id 非 str 时返回空串，
        # 退化用会话 ID 作冷却键，保证限流维度不失效
        sender_id = event.get_sender_id() or f"session:{event.get_session_id()}"
        if not await self._check_cooldown(event, sender_id, group_id):
            return None
        return group_id, sender_id

    async def _check_cooldown(
        self, event: AstrMessageEvent, sender_id: str, group_id: str
    ) -> bool:
        """用户/群双冷却检查：任一仍在冷却 → 提示剩余秒数（向上取整）并返回 False。"""
        now = time.monotonic()
        user_cd = await self._int_setting(
            "summary_user_cooldown", _DEFAULT_USER_COOLDOWN
        )
        last = self._user_last.get(sender_id)
        if last is not None and now - last < user_cd:
            remain = math.ceil(user_cd - (now - last))
            await self._reply(event, _MSG_TOO_FREQUENT.format(remain=remain))
            return False
        group_cd = await self._int_setting(
            "summary_group_cooldown", _DEFAULT_GROUP_COOLDOWN
        )
        last = self._group_last.get(group_id)
        if last is not None and now - last < group_cd:
            remain = math.ceil(group_cd - (now - last))
            await self._reply(event, _MSG_TOO_FREQUENT.format(remain=remain))
            return False
        return True

    def _stamp_cooldown(self, sender_id: str, group_id: str) -> None:
        """盖限流时间戳（用户 + 群各记当前 monotonic）。

        仅在参数校验通过、真正开始执行前调用，避免非法参数等无效指令白耗冷却。
        """
        now = time.monotonic()
        self._user_last[sender_id] = now
        self._group_last[group_id] = now

    # ------------------------------------------------------------------
    # 参数解析校验（步骤 5）：失败发提示并返回 None，不查库不调 LLM
    # ------------------------------------------------------------------

    async def _parse_count_arg(self, event: AstrMessageEvent, arg: str) -> int | None:
        """count 参数：``^\\d+$`` 且 >0；超过 summary_max_count 拒绝并提示上限。"""
        stripped = arg.strip()
        if not _COUNT_RE.match(stripped) or int(stripped) <= 0:
            await self._reply(event, _COUNT_USAGE)
            return None
        count = int(stripped)
        max_count = await self._int_setting("summary_max_count", _DEFAULT_MAX_COUNT)
        if count > max_count:
            await self._reply(event, f"最大支持 {max_count} 条，请调整参数")
            return None
        return count

    async def _parse_window_arg(
        self, event: AstrMessageEvent, arg: str
    ) -> tuple[timedelta, str] | None:
        """window 参数：``^\\d+[hd]$``（h=小时/d=天）且 >0；换算小时数超上限拒绝。

        Returns:
            (时间窗口长度, 范围描述「最近 X 小时」/「最近 X 天」)；
            参数非法或超限 → 发送对应提示并返回 None。
        """
        matched = _WINDOW_RE.match(arg.strip())
        if not matched or int(matched.group(1)) <= 0:
            await self._reply(event, _WINDOW_USAGE)
            return None
        value = int(matched.group(1))
        unit = matched.group(2)
        hours = value if unit == "h" else value * 24
        max_hours = await self._int_setting("summary_max_hours", _DEFAULT_MAX_HOURS)
        if hours > max_hours:
            await self._reply(
                event,
                f"最大支持 {max_hours} 小时（{max_hours // 24} 天），请调整参数",
            )
            return None
        if unit == "h":
            return timedelta(hours=value), f"最近 {value} 小时"
        return timedelta(days=value), f"最近 {value} 天"

    # ------------------------------------------------------------------
    # 执行流程（校验通过后）
    # ------------------------------------------------------------------

    async def _run_summary(
        self,
        event: AstrMessageEvent,
        group_id: str,
        outcome: FetchOutcome,
        scope_desc: str,
    ) -> None:
        """fetch 结果 → 总结 → 注入 sources → 持久化 → 渲染 → 发送。"""
        if not outcome.messages:
            if outcome.onebot_error:
                # 空素材且 OneBot 补齐失败：日志补记降级原因，不阻断提示
                logger.info(
                    "[HistorySummary] 范围内无可总结素材（OneBot 补齐失败原因: %s）",
                    outcome.onebot_error,
                )
            await self._reply(event, _MSG_EMPTY_MATERIAL)
            return

        output_mode = (
            await self._config_mgr.get_summary_setting("summary_output_mode")
        ).strip() or "forward"

        result = await self._summarizer.summarize(
            event, outcome.messages, scope_desc, output_mode
        )
        # E（summarizer）返回的 result.sources 是空 dict，必须注入 fetcher 的
        # 数据源构成：storage 落盘与 formatter 统计节点都要消费该字段
        result.sources = outcome.sources

        try:
            await self.storage.save(group_id, result)
        except Exception:
            # 保存失败仅 warning，不阻断消息发送
            logger.warning(
                "[HistorySummary] 总结结果持久化失败，不阻断发送", exc_info=True
            )

        chain = await self._formatter.render(result, output_mode)
        await event.send(chain)

    # ------------------------------------------------------------------
    # 触发反馈（限流盖章后、fetch/LLM 前的即时确认）
    # ------------------------------------------------------------------

    async def _send_feedback(self, event: AstrMessageEvent) -> None:
        """指令生效后即时反馈：reaction 贴表情（失败降级文字）/ text 文字 / none 关闭。

        模式取 ``summary_feedback_mode``（strip/lower 归一）；未知值（配置污染）
        记 warning 且不发消息，避免意外打扰。任何异常仅 warning，
        绝不冒泡、绝不阻断主流程。
        """
        try:
            mode = (
                (await self._config_mgr.get_summary_setting("summary_feedback_mode"))
                .strip()
                .lower()
            )
            if mode == "none":
                return
            if mode == "text":
                await self._reply(event, await self._feedback_text())
                return
            if mode == "reaction":
                if await self._react_to_trigger(event):
                    return
                # 贴表情失败 → 降级文字提示，保证用户始终收到确认
                await self._reply(event, await self._feedback_text())
                return
            logger.warning(
                "[HistorySummary] 未知的 summary_feedback_mode: %r，跳过触发反馈",
                mode,
            )
        except Exception:
            logger.warning("[HistorySummary] 触发反馈失败，不影响主流程", exc_info=True)

    async def _feedback_text(self) -> str:
        """反馈文案：读 ``summary_feedback_text``；用户清空（空白）回退内置默认。"""
        text = (
            await self._config_mgr.get_summary_setting("summary_feedback_text")
        ).strip()
        return text or _DEFAULT_FEEDBACK_TEXT

    async def _react_to_trigger(self, event: AstrMessageEvent) -> bool:
        """经 OneBot 扩展 action 在触发消息上贴 👍（``set_msg_emoji_like``）。

        成功返回 True；任何失败返回 False（warning 日志），失败路径共四种：
        无 message_id / 取不到协议端 client / message_id 无法转 int /
        协议端不支持该 action（抛 ActionFailed 等）——均由调用方降级文字。
        client 获取方式参照 onebot.py 的防御式写法（getattr 取 bot、
        hasattr 检查 api）。
        """
        message_obj = getattr(event, "message_obj", None)
        message_id = getattr(message_obj, "message_id", None)
        if not message_id:
            logger.warning(
                "[HistorySummary] 触发消息缺少 message_id，贴表情不可用，降级文字提示"
            )
            return False
        client = getattr(event, "bot", None)
        if client is None or not hasattr(client, "api"):
            logger.warning(
                "[HistorySummary] 贴表情失败：取不到协议端 client"
                "（event.bot 为空或非 aiocqhttp 平台），降级文字提示"
            )
            return False
        try:
            message_id_int = int(str(message_id))
        except (TypeError, ValueError):
            logger.warning(
                "[HistorySummary] 贴表情失败：message_id 无法转为整数（%r），"
                "降级文字提示",
                message_id,
            )
            return False
        try:
            await client.api.call_action(
                _REACTION_ACTION,
                message_id=message_id_int,
                emoji_id=_REACTION_EMOJI_ID,
            )
        except Exception:
            logger.warning(
                "[HistorySummary] 贴表情失败（协议端可能不支持 %s），降级文字提示",
                _REACTION_ACTION,
                exc_info=True,
            )
            return False
        return True

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    async def _reply(self, event: AstrMessageEvent, text: str) -> None:
        """发送提示性消息：plain_result + await send（绝不 yield）。

        ``MessageEventResult`` 是 ``MessageChain`` 子类，可直接传给
        ``event.send``。发送失败仅 warning，避免错误分支二次崩溃。
        """
        try:
            await event.send(event.plain_result(text))
        except Exception:
            logger.warning("[HistorySummary] 发送提示消息失败: %s", text, exc_info=True)

    async def _int_setting(self, key: str, default: int) -> int:
        """读取 int 配置（get_summary_setting_typed 已含回退，此处最终防御异常类型）。"""
        value = await self._config_mgr.get_summary_setting_typed(key)
        if isinstance(value, bool) or not isinstance(value, int):
            return default
        return value
