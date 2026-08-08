"""公共数据模型（v0.3 群聊历史自动总结）。

定义总结子包各层共享的数据结构：

- ``ChatMessage``：MySQL 与 OneBot 两源归一化后的统一消息模型
- ``StatsResult``：规则统计结果（总数/参与者/时间跨度/活跃排行）
- ``SummaryResult``：总结引擎完整产出（统计 + LLM 板块摘要 + 元数据）
- ``FetchOutcome``：混合数据获取结果（已去重、升序、已过滤的消息 + 数据源构成）

以及两个归一化纯函数（供 fetcher / onebot / summarizer 复用）：

- ``mysql_row_to_message``：``MySQLManager.query_messages`` 返回的行 → ChatMessage
- ``parse_onebot_message``：OneBot v11 群消息对象 → ChatMessage（仅保留文本段）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from astrbot.api import logger

# ---------------------------------------------------------------------------
# 数据结构（契约见 开发/v0.3/分工.md「接口约定 → 公共数据模型」，改动需同步下游）
# ---------------------------------------------------------------------------


@dataclass
class ChatMessage:
    """统一消息模型（MySQL 与 OneBot 两源归一化后的结构）。"""

    timestamp: datetime  # 消息时间
    group_id: str  # 群号（字符串）
    sender_id: str  # 发送者 QQ（字符串）
    sender_name: str  # 发送者昵称
    content: str  # 纯文本内容（非文本消息已被过滤）
    message_id: str  # 消息 ID（去重主键，可能为空）
    source: str  # "mysql" | "onebot"


@dataclass
class StatsResult:
    """规则统计结果。"""

    total: int  # 素材消息总数
    participant_count: int  # 参与者人数
    time_start: datetime | None  # 素材最早时间
    time_end: datetime | None  # 素材最晚时间
    top_senders: list[tuple[str, str, int]]  # (sender_id, sender_name, 条数) Top N
    truncated: bool = False  # 是否因长度预算被截断


@dataclass
class SummaryResult:
    """总结引擎完整产出。"""

    stats: StatsResult
    sections: list[
        tuple[str, str]
    ]  # [(板块标题, 板块内容)]，按 4 板块顺序，解析失败则为单段
    raw_llm_text: str  # LLM 原始输出
    provider_id: str  # 实际使用的 provider
    messages_used: int  # 送入 LLM 的消息条数
    sources: dict[str, int] = field(default_factory=dict)  # {"mysql": n, "onebot": m}
    scope_desc: str = ""  # 范围描述，如「最近 512 条」/「最近 24 小时」


@dataclass
class FetchOutcome:
    """混合数据获取结果。"""

    messages: list[ChatMessage]  # 已去重、按时间升序、已过滤（忽略名单/bot自身/非文本）
    sources: dict[str, int] = field(default_factory=dict)  # {"mysql": n, "onebot": m}
    onebot_attempted: bool = False  # 是否尝试过 OneBot 补齐
    onebot_error: str | None = None  # 降级原因（用于日志/提示，不阻断）


# ---------------------------------------------------------------------------
# 归一化纯函数
# ---------------------------------------------------------------------------


def mysql_row_to_message(row: dict) -> ChatMessage:
    """将 ``MySQLManager.query_messages`` 返回的单行记录归一化为 ChatMessage。

    row 字段：id, timestamp, group_id, sender_id, sender_name,
    message_type, content, message_id。其中 ``timestamp`` 为
    ``"YYYY-MM-DD HH:MM:SS"`` 字符串（query_messages 已 str() 化）。

    时间戳解析策略：先按 ``%Y-%m-%d %H:%M:%S`` 解析；失败回退
    ``datetime.fromisoformat``；仍失败则取 ``datetime.fromtimestamp(0)``
    （epoch）并记 warning，保证不因单条脏数据中断整体流程。
    """
    raw_ts = row.get("timestamp", "")
    try:
        ts = datetime.strptime(str(raw_ts), "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        try:
            ts = datetime.fromisoformat(str(raw_ts))
        except (ValueError, TypeError):
            logger.warning(
                f"[HistorySummary] 无法解析 MySQL 消息时间戳 {raw_ts!r}，回退为 epoch(1970-01-01)"
            )
            ts = datetime.fromtimestamp(0)

    return ChatMessage(
        timestamp=ts,
        group_id=str(row.get("group_id") or ""),
        sender_id=str(row.get("sender_id") or ""),
        sender_name=str(row.get("sender_name") or ""),
        content=str(row.get("content") or ""),
        message_id=str(row.get("message_id") or ""),  # 可能为空串（NULL/历史数据）
        source="mysql",
    )


def parse_onebot_message(raw: dict, group_id: str) -> ChatMessage | None:
    """将 OneBot v11 群消息对象归一化为 ChatMessage。

    Args:
        raw: 单条消息对象（``get_group_msg_history`` 返回 messages[] 的元素），
            形如 ``{"time": <unix 秒>, "message_id": ...,
            "sender": {"user_id": int|str, "nickname": str},
            "message": [{"type": "text", "data": {"text": "..."}}, ...]}``
        group_id: 该消息所属群号（raw 中未必携带，由调用方传入）

    Returns:
        ChatMessage | None:
        - 仅拼接 ``type == "text"`` 段的 ``data["text"]``，非文本段（图片/
          语音/表情等）完全忽略；
        - 去空白后无文本内容（如纯图片消息）→ 返回 None，不纳入素材；
        - 任何字段缺失/类型异常 → 返回 None 并记 debug 日志（不抛异常）。
    """
    try:
        parts: list[str] = []
        for seg in raw["message"]:
            if seg.get("type") != "text":
                continue
            data = seg.get("data") or {}
            parts.append(str(data.get("text") or ""))
        content = "".join(parts).strip()
        if not content:
            return None

        sender = raw["sender"]
        return ChatMessage(
            timestamp=datetime.fromtimestamp(int(raw["time"])),
            group_id=str(group_id),
            sender_id=str(sender["user_id"]),
            sender_name=str(sender.get("nickname") or ""),
            content=content,
            message_id=str(raw.get("message_id") or ""),
            source="onebot",
        )
    except Exception as e:
        logger.debug(f"[HistorySummary] 解析 OneBot 消息失败，已跳过: {e}")
        return None
