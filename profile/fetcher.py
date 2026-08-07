"""人物分析数据获取层（模块 D）。

以 MySQL 为主、OneBot 协议端缓存为辅获取目标用户的发言素材；关系上下文开关
开启时进一步双向识别互动对象（partners）并拉取其消息，供统计引擎与 LLM 消费。
契约见 ``开发/v0.4.0/分工.md``「共享接口契约 / Module D」，对外仅一个公开方法：

- :meth:`ProfileFetcher.fetch`：按 :class:`ProfileTarget` 拉取目标消息 +
  关系上下文消息，产出 :class:`ProfileFetchOutcome`

设计要点：

- **目标消息**：
  - scope=="group"：分页 ``query_messages(group_id=..., sender_id=...)`` 拉至
    ``profile_max_count``（默认 2000）上限；``query_messages`` 固定 ``ORDER BY
    timestamp DESC``，多页拉取后统一按时间升序归并；有效条数 ``< 期望 ×
    _MIN_MYSQL_RATIO`` 且 ``event`` 非 None → 经 OneBot 拉当前群近期消息，筛出
    ``sender_id == target`` 的文本消息补齐。
  - scope=="all"：``query_messages(group_id=None, sender_id=...)`` 跨所有已保存
    群，分页拉至上限，**不做 OneBot 补齐**（无法遍历所有群；Web 全局触发
    event=None）。
  - 去重（v0.4.5 F12，沿用 summary fetcher 策略）：有合法 message_id 的消息
    只按 message_id 主键去重；退化键 ``(秒级时间戳, sender_id, content[:32])``
    仅对 message_id 为空的消息登记与检查（含 sender_id 避免关系扫描池跨发送者
    误杀），覆盖「两源同一消息但 message_id 为空」的交叉重复。设计取舍：
    宁可两侧各留一份重复（一侧有 id 一侧无 id 的同一消息可能双存），也不可
    借退化键误删合法消息（同一用户 1 秒内复读相同短消息否则会被误杀）；
    最终时间升序，超上限保留最近的。
- **OneBot 拉取**：本模块自持原始消息拉取 :func:`_fetch_raw_group_history`
  （多轮 ``message_seq`` 翻页 / 超时 / 超量请求 / 短页终止的范式与参数口径同
  ``summary/onebot.py``），并经 :func:`_onebot_raw_to_profile_message` 解析为
  携带 ``at_list`` / ``reply_id`` 的 :class:`ProfileMessage`。不复用
  ``summary.onebot.fetch_group_history`` 返回值的原因：其归一化产物 ChatMessage
  不携带 @/回复标记（关系分析必需），且导入会牵出 summary 整条服务链；本模块
  与其平行、互不依赖（PRD §3.0），仅复用其经验范式。
- **关系上下文**（受 ``profile_relation_context`` 开关控制；关闭时
  context_messages=[] / partners=[] / relation_context_complete=True）：
  1. 互动对象识别（三路信号聚合计数）：目标 @ 过的人（at_list 频次）+ 目标
     回复过的人（reply_id 去重后经 ``get_messages_by_ids`` 反查被回复者）+
     他人→目标（同群扫描池与 OneBot 近期池中 at_list 含目标或 reply_id 指向
     目标消息者）；三路按消息键去重合并，频次降序取 Top
     ``profile_relation_max_partners``（默认 10），name 取最近一次出现的昵称。
  2. 对每个 Top partner 按同范围（scope=group 限该群）拉最近
     ``_CONTEXT_PER_PARTNER`` 条文本消息作为 context_messages。
  3. OneBot 实时补齐仅在 scope=group 且 event 非 None 时执行，补强近期
     「他人→目标」信号（弥补存量行 at/reply 标记缺失）。
  4. 全程容错：任一步异常降级为空/部分结果，relation_context_complete 置
     False，OneBot 原因记 onebot_error，绝不向上抛。
- **sources 统计**：目标消息 + 上下文消息合并、去重过滤后实际保留条数
  ``{"mysql": n, "onebot": m}``。
"""

from __future__ import annotations

import asyncio
import math
from datetime import datetime
from typing import TYPE_CHECKING

from astrbot.api import logger

from ..db_mysql import QUERY_TIMEOUT_SECONDS

from .models import (
    ProfileFetchOutcome,
    ProfileMessage,
    ProfileTarget,
    mysql_row_to_profile_message,
)

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent

    from ..db_config import ConfigManager
    from ..db_mysql import MySQLManager

# ===== 模块常量 =====
# 有效条数 < 期望 × 本比例 且 event 可用 → 触发 OneBot 补齐（同 summary 口径）
_MIN_MYSQL_RATIO = 0.8
_DEFAULT_MAX_COUNT = 2000  # profile_max_count 非法/缺失时的兜底
_DEFAULT_MAX_PARTNERS = 10  # profile_relation_max_partners 非法/缺失时的兜底
_PAGE_SIZE = 500  # query_messages 分页步长（翻页期间保持恒定，避免 offset 错位）
_ONEBOT_TARGET_FETCH = 500  # 目标阶段 OneBot 补齐原始条数上限
_SCAN_POOL_SIZE = 200  # 关系阶段同群他人消息扫描池大小（最近 N 条）
_ONEBOT_RELATION_FETCH = 200  # 关系阶段 OneBot 近期全量拉取上限
_CONTEXT_PER_PARTNER = 50  # 每个互动对象的上下文消息上限

# ===== OneBot 拉取参数（口径同 summary/onebot.py）=====
_ONEBOT_TIMEOUT = 15  # 单轮协议端调用超时（秒）
_ONEBOT_MAX_ROUNDS = 5  # 最大翻页轮数（防 seq 不递减死循环）
_ONEBOT_ROUND_DELAY = 0.3  # 轮间延迟（秒），规避协议端限频
_ONEBOT_OVERFETCH = 1.3  # 每轮超量请求系数，补偿非文本过滤损耗
_ONEBOT_PER_ROUND_CAP = 1000  # 单轮请求条数上限

# 视为「含文本」的 message_type（入库约定仅 text/mixed 入文本表，兼作脏数据防御）
_TEXT_MESSAGE_TYPES = frozenset({"text", "mixed"})


# ---------------------------------------------------------------------------
# OneBot 原始消息解析 / 拉取（模块级纯函数与辅助）
# ---------------------------------------------------------------------------


def _onebot_raw_to_profile_message(raw: dict, group_id: str) -> ProfileMessage | None:
    """将 OneBot v11 群消息对象解析为携带 @/回复标记的 ProfileMessage。

    区别于 ``summary.models.parse_onebot_message`` 的纯文本归一化：额外提取
    ``at`` 段的 ``data.qq``（剔除 ``"all"`` 即 @全体成员，去重保序）汇入
    ``at_list``，提取首个 ``reply`` 段的 ``data.id`` 作为 ``reply_id`` —— 二者
    是人物关系分析的数据基础。

    Args:
        raw: ``get_group_msg_history`` 返回 messages[] 的单条原始消息对象。
        group_id: 该消息所属群号（raw 中未必携带，由调用方传入）。

    Returns:
        ProfileMessage | None：拼接所有 ``text`` 段为 content，去空白后为空
        （纯图片等非文本消息）→ None；任何字段缺失/类型异常 → None（不抛）。
    """
    try:
        parts: list[str] = []
        at_list: list[str] = []
        reply_id = ""
        for seg in raw["message"]:
            if not isinstance(seg, dict):
                continue
            data = seg.get("data")
            if not isinstance(data, dict):
                data = {}
            seg_type = seg.get("type")
            if seg_type == "text":
                parts.append(str(data.get("text") or ""))
            elif seg_type == "at":
                qq = str(data.get("qq") or "").strip()
                if qq and qq.lower() != "all" and qq not in at_list:
                    at_list.append(qq)
            elif seg_type == "reply":
                value = str(data.get("id") or "").strip()
                if value and not reply_id:
                    reply_id = value
        content = "".join(parts).strip()
        if not content:
            return None
        sender = raw.get("sender")
        if not isinstance(sender, dict):
            sender = {}
        return ProfileMessage(
            timestamp=datetime.fromtimestamp(int(raw["time"])),
            group_id=str(group_id),
            sender_id=str(sender.get("user_id") or ""),
            sender_name=str(sender.get("nickname") or ""),
            content=content,
            message_id=str(raw.get("message_id") or ""),
            source="onebot",
            at_list=at_list,
            reply_id=reply_id,
        )
    except Exception as e:
        logger.debug("[Profile] 解析 OneBot 消息失败，已跳过: %s", e)
        return None


async def _fetch_raw_group_history(
    event: AstrMessageEvent | None, group_id: str, count: int
) -> tuple[list[dict], str]:
    """经 OneBot 协议端拉取指定群的**原始**近期消息（多轮 message_seq 翻页）。

    范式与参数口径同 ``summary/onebot.py::fetch_group_history``：官方写法
    ``client = event.bot`` → ``client.api.call_action("get_group_msg_history",
    ...)``；每轮超量请求补偿过滤损耗，短页（缓存到头）/ 凑满 / 无 seq 三种
    终止条件；跨轮 message_id 去重。与之不同处：返回原始 dict 而非归一化产物
    （交由 :func:`_onebot_raw_to_profile_message` 保留 @/回复标记），且失败不
    抛异常，以 ``(messages, error)`` 二元组返回，由调用方降级。

    Args:
        event: 当前消息事件（用于获取协议端 client）；为 None 直接报错降级。
        group_id: 群号（字符串）。
        count: 期望拉取条数；协议端缓存不足时返回少于该值（含空列表）。

    Returns:
        (messages, error)：messages 为原始消息 dict 列表；error 非空表示失败
        原因（首轮失败时为完整失败，第 2 轮起失败仅记日志并返回已拉部分，
        error 仍为空）。
    """
    client = getattr(event, "bot", None)
    if client is None or not hasattr(client, "api"):
        return [], "取不到协议端 client（event.bot 为空）"
    try:
        gid = int(group_id)
    except (TypeError, ValueError):
        return [], f"group_id 非法：{group_id!r}"
    if count <= 0:
        return [], ""
    target = int(count)

    result: list[dict] = []
    seen_ids: set[str] = set()  # 跨轮 message_id 去重（翻页边界可能重叠）
    message_seq = 0  # 0 = 从最新消息开始

    for round_no in range(1, _ONEBOT_MAX_ROUNDS + 1):
        remaining = target - len(result)
        if remaining <= 0:
            break
        request_count = min(
            math.ceil(remaining * _ONEBOT_OVERFETCH), _ONEBOT_PER_ROUND_CAP
        )
        try:
            resp = await asyncio.wait_for(
                client.api.call_action(
                    "get_group_msg_history",
                    group_id=gid,
                    message_seq=message_seq,
                    count=request_count,
                ),
                timeout=_ONEBOT_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "[Profile] OneBot 历史拉取超时（第 %d 轮，>%ds）：group_id=%s",
                round_no,
                _ONEBOT_TIMEOUT,
                group_id,
                exc_info=True,
            )
            if not result:
                return result, f"协议端调用超时（>{_ONEBOT_TIMEOUT}s）"
            break
        except Exception as exc:
            # retcode 非 0（ActionFailed）、连接断开等统一归并；
            # CancelledError 属 BaseException，取消语义保持透传。
            logger.warning(
                "[Profile] OneBot 历史拉取失败（第 %d 轮）：group_id=%s",
                round_no,
                group_id,
                exc_info=True,
            )
            if not result:
                return result, f"协议端调用失败：{type(exc).__name__}: {exc}"
            break

        messages = resp.get("messages") if isinstance(resp, dict) else None
        if not isinstance(messages, list) or not messages:
            break

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
            if msg_id:
                seen_ids.add(msg_id)
            result.append(raw)

        if len(messages) < request_count or len(result) >= target:
            break
        if next_seq is None:
            logger.warning(
                "[Profile] OneBot 协议端未返回 message_seq，无法翻页，止于第 %d 轮",
                round_no,
            )
            break
        message_seq = next_seq
        if round_no < _ONEBOT_MAX_ROUNDS:
            await asyncio.sleep(_ONEBOT_ROUND_DELAY)

    return result, ""


# ---------------------------------------------------------------------------
# 去重辅助
# ---------------------------------------------------------------------------


def _message_key(msg: ProfileMessage) -> tuple:
    """消息去重键：有 message_id 用之，否则退化 (秒级时间戳, sender_id, content[:32])。"""
    if msg.message_id:
        return ("id", msg.message_id)
    return ("fb", int(msg.timestamp.timestamp()), msg.sender_id, msg.content[:32])


def _dedup_and_sort(msgs: list[ProfileMessage]) -> list[ProfileMessage]:
    """去重 + 时间升序（沿用 summary fetcher 策略，v0.4.5 F12）。

    - 有合法 message_id 的消息：只按 message_id 主键去重，不登记也不检查
      退化键——否则同一用户 1 秒内连发相同短消息（复读）时，第二条合法
      消息会因退化键撞车被误杀；
    - message_id 为空的消息：只按退化键 ``(秒级时间戳, sender_id,
      content[:32])`` 登记与检查，覆盖「两源同一消息但 message_id 为空」
      的交叉重复。
    设计取舍：宁可两侧各留一份重复（同一消息一侧有 id、一侧无 id 时两键
    互不命中，会双份保留），也不可借退化键误删合法消息。
    入参顺序即冲突保留优先级（MySQL 在前 → 同键保留主数据源一方）。
    """
    seen_ids: set[str] = set()
    seen_fallback: set[tuple[int, str, str]] = set()
    kept: list[ProfileMessage] = []
    for msg in msgs:
        if msg.message_id:
            if msg.message_id in seen_ids:
                continue
            seen_ids.add(msg.message_id)
        else:
            fallback_key = (
                int(msg.timestamp.timestamp()),
                msg.sender_id,
                msg.content[:32],
            )
            if fallback_key in seen_fallback:
                continue
            seen_fallback.add(fallback_key)
        kept.append(msg)
    kept.sort(key=lambda m: m.timestamp)
    return kept


# ---------------------------------------------------------------------------
# ProfileFetcher
# ---------------------------------------------------------------------------


class ProfileFetcher:
    """人物分析数据获取器：目标消息（MySQL 主 + OneBot 补）+ 关系上下文。"""

    def __init__(self, mysql_mgr: MySQLManager, config_mgr: ConfigManager) -> None:
        self._mysql_mgr = mysql_mgr
        self._config_mgr = config_mgr

    # ------------------------------------------------------------------
    # 公开接口（契约见 开发/v0.4.0/分工.md「Module D」，不得私改签名）
    # ------------------------------------------------------------------

    async def fetch(
        self, target: ProfileTarget, event: AstrMessageEvent | None
    ) -> ProfileFetchOutcome:
        """拉取目标消息与关系上下文消息。

        Args:
            target: 分析目标（scope="group" 单群 / "all" 全局）。
            event: 当前消息事件；**为 None 表示 Web 全局触发**（不做任何
                OneBot 补齐）。单群场景必传（用于 OneBot 协议端 client）。

        Returns:
            ProfileFetchOutcome：目标消息（去重升序，至多 profile_max_count
            条）+ 互动对象消息与排行（关系开关关闭时为空）+ sources 构成。
            任何环节失败均降级返回，绝不向上抛异常。
        """
        outcome = ProfileFetchOutcome(
            target_messages=[], context_messages=[], partners=[]
        )
        try:
            max_count = await self._int_setting("profile_max_count", _DEFAULT_MAX_COUNT)
            if max_count <= 0:
                max_count = _DEFAULT_MAX_COUNT

            # ---- 目标消息 ----
            outcome.target_messages = await self._fetch_target_messages(
                target, max_count, event, outcome
            )

            # ---- 关系上下文（受开关控制）----
            if await self._bool_setting("profile_relation_context", True):
                max_partners = await self._int_setting(
                    "profile_relation_max_partners", _DEFAULT_MAX_PARTNERS
                )
                if max_partners <= 0:
                    max_partners = _DEFAULT_MAX_PARTNERS
                await self._build_relation_context(
                    target, outcome.target_messages, event, outcome, max_partners
                )
        except Exception:
            logger.warning(
                "[Profile] 数据获取出现未预期异常，降级返回已得结果", exc_info=True
            )
            outcome.relation_context_complete = False

        # sources：目标 + 上下文合并，去重过滤后实际保留条数
        sources = {"mysql": 0, "onebot": 0}
        for msg in (*outcome.target_messages, *outcome.context_messages):
            sources[msg.source] = sources.get(msg.source, 0) + 1
        outcome.sources = sources

        logger.info(
            "[Profile] 数据获取完成 | scope=%s 目标=%d 上下文=%d 互动对象=%d"
            " sources=%s 关系上下文完整=%s",
            target.scope,
            len(outcome.target_messages),
            len(outcome.context_messages),
            len(outcome.partners),
            sources,
            outcome.relation_context_complete,
        )
        return outcome

    async def get_all_groups_summary(self) -> list[dict]:
        """全量群清单：chat_history 中实际有数据的群（v0.5.6 群下拉模式感知配套）。

        all_mode（全局记录模式）下白名单表为空，Web「发起分析」群下拉失去
        数据源，改以「有数据的群」为准（口径与 stats 仓储
        ``get_all_groups_summary`` 一致）。不限时间窗口、不截断（群数量级
        有限，走 idx_group_time 索引分组扫描）。

        口径：按 group_id 分组，COUNT(*) 计消息数、MAX(timestamp) 记最近
        活跃时刻；排序 COUNT(*) DESC、同数 group_id ASC（输出确定性）。

        Returns:
            list[dict]: [{"group_id": str, "count": int,
                "last_active": datetime | None}]

        Raises:
            Exception: 查询失败（由 service 层兜底为空列表）。
        """
        sql = (
            "SELECT group_id, COUNT(*) AS cnt, MAX(timestamp) "
            "FROM chat_history GROUP BY group_id "
            "ORDER BY cnt DESC, group_id ASC"
        )
        async with self._mysql_mgr.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await asyncio.wait_for(
                    cur.execute(sql), timeout=QUERY_TIMEOUT_SECONDS
                )
                rows = await cur.fetchall()
        return [
            {
                "group_id": str(row[0]),
                "count": int(row[1] or 0),
                "last_active": row[2],
            }
            for row in rows
        ]

    # ------------------------------------------------------------------
    # 目标消息
    # ------------------------------------------------------------------

    async def _fetch_target_messages(
        self,
        target: ProfileTarget,
        max_count: int,
        event: AstrMessageEvent | None,
        outcome: ProfileFetchOutcome,
    ) -> list[ProfileMessage]:
        """拉取目标自身消息：MySQL 分页（DESC → 升序归并）→ [单群 OneBot 补齐]。"""
        rows: list[dict] = []
        page = 1
        query_kwargs: dict = {"sender_id": target.sender_id}
        if target.scope == "group":
            query_kwargs["group_id"] = target.group_id

        while len(rows) < max_count:
            records, total, ok = await self._query_mysql(
                page=page, page_size=_PAGE_SIZE, **query_kwargs
            )
            if not ok or not records:
                break  # 查询异常已记日志；空页表示到头
            rows.extend(records)
            if page * _PAGE_SIZE >= total:
                break
            page += 1

        msgs = _dedup_and_sort(self._rows_to_profile_messages(rows))

        # 不足判定 → OneBot 补齐（仅单群且 event 可用；全局无法遍历所有群）
        if (
            target.scope == "group"
            and event is not None
            and len(msgs) < max_count * _MIN_MYSQL_RATIO
        ):
            need = min(max_count - len(msgs), _ONEBOT_TARGET_FETCH)
            onebot_msgs, _err = await self._fetch_onebot_profile_messages(
                event, target.group_id, need, outcome
            )
            # 全群近期消息中筛出目标发送者的文本消息
            onebot_msgs = [m for m in onebot_msgs if m.sender_id == target.sender_id]
            if onebot_msgs:
                msgs = _dedup_and_sort([*msgs, *onebot_msgs])

        if len(msgs) > max_count:
            msgs = msgs[-max_count:]  # 升序尾部即最近的消息
        return msgs

    # ------------------------------------------------------------------
    # 关系上下文
    # ------------------------------------------------------------------

    async def _build_relation_context(
        self,
        target: ProfileTarget,
        target_messages: list[ProfileMessage],
        event: AstrMessageEvent | None,
        outcome: ProfileFetchOutcome,
        max_partners: int,
    ) -> None:
        """识别互动对象并拉取其消息，结果写入 outcome。

        全程容错：任一子步骤异常仅降级（partners/context 为空或部分）并将
        ``relation_context_complete`` 置 False，绝不向上抛。
        """
        counter: dict[str, int] = {}  # sender_id → 互动频次
        names: dict[str, tuple[datetime, str]] = {}  # sender_id → (最近时间, 昵称)
        inter_keys: set = (
            set()
        )  # 「他人→目标」证据消息去重键（MySQL 扫描 + OneBot 共用）
        target_msg_ids = {m.message_id for m in target_messages if m.message_id}

        def note_name(sender_id: str, name: str, ts: datetime) -> None:
            """登记昵称：同一 sender_id 保留最近一次出现的非空昵称。"""
            if not sender_id or not name:
                return
            prev = names.get(sender_id)
            if prev is None or ts >= prev[0]:
                names[sender_id] = (ts, name)

        def bump(sender_id: str) -> None:
            """互动计数（剔除空与目标自身）。"""
            if sender_id and sender_id != target.sender_id:
                counter[sender_id] = counter.get(sender_id, 0) + 1

        def hits_target(msg: ProfileMessage) -> bool:
            """该消息是否指向目标（@ 或回复目标的消息）。"""
            if target.sender_id in msg.at_list:
                return True
            return bool(msg.reply_id) and msg.reply_id in target_msg_ids

        # 1) 目标 @ 过的人：聚合目标消息 at_list 频次
        try:
            for msg in target_messages:
                for qq in msg.at_list:
                    if qq and qq.lower() != "all":
                        bump(qq)
        except Exception:
            logger.warning("[Profile] 关系上下文：@ 聚合异常，已降级", exc_info=True)
            outcome.relation_context_complete = False

        # 2) 目标回复过的人：reply_id 去重 → get_messages_by_ids 反查被回复者
        try:
            reply_ids = sorted({m.reply_id for m in target_messages if m.reply_id})
            if reply_ids:
                rows = await self._mysql_mgr.get_messages_by_ids(reply_ids)
                if not isinstance(rows, list):
                    rows = []
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    replied = mysql_row_to_profile_message(row)
                    if not replied.sender_id or replied.sender_id == target.sender_id:
                        continue
                    bump(replied.sender_id)
                    note_name(replied.sender_id, replied.sender_name, replied.timestamp)
        except Exception:
            logger.warning("[Profile] 关系上下文：回复反查异常，已降级", exc_info=True)
            outcome.relation_context_complete = False

        # 3) 他人→目标：同群扫描池（仅单群场景；全局无固定群可扫描）
        if target.scope == "group":
            try:
                records, _total, ok = await self._query_mysql(
                    group_id=target.group_id, page=1, page_size=_SCAN_POOL_SIZE
                )
                if not ok:
                    outcome.relation_context_complete = False
                else:
                    # 扫描池不过滤非文本：纯图片 @/回复同样是互动证据
                    for msg in self._rows_to_profile_messages(records, text_only=False):
                        if not msg.sender_id or msg.sender_id == target.sender_id:
                            continue
                        note_name(msg.sender_id, msg.sender_name, msg.timestamp)
                        if hits_target(msg):
                            key = _message_key(msg)
                            if key not in inter_keys:
                                inter_keys.add(key)
                                bump(msg.sender_id)
            except Exception:
                logger.warning(
                    "[Profile] 关系上下文：同群扫描异常，已降级", exc_info=True
                )
                outcome.relation_context_complete = False

        # 4) OneBot 实时补齐（仅单群 + event）：补强近期「他人→目标」信号
        if target.scope == "group" and event is not None:
            try:
                pool, err = await self._fetch_onebot_profile_messages(
                    event, target.group_id, _ONEBOT_RELATION_FETCH, outcome
                )
                if err:
                    outcome.relation_context_complete = False
                for msg in pool:
                    if not msg.sender_id or msg.sender_id == target.sender_id:
                        continue
                    note_name(msg.sender_id, msg.sender_name, msg.timestamp)
                    if hits_target(msg):
                        key = _message_key(msg)
                        if key not in inter_keys:  # 与库内扫描数据去重
                            inter_keys.add(key)
                            bump(msg.sender_id)
            except Exception:
                logger.warning(
                    "[Profile] 关系上下文：OneBot 补齐异常，已降级", exc_info=True
                )
                outcome.relation_context_complete = False

        # 5) Top partners → 按同范围拉取各对象最近若干条上下文消息
        try:
            ranked = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[
                :max_partners
            ]
            context: list[ProfileMessage] = []
            ctx_keys: set = {_message_key(m) for m in target_messages}
            for sid, _cnt in ranked:
                ctx_kwargs: dict = {
                    "sender_id": sid,
                    "page": 1,
                    "page_size": _CONTEXT_PER_PARTNER,
                }
                if target.scope == "group":
                    ctx_kwargs["group_id"] = target.group_id
                records, _total, ok = await self._query_mysql(**ctx_kwargs)
                if not ok:
                    outcome.relation_context_complete = False
                    continue
                for msg in self._rows_to_profile_messages(records):
                    note_name(msg.sender_id, msg.sender_name, msg.timestamp)
                    key = _message_key(msg)
                    if key in ctx_keys:  # 与目标消息/已拉对象消息去重
                        continue
                    ctx_keys.add(key)
                    context.append(msg)
            context.sort(key=lambda m: m.timestamp)
            outcome.context_messages = context
            outcome.partners = [
                (sid, names[sid][1] if sid in names else "", cnt) for sid, cnt in ranked
            ]
        except Exception:
            logger.warning(
                "[Profile] 关系上下文：对象/上下文拉取异常，已降级", exc_info=True
            )
            outcome.relation_context_complete = False

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    async def _fetch_onebot_profile_messages(
        self,
        event: AstrMessageEvent | None,
        group_id: str,
        count: int,
        outcome: ProfileFetchOutcome,
    ) -> tuple[list[ProfileMessage], str]:
        """OneBot 原始拉取 + ProfileMessage 解析；失败只降级，绝不向上抛。

        Returns:
            (messages, error)：error 非空为失败原因（已记 warning 并写入
            outcome.onebot_error）；返回空列表是正常结果（协议端缓存不足）。
        """
        outcome.onebot_attempted = True
        raw_msgs, err = await _fetch_raw_group_history(event, group_id, count)
        if err:
            logger.warning("[Profile] OneBot 拉取失败，仅以 MySQL 数据继续：%s", err)
            outcome.onebot_error = err
        msgs: list[ProfileMessage] = []
        for raw in raw_msgs:
            msg = _onebot_raw_to_profile_message(raw, group_id)
            if msg is not None:
                msgs.append(msg)
        return msgs, err

    async def _query_mysql(self, **kwargs) -> tuple[list[dict], int, bool]:
        """调用 ``query_messages`` 并兜底异常。

        Returns:
            (records, total, ok)：ok=False 表示查询异常/返回结构非法（已记
            warning），调用方据此终止分页或标记关系上下文不完整。
        """
        try:
            result = await self._mysql_mgr.query_messages(**kwargs)
        except Exception:
            logger.warning("[Profile] MySQL 查询异常，降级为空结果继续", exc_info=True)
            return [], 0, False
        if not isinstance(result, dict):
            return [], 0, False
        records = result.get("records")
        if not isinstance(records, list):
            records = []
        try:
            total = int(result.get("total") or 0)
        except (TypeError, ValueError):
            total = 0
        return records, total, True

    @staticmethod
    def _rows_to_profile_messages(
        records: list[dict], text_only: bool = True
    ) -> list[ProfileMessage]:
        """MySQL 行 → ProfileMessage。

        text_only=True（默认）时行级剔除非文本（content 非空 + message_type ∈
        {text, mixed}），用于目标消息与上下文消息（喂 LLM 需文本）；
        text_only=False 保留全部行，用于关系扫描池（纯图片 @/回复亦为互动证据）。
        """
        msgs: list[ProfileMessage] = []
        for row in records or []:
            if not isinstance(row, dict):
                continue
            if text_only:
                content = row.get("content")
                if not isinstance(content, str) or not content.strip():
                    continue
                if str(row.get("message_type") or "text") not in _TEXT_MESSAGE_TYPES:
                    continue
            msgs.append(mysql_row_to_profile_message(row))
        return msgs

    async def _int_setting(self, key: str, default: int) -> int:
        """读取 int 配置（get_profile_setting_typed 已含回退，此处为最终兜底）。"""
        try:
            value = await self._config_mgr.get_profile_setting_typed(key)
        except Exception:
            return default
        if isinstance(value, bool) or not isinstance(value, int):
            return default
        return value

    async def _bool_setting(self, key: str, default: bool) -> bool:
        """读取 bool 配置（get_profile_setting_typed 已含回退，此处为最终兜底）。"""
        try:
            value = await self._config_mgr.get_profile_setting_typed(key)
        except Exception:
            return default
        if not isinstance(value, bool):
            return default
        return value
