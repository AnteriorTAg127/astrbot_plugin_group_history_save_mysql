"""按群消息触发的自动补库服务（v0.6.1 重构）。

插件重启后，某个群的第一条新消息到达时触发该群补库：用这条消息自带的真实
``message_seq`` 作 ``get_group_msg_history`` 起点，从它往前拉停机窗口的缺口
消息（NapCat 走 ``getMsgHistory`` 正式历史，不依赖 aio 最新视图），补齐
``chat_history`` / ``image_records``。每个群在本重启周期内只补库一次；补库
期间该群新消息经 MessageSaver 门控缓冲，补库完成后带去重 flush。

- **触发**：``maybe_trigger(event)`` 由主插件 ``on_group_message`` 调用，
  幂等（每群一次）；MySQL 未就绪时不触发（后续消息再试）。
- **起点 seq**：用触发消息的真实 message_seq，协议端返回该 seq 之前的正式历史，
  正好覆盖停机窗口缺口（该消息本身由实时路径经缓冲 flush 入库）。
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
BACKFILL_HOURS_DEFAULT = 12  # 默认窗口小时数
BACKFILL_HOURS_MIN = 1  # 窗口下限
BACKFILL_HOURS_MAX = 168  # 窗口上限（7 天）
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


class ReloadBackfill:
    """按群消息触发的自动补库服务：MySQL 就绪后，群首条消息到达时补该群停机缺口。"""

    def __init__(self, context, mysql_mgr, config_mgr, saver=None, stats_service=None):
        self.context = context
        self.mysql_mgr = mysql_mgr
        self.config_mgr = config_mgr
        self.saver = saver  # MessageSaver（补库门控缓冲；None 时 begin/end 防御性跳过）
        self.stats_service = stats_service  # StatsService（快照回填收尾链）
        self._backfilled_groups: set[str] = set()  # 本重启周期已补库的群（每群一次）
        self._tasks: set[asyncio.Task] = set()  # 在跑补库任务
        self._last_snapshot_ts = 0.0  # 快照回填节流时间戳（monotonic 秒）

    async def maybe_trigger(self, event):
        """重启后某群第一条消息到达时触发该群补库（每群一次，幂等）。

        用这条消息自带的真实 ``message_seq`` 作 ``get_group_msg_history`` 起点，
        从它往前拉停机窗口缺口（NapCat 走正式历史查询，不依赖 aio 最新视图）。
        触发后该群进入 MessageSaver 门控（新消息缓冲），补库完成后带去重 flush
        并节流触发快照回填。MySQL 未就绪时跳过本次（后续消息再试，不标记已补库）。
        """
        if self.saver is None or not self.saver.is_initialized:
            return
        try:
            group_id = str(event.get_group_id())
            if not group_id or group_id in self._backfilled_groups:
                return  # 无群号或该群本周期已补库（后续消息不重复触发）
            # 取这条消息的真实 message_seq 作补库起点
            seq = None
            raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
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
            window_start = await self._compute_window_start()
            task = asyncio.create_task(
                self._backfill_group(group_id, seq, window_start, round_cap, max_rounds)
            )
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
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

    async def _compute_window_start(self) -> datetime:
        """计算补库窗口起点：max(上次卸载时间, now - backfill_hours)，无记录回退 hours。"""
        try:
            hours = int(await self.config_mgr.get_setting("backfill_hours", "12"))
        except (ValueError, TypeError):
            hours = BACKFILL_HOURS_DEFAULT
        hours = max(BACKFILL_HOURS_MIN, min(BACKFILL_HOURS_MAX, hours))
        window_start = datetime.now() - timedelta(hours=hours)
        try:
            last_ts_raw = (
                await self.config_mgr.get_setting("last_terminate_time", "")
            ).strip()
            if last_ts_raw:
                last_dt = datetime.fromtimestamp(int(float(last_ts_raw)))
                window_start = max(window_start, last_dt)
        except (ValueError, TypeError, OSError, OverflowError):
            pass
        return window_start

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
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _backfill_group(
        self,
        group_id: str,
        start_seq: int,
        window_start,
        round_cap: int,
        max_rounds: int,
    ) -> dict:
        """对单个群执行补库：从 start_seq 往前翻页 → 解析 + 窗口过滤 → 排序 → 双去重 → 入库。

        收尾链：外层 try/finally——翻页/入库写完后，finally 中
        ``saver.end_backfill(group_id)``（带去重 flush 该群缓冲新消息）→
        节流触发快照回填；取消/异常时 finally 仍执行（数据保全）。
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

            # 2) 多轮翻页拉取原始消息（范式同 core/summary/onebot.py，内联实现）。
            #    第 1 轮从触发消息的真实 seq 开始（NapCat 走 getMsgHistory 正式历史），
            #    返回该 seq 之前的更旧消息；后续轮用本轮最旧 seq 继续往前翻。
            raw_messages: list = []
            seen_ids: set[str] = set()  # 跨轮 message_id 去重（翻页边界可能重叠）
            message_seq = start_seq
            window_start_unix = window_start.timestamp()  # 窗口提前终止判定用 unix 秒
            for round_no in range(1, max_rounds + 1):
                # 每轮请求条数 = min(单轮上限, 剩余上限)：总上限 = 单轮上限 × 最大轮数
                request_count = min(round_cap, total_cap - len(raw_messages))
                try:
                    resp = await asyncio.wait_for(
                        client.api.call_action(
                            "get_group_msg_history",
                            group_id=int(group_id),
                            message_seq=message_seq,
                            count=request_count,
                        ),
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
                    break

                # 逐条收录原始消息 + 记录本轮最早 seq 供翻页 + 本轮最旧 time 供窗口提前终止
                next_seq: int | None = None
                round_min_time: float | None = None
                round_raw: list[dict] = []  # 本轮新增消息（重叠边界比对用）
                for raw in messages:
                    if not isinstance(raw, dict):
                        continue
                    seq = raw.get("message_seq")
                    if not isinstance(seq, int):
                        seq = raw.get("seq")
                    if isinstance(seq, int) and (next_seq is None or seq < next_seq):
                        next_seq = seq
                    t_raw = raw.get("time")
                    if isinstance(t_raw, (int, float)) and not isinstance(t_raw, bool):
                        if round_min_time is None or t_raw < round_min_time:
                            round_min_time = t_raw
                    msg_id = str(raw.get("message_id") or "")
                    if msg_id and msg_id in seen_ids:
                        continue
                    seen_ids.add(msg_id)
                    raw_messages.append(raw)
                    round_raw.append(raw)

                # 终止条件：窗口提前终止 / 重叠边界停止 / 已凑满上限 / 短页（缓存到头）/
                # 无 seq 无法翻页
                if round_min_time is not None and round_min_time < window_start_unix:
                    # 本轮最旧消息已在窗口外；页面按 seq 从新到旧，更旧的轮次必然也在窗口外
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
                # 批内 URL 去重收窄为单消息内（F10）；跨消息去重靠 existing_urls 实时更新。
                # existing_urls 入库前是一次性快照，但批内同一 URL 出现在多条消息时，
                # 第一条插入成功后须追加进 existing_urls，否则第二条会再次通过检查
                # 并 INSERT，产生重复 image_records 行（无唯一索引兜底时累积）。
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
                        # 实时更新快照，使批内后续消息看到刚写入的 URL
                        existing_urls.add(url)

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
