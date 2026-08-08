"""重载自动补库服务（v0.6.0）。

插件加载/重载、MySQL 初始化成功后，经 OneBot 协议端 ``get_group_msg_history``
拉取白名单启用群近 ``backfill_hours`` 小时历史消息（文本 + 图片 URL）补齐
``chat_history`` / ``image_records``，弥补重载窗口与协议端缓存期间的消息缺口。

- **去重**：按 ``message_id`` 批量比对已存在记录（chat_history 已存在跳过）；
  图片 URL 按 ``(group_id, image_url)`` 去重（避免纯图片消息跨重载重复入库）。
- **后台任务**：``start()`` 以 ``asyncio.create_task`` 自持句柄，不阻塞插件启动；
  失败仅记日志，不冒泡。
- **取消安全**：``stop()`` 采用 v0.4.5 取消安全范式（cancel + await + 吞
  CancelledError），保证 terminate 期间任务干净退出。
- **翻页范式**：复用 core/summary/onebot.py 的多轮翻页（``message_seq`` 从新到旧、
  单轮上限、轮间延迟、跨轮 message_id 去重、首轮失败抛错、后续轮失败降级）。
- **翻页参数可配置**：单轮请求条数 / 最大轮数由插件设置 ``backfill_round_cap``
  （默认 200）/ ``backfill_max_rounds``（默认 5）控制，夹取到合理范围。
- **重叠边界停止**：翻页前预加载该群最近已记录消息的 message_id / 文本内容，
  每轮拉取后与参照集对比——若本轮拉取的消息与已有记录多数相同，说明已到达
  「已记录边界」，即停止翻页，随后去重入库。
"""

import asyncio
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


def _raw_has_content(raw: dict) -> bool:
    """判断 OneBot 原始消息是否含 text/image 段。

    用于区分「正常无内容消息」（视频/表情/语音/转发等，无 text/image 段，
    是正常历史内容，不算解析失败）与「真有 text/image 但解析失败」。
    """
    segments = raw.get("message")
    if not isinstance(segments, list):
        return False
    return any(
        isinstance(seg, dict) and seg.get("type") in ("text", "image")
        for seg in segments
    )


class ReloadBackfill:
    """重载自动补库服务：MySQL 就绪后从 OneBot 拉取历史消息补齐窗口缺口。"""

    def __init__(self, context, mysql_mgr, config_mgr, saver=None, stats_service=None):
        self.context = context
        self.mysql_mgr = mysql_mgr
        self.config_mgr = config_mgr
        self.saver = saver  # MessageSaver（补库门控缓冲；None 时 begin/end 防御性跳过）
        self.stats_service = stats_service  # StatsService（快照启动回填收尾链）
        self._task: asyncio.Task | None = None

    async def start(self):
        """启动重载自动补库后台任务（幂等；开关关闭时跳过）。

        读 ``backfill_enabled``（非 "true" 跳过）、``backfill_hours``（int 转换
        失败回退 12，夹取 [1,168]）、``backfill_round_cap``（默认 200，夹取
        [1,5000]）与 ``backfill_max_rounds``（默认 5，夹取 [1,50]）；窗口起点取
        「上次卸载时间（停机缺口）与 now - backfill_hours」的较新者（无记录/非法
        回退 backfill_hours，R2）；创建任务前调用 ``saver.begin_backfill()``
        门控实时消息进缓冲不落库（F3）。整体 try/except 兜底（CancelledError 透传）。
        """
        if self._task is not None and not self._task.done():
            return
        try:
            setting = await self.config_mgr.get_setting("backfill_enabled", "true")
            if (setting or "").strip().lower() != "true":
                logger.info(
                    "[HistorySave] 重载自动补库：开关未开启（backfill_enabled=%s），跳过",
                    setting,
                )
                return
            try:
                hours = int(await self.config_mgr.get_setting("backfill_hours", "12"))
            except (ValueError, TypeError):
                hours = BACKFILL_HOURS_DEFAULT
            hours = max(BACKFILL_HOURS_MIN, min(BACKFILL_HOURS_MAX, hours))
            # 翻页参数：单轮上限 / 最大轮数可自定义（默认 200 / 5），夹取到合理范围
            round_cap = await self._read_int_setting(
                "backfill_round_cap", DEFAULT_ROUND_CAP, ROUND_CAP_MIN, ROUND_CAP_MAX
            )
            max_rounds = await self._read_int_setting(
                "backfill_max_rounds",
                DEFAULT_MAX_ROUNDS,
                MAX_ROUNDS_MIN,
                MAX_ROUNDS_MAX,
            )
            # 窗口起点 = 上次卸载时间（停机缺口）与 now - backfill_hours 的较新者；
            # 无记录/非法回退 backfill_hours，将拉取量收窄到停机缺口（R2）
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
            # 补库门控：实时消息开始缓冲不落库，避免与补库写路径并发（F3）
            if self.saver is not None:
                self.saver.begin_backfill()
            self._task = asyncio.create_task(
                self._run_all(window_start, round_cap, max_rounds)
            )
            logger.info(
                "[HistorySave] 重载自动补库：后台任务已启动（窗口 %s 小时，"
                "单轮 %d 条，最多 %d 轮）",
                hours,
                round_cap,
                max_rounds,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("[HistorySave] 重载自动补库启动失败", exc_info=True)

    async def _read_int_setting(self, key: str, default: int, lo: int, hi: int) -> int:
        """读取整数插件设置并夹取到 [lo, hi]；读取/转换失败回退 default。"""
        try:
            val = int(await self.config_mgr.get_setting(key, str(default)))
        except (ValueError, TypeError):
            val = default
        return max(lo, min(hi, val))

    async def stop(self):
        """停止补库任务：cancel + await + 吞 CancelledError（v0.4.5 取消安全范式）。"""
        task = self._task
        self._task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _run_all(self, window_start, round_cap: int, max_rounds: int):
        """逐群串行补库并输出汇总日志；单群失败不阻断其他群。

        收尾链：外层 try/finally——补库历史全部写完后，finally 中依次
        ``saver.end_backfill()``（带去重 flush 缓冲新消息，F3）→
        ``stats_service.startup_backfill()``（快照层纳入回填历史，F6）；
        任务被取消/异常时 finally 仍执行（数据保全，R1），各步失败仅记日志。
        """
        try:
            groups = await self._resolve_groups()
            if not groups:
                logger.info(
                    "[HistorySave] 重载自动补库：无待补库的群"
                    "（白名单为空或全部关闭，或全群模式无数据），跳过"
                )
                return
            total_pulled = 0
            total_text = 0
            total_images = 0
            total_skipped = 0
            for group in groups:
                try:
                    result = await self._backfill_group(
                        group, window_start, round_cap, max_rounds
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning(
                        "[HistorySave] 重载自动补库：群 %s 补库失败",
                        group.get("group_id"),
                        exc_info=True,
                    )
                    continue
                total_pulled += result["pulled"]
                total_text += result["inserted_text"]
                total_images += result["inserted_images"]
                total_skipped += result["skipped"]
                logger.info(
                    "[HistorySave] 重载自动补库：群 %s 完成（拉取 %d 条，"
                    "新增文本 %d，新增图片 %d，跳过 %d）",
                    group.get("group_id"),
                    result["pulled"],
                    result["inserted_text"],
                    result["inserted_images"],
                    result["skipped"],
                )
            logger.info(
                "[HistorySave] 重载自动补库：全部完成（共 %d 个群，拉取 %d 条，"
                "新增文本 %d，新增图片 %d，跳过 %d）",
                len(groups),
                total_pulled,
                total_text,
                total_images,
                total_skipped,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("[HistorySave] 重载自动补库任务异常", exc_info=True)
        finally:
            # 收尾链（F3/F6/R1）：补库历史写完 → 缓冲新消息带去重写入 → 快照层
            # 纳入回填历史；取消/异常时 finally 仍执行，各步独立 try/except 不冒泡
            if self.saver is not None:
                try:
                    await self.saver.end_backfill()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning(
                        "[HistorySave] 重载自动补库：缓冲消息带去重 flush 失败",
                        exc_info=True,
                    )
            if self.stats_service is not None:
                try:
                    await self.stats_service.startup_backfill()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning(
                        "[HistorySave] 重载自动补库：触发快照启动回填失败",
                        exc_info=True,
                    )

    async def _resolve_groups(self) -> list[dict]:
        """解析待补库群清单（all_mode 感知，范式同 stats/profile 群列表修复）。

        白名单模式：group_config 白名单中 enabled 的群。
        all_mode 全局记录模式：录制覆盖所有群、白名单无意义，改以
        chat_history 有数据的群为准——补库只补缺口，无数据的群拉协议端
        也是空，跳过即可。
        """
        try:
            all_mode = (
                await self.config_mgr.get_setting("all_mode", "false")
            ).strip().lower() == "true"
        except Exception:
            all_mode = False
        if all_mode:
            try:
                ids = await self.mysql_mgr.get_all_group_ids()
            except Exception:
                logger.warning(
                    "[HistorySave] 重载自动补库：全群模式读取群清单失败，跳过本轮补库",
                    exc_info=True,
                )
                return []
            return [{"group_id": gid, "enabled": True} for gid in ids or []]
        groups = await self.config_mgr.get_groups()
        return [g for g in groups if g.get("enabled")]

    async def _backfill_group(
        self, group, window_start, round_cap: int, max_rounds: int
    ) -> dict:
        """对单个启用群执行补库：翻页拉取（重叠边界停止）→ 解析 + 窗口过滤 → 排序 → 双去重 → 入库。

        插入顺序最旧→最新：保证 chat_history 自增 id 与时间单调（F2）。
        翻页前预加载该群最近已记录消息的 message_id/内容作重叠参照，每轮拉取后
        若本轮与已记录多数相同即停止翻页（到达已记录边界，v0.6.1）。
        Returns:
            dict: {"pulled", "inserted_text", "inserted_images", "skipped"}；
            pulled = 该群拉取并进入去重流程的总条数（= 解析通过且窗口内的条数）。
        """
        group_id = str(group.get("group_id"))
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

        # 2) 多轮翻页拉取原始消息（范式同 core/summary/onebot.py，内联实现）
        raw_messages: list = []
        seen_ids: set[str] = set()  # 跨轮 message_id 去重（翻页边界可能重叠）
        message_seq = 0  # 0 = 从最新消息开始
        window_start_unix = window_start.timestamp()  # 窗口提前终止判定用 unix 秒
        for round_no in range(1, max_rounds + 1):
            # 每轮请求条数 = min(单轮上限, 剩余上限)：总上限 = 单轮上限 × 最大轮数，
            # message_seq 翻页真实生效（F1）
            request_count = min(round_cap, total_cap - len(raw_messages))
            try:
                # 第 1 轮不传 message_seq（协议端按最新开始）：NapCat 等协议端
                # 对 message_seq=0 报「消息0不存在」，首轮失败会整群作废
                params = {"group_id": int(group_id), "count": request_count}
                if message_seq:
                    params["message_seq"] = message_seq
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
                if round_no == 1:
                    raise  # 首轮失败整组作废（由 _run_all 捕获该群）
                break  # 第 2 轮起失败降级：用已拉到的部分
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
                if round_no == 1:
                    raise
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
                if comparable and matches / comparable >= BACKFILL_OVERLAP_THRESHOLD:
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
            # 区分「正常无内容消息」（视频/表情/语音/转发等，无 text/image 段，
            # 是正常历史内容，不计失败）与「真有 text/image 但解析失败」
            has_text_image = _raw_has_content(raw)
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
            existing_ids = await self.mysql_mgr.get_existing_message_ids(group_id, ids)
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
            # 批内 URL 去重收窄为单消息内（F10）；跨消息/跨轮次去重仍由 existing_urls 承担
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

        # 7) 汇总：pulled = 该群拉取并进入去重流程的总条数
        return {
            "pulled": len(parsed),
            "inserted_text": inserted_text,
            "inserted_images": inserted_images,
            "skipped": skipped,
        }
