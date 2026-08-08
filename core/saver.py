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
    """

    def __init__(self, mysql_mgr, config_mgr, stats_service=None):
        self.mysql_mgr = mysql_mgr
        self.config_mgr = config_mgr
        self.stats_service = stats_service
        self._initialized = False
        # 重试耗尽后置 True：消息直接丢弃（不再缓冲），停止一切存储活动
        self._db_gave_up = False
        # MySQL 后台初始化窗口内的消息缓冲（FIFO，溢出自动丢弃最旧记录）
        self._pending_records: deque[dict] = deque(maxlen=2000)
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

    async def handle_group_message(self, event: AstrMessageEvent):
        """监听群消息并保存到 MySQL。

        MySQL 后台初始化窗口内（首次启动/重连）消息先缓冲，
        初始化成功后统一补录，避免该窗口内的消息丢失。
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

            if not self._initialized:
                # MySQL 尚在后台初始化：缓冲待补录，避免启动窗口丢消息
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
        """MySQL 初始化窗口内缓冲消息，待初始化成功后补录。

        缓冲区为固定容量 deque（maxlen=2000），溢出时自动丢弃最旧记录；
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

    async def flush_pending(self):
        """将初始化窗口内缓冲的消息补录到 MySQL。

        单条失败仅记日志并继续，避免一条坏数据阻塞整批补录。
        补录失败不再退回缓冲（避免自引用循环）：_persist_message 以
        rebuffer_on_fail=False 调用，失败记录记日志后丢弃。
        """
        if not self._pending_records:
            return
        pending = list(self._pending_records)
        self._pending_records.clear()
        ok = 0
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
            except Exception as e:
                logger.error(f"[HistorySave] 补录缓冲消息失败: {e}")
        logger.info(f"[HistorySave] 启动窗口缓冲消息补录完成: {ok}/{len(pending)} 条")
