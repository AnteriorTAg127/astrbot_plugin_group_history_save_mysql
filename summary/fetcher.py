"""混合数据获取层（模块 D）。

以 MySQL 为主、OneBot 协议端缓存为辅获取群聊历史素材，供总结引擎消费。
契约见 开发/v0.3/分工.md「接口约定 → HistoryFetcher」，对外仅两个公开方法：

- :meth:`HistoryFetcher.fetch_by_count`：数量模式（最近 N 条）
- :meth:`HistoryFetcher.fetch_by_window`：时间模式（时间窗口内）

设计要点：

- **MySQL 优先**：复用 ``MySQLManager.query_messages``（只读，不修改）。
  该方法固定 ``ORDER BY timestamp DESC``（新→旧），time_start/time_end
  期望 ``"YYYY-MM-DD HH:MM:SS"`` 字符串（SQL 中以 ``timestamp >= / <=`` 比较）。
  数量模式取第 1 页 page_size=N 即「最近 N 条」，归一化后反转为时间升序；
  时间模式按窗口查询，page_size 取 ``summary_max_count``（与总结引擎长度
  预算口径一致：超量时保留最近的消息）。
- **不足补齐**：数量模式 ``m < count × summary_min_mysql_ratio`` 时经
  ``summary/onebot.py`` 拉取 ``min(count - m, summary_onebot_max_fetch)`` 条；
  时间模式 ``m == 0`` 或「窗口内数据已取全（total <= m）且 MySQL 最早一条
  晚于 window_start + summary_gap_tolerance_minutes」时拉取
  ``summary_onebot_max_fetch`` 条。OneBot 结果在时间模式下再按窗口过滤。
- **过滤**（合并后统一执行）：非文本剔除（MySQL 侧按 content 非空 +
  message_type ∈ {text, mixed} 于行级完成；OneBot 侧 parse 时已过滤）、
  bot 自身消息剔除（``event.get_self_id()``，取不到则跳过并只告警一次）、
  忽略名单剔除（``config_mgr.get_ignore_senders``）。
- **去重**：有合法 ``message_id`` 的消息只按 message_id 主键去重；退化键
  ``(秒级时间戳, sender_id, content[:32])`` 仅对 message_id 为空的消息登记与
  检查，覆盖「两源同一消息但 message_id 为空」的交叉场景。设计取舍：宁可
  两侧各留一份重复（一侧有 id 一侧无 id 的同一消息可能双存），也不可借
  退化键误删合法消息——否则同一用户 1 秒内连发相同短消息（复读）时，
  第二条会因退化键撞车被误杀（v0.4.5 F12）。
- **容错**：MySQL 查询异常记 warning(exc_info=True) 后视为空结果继续；
  OneBot 失败捕获 ``OneBotHistoryError`` 记 warning 并写入 ``onebot_error``，
  两个公开方法不向上抛数据源异常。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from .models import ChatMessage, FetchOutcome, mysql_row_to_message
from .onebot import OneBotHistoryError, fetch_group_history

if TYPE_CHECKING:
    from ..db_config import ConfigManager
    from ..db_mysql import MySQLManager

# query_messages 的 time_start/time_end 期望格式（与表 timestamp 列字符串比较一致）
_TS_FORMAT = "%Y-%m-%d %H:%M:%S"

# 视为「含文本」的 message_type 取值（insert_message 约定仅 text/mixed 入文本表，
# 纯图片不落库；此处兼作脏数据防御）
_TEXT_MESSAGE_TYPES = frozenset({"text", "mixed"})


class HistoryFetcher:
    """混合历史获取器：MySQL 优先 + OneBot 补齐 + 过滤去重合并。"""

    def __init__(self, mysql_mgr: MySQLManager, config_mgr: ConfigManager) -> None:
        self._mysql_mgr = mysql_mgr
        self._config_mgr = config_mgr
        # bot 自身 ID 取不到时只告警一次，避免高频指令下日志刷屏
        self._bot_id_warned = False

    # ------------------------------------------------------------------
    # 公开接口（契约见 开发/v0.3/分工.md，不得私改签名）
    # ------------------------------------------------------------------

    async def fetch_by_count(
        self, *, group_id: str, event: AstrMessageEvent, count: int
    ) -> FetchOutcome:
        """数量模式：获取该群最近 ``count`` 条文本消息。

        MySQL 以 ``timestamp DESC`` 取前 count 条（即最近 count 条），归一化后
        反转为时间升序。有效条数 ``m < count × summary_min_mysql_ratio`` 时
        经 OneBot 补齐 ``min(count - m, summary_onebot_max_fetch)`` 条。

        Args:
            group_id: 群号（字符串）
            event: 当前消息事件（用于 OneBot 协议端 client 与 bot 自身 ID）
            count: 期望条数

        Returns:
            FetchOutcome: 已过滤、去重、时间升序的消息与数据源构成；
            两源任一失败均不抛异常，仅降级并在 onebot_error 记录原因。
        """
        outcome = FetchOutcome(messages=[])

        # MySQL：最近 count 条（query_messages 固定 DESC，页 1 即最近的一页）
        records, _total = await self._query_mysql(
            group_id=group_id, page=1, page_size=count
        )
        mysql_msgs = self._rows_to_messages(records)
        m = len(mysql_msgs)

        # 不足判定 → OneBot 补齐
        min_ratio = await self._float_setting("summary_min_mysql_ratio", 0.8)
        if m < count * min_ratio:
            max_fetch = await self._int_setting("summary_onebot_max_fetch", 200)
            need = min(count - m, max_fetch)
            onebot_msgs = await self._fetch_onebot(event, group_id, need, outcome)
        else:
            onebot_msgs = []

        outcome.messages, outcome.sources = await self._merge_finalize(
            event, group_id, mysql_msgs, onebot_msgs
        )
        return outcome

    async def fetch_by_window(
        self,
        *,
        group_id: str,
        event: AstrMessageEvent,
        window_start: datetime,
        window_end: datetime,
    ) -> FetchOutcome:
        """时间模式：获取该群 ``[window_start, window_end]`` 窗口内的文本消息。

        MySQL 按窗口查询（page_size 取 ``summary_max_count``，超量时与总结引擎
        一致保留最近的消息）。不足判定：``m == 0``，或窗口内数据已取全
        （total <= m）且 MySQL 最早一条消息晚于
        ``window_start + summary_gap_tolerance_minutes`` 分钟（说明窗口头部
        存在缺口）→ 经 OneBot 补齐 ``summary_onebot_max_fetch`` 条，补齐结果
        合并后再按窗口范围过滤。

        Args:
            group_id: 群号（字符串）
            event: 当前消息事件（用于 OneBot 协议端 client 与 bot 自身 ID）
            window_start: 窗口开始时间（含）
            window_end: 窗口结束时间（含）

        Returns:
            FetchOutcome: 已过滤、去重、时间升序的消息与数据源构成；
            两源任一失败均不抛异常，仅降级并在 onebot_error 记录原因。
        """
        outcome = FetchOutcome(messages=[])

        # MySQL：窗口查询（time_start/time_end 为 "YYYY-MM-DD HH:MM:SS" 字符串）
        page_size = await self._int_setting("summary_max_count", 1000)
        records, total = await self._query_mysql(
            group_id=group_id,
            time_start=window_start.strftime(_TS_FORMAT),
            time_end=window_end.strftime(_TS_FORMAT),
            page=1,
            page_size=page_size,
        )
        mysql_msgs = self._rows_to_messages(records)
        m = len(mysql_msgs)

        # 不足判定：空结果，或数据取全后最早一条仍晚于容差下限（窗口头部有缺口）
        gap_minutes = await self._int_setting("summary_gap_tolerance_minutes", 30)
        need_onebot = m == 0
        if not need_onebot and total <= m:
            gap_limit = window_start + timedelta(minutes=gap_minutes)
            if mysql_msgs[0].timestamp > gap_limit:
                need_onebot = True

        if need_onebot:
            max_fetch = await self._int_setting("summary_onebot_max_fetch", 200)
            onebot_msgs = await self._fetch_onebot(event, group_id, max_fetch, outcome)
            # OneBot 返回的是协议端缓存的近期消息，合并前按窗口范围裁剪
            onebot_msgs = [
                msg
                for msg in onebot_msgs
                if window_start <= msg.timestamp <= window_end
            ]
        else:
            onebot_msgs = []

        outcome.messages, outcome.sources = await self._merge_finalize(
            event, group_id, mysql_msgs, onebot_msgs
        )
        return outcome

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    async def _query_mysql(self, **kwargs) -> tuple[list[dict], int]:
        """调用 ``query_messages`` 并兜底异常：失败记 warning 后视为空结果。

        Returns:
            (records, total)：records 为原始行字典列表；total 为窗口/条件下的
            总条数（用于时间模式判断窗口数据是否取全）。
        """
        try:
            result = await self._mysql_mgr.query_messages(**kwargs)
        except Exception:
            logger.warning(
                "[HistorySummary] MySQL 查询异常，降级为空结果继续", exc_info=True
            )
            return [], 0
        if not isinstance(result, dict):
            return [], 0
        records = result.get("records")
        if not isinstance(records, list):
            records = []
        try:
            total = int(result.get("total") or 0)
        except (TypeError, ValueError):
            total = 0
        return records, total

    @staticmethod
    def _rows_to_messages(records: list[dict]) -> list[ChatMessage]:
        """MySQL 行 → ChatMessage：行级剔除非文本，随后反转为时间升序。

        非文本判定：content 为非空字符串且 message_type ∈ {text, mixed}
        （纯图片消息本不入文本表，此判定兼作脏数据防御）。
        query_messages 固定 ``ORDER BY timestamp DESC``，故反转即升序；
        最终合并阶段还会全局再排序，此处升序主要服务于时间模式的缺口判定。
        """
        msgs: list[ChatMessage] = []
        for row in records:
            if not isinstance(row, dict):
                continue
            content = row.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            if str(row.get("message_type") or "text") not in _TEXT_MESSAGE_TYPES:
                continue
            msgs.append(mysql_row_to_message(row))
        msgs.reverse()  # DESC → ASC
        return msgs

    async def _fetch_onebot(
        self,
        event: AstrMessageEvent,
        group_id: str,
        count: int,
        outcome: FetchOutcome,
    ) -> list[ChatMessage]:
        """经 OneBot 协议端补齐；失败只记日志并写 onebot_error，绝不向上抛。

        返回空列表是正常结果（协议端缓存不足），不视为错误。
        """
        outcome.onebot_attempted = True
        if count <= 0:
            return []
        try:
            return await fetch_group_history(event, group_id, count)
        except OneBotHistoryError as exc:
            logger.warning(
                "[HistorySummary] OneBot 补齐失败，仅以 MySQL 数据继续：%s",
                exc.reason,
            )
            outcome.onebot_error = exc.reason
            return []

    async def _merge_finalize(
        self,
        event: AstrMessageEvent,
        group_id: str,
        mysql_msgs: list[ChatMessage],
        onebot_msgs: list[ChatMessage],
    ) -> tuple[list[ChatMessage], dict[str, int]]:
        """合并两源：过滤（bot 自身 / 忽略名单）→ 去重 → 升序 → 数据源计数。

        去重键（v0.4.5 F12）：
        - 有合法 message_id 的消息：只按 message_id 主键去重，不登记也不检查
          退化键——否则同一用户 1 秒内连发相同短消息（复读）时，第二条合法
          消息会因退化键撞车被误杀；
        - message_id 为空的消息：只按退化键 (秒级时间戳, sender_id,
          content[:32]) 登记与检查，覆盖「两源同一消息但 message_id 为空」
          的交叉重复。
        设计取舍：宁可两侧各留一份重复（同一消息一侧有 id、一侧无 id 时
        两键互不命中，会双份保留），也不可借退化键误删合法消息。
        sources 以去重 + 过滤后实际保留的条数统计。
        """
        # --- bot 自身 ID（取不到则跳过该过滤，只告警一次） ---
        bot_self_id = self._resolve_bot_self_id(event)
        if not bot_self_id and not self._bot_id_warned:
            logger.warning(
                "[HistorySummary] 无法获取 bot 自身 ID，跳过 bot 自身消息剔除"
            )
            self._bot_id_warned = True

        # --- 忽略名单（get_ignore_senders 失败时按其契约返回 []） ---
        ignore_rows = await self._config_mgr.get_ignore_senders(group_id)
        ignore_ids = {
            str(item["sender_id"]) for item in ignore_rows if item.get("sender_id")
        }

        seen_msg_ids: set[str] = set()
        seen_fallback: set[tuple[int, str, str]] = set()
        kept: list[ChatMessage] = []
        # MySQL 在前：同键冲突时保留主数据源的一方
        for msg in (*mysql_msgs, *onebot_msgs):
            if bot_self_id and msg.sender_id == bot_self_id:
                continue
            if msg.sender_id in ignore_ids:
                continue
            if msg.message_id:
                # 有合法 message_id：仅按主键去重（不登记/不检查退化键，
                # 避免复读消息被误杀，见 docstring）
                if msg.message_id in seen_msg_ids:
                    continue
                seen_msg_ids.add(msg.message_id)
            else:
                # message_id 为空：仅按退化键登记与检查
                fallback_key = (
                    int(msg.timestamp.timestamp()),
                    msg.sender_id,
                    msg.content[:32],
                )
                if fallback_key in seen_fallback:
                    continue
                seen_fallback.add(fallback_key)
            kept.append(msg)

        kept.sort(key=lambda msg: msg.timestamp)
        sources = {"mysql": 0, "onebot": 0}
        for msg in kept:
            sources[msg.source] = sources.get(msg.source, 0) + 1
        return kept, sources

    @staticmethod
    def _resolve_bot_self_id(event: AstrMessageEvent) -> str:
        """获取 bot 自身 ID：优先官方 ``event.get_self_id()``（内部即防御式
        getattr，返回字符串），回退直读 ``message_obj.self_id``；取不到返回空串。
        """
        sid = ""
        try:
            get_self_id = getattr(event, "get_self_id", None)
            if callable(get_self_id):
                sid = str(get_self_id() or "")
        except Exception:
            sid = ""
        if not sid:
            try:
                message_obj = getattr(event, "message_obj", None)
                sid = str(getattr(message_obj, "self_id", "") or "")
            except Exception:
                sid = ""
        return sid.strip()

    async def _float_setting(self, key: str, default: float) -> float:
        """读取 float 配置（get_summary_setting_typed 已含回退，此处为最终兜底）。"""
        value = await self._config_mgr.get_summary_setting_typed(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return default
        return float(value)

    async def _int_setting(self, key: str, default: int) -> int:
        """读取 int 配置（get_summary_setting_typed 已含回退，此处为最终兜底）。"""
        value = await self._config_mgr.get_summary_setting_typed(key)
        if isinstance(value, bool) or not isinstance(value, int):
            return default
        return value
