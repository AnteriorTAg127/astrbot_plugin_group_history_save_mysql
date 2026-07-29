"""OneBot 群聊历史拉取封装（模块 C）。

经 aiocqhttp 协议端（Lagrange / NapCat / go-cqhttp 等）调用 OneBot v11 标准
action ``get_group_msg_history``，拉取指定群的近期历史消息，并逐条交给
``models.parse_onebot_message`` 归一化为统一数据模型 :class:`ChatMessage`。

设计要点（见 开发/v0.3/分工.md「OneBot 封装」契约与 prd.md 第 4/7 节）：

- **仅提取文本段**：消息段数组中只取 ``type == "text"`` 的 ``data.text`` 拼接，
  图片/语音/视频/表情等非文本段完全忽略（段解析与过滤由
  ``parse_onebot_message`` 完成；无文本内容的消息返回 None 被跳过）。
- **协议端缓存限制**：``get_group_msg_history`` 只能拉到协议端缓存内的近期
  消息，更久的历史不在其能力范围内，由上层 fetcher 依赖 MySQL 覆盖。
- **失败降级**：本模块不吞错也不崩溃插件——任何异常（超时、协议端 retcode
  非 0 抛出的 ActionFailed、连接异常、返回结构异常等）统一记录
  ``logger.warning("[HistorySummary] ...", exc_info=True)`` 后抛出
  :class:`OneBotHistoryError`，由上层 fetcher（模块 D）捕获并以 MySQL 数据降级。

协议端 client 的获取方式依据 AstrBot 官方开发文档
``docs/zh/dev/star/guides/other.md``「调用 QQ 协议端 API」一节：
``client = event.bot``，随后 ``await client.api.call_action(action, **payloads)``。
"""

import asyncio

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from .models import ChatMessage, parse_onebot_message

# 协议端调用超时（秒）。get_group_msg_history 正常在百毫秒级返回，
# 15s 仅作为协议端假死/网络异常时的兜底上限，超时后由上层 fetcher 降级。
DEFAULT_TIMEOUT = 15


class OneBotHistoryError(Exception):
    """OneBot 群历史拉取失败。

    Attributes:
        reason: 人类可读的简短失败原因（不含完整堆栈；完整堆栈已在抛出前
            以 ``logger.warning(..., exc_info=True)`` 记录），供上层 fetcher
            写入 ``FetchOutcome.onebot_error`` 用于日志与用户提示。
    """

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


async def fetch_group_history(
    event: AstrMessageEvent, group_id: str, count: int
) -> list[ChatMessage]:
    """经 OneBot 协议端拉取指定群的近期历史消息。

    调用链：``event.bot`` → ``client.api.call_action("get_group_msg_history", ...)``，
    依据 AstrBot 官方文档 ``docs/zh/dev/star/guides/other.md``「调用 QQ 协议端 API」。

    Args:
        event: 当前消息事件，用于获取协议端 client（``event.bot``）。
        group_id: 群号（字符串形式，内部转 int 传给协议端）。
        count: 期望拉取条数（协议端按其缓存与自身上限返回，可能少于该值）。

    Returns:
        归一化后的文本消息列表，按 :attr:`ChatMessage.timestamp` **升序**排列。
        协议端返回为空或全部消息无文本内容时返回空列表（非异常）。

    Raises:
        OneBotHistoryError: 任何失败统一抛出，携带 ``reason`` 简短原因，包括：

            - 取不到协议端 client（非 aiocqhttp 平台 / event.bot 为空）
            - group_id 非数字
            - 调用超时（> ``DEFAULT_TIMEOUT`` 秒）
            - 协议端不支持该 action（retcode 非 0，aiocqhttp 抛出 ActionFailed）
            - 连接异常或返回结构异常

            调用方（fetcher）应捕获本异常并降级，不应让其冒泡终止流程。
    """
    # 1) 获取协议端 client（官方文档写法：client = event.bot）
    client = getattr(event, "bot", None)
    if client is None or not hasattr(client, "api"):
        logger.warning(
            "[HistorySummary] OneBot 历史拉取失败：取不到协议端 client"
            "（event.bot 为空或非 aiocqhttp 平台），group_id=%s",
            group_id,
        )
        raise OneBotHistoryError("取不到协议端 client（event.bot 为空）")

    try:
        gid = int(group_id)
    except (TypeError, ValueError):
        logger.warning(
            "[HistorySummary] OneBot 历史拉取失败：group_id 非法（%r），无法转为整数",
            group_id,
        )
        raise OneBotHistoryError(f"group_id 非法：{group_id!r}")

    # 2) 调用协议端 action（超时保护；retcode 非 0 时 aiocqhttp 抛 ActionFailed）
    try:
        resp = await asyncio.wait_for(
            client.api.call_action(
                "get_group_msg_history",
                group_id=gid,
                message_seq=0,
                count=count,
            ),
            timeout=DEFAULT_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "[HistorySummary] OneBot 历史拉取超时（>%ds）：group_id=%s, count=%s",
            DEFAULT_TIMEOUT,
            group_id,
            count,
            exc_info=True,
        )
        raise OneBotHistoryError(f"协议端调用超时（>{DEFAULT_TIMEOUT}s）")
    except Exception as exc:
        # 协议端 retcode 错误（ActionFailed）、连接断开等统一归并于此；
        # CancelledError 属 BaseException，不会被捕获，任务取消语义保持透传。
        logger.warning(
            "[HistorySummary] OneBot 历史拉取失败：group_id=%s, count=%s",
            group_id,
            count,
            exc_info=True,
        )
        raise OneBotHistoryError(
            f"协议端调用失败：{type(exc).__name__}: {exc}"
        ) from exc

    # 3) 提取 messages 列表（返回结构异常/缺字段一律按空处理）
    messages = resp.get("messages") if isinstance(resp, dict) else None
    if not isinstance(messages, list):
        return []

    # 4) 逐条归一化：parse_onebot_message 仅提取文本段，无文本返回 None 跳过；
    #    单条解析异常仅跳过该条（防御性，正常路径由 parse 内部保证不抛）。
    result: list[ChatMessage] = []
    for raw in messages:
        if not isinstance(raw, dict):
            continue
        try:
            msg = parse_onebot_message(raw, group_id)
        except Exception:
            logger.warning(
                "[HistorySummary] OneBot 单条消息解析异常，已跳过：message_id=%r",
                raw.get("message_id"),
                exc_info=True,
            )
            continue
        if msg is not None:
            result.append(msg)

    # 5) 协议端返回通常为新→旧，统一按时间升序返回供上层合并
    result.sort(key=lambda m: m.timestamp)
    return result
