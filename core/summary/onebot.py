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
- **多轮翻页**：各协议端对单次 ``count`` 上限的实现不一（部分实现单次硬限
  ~200 条），故按 ``message_seq`` 从新到旧多轮翻页累计，直到凑满请求量 /
  协议端返回不足一轮（缓存到头）/ 达到轮数上限；每轮超量请求补偿下游过滤
  损耗。支持大 count 的协议端第一轮即因短页终止，行为等价单次调用。
- **失败降级**：本模块不吞错也不崩溃插件——任何异常（超时、协议端 retcode
  非 0 抛出的 ActionFailed、连接异常、返回结构异常等）统一记录
  ``logger.warning("[HistorySummary] ...", exc_info=True)`` 后抛出
  :class:`OneBotHistoryError`，由上层 fetcher（模块 D）捕获并以 MySQL 数据降级。

协议端 client 的获取方式依据 AstrBot 官方开发文档
``docs/zh/dev/star/guides/other.md``「调用 QQ 协议端 API」一节：
``client = event.bot``，随后 ``await client.api.call_action(action, **payloads)``。
"""

import asyncio
import math

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from .models import ChatMessage, parse_onebot_message

# 协议端调用超时（秒）。get_group_msg_history 正常在百毫秒级返回，
# 15s 仅作为协议端假死/网络异常时的兜底上限，超时后由上层 fetcher 降级。
DEFAULT_TIMEOUT = 15

# ===== 多轮翻页参数 =====
# 各协议端对 get_group_msg_history 单次 count 上限的实现不一：NapCat/LLOneBot
# 系可一次返回上千条，部分实现单次硬限 ~200 条。统一以 message_seq 翻页兼容两者。
MAX_ROUNDS = 5  # 最大翻页轮数：防协议端异常（如 seq 不递减）导致死循环
ROUND_DELAY_SECONDS = 0.3  # 轮间延迟（秒）：规避协议端接口限频
OVERFETCH_FACTOR = 1.3  # 每轮超量请求系数：补偿下游过滤丢弃的非文本/bot 自身消息
PER_ROUND_CAP = 1000  # 单轮请求条数上限：防异常大 count 压垮协议端


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
    """经 OneBot 协议端拉取指定群的近期历史消息（按 message_seq 多轮翻页）。

    调用链：``event.bot`` → ``client.api.call_action("get_group_msg_history", ...)``，
    依据 AstrBot 官方文档 ``docs/zh/dev/star/guides/other.md``「调用 QQ 协议端 API」。

    Args:
        event: 当前消息事件，用于获取协议端 client（``event.bot``）。
        group_id: 群号（字符串形式，内部转 int 传给协议端）。
        count: 期望拉取条数。单次请求能力受协议端实现约束（常见 ~200 条
            硬限），本函数以 message_seq 多轮翻页累计至该值；协议端缓存
            不足时返回少于该值（含空列表），不会超过。

    Returns:
        归一化后的文本消息列表，按 :attr:`ChatMessage.timestamp` **升序**排列，
        长度不超过 ``count``（超量请求多拉到的部分丢弃最旧的）。
        协议端缓存为空或全部消息无文本内容时返回空列表（非异常）。

    Raises:
        OneBotHistoryError: 任何失败统一抛出，携带 ``reason`` 简短原因，包括：

            - 取不到协议端 client（非 aiocqhttp 平台 / event.bot 为空）
            - group_id 非数字
            - 调用超时（> ``DEFAULT_TIMEOUT`` 秒）
            - 协议端不支持该 action（retcode 非 0，aiocqhttp 抛出 ActionFailed）
            - 连接异常或返回结构异常

            仅当**首轮**即失败（尚无任何已拉取数据）时抛出；第 2 轮起的失败
            降级为返回已拉到的部分消息（记 warning），不抛异常。
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

    if count <= 0:
        return []
    target = int(count)

    # 2) 多轮翻页拉取：从最新一页起经 message_seq 向旧翻，直到凑满 / 协议端
    #    返回不足一轮（缓存到头）/ 达到轮数上限。各协议端对单次 count 上限的
    #    实现不一（有的支持一次返回上千条，有的硬限 ~200），翻页对两者皆兼容：
    #    支持大 count 的协议端第一轮即因短页终止，行为等价单次调用。
    result: list[ChatMessage] = []
    seen_ids: set[str] = set()  # 跨轮 message_id 去重（翻页边界可能返回重叠消息）
    message_seq = 0  # 0 = 从最新消息开始

    for round_no in range(1, MAX_ROUNDS + 1):
        remaining = target - len(result)
        if remaining <= 0:
            break
        # 超量请求补偿下游过滤损耗（非文本/bot 自身/忽略名单在 fetcher 层剔除）
        request_count = min(math.ceil(remaining * OVERFETCH_FACTOR), PER_ROUND_CAP)

        # 3) 调用协议端 action（每轮独立超时保护；retcode 非 0 时 aiocqhttp
        #    抛 ActionFailed）。第 2 轮起失败只降级返回已有数据，不抛异常。
        try:
            resp = await asyncio.wait_for(
                client.api.call_action(
                    "get_group_msg_history",
                    group_id=gid,
                    message_seq=message_seq,
                    count=request_count,
                ),
                timeout=DEFAULT_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "[HistorySummary] OneBot 历史拉取超时（第 %d 轮，>%ds）："
                "group_id=%s, message_seq=%s, count=%s",
                round_no,
                DEFAULT_TIMEOUT,
                group_id,
                message_seq,
                request_count,
                exc_info=True,
            )
            if not result:
                raise OneBotHistoryError(f"协议端调用超时（>{DEFAULT_TIMEOUT}s）")
            logger.warning(
                "[HistorySummary] OneBot 第 %d 轮超时，返回已拉到的 %d 条消息",
                round_no,
                len(result),
            )
            break
        except Exception as exc:
            # 协议端 retcode 错误（ActionFailed）、连接断开等统一归并于此；
            # CancelledError 属 BaseException，不会被捕获，任务取消语义保持透传。
            logger.warning(
                "[HistorySummary] OneBot 历史拉取失败（第 %d 轮）：group_id=%s,"
                " message_seq=%s, count=%s",
                round_no,
                group_id,
                message_seq,
                request_count,
                exc_info=True,
            )
            if not result:
                raise OneBotHistoryError(
                    f"协议端调用失败：{type(exc).__name__}: {exc}"
                ) from exc
            logger.warning(
                "[HistorySummary] OneBot 第 %d 轮拉取失败，返回已拉到的 %d 条消息",
                round_no,
                len(result),
            )
            break

        # 4) 提取 messages 列表（返回结构异常/空页一律视为缓存到头）
        messages = resp.get("messages") if isinstance(resp, dict) else None
        if not isinstance(messages, list) or not messages:
            break

        # 5) 逐条归一化 + 跨轮去重；同时记录本轮最早 message_seq 供翻页
        #    （next_seq 基于含非文本消息的整轮计算，而非仅保留的文本消息）。
        next_seq: int | None = None
        for raw in messages:
            if not isinstance(raw, dict):
                continue
            seq = raw.get("message_seq")
            if not isinstance(seq, int):
                seq = raw.get("seq")
            if isinstance(seq, int) and (next_seq is None or seq < next_seq):
                next_seq = seq
            msg_id = str(raw.get("message_id") or "")
            if msg_id and msg_id in seen_ids:
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
            if msg is None:
                continue  # 非文本消息：parse 按约定返回 None
            if msg_id:
                seen_ids.add(msg_id)
            result.append(msg)

        # 6) 终止条件：短页（到头）/ 已凑满 / 协议端未回 seq 无法翻页
        if len(messages) < request_count or len(result) >= target:
            break
        if next_seq is None:
            logger.warning(
                "[HistorySummary] OneBot 协议端未返回 message_seq，无法翻页，"
                "止于第 %d 轮（%d 条）",
                round_no,
                len(result),
            )
            break
        message_seq = next_seq
        if round_no < MAX_ROUNDS:
            await asyncio.sleep(ROUND_DELAY_SECONDS)

    # 7) 超量请求可能使结果略多于 target：升序排列后保留最近的 target 条
    result.sort(key=lambda m: m.timestamp)
    if len(result) > target:
        result = result[-target:]
    return result
