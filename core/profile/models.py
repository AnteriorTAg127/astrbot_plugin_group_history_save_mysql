"""公共数据模型（v0.4.0 人物分析）。

定义人物分析子包各层共享的数据结构：

- ``ProfileTarget``：分析目标（单群或全局）
- ``ProfileMessage``：MySQL 与 OneBot 两源归一化后的统一消息模型（含 @/回复标记）
- ``ProfileStats``：确定性统计结果（时间分布/长度/互动排行，无 AI、不幻觉）
- ``ProfileResult``：人物分析引擎完整产出（统计 + LLM 板块叙述 + 元数据）
- ``ProfileFetchOutcome``：人物数据获取结果（目标消息 + 关系上下文消息 + 互动对象）

以及一个归一化纯函数（供 fetcher / storage / service 复用）：

- ``mysql_row_to_profile_message``：MySQL 查询返回的行 → ProfileMessage

设计对齐 ``summary/models.py``：时间戳三级回退、NULL 兜底、异常不中断整体流程。
契约见 ``开发/v0.4.0/分工.md``「共享接口契约 → 数据模型」，改动需同步下游。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from astrbot.api import logger

# ---------------------------------------------------------------------------
# 数据结构（契约见 开发/v0.4.0/分工.md「共享接口契约 → 数据模型」，改动需同步下游）
# ---------------------------------------------------------------------------


@dataclass
class ProfileTarget:
    """人物分析目标（单群或全局）。"""

    sender_id: str  # 目标用户 QQ（字符串）
    sender_name: str  # 目标用户昵称
    scope: str  # "group" | "all"
    group_id: str  # 群号；scope == "all" 时为空串 ""


@dataclass
class ProfileMessage:
    """人物分析统一消息模型（MySQL 与 OneBot 两源归一化，含 @/回复标记）。

    相对 ``summary.models.ChatMessage`` 额外承载 ``at_list`` / ``reply_id``，
    作为人物关系分析的数据基础；独立成类而非污染共享的 ChatMessage 契约。
    """

    timestamp: datetime  # 消息时间
    group_id: str  # 群号（字符串）
    sender_id: str  # 发送者 QQ（字符串）
    sender_name: str  # 发送者昵称
    content: str  # 纯文本内容（非文本消息已被过滤）
    message_id: str  # 消息 ID（去重主键，可能为空）
    source: str  # "mysql" | "onebot"
    at_list: list[str] = field(default_factory=list)  # 本条消息 @ 的 QQ 列表
    reply_id: str = ""  # 回复的目标消息 message_id；无则空串


@dataclass
class ProfileStats:
    """确定性统计结果（无 AI，供图表与 LLM 共同消费）。"""

    total: int  # 目标消息总数
    group_count: int  # 涉及群数量
    group_breakdown: list[tuple[str, int]]  # [(group_id, count)]（全局模式）
    time_start: datetime | None  # 最早发言时间
    time_end: datetime | None  # 最晚发言时间
    active_days: int  # 有发言的天数
    hour_dist: list[int]  # 按小时分布，len 24（0–23 点各多少条）
    weekday_dist: list[int]  # 按星期分布，len 7（Mon=0..Sun=6）
    peak_hour: int  # 最活跃小时
    peak_weekday: int  # 最活跃星期
    avg_length: float  # 平均消息字符数
    total_chars: int  # 总字符数
    emoji_ratio: float  # 含 emoji 的消息占比（Unicode 表情范围检测）
    question_ratio: float  # 以问号结尾的消息占比（粗略「提问倾向」）
    top_partners: list[tuple[str, str, int]]  # [(sender_id, name, count)] 互动对象排行
    truncated: bool = False  # 是否因长度预算被截断


@dataclass
class ProfileResult:
    """人物分析引擎完整产出。"""

    target: ProfileTarget  # 分析目标
    stats: ProfileStats  # 确定性统计
    sections: list[tuple[str, str]]  # [(板块标题, 板块内容)]，解析失败则为单段
    raw_llm_text: str  # LLM 原始输出
    provider_id: str  # 实际使用的 provider（降级链成功节点）
    messages_used: int  # 送入 LLM 的消息条数
    sources: dict[str, int] = field(default_factory=dict)  # {"mysql": n, "onebot": m}
    relation_context_complete: bool = True  # 关系上下文是否完整（降级时为 False）
    scope_desc: str = ""  # 范围描述，如「某群」/「全局 N 个群」
    created_at: str = ""  # 生成时间（字符串，落盘/展示用）


@dataclass
class ProfileFetchOutcome:
    """人物数据获取结果。"""

    target_messages: list[ProfileMessage]  # 目标消息（已去重、按时间升序）
    context_messages: list[ProfileMessage]  # 互动对象消息（关系开关关闭时为 []）
    partners: list[tuple[str, str, int]]  # 互动对象排行 [(sender_id, name, count)]
    sources: dict[str, int] = field(default_factory=dict)  # {"mysql": n, "onebot": m}
    onebot_attempted: bool = False  # 是否尝试过 OneBot 补齐
    onebot_error: str | None = None  # 降级原因（用于日志/提示，不阻断）
    relation_context_complete: bool = True  # 关系上下文是否完整（降级时为 False）


# ---------------------------------------------------------------------------
# 归一化纯函数
# ---------------------------------------------------------------------------


def _parse_at_list(raw) -> list[str]:
    """将 DB 中 at_list 列（英文逗号分隔字符串）归一化为字符串列表。

    - ``None`` / 空串 / 纯空白 → ``[]``
    - ``"123,456"`` → ``["123", "456"]``
    - 逐项 ``strip``，丢弃空白项（``"123, ,456,"`` → ``["123", "456"]``）
    """
    if raw is None:
        return []
    text = str(raw).strip()
    if not text:
        return []
    return [item.strip() for item in text.split(",") if item.strip()]


def mysql_row_to_profile_message(row: dict) -> ProfileMessage:
    """将 MySQL 查询返回的单行记录归一化为 ProfileMessage。

    row 字段：id, timestamp, group_id, sender_id, sender_name, message_type,
    content, message_id, at_list, reply_id。其中 ``timestamp`` 为
    ``"YYYY-MM-DD HH:MM:SS"`` 字符串（query_messages 已 str() 化）；
    ``at_list`` / ``reply_id`` 为 v0.4.0 新增列，旧行为 NULL。

    时间戳解析策略（镜像 ``summary.models.mysql_row_to_message`` 三级回退）：
    先按 ``%Y-%m-%d %H:%M:%S`` 解析；失败回退 ``datetime.fromisoformat``；
    仍失败则取 ``datetime.fromtimestamp(0)``（epoch）并记 warning，保证不因
    单条脏数据中断整体流程。

    归一化细则：
    - ``at_list``：逗号分隔串 → 列表（空串/NULL → []，逐项 strip 去空），见
      :func:`_parse_at_list`；
    - ``reply_id``：NULL → ``""``；
    - 其余文本字段：``str(... or "")`` 兜底 None。
    """
    raw_ts = row.get("timestamp", "")
    try:
        ts = datetime.strptime(str(raw_ts), "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        try:
            ts = datetime.fromisoformat(str(raw_ts))
        except (ValueError, TypeError):
            logger.warning(
                f"[Profile] 无法解析 MySQL 消息时间戳 {raw_ts!r}，回退为 epoch(1970-01-01)"
            )
            ts = datetime.fromtimestamp(0)

    return ProfileMessage(
        timestamp=ts,
        group_id=str(row.get("group_id") or ""),
        sender_id=str(row.get("sender_id") or ""),
        sender_name=str(row.get("sender_name") or ""),
        content=str(row.get("content") or ""),
        message_id=str(row.get("message_id") or ""),  # 可能为空串（NULL/历史数据）
        source="mysql",
        at_list=_parse_at_list(row.get("at_list")),
        reply_id=str(row.get("reply_id") or ""),  # NULL → ""
    )
