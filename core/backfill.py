"""按群消息触发的自动补库服务（v0.8.0 重构窗口）。

插件重启后，某个群的第一条新消息到达时触发该群补库：用这条消息自带的真实
``message_seq`` 作 ``get_group_msg_history`` 起点，从它往前拉停机窗口的缺口
消息（NapCat 走 ``getMsgHistory`` 正式历史，不依赖 aio 最新视图），补齐
``chat_history`` / ``image_records``。每个群在本重启周期内只补库一次；补库
期间该群新消息经 MessageSaver 门控缓冲，补库完成后带去重 flush。

- **触发**：``maybe_trigger(event)`` 由主插件 ``on_group_message`` 调用，
  幂等（每群一次）；MySQL 未就绪时不触发（后续消息再试）。
- **起点 seq**：用触发消息的真实 message_seq，协议端返回该 seq 之前的正式历史，
  正好覆盖窗口缺口（该消息本身由实时路径经缓冲 flush 入库）。
- **窗口**：以该群**最后一条已记录消息的时间 − 5 分钟重叠**为起点，只补
  已记录边界之后的缺口；群无记录回退 ``now - backfill_hours``。比 v0.6.1 用
  ``last_terminate_time``（插件卸载时刻）更贴近真实数据缺口——卸载时间并不代表
  该群数据完整性到该时刻（曾导致快速重载把窗口压到近零、真实大缺口补不上）。
  ``force_backfill()`` 供 ``/补库`` 管理员指令调用：可指定 hours 用 ``now - hours``，
  绕过「每群每重启一次」限制。
- **去重**：message_id / 图片 URL 双维；重叠边界停止（预加载最近已记录消息比对）。
- **取消安全**：``stop()`` 取消所有在跑任务，各任务 finally 仍执行
  ``saver.end_backfill(group_id)`` 带去重 flush（数据保全）。
- **快照回填**：补库完成后节流触发 ``stats_service.startup_backfill()``。
"""

import asyncio
import time
from datetime import datetime, timedelta

from astrbot.api import logger

from .parsing import parse_onebot_raw_message

# ===== 拉取/翻页参数 =====（默认值如下；单轮上限 / 最大轮数经插件设置
# backfill_round_cap / backfill_max_rounds 可自定义，运行时夹取到合理范围）
DEFAULT_ROUND_CAP = 200  # 单轮请求条数上限（默认；协议端常见单次硬限 ~200 条）
DEFAULT_MAX_ROUNDS = 5  # 最大翻页轮数（默认）
ROUND_CAP_MIN, ROUND_CAP_MAX = 1, 5000  # 单轮上限夹取范围
MAX_ROUNDS_MIN, MAX_ROUNDS_MAX = 1, 50  # 最大轮数夹取范围
BACKFILL_OVERLAP_THRESHOLD = (
    0.5  # 重叠停止阈值：某轮拉取与已记录消息多数（≥50%）相同即停
)
ROUND_DELAY_SECONDS = 0.3  # 轮间延迟（秒），规避协议端限频
DEFAULT_TIMEOUT = 15  # 协议端调用超时（秒）
BACKFILL_HOURS_DEFAULT = (
    12  # 默认窗口小时数（无记录群回退用 / force 指令不传 hours 时）
)
BACKFILL_HOURS_MIN = 1  # 窗口下限
BACKFILL_HOURS_MAX = 168  # 窗口上限（7 天）
BACKFILL_OVERLAP_MINUTES = (
    5  # 最后记录时间往前偏移的重叠量（分钟），兜住时间口径小偏差与边界漏拉
)
SNAPSHOT_BACKFILL_THROTTLE = 60  # 快照回填节流秒数（连续多群补库只触发一次）

_ZERO_COUNTS = {
    "pulled": 0,
    "inserted_text": 0,
    "inserted_images": 0,
    "skipped": 0,
}


def _raw_text_content(raw: dict) -> str:
    """提取 OneBot 原始消息的纯文本内容（重叠边界比对用，口径同实时路径 "\n".join）。"""
    parts: list[str] = []
    segments = raw.get("message")
    if isinstance(segments, list):
        for seg in segments:
            if not isinstance(seg, dict) or seg.get("type") != "text":
                continue
            data = seg.get("data") or {}
            if not isinstance(data, dict):
                continue
            text = str(data.get("text") or "").strip()
            if text:
                parts.append(text)
    return "\n".join(parts)


def _raw_has_extractable_content(raw: dict) -> bool:
    """判断消息是否含可入库的文本或 http(s) 图片链接。

    用于区分「正常跳过」与「真解析失败」：图片 URL 非 http（NapCat 等返回本地
    路径）、视频/表情/语音等无文本无 http 图片的消息是正常历史内容，不算失败；
    有非空文本或 http 图片 URL 却解析失败（如 time 非法/结构异常）才算真失败。
    """
    segments = raw.get("message")
    if not isinstance(segments, list):
        return False
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        data = seg.get("data") or {}
        if not isinstance(data, dict):
            continue
        seg_type = seg.get("type")
        if seg_type == "text" and str(data.get("text") or "").strip():
            return True
        if seg_type == "image":
            url = data.get("url")
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                return True
            file = data.get("file")
            if isinstance(file, str) and file.startswith(("http://", "https://")):
                return True
    return False


def _fmt_unix_sec(value) -> str:
    """把 unix 秒格式化为 HH:MM:SS（诊断日志用）；非法值回退原始字符串。"""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "-"
    try:
        return datetime.fromtimestamp(value).strftime("%H:%M:%S")
    except (OSError, OverflowError, ValueError):
        return str(value)


def _fmt_round_max_time(messages: list) -> str:
    """取一轮返回消息的最大 time 并格式化为 HH:MM:SS（诊断日志用）。"""
    max_t = None
    for m in messages:
        if not isinstance(m, dict):
            continue
        t = m.get("time")
        if isinstance(t, (int, float)) and not isinstance(t, bool):
            max_t = t if max_t is None else max(max_t, t)
    return _fmt_unix_sec(max_t)


class ReloadBackfill:
    """按群消息触发的自动补库服务：MySQL 就绪后，群首条消息到达时补该群
    已记录边界之后的缺口（窗口起点 = 最后记录时间 − 5 分钟）。"""

    def __init__(self, context, mysql_mgr, config_mgr, saver=None, stats_service=None):
        self.context = context
        self.mysql_mgr = mysql_mgr
        self.config_mgr = config_mgr
        self.saver = saver  # MessageSaver（补库门控缓冲；None 时 begin/end 防御性跳过）
        self.stats_service = stats_service  # StatsService（快照回填收尾链）
        self._backfilled_groups: set[str] = set()  # 本重启周期已补库的群（每群一次）
        self._active_groups: set[str] = (
            set()
        )  # 正在跑补库的群（防自动触发与 force 指令并发重复跑）
        self._tasks: set[asyncio.Task] = set()  # 在跑补库任务
        self._last_snapshot_ts = 0.0  # 快照回填节流时间戳（monotonic 秒）

    async def maybe_trigger(self, event):
        """重启后某群第一条消息到达时触发该群补库（每群一次，幂等）。

        触发后该群进入 MessageSaver 门控（新消息缓冲），补库完成后带去重 flush
        并节流触发快照回填。MySQL 未就绪时跳过本次（后续消息再试，不标记已补库）。
        补库 round 1 走协议端最新视图（不传 message_seq），天然覆盖激活消息与其前
        的窗口缺口；``message_seq`` 仅在最新视图为空时回退作翻页起点。
        窗口起点取该群最后一条已记录消息时间 − 重叠量（无记录回退 backfill_hours）。
        """
        if self.saver is None or not self.saver.is_initialized:
            return
        try:
            group_id = str(event.get_group_id())
            if not group_id or group_id in self._backfilled_groups:
                return  # 无群号或该群本周期已补库（后续消息不重复触发）
            # 取这条消息的真实 message_seq（最新视图为空时的回退锚点）
            seq = None
            raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
            # 撤回/戳一戳等 notice 事件被适配器包装成群消息事件到达，非真实消息；
            # 忽略且不标记已补库，留待后续真实消息触发补库
            if isinstance(raw, dict) and raw.get("post_type") not in (None, "message"):
                return
            if isinstance(raw, dict):
                seq = raw.get("message_seq") or raw.get("seq")
            if not isinstance(seq, int):
                logger.warning(
                    "[HistorySave] 重载自动补库：群 %s 消息无 message_seq，"
                    "无法定位补库起点，跳过该群",
                    group_id,
                )
                self._backfilled_groups.add(group_id)  # 标记避免每次消息都尝试
                return
            self._backfilled_groups.add(group_id)  # 防重入
            if group_id in self._active_groups:
                return  # 该群已有补库任务在跑（force 指令等），本次不再重复触发
            if self.saver is not None:
                self.saver.begin_backfill(group_id)
            round_cap = await self._read_int_setting(
                "backfill_round_cap", DEFAULT_ROUND_CAP, ROUND_CAP_MIN, ROUND_CAP_MAX
            )
            max_rounds = await self._read_int_setting(
                "backfill_max_rounds",
                DEFAULT_MAX_ROUNDS,
                MAX_ROUNDS_MIN,
                MAX_ROUNDS_MAX,
            )
            window_start = await self._compute_window_start(group_id)
            self._active_groups.add(group_id)
            task = asyncio.create_task(
                self._backfill_group(group_id, seq, window_start, round_cap, max_rounds)
            )
            self._tasks.add(task)

            def _on_done(t: asyncio.Task):
                self._tasks.discard(t)
                self._active_groups.discard(group_id)

            task.add_done_callback(_on_done)
            logger.info(
                "[HistorySave] 重载自动补库：群 %s 首条消息触发（起点 seq=%s，"
                "窗口 %s，单轮 %d 条，最多 %d 轮）",
                group_id,
                seq,
                window_start.strftime("%Y-%m-%d %H:%M:%S"),
                round_cap,
                max_rounds,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("[HistorySave] 重载自动补库触发失败", exc_info=True)

    async def _compute_window_start(
        self, group_id: str | None = None, hours: int | None = None
    ) -> datetime:
        """计算补库窗口起点（补该群已记录边界之后的缺口）。

        优先级：
        1. force 指令显式传 ``hours``：``now - hours``（强制回补指定时长）；
        2. 该群有已记录消息：``最后一条已记录时间 - BACKFILL_OVERLAP_MINUTES``
           （只补最后一条已知记录之后的缺口，重叠量兜住时间口径小偏差）；
        3. 群无记录 / 读取失败：回退 ``now - backfill_hours``（品牌新群尽量多拉）。
        不再依赖 v0.6.1 的 ``last_terminate_time``（插件卸载时刻 ≠ 该群数据完整
        到该时刻，快速重载会把窗口压到近零导致真实大缺口补不上）。
        """
        if hours is not None:
            hours = max(BACKFILL_HOURS_MIN, min(BACKFILL_HOURS_MAX, hours))
            return datetime.now() - timedelta(hours=hours)
        if group_id:
            try:
                last_rec = await self.mysql_mgr.get_last_message_time(group_id)
            except Exception:
                last_rec = None  # 读取失败按无记录回退，不阻断补库
            if last_rec is not None:
                window_start = last_rec - timedelta(minutes=BACKFILL_OVERLAP_MINUTES)
                # 防御：异常未来时间戳（时钟偏移）不应把窗口推到未来
                if window_start > datetime.now():
                    window_start = datetime.now()
                return window_start
        try:
            hours = int(await self.config_mgr.get_setting("backfill_hours", "12"))
        except (ValueError, TypeError):
            hours = BACKFILL_HOURS_DEFAULT
        hours = max(BACKFILL_HOURS_MIN, min(BACKFILL_HOURS_MAX, hours))
        return datetime.now() - timedelta(hours=hours)

    async def force_backfill(self, group_id, hours: int | None = None) -> bool:
        """管理员指令触发的强制补库：对指定群立即补一次，绕过「每群每重启一次」。

        与自动触发共用同一补库流程：门控缓冲 → 多轮拉取 → 窗口过滤 → 双去重 →
        入库 → 收尾链（带去重 flush + 节流快照回填）。窗口默认按「该群最后记录
        时间 − 5 分钟」（无记录回退 backfill_hours）；显式传 ``hours`` 用
        ``now - hours`` 强制回补指定时长。

        Args:
            group_id: 目标群号（int/str 均可）
            hours: 可选强制窗口小时数（夹取 [1,168]）；None 用默认窗口逻辑

        Returns:
            bool: True=任务已创建；False=存储未就绪或该群补库已在跑（不重复）
        """
        group_id = str(group_id)
        if self.saver is None or not self.saver.is_initialized:
            return False
        if group_id in self._active_groups:
            logger.warning(
                "[HistorySave] 强制补库：群 %s 补库已在执行，跳过本次",
                group_id,
            )
            return False
        try:
            if self.saver is not None:
                self.saver.begin_backfill(group_id)
            round_cap = await self._read_int_setting(
                "backfill_round_cap", DEFAULT_ROUND_CAP, ROUND_CAP_MIN, ROUND_CAP_MAX
            )
            max_rounds = await self._read_int_setting(
                "backfill_max_rounds",
                DEFAULT_MAX_ROUNDS,
                MAX_ROUNDS_MIN,
                MAX_ROUNDS_MAX,
            )
            window_start = await self._compute_window_start(group_id, hours=hours)
            self._active_groups.add(group_id)
            task = asyncio.create_task(
                self._backfill_group(group_id, 0, window_start, round_cap, max_rounds)
            )
            self._tasks.add(task)

            def _on_done(t: asyncio.Task):
                self._tasks.discard(t)
                self._active_groups.discard(group_id)

            task.add_done_callback(_on_done)
            logger.info(
                "[HistorySave] 强制补库：群 %s 已启动（窗口 %s%s，单轮 %d 条，最多 %d 轮）",
                group_id,
                window_start.strftime("%Y-%m-%d %H:%M:%S"),
                f"，回首 {hours}h" if hours is not None else "（按最后记录-5min）",
                round_cap,
                max_rounds,
            )
            return True
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("[HistorySave] 强制补库启动失败", exc_info=True)
            return False

    async def _read_int_setting(self, key: str, default: int, lo: int, hi: int) -> int:
        """读取整数插件设置并夹取到 [lo, hi]；读取/转换失败回退 default。"""
        try:
            val = int(await self.config_mgr.get_setting(key, str(default)))
        except (ValueError, TypeError):
            val = default
        return max(lo, min(hi, val))

    async def stop(self):
        """停止所有补库任务：cancel + gather + 吞 CancelledError（v0.4.5 范式）。

        各任务 finally 仍执行 ``saver.end_backfill(group_id)`` 带去重 flush，
        保证 terminate 期间已缓冲数据不丢失。
        """
        tasks = list(self._tasks)
        self._tasks.clear()
        if not tasks:
            return
        # 先让尚未被事件循环调度过的任务跑起来：直接 cancel 一个从未启动的 Task 会
        # 丢弃整条协程、其 try/finally 不执行（该群门控不解除、缓冲不 flush → 数据
        # 丢失）。yield 一次事件循环后，每个任务至少已运行到首个真实 await（收尾链
        # finally 已武装），此时再 cancel 才能触发 finally 走 end_backfill。
        await asyncio.sleep(0)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _backfill_group(
        self,
        group_id: str,
        start_seq: int | None = None,
        window_start=None,
        round_cap: int = DEFAULT_ROUND_CAP,
        max_rounds: int = DEFAULT_MAX_ROUNDS,
    ) -> dict:
        """对单个群执行补库：多轮翻页拉取 → 解析 + 窗口过滤 → 排序 → 双去重 → 入库。

        ``get_group_msg_history`` 的方向语义因协议端而异：NapCat 传入 message_seq 时
        默认返回比锚点**更新**的消息（曾导致只拉到激活消息 1 条），需
        ``reverse_order=true`` 才返回更旧；go-cqhttp/Lagrange 默认即返回更旧。为规避
        该偏差，round 1 不传 message_seq（协议端按最新视图返回最新 count 条，天然包含
        激活消息与窗口缺口）；后续轮用本轮最旧（最小 time）消息的 seq 向更旧翻页，
        首个 message_seq 轮次以 round 1 最旧消息 time 为参照探测方向并固定
        ``reverse_order``。每轮翻页锚点取本轮最旧（最小 time）消息的 seq——NapCat 的
        message_seq 是 msgId 的哈希短 ID，不随消息时间单调，不能取最小值当最旧锚点。

        收尾链：外层 try/finally——翻页/入库写完后，finally 中
        ``saver.end_backfill(group_id)``（带去重 flush 该群缓冲新消息）→
        节流触发快照回填；取消/异常时 finally 仍执行（数据保全）。
        Args:
            start_seq: 激活消息的 message_seq（自动触发传入）；round 1 最新视图为空
                （冷缓存）时回退作翻页起点。强制补库（force_backfill）传 None/0，
                最新视图为空则止步不翻页。
            window_start: 窗口起点（datetime，含 = 需补的最早消息边界）。
            round_cap: 单轮请求条数上限。
            max_rounds: 最大翻页轮数。
        Returns:
            dict: {"pulled", "inserted_text", "inserted_images", "skipped"}；
            pulled = 该群拉取并进入去重流程的总条数。
        """
        try:
            total_cap = round_cap * max_rounds  # 单群总上限 = 单轮上限 × 最大轮数

            # 预加载该群最近 round_cap 条已记录消息的 message_id / 文本内容（重叠参照）
            existing_ids: set[str] = set()
            existing_contents: set[str] = set()
            try:
                recent = await self.mysql_mgr.get_recent_messages(group_id, round_cap)
                for rec in recent or []:
                    if rec.get("message_id"):
                        existing_ids.add(rec["message_id"])
                    if rec.get("content"):
                        existing_contents.add(rec["content"])
            except Exception:
                logger.warning(
                    "[HistorySave] 重载自动补库：群 %s 预加载已记录消息失败，"
                    "退化为窗口/轮数停止",
                    group_id,
                    exc_info=True,
                )

            # 1) 找 aiocqhttp 协议端 client；找不到整批跳过（返回全零计数）
            client = None
            try:
                insts = self.context.platform_manager.get_insts()
            except Exception:
                insts = []
            for inst in insts or []:
                try:
                    meta = inst.meta()
                except Exception:
                    meta = None
                if meta and getattr(meta, "name", None) == "aiocqhttp":
                    client = inst.get_client()
                    break
            if client is None or not hasattr(client, "api"):
                logger.warning(
                    "[HistorySave] 重载自动补库：群 %s 取不到 aiocqhttp 协议端"
                    " client，跳过该群",
                    group_id,
                )
                return dict(_ZERO_COUNTS)

            # 2) 多轮翻页拉取原始消息。
            #    round 1 不传 message_seq（协议端按最新开始）：NapCat 走
            #    getAioFirstViewLatestMsgs 最新视图返回最新 count 条，天然包含激活消息
            #    与停机缺口，规避 NapCat 按 message_seq 翻页「默认返回比锚点更新」的
            #    方向偏差（曾导致只拉到激活消息 1 条）。后续轮用本轮最旧（最小 time）
            #    消息的 seq 向更旧翻页——NapCat 需 reverse_order=true，go-cqhttp/
            #    Lagrange 默认即返回更旧；首个 message_seq 轮次以 round 1 最旧消息
            #    time 为参照探测方向并固定，防翻页退回同一页。
            raw_messages: list = []
            seen_ids: set[str] = set()  # 跨轮 message_id 去重（翻页边界可能重叠）
            message_seq = 0  # 0 = round 1 不传 message_seq（按最新开始）
            use_reverse_order: bool | None = (
                None  # 方向语义；首个 message_seq 轮次探测后固定
            )
            round1_oldest_time: float | None = (
                None  # round 1 最旧消息 time，方向探测参照
            )
            window_start_unix = window_start.timestamp()  # 窗口提前终止判定用 unix 秒
            round_no = 0
            while round_no < max_rounds:
                round_no += 1
                # 每轮请求条数 = min(单轮上限, 剩余上限)：总上限 = 单轮上限 × 最大轮数
                request_count = min(round_cap, total_cap - len(raw_messages))
                try:
                    params = {"group_id": int(group_id), "count": request_count}
                    # round 1 不传 message_seq（按最新开始）；后续轮传本轮最旧消息 seq
                    if message_seq:
                        params["message_seq"] = message_seq
                    # 标准语义（探测得 False）不显式传参；NapCat 语义（探测得 True）
                    # 才需 reverse_order=true 才能继续向更旧翻页
                    if use_reverse_order is True:
                        params["reverse_order"] = True
                    resp = await asyncio.wait_for(
                        client.api.call_action("get_group_msg_history", **params),
                        timeout=DEFAULT_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "[HistorySave] 重载自动补库：群 %s 第 %d 轮拉取超时（>%ds），"
                        "group_id=%s, message_seq=%s",
                        group_id,
                        round_no,
                        DEFAULT_TIMEOUT,
                        group_id,
                        message_seq,
                        exc_info=True,
                    )
                    break
                except Exception:
                    logger.warning(
                        "[HistorySave] 重载自动补库：群 %s 第 %d 轮拉取失败，"
                        "group_id=%s, message_seq=%s",
                        group_id,
                        round_no,
                        group_id,
                        message_seq,
                        exc_info=True,
                    )
                    break

                messages = resp.get("messages") if isinstance(resp, dict) else None
                if not isinstance(messages, list) or not messages:
                    # round 1 最新视图为空（冷缓存）：回退到激活消息锚点向前翻
                    if round_no == 1 and not message_seq and start_seq:
                        message_seq = start_seq
                        use_reverse_order = True  # NapCat 语义（更旧）
                        round_no -= 1
                        continue
                    break

                # 方向语义探测（首个带 message_seq 的轮次）：标准协议端（go-cqhttp/
                # Lagrange）默认返回比锚点更旧的消息；NapCat 默认返回更新的消息（翻页
                # 退回同一页）。以 round 1 最旧消息 time 为参照——取回的消息含更旧 →
                # 标准（不传 reverse_order）；仅含更新（无更旧）→ NapCat，翻转
                # reverse_order=true 重试本轮。
                if (
                    use_reverse_order is None
                    and message_seq
                    and round1_oldest_time is not None
                ):
                    comparable_times = [
                        m["time"]
                        for m in messages
                        if isinstance(m, dict)
                        and isinstance(m.get("time"), (int, float))
                        and not isinstance(m.get("time"), bool)
                    ]
                    if comparable_times:
                        if any(
                            t > round1_oldest_time for t in comparable_times
                        ) and not any(t < round1_oldest_time for t in comparable_times):
                            use_reverse_order = True
                            round_no -= 1  # 以修正后的方向重试本轮，不消耗轮次
                            continue
                        use_reverse_order = False

                # 逐条收录原始消息 + 记录本轮最旧（最小 time）消息的 seq 供翻页 +
                # 本轮最旧 time 供窗口提前终止。NapCat 的 message_seq 是 msgId 的
                # 哈希短 ID，不随消息时间单调，不能取最小值当「最旧」锚点。
                next_seq: int | None = None  # 本轮最旧（最小 time）消息的 seq
                next_seq_fallback: int | None = None  # time 缺失时退化为最小 seq
                round_min_time: float | None = None
                round_raw: list[dict] = []  # 本轮新增消息（重叠边界比对用）
                for raw in messages:
                    if not isinstance(raw, dict):
                        continue
                    seq = raw.get("message_seq")
                    if not isinstance(seq, int):
                        seq = raw.get("seq")
                    if isinstance(seq, int) and (
                        next_seq_fallback is None or seq < next_seq_fallback
                    ):
                        next_seq_fallback = seq
                    t_raw = raw.get("time")
                    if isinstance(t_raw, (int, float)) and not isinstance(t_raw, bool):
                        if round_min_time is None or t_raw < round_min_time:
                            round_min_time = t_raw
                            next_seq = seq if isinstance(seq, int) else None
                    msg_id = str(raw.get("message_id") or "")
                    if msg_id and msg_id in seen_ids:
                        continue
                    seen_ids.add(msg_id)
                    raw_messages.append(raw)
                    round_raw.append(raw)
                if next_seq is None:
                    next_seq = next_seq_fallback
                if round_no == 1:
                    round1_oldest_time = round_min_time

                logger.info(
                    "[HistorySave] 重载自动补库：群 %s 第 %d 轮返回 %d 条"
                    "（message_seq=%s, reverse_order=%s，time 范围 %s~%s）",
                    group_id,
                    round_no,
                    len(messages),
                    message_seq,
                    params.get("reverse_order"),
                    _fmt_unix_sec(round_min_time),
                    _fmt_round_max_time(messages),
                )

                # 本轮全部去重、无新消息 → 无法继续向更旧推进，停止翻页
                if not round_raw:
                    break

                # 终止条件：窗口提前终止 / 重叠边界停止 / 已凑满上限 / 短页（缓存到头）/
                # 无 seq 无法翻页
                if round_min_time is not None and round_min_time < window_start_unix:
                    # 本轮最旧消息已在窗口外；按时间向更旧翻页，更旧的轮次必然也在窗口外
                    break
                if existing_ids or existing_contents:
                    # 重叠边界停止：本轮拉取的消息与已记录消息多数相同（按 message_id，
                    # 空 id 用文本内容兜底），说明已到达已记录边界，无需继续拉取旧消息
                    matches = 0
                    comparable = 0
                    for raw in round_raw:
                        mid = str(raw.get("message_id") or "")
                        if mid:
                            comparable += 1
                            if mid in existing_ids:
                                matches += 1
                        else:
                            content = _raw_text_content(raw)
                            if content:
                                comparable += 1
                                if content in existing_contents:
                                    matches += 1
                    if (
                        comparable
                        and matches / comparable >= BACKFILL_OVERLAP_THRESHOLD
                    ):
                        logger.info(
                            "[HistorySave] 重载自动补库：群 %s 第 %d 轮命中已记录边界"
                            "（重叠 %d/%d），停止翻页",
                            group_id,
                            round_no,
                            matches,
                            comparable,
                        )
                        break
                if len(raw_messages) >= total_cap:
                    break
                if len(messages) < request_count:
                    break
                if next_seq is None:
                    logger.warning(
                        "[HistorySave] 重载自动补库：群 %s 协议端未返回 message_seq，"
                        "止于第 %d 轮（%d 条）",
                        group_id,
                        round_no,
                        len(raw_messages),
                    )
                    break
                message_seq = next_seq
                if round_no < max_rounds:
                    await asyncio.sleep(ROUND_DELAY_SECONDS)

            # 3) 解析 + 窗口过滤：仅保留窗口内（timestamp >= window_start）的消息
            parsed: list[dict] = []
            parse_failed = 0
            for raw in raw_messages:
                # 区分「正常跳过」（图片本地路径/视频/表情等无可提取内容）与
                # 「真有文本或 http 图片但解析失败」
                has_text_image = _raw_has_extractable_content(raw)
                try:
                    item = parse_onebot_raw_message(raw, group_id)
                except Exception:
                    item = None
                if item is None:
                    if has_text_image:
                        parse_failed += 1
                        logger.debug(
                            "[HistorySave] 重载自动补库：解析单条消息失败（群 %s），已跳过",
                            group_id,
                        )
                    continue
                if item["timestamp"] < window_start:
                    continue
                parsed.append(item)
            if parse_failed > 0:
                logger.warning(
                    "[HistorySave] 重载自动补库：群 %s 有 %d 条含文本/图片的消息解析失败"
                    "被跳过，需核查协议端消息结构",
                    group_id,
                    parse_failed,
                )

            # 4) 排序：最旧→最新，保证 chat_history 自增 id 与时间单调（F2；
            #    stats 侧 MAX(id)=窗口内最后一条 的昵称查询假设依赖 id 与时间同向）
            parsed.sort(key=lambda item: item["timestamp"])

            # 5) 去重：message_id 批量比对已存在；图片 URL 按群批量比对已存在
            ids = [item["message_id"] for item in parsed if item["message_id"]]
            existing_ids: set[str] = set()
            if ids:
                existing_ids = await self.mysql_mgr.get_existing_message_ids(
                    group_id, ids
                )
            pending_urls: list[str] = []
            for item in parsed:
                if item["message_id"] and item["message_id"] in existing_ids:
                    continue
                pending_urls.extend(item["image_urls"])
            existing_urls: set[str] = set()
            if pending_urls:
                existing_urls = await self.mysql_mgr.get_existing_image_urls(
                    group_id, pending_urls
                )

            # 6) 逐条入库：空 message_id 跳过（F4）；message_id 已存在跳过；
            #    文本/图片分别入库，单条失败不中断
            skipped = 0
            inserted_text = 0
            inserted_images = 0
            empty_id_count = 0
            for item in parsed:
                if not item["message_id"]:
                    # 空 message_id 只来自实时缓冲单源路径，补库侧跳过（F4）
                    skipped += 1
                    empty_id_count += 1
                    continue
                if item["message_id"] in existing_ids:
                    skipped += 1
                    continue
                text = item["text"]
                image_urls = item["image_urls"]
                if text:
                    text_ok = await self.mysql_mgr.insert_chat_message(
                        group_id=group_id,
                        sender_id=item["sender_id"],
                        sender_name=item["sender_name"],
                        message_type="mixed" if image_urls else "text",
                        content=text,
                        message_id=item["message_id"],
                        at_list=item["at_list"],
                        reply_id=item["reply_id"],
                        timestamp=item["timestamp"],
                    )
                    if text_ok:
                        inserted_text += 1
                # 图片 URL 去重收窄为单消息内（F10）：同一条消息内重复 URL 只入一次
                # （dict.fromkeys）；不同消息共用同一 URL 各写一行，与实时路径口径对齐
                # （实时路径每消息逐条 insert_image_record，不因 URL 已被其他消息写入
                # 而跳过）。existing_urls 是一次性快照，只兜「批次开始前库中已存在」的
                # URL（跨批次/历史去重）；批内新写入不回填 existing_urls。
                for url in dict.fromkeys(image_urls):
                    if url in existing_urls:
                        continue
                    img_ok = await self.mysql_mgr.insert_image_record(
                        group_id=group_id,
                        sender_id=item["sender_id"],
                        image_url=url,
                        sender_name=item["sender_name"],
                        timestamp=item["timestamp"],
                    )
                    if img_ok:
                        inserted_images += 1

            if empty_id_count > 0:
                logger.warning(
                    "[HistorySave] 重载自动补库：群 %s 跳过 %d 条空 message_id 消息"
                    "（补库侧不写库，由实时缓冲覆盖）",
                    group_id,
                    empty_id_count,
                )

            result = {
                "pulled": len(parsed),
                "inserted_text": inserted_text,
                "inserted_images": inserted_images,
                "skipped": skipped,
            }
            logger.info(
                "[HistorySave] 重载自动补库：群 %s 完成（拉取 %d 条，新增文本 %d，"
                "新增图片 %d，跳过 %d）",
                group_id,
                result["pulled"],
                result["inserted_text"],
                result["inserted_images"],
                result["skipped"],
            )
            return result
        finally:
            # 收尾链：该群门控解除 + 带去重 flush 缓冲新消息 → 节流触发快照回填；
            # 取消/异常时 finally 仍执行，各步独立 try/except 不冒泡
            if self.saver is not None:
                try:
                    await self.saver.end_backfill(group_id)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning(
                        "[HistorySave] 重载自动补库：群 %s 缓冲消息带去重 flush 失败",
                        group_id,
                        exc_info=True,
                    )
            await self._maybe_snapshot_backfill()

    async def _maybe_snapshot_backfill(self):
        """节流触发快照启动回填（连续多群补库只触发一次，避免重复重算）。"""
        now = time.monotonic()
        if now - self._last_snapshot_ts < SNAPSHOT_BACKFILL_THROTTLE:
            return
        self._last_snapshot_ts = now
        if self.stats_service is None:
            return
        try:
            await self.stats_service.startup_backfill()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "[HistorySave] 重载自动补库：触发快照启动回填失败", exc_info=True
            )
