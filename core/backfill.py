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
"""

import asyncio
from datetime import datetime, timedelta

from astrbot.api import logger

from .parsing import parse_onebot_raw_message

# ===== 拉取/翻页参数 =====（值严格如下，注释说明含义）
BACKFILL_MAX_PER_GROUP = 1000  # 每组每轮补库最多拉取条数
DEFAULT_TIMEOUT = 15  # 协议端调用超时（秒）
MAX_ROUNDS = 5  # 最大翻页轮数
ROUND_DELAY_SECONDS = 0.3  # 轮间延迟（秒），规避协议端限频
PER_ROUND_CAP = 1000  # 单轮请求条数上限
BACKFILL_HOURS_DEFAULT = 12  # 默认窗口小时数
BACKFILL_HOURS_MIN = 1  # 窗口下限
BACKFILL_HOURS_MAX = 168  # 窗口上限（7 天）

_ZERO_COUNTS = {
    "pulled": 0,
    "inserted_text": 0,
    "inserted_images": 0,
    "skipped": 0,
}


class ReloadBackfill:
    """重载自动补库服务：MySQL 就绪后从 OneBot 拉取历史消息补齐窗口缺口。"""

    def __init__(self, context, mysql_mgr, config_mgr):
        self.context = context
        self.mysql_mgr = mysql_mgr
        self.config_mgr = config_mgr
        self._task: asyncio.Task | None = None

    async def start(self):
        """启动重载自动补库后台任务（幂等；开关关闭时跳过）。

        读 ``backfill_enabled``（非 "true" 跳过）与 ``backfill_hours``（int 转换
        失败回退 12，夹取 [1,168]），计算窗口起点后以 create_task 发起
        后台任务；整体 try/except 兜底（CancelledError 透传）。
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
            window_start = datetime.now() - timedelta(hours=hours)
            self._task = asyncio.create_task(self._run_all(window_start))
            logger.info(
                "[HistorySave] 重载自动补库：后台任务已启动（窗口 %s 小时）",
                hours,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("[HistorySave] 重载自动补库启动失败", exc_info=True)

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

    async def _run_all(self, window_start):
        """逐群串行补库并输出汇总日志；单群失败不阻断其他群。"""
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
                    result = await self._backfill_group(group, window_start)
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

    async def _backfill_group(self, group, window_start) -> dict:
        """对单个启用群执行补库：翻页拉取 → 解析 + 窗口过滤 → 双去重 → 入库。

        Returns:
            dict: {"pulled", "inserted_text", "inserted_images", "skipped"}；
            pulled = 该群拉取并进入去重流程的总条数（= 解析通过且窗口内的条数）。
        """
        group_id = str(group.get("group_id"))

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
        for round_no in range(1, MAX_ROUNDS + 1):
            try:
                resp = await asyncio.wait_for(
                    client.api.call_action(
                        "get_group_msg_history",
                        group_id=int(group_id),
                        message_seq=message_seq,
                        count=PER_ROUND_CAP,
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

            # 逐条收录原始消息 + 记录本轮最早 seq 供翻页
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
                seen_ids.add(msg_id)
                raw_messages.append(raw)

            # 终止条件：短页（缓存到头）/ 已凑满上限 / 协议端未回 seq 无法翻页
            if len(raw_messages) >= BACKFILL_MAX_PER_GROUP:
                break
            if len(messages) < PER_ROUND_CAP:
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
            if round_no < MAX_ROUNDS:
                await asyncio.sleep(ROUND_DELAY_SECONDS)

        # 3) 解析 + 窗口过滤：仅保留窗口内（timestamp >= window_start）的消息
        parsed: list[dict] = []
        for raw in raw_messages:
            try:
                item = parse_onebot_raw_message(raw, group_id)
            except Exception:
                item = None
            if item is None:
                logger.debug(
                    "[HistorySave] 重载自动补库：解析单条消息失败（群 %s），已跳过",
                    group_id,
                )
                continue
            if item["timestamp"] < window_start:
                continue
            parsed.append(item)

        # 4) 去重：message_id 批量比对已存在；图片 URL 按群批量比对已存在
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

        # 5) 逐条入库：message_id 已存在跳过；文本/图片分别入库，单条失败不中断
        skipped = 0
        inserted_text = 0
        inserted_images = 0
        inserted_urls: set[str] = set()  # 本批已插入 URL（防批内重复）
        for item in parsed:
            if item["message_id"] and item["message_id"] in existing_ids:
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
            for url in image_urls:
                if url in existing_urls or url in inserted_urls:
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
                    inserted_urls.add(url)

        # 6) 汇总：pulled = 该群拉取并进入去重流程的总条数
        return {
            "pulled": len(parsed),
            "inserted_text": inserted_text,
            "inserted_images": inserted_images,
            "skipped": skipped,
        }
