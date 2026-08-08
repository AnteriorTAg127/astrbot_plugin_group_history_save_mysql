"""消息保存器（v0.6.0 自 main.py 迁出）。

承接 main.py ``GroupHistoryPlugin`` 迁出的消息保存逻辑（解析/缓冲/落库/补录），
纯搬移零行为变化：

- :func:`MessageSaver.handle_group_message`：原 ``on_group_message`` 函数体
  （到达时刻/群成员提取、白名单检查、stats 推送目标登记、消息链解析、
  图片链接提取、@ 与回复提取、无内容丢弃、未初始化缓冲、落库分支）
- :func:`MessageSaver._buffer_message` / :func:`MessageSaver._persist_message`：
  逐字迁移
- :func:`MessageSaver.flush_pending`：原 ``_flush_pending_records``（更名，
  内部逻辑不变）
"""

import time
from collections import deque
from datetime import datetime

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from .parsing import extract_image_urls
from .profile.capture import extract_at_targets, extract_reply_id


class MessageSaver:
    """消息保存器：监听群消息并保存到 MySQL（解析/缓冲/落库/补录）。

    MySQL 后台初始化窗口内（首次启动/重连）消息先缓冲，初始化成功后统一补录，
    避免该窗口内的消息丢失；重试耗尽（_db_gave_up）后直接丢弃。
    v0.6.1 补库门控缓冲：补库进行期间（_backfill_running）实时消息同样先缓冲，
    补库全部写完后再由 end_backfill 带去重 flush（message_id/图片 URL 双维查重），
    将实时与补库两条写路径由并发改串行，消除竞态重复行。
    """

    def __init__(self, mysql_mgr, config_mgr, stats_service=None):
        self.mysql_mgr = mysql_mgr
        self.config_mgr = config_mgr
        self.stats_service = stats_service
        self._initialized = False
        # 重试耗尽后置 True：消息直接丢弃（不再缓冲），停止一切存储活动
        self._db_gave_up = False
        # 补库进行中的群集合（v0.6.1 消息触发式补库）：这些群的实时消息先缓冲
        # 不落库，该群补库全部写完后再带去重 flush（其余群正常实时落库）
        self._backfill_groups: set[str] = set()
        # 初始化窗口 / 补库门控期间的缓冲（FIFO，溢出自动丢弃最旧记录）
        self._pending_records: deque[dict] = deque(maxlen=5000)
        self._last_drop_warn = 0.0  # 缓冲区溢出告警节流时间戳（monotonic 秒）

    def set_initialized(self):
        """标记 MySQL 初始化完成：此后消息直接落库而非缓冲。"""
        self._initialized = True

    @property
    def is_initialized(self) -> bool:
        """MySQL 是否已完成初始化（供 main.py 后台重试循环读取）。"""
        return self._initialized

    def mark_gave_up(self) -> int:
        """标记重试耗尽：停用存储并清空缓冲，返回被丢弃的缓冲条数（供日志）。"""
        self._db_gave_up = True
        dropped = len(self._pending_records)
        self._pending_records.clear()
        return dropped

    @property
    def has_pending(self) -> bool:
        """是否存在待补录的缓冲消息。"""
        return bool(self._pending_records)

    def begin_backfill(self, group_id: str):
        """标记某群补库开始：此后该群实时消息先缓冲不落库，补库写完后再 flush。"""
        self._backfill_groups.add(group_id)

    async def end_backfill(self, group_id: str) -> int:
        """标记某群补库结束，带去重 flush 该群补库期间缓冲的实时消息。

        先从门控集合移除该群（此后该群新消息恢复实时落库），再以 dedup=True
        按群调用 flush_pending：批量查已存在 message_id / 图片 URL，跳过补库
        已写入的记录后落库，返回落库成功条数。
        """
        self._backfill_groups.discard(group_id)
        return await self.flush_pending(dedup=True, group_id=group_id)

    async def handle_group_message(self, event: AstrMessageEvent):
        """监听群消息并保存到 MySQL。

        MySQL 后台初始化窗口内（首次启动/重连）消息先缓冲，
        初始化成功后统一补录，避免该窗口内的消息丢失。
        补库进行中（v0.6.1 门控缓冲）同样先缓冲，补库完成后再带去重 flush。
        重试耗尽（_db_gave_up）后直接丢弃，不再做任何存储动作。
        """
        if self._db_gave_up:
            return
        try:
            # 记录消息到达时刻：实时入库与缓冲补录共用，
            # 保证补录消息的时间戳为真实到达时刻而非补录时刻（F11）
            arrived_at = datetime.now()
            group_id = str(event.get_group_id())
            sender_id = str(event.get_sender_id())
            sender_name = event.get_sender_name()
            message_id = event.message_obj.message_id or ""

            # 检查群是否在白名单中（本地白名单以整数群号匹配）
            try:
                group_id_int = int(group_id)
            except (ValueError, TypeError):
                return
            if not await self.config_mgr.is_group_enabled(group_id_int):
                return

            # v0.5.0 数据分析：白名单群的消息经过即登记 群→umo 推送目标缓存
            # （定时日报/周报经 context.send_message(umo) 主动推送；服务未创建时跳过）
            if self.stats_service is not None:
                self.stats_service.record_group_umo(group_id, event.unified_msg_origin)

            # 解析消息链
            message_chain = event.get_messages()
            text_parts = []
            image_count = 0

            for component in message_chain:
                if isinstance(component, Comp.Plain):
                    text = component.text.strip()
                    if text:
                        text_parts.append(text)
                elif isinstance(component, Comp.Image):
                    image_count += 1

            # 提取图片原始链接（优先 OneBot 原始事件中的图片消息段）
            image_urls = extract_image_urls(event.message_obj, message_chain)
            if image_count > 0 and not image_urls:
                logger.warning("[HistorySave] 图片消息未能提取到原始链接,已跳过入库")

            # 提取 @ 对象与回复目标（v0.4 人物分析关系数据基础；防御式纯函数，失败为空不阻断）
            at_targets = extract_at_targets(message_chain)
            at_list_str = ",".join(at_targets)
            reply_id = extract_reply_id(event)

            if not text_parts and not image_urls:
                return

            if not self._initialized or group_id in self._backfill_groups:
                # MySQL 尚在后台初始化，或该群补库进行中（v0.6.1 消息触发式门控）：
                # 实时消息先缓冲不落库，待初始化完成 / 该群补库全部写完后再统一补录
                self._buffer_message(
                    group_id,
                    sender_id,
                    sender_name,
                    text_parts,
                    image_urls,
                    message_id,
                    at_list=at_list_str,
                    reply_id=reply_id,
                    timestamp=arrived_at,
                )
                return

            await self._persist_message(
                group_id,
                sender_id,
                sender_name,
                text_parts,
                image_urls,
                message_id,
                at_list=at_list_str,
                reply_id=reply_id,
                timestamp=arrived_at,
            )

        except Exception as e:
            logger.error(f"[HistorySave] 处理群消息时出错: {e}", exc_info=True)

    def _buffer_message(
        self,
        group_id: str,
        sender_id: str,
        sender_name: str,
        text_parts: list[str],
        image_urls: list[str],
        message_id: str,
        at_list: str = "",
        reply_id: str = "",
        timestamp: datetime | None = None,
    ):
        """缓冲消息，待初始化完成 / 补库结束（带去重）后统一补录。

        缓冲区为固定容量 deque（maxlen=5000），溢出时自动丢弃最旧记录；
        丢弃告警每 60 秒最多输出一次，避免刷屏。

        Args:
            timestamp: 消息到达时刻；None 时取当前时间。补录时透传给
                insert_chat_message，保证补录消息使用真实到达时间（F11）
        """
        if len(self._pending_records) == self._pending_records.maxlen:
            now = time.monotonic()
            if now - self._last_drop_warn >= 60:
                self._last_drop_warn = now
                logger.warning("[HistorySave] 消息缓冲区已满，最旧的缓冲消息将被丢弃")
        self._pending_records.append(
            {
                "group_id": group_id,
                "sender_id": sender_id,
                "sender_name": sender_name,
                "text_parts": text_parts,
                "image_urls": image_urls,
                "message_id": message_id,
                "at_list": at_list,
                "reply_id": reply_id,
                # 记录消息到达时刻，补录时透传入库（F11）
                "timestamp": timestamp or datetime.now(),
            }
        )

    async def _persist_message(
        self,
        group_id: str,
        sender_id: str,
        sender_name: str,
        text_parts: list[str],
        image_urls: list[str],
        message_id: str,
        at_list: str = "",
        reply_id: str = "",
        timestamp: datetime | None = None,
        rebuffer_on_fail: bool = True,
    ) -> bool:
        """将一条已解析的消息写入 MySQL（文本记录 + 图片记录）。

        Args:
            timestamp: 消息到达时刻；None 时由 insert_chat_message 回退为当前时间。
                补录路径透传缓冲时记录的到达时刻，保证时间窗统计正确（F11）
            rebuffer_on_fail: 写入失败时是否将失败部分退回 _pending_records 等待
                下次补录。实时路径为 True；补录路径必须传 False，避免失败记录
                反复入缓冲形成自引用循环（F5）

        Returns:
            bool: 文本与图片是否全部写入成功
        """
        all_ok = True

        # 保存文本消息
        text_ok = True
        if text_parts:
            content = "\n".join(text_parts)
            msg_type = "mixed" if image_urls else "text"
            text_ok = await self.mysql_mgr.insert_chat_message(
                group_id=group_id,
                sender_id=sender_id,
                sender_name=sender_name,
                message_type=msg_type,
                content=content,
                message_id=message_id,
                at_list=at_list,
                reply_id=reply_id,
                timestamp=timestamp,
            )
            if not text_ok:
                all_ok = False

        # 保存图片记录（收集失败链接，便于只退回失败部分）
        failed_urls: list[str] = []
        for url in image_urls:
            img_ok = await self.mysql_mgr.insert_image_record(
                group_id=group_id,
                sender_id=sender_id,
                image_url=url,
                sender_name=sender_name,
            )
            if img_ok:
                continue
            all_ok = False
            failed_urls.append(url)

        # 写入失败兜底（F5）：仅实时路径把失败部分退回缓冲等待下次补录，
        # 不再静默丢失。只退回失败部分（文本失败 → 保留文本；图片失败 →
        # 仅保留失败链接），补录重放时不会重复写入已成功的部分。
        # 补录路径（rebuffer_on_fail=False）失败仅记日志后丢弃，由下方
        # 各 insert 的 ERROR 日志与 _flush_pending_records 的计数体现。
        if not all_ok and rebuffer_on_fail:
            logger.warning(
                f"[HistorySave] 消息写入 MySQL 失败，已退回缓冲区等待补录"
                f"（群 {group_id}，消息 {message_id or '无ID'}）"
            )
            self._buffer_message(
                group_id,
                sender_id,
                sender_name,
                text_parts if not text_ok else [],
                failed_urls,
                message_id,
                at_list=at_list if not text_ok else "",
                reply_id=reply_id if not text_ok else "",
                timestamp=timestamp,
            )

        return all_ok

    async def flush_pending(self, dedup: bool = False, group_id: str | None = None) -> int:
        """将缓冲的消息补录到 MySQL。

        默认路径（dedup=False，MySQL 初始化窗口调用）行为与 v0.6.0 基本一致：
        逐条落库。与旧实现的差异：单条写入失败时把该条回写 _pending_records 左端，
        等待下次 flush 重试，避免 MySQL 短暂不可用时整批缓冲被静默清空。
        去重路径（dedup=True，end_backfill 调用）：补库刚写完，按群批量查询已存在
        message_id / 图片 URL，跳过补库已写入的整条记录、过滤已存在的图片 URL 后
        再落库，从数据面消除两条写路径的重复行（不建唯一索引）；空 message_id 的
        记录不去重、正常落库（v0.6.1 设计：空 id 消息只来自实时缓冲单源）。
        group_id: 仅 flush 该群的缓冲记录（v0.6.1 按群门控）；None 全量 flush。

        Returns:
            int: 成功落库的条数（含跳过的去重条目不计）；空缓冲或全失败返回 0。
            旧实现 dedup=False 时返回 None，现统一为 int，调用方无需区分类型。
        """
        if not self._pending_records:
            return 0
        if group_id is not None:
            pending = [r for r in self._pending_records if r["group_id"] == group_id]
            remaining = [
                r for r in self._pending_records if r["group_id"] != group_id
            ]
            self._pending_records.clear()
            self._pending_records.extend(remaining)
        else:
            pending = list(self._pending_records)
            self._pending_records.clear()
        ok = 0
        # 失败回写缓冲的辅助函数：左端追加保序，等待下次 flush 重试。
        # deque maxlen=5000，回写超出时自动丢弃最旧记录（已有溢出告警机制）。
        def _rebuffer(record: dict) -> None:
            self._pending_records.appendleft(record)

        if not dedup:
            # —— 初始化窗口路径（v0.6.0 既有行为 + 失败回缓冲）——
            for record in pending:
                try:
                    flushed = await self._persist_message(
                        record["group_id"],
                        record["sender_id"],
                        record["sender_name"],
                        record["text_parts"],
                        record["image_urls"],
                        record["message_id"],
                        at_list=record.get("at_list", ""),
                        reply_id=record.get("reply_id", ""),
                        # 透传缓冲时记录的到达时刻；旧格式记录无该字段时回退 None
                        timestamp=record.get("timestamp"),
                        rebuffer_on_fail=False,
                    )
                    if flushed:
                        ok += 1
                    else:
                        # _persist_message 返回 False 表示写入失败但未抛异常
                        # （insert_xxx 内部已吞错返回 False）；回缓冲等下次重试，
                        # 避免缓冲被静默清空导致数据丢失
                        _rebuffer(record)
                except Exception as e:
                    logger.error(f"[HistorySave] 补录缓冲消息失败: {e}")
                    # 异常路径同样回缓冲：可能是连接临时断开，下次 flush 可恢复
                    _rebuffer(record)
            logger.info(
                f"[HistorySave] 启动窗口缓冲消息补录完成: {ok}/{len(pending)} 条"
                f"（失败 {len(pending) - ok} 条已回缓冲）"
            )
            return ok

        # —— 补库门控去重路径（end_backfill 调用）——
        # 按群分组，逐组批量查已存在 message_id / 图片 URL；两个查询各自兜底：
        # 异常记 error 日志并退化为空集（按"全不存在"处理），不阻断 flush
        by_group: dict[str, list[dict]] = {}
        for record in pending:
            by_group.setdefault(record["group_id"], []).append(record)
        for gid, records in by_group.items():
            existing_ids: set[str] = set()
            existing_urls: set[str] = set()
            ids = [r["message_id"] for r in records if r["message_id"]]
            urls = [url for r in records for url in r["image_urls"] if url]
            try:
                if ids:
                    existing_ids = await self.mysql_mgr.get_existing_message_ids(
                        gid, ids
                    )
            except Exception as e:
                logger.error(f"[HistorySave] 查询已存在消息 ID 失败: {e}")
            try:
                if urls:
                    existing_urls = await self.mysql_mgr.get_existing_image_urls(
                        gid, urls
                    )
            except Exception as e:
                logger.error(f"[HistorySave] 查询已存在图片链接失败: {e}")
            for record in records:
                # 空 message_id 不去重、正常落库（v0.6.1 设计：空 id 消息只来自
                # 实时缓冲单源，补库侧已跳过，无重复源）
                if not record["message_id"]:
                    remain_urls = record["image_urls"]
                # 补库已写入的整条记录：跳过，计数不算成功
                elif record["message_id"] in existing_ids:
                    continue
                else:
                    # 过滤掉补库已写入的图片 URL，其余链接照常落库
                    remain_urls = [
                        url for url in record["image_urls"] if url not in existing_urls
                    ]
                try:
                    flushed = await self._persist_message(
                        record["group_id"],
                        record["sender_id"],
                        record["sender_name"],
                        record["text_parts"],
                        remain_urls,
                        record["message_id"],
                        at_list=record.get("at_list", ""),
                        reply_id=record.get("reply_id", ""),
                        timestamp=record.get("timestamp"),
                        rebuffer_on_fail=False,
                    )
                    if flushed:
                        ok += 1
                    else:
                        _rebuffer(record)
                except Exception as e:
                    logger.error(f"[HistorySave] 补录缓冲消息失败: {e}")
                    _rebuffer(record)
        failed = len(pending) - ok
        logger.info(
            f"[HistorySave] 补库期间缓冲消息补录完成: {ok}/{len(pending)} 条"
            + (f"（失败 {failed} 条已回缓冲）" if failed else "")
        )
        return ok
