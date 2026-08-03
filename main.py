"""群聊记录存储插件主入口。

监听 QQ 群消息，将文本和图片分别存入 MySQL，提供管理指令和 Web 管理后台。
"""

import asyncio
import time
from collections import deque
from datetime import datetime

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.star.filter.command import GreedyStr

from .cleaner import ImageCleaner
from .db_config import ConfigManager
from .db_mysql import MySQLManager
from .profile.capture import extract_at_targets, extract_reply_id
from .profile.service import ProfileService
from .stats import StatsBuildError, StatsService
from .stats.models import StatsQuery
from .stats.parser import USAGE_TEXT, StatsParseError, parse_stats_args
from .summary import SummaryService
from .web_api import WebAPI

# 后台 MySQL 初始化的最大连续失败次数：超过后放弃重试并停用存储功能，
# 避免数据库长期不可用时无限刷日志。恢复方式：修正配置后在插件管理重启插件。
MAX_INIT_ATTEMPTS = 5


def extract_image_urls(message_obj, message_chain) -> list[str]:
    """提取图片的原始 http(s) 链接。

    优先从 OneBot 原始事件(message_obj.raw_message)的图片消息段提取 QQ 下发的
    原始链接;raw_message 不可用或提取不到时,回退到遍历消息链中的 Image 组件。
    本地文件路径一律不返回(新版 AstrBot 预处理会把组件 url 改写为本地临时路径)。

    Args:
        message_obj: event.message_obj(AstrBotMessage,其 raw_message 为 OneBot 原始事件)
        message_chain: event.get_messages() 返回的消息组件列表

    Returns:
        list[str]: 以 http 开头的图片链接列表
    """
    urls: list[str] = []

    # 优先从 OneBot 原始事件的消息段数组提取(QQ 下发的原始链接)
    raw = getattr(message_obj, "raw_message", None)
    segments = raw.get("message") if isinstance(raw, dict) else None
    if isinstance(segments, list):
        for seg in segments:
            try:
                if not isinstance(seg, dict) or seg.get("type") != "image":
                    continue
                data = seg.get("data") or {}
                if not isinstance(data, dict):
                    continue
                # 优先取 data.url;部分实现(如 Lagrange)把链接放在 data.file
                url = data.get("url")
                if isinstance(url, str) and url.startswith(("http://", "https://")):
                    urls.append(url)
                    continue
                file = data.get("file")
                if isinstance(file, str) and file.startswith(("http://", "https://")):
                    urls.append(file)
            except Exception as e:
                logger.debug(f"[HistorySave] 解析图片消息段失败: {e}")
                continue

    if urls:
        return urls

    # 回退:遍历消息链中的 Image 组件(本地临时路径一律不收集)
    try:
        for component in message_chain or []:
            if not isinstance(component, Comp.Image):
                continue
            value = component.url or component.file
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                urls.append(value)
    except Exception as e:
        logger.debug(f"[HistorySave] 遍历消息链提取图片链接失败: {e}")

    return urls


def stats_fallback_text(data, label: str) -> str:
    """T2I 渲染失败时的降级纯文本摘要（单行）。

    内容：时间范围 label + 总消息数 + 活跃成员数 + Top3 发言人（「昵称: N条」，
    昵称缺失时回退 QQ 号）。供 ``/群统计`` 指令渲染失败兜底使用。

    Args:
        data: ``StatsData``（或其鸭子同构对象），需含 total_messages /
            active_senders / sender_ranking（取前三条）。
        label: 时间范围展示文案（如「今日」/「近7天」）。
    """
    tops = []
    for item in (getattr(data, "sender_ranking", None) or [])[:3]:
        name = getattr(item, "sender_name", "") or getattr(item, "sender_id", "")
        tops.append(f"{name}: {getattr(item, 'count', 0)}条")
    top_text = "、".join(tops) if tops else "暂无发言数据"
    return (
        f"【{label}】总消息数：{getattr(data, 'total_messages', 0)}，"
        f"活跃成员：{getattr(data, 'active_senders', 0)}，"
        f"Top3 发言人：{top_text}"
    )


@register(
    "astrbot_plugin_group_history_save_mysql",
    "AnteriorTAg127",
    "将 QQ 群聊天记录保存到 MySQL，支持 Web 管理后台与群聊历史自动总结（MySQL 优先 + 协议端补齐）；人物分析支持群成员发言习惯与画像分析（@ 或 QQ 触发，Web 可跨群）；数据分析支持 Web 实时统计面板与 /群统计 指令报告卡（定时日报/周报推送 + 图片小时级快照统计）",
    "0.5.1",
)
class GroupHistoryPlugin(Star):
    """群聊记录存储插件。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config

        # 初始化 MySQL 管理器（动态连接池）
        # config 可能为 None（框架在 _conf_schema.json 缺失时以 config=None 实例化插件），
        # 统一用空 dict 兜底，保证默认值生效
        cfg = config or {}
        self.mysql_mgr = MySQLManager(
            host=cfg.get("mysql_host", "127.0.0.1"),
            port=cfg.get("mysql_port", 3306),
            user=cfg.get("mysql_user", "root"),
            password=cfg.get("mysql_password", ""),
            database=cfg.get("mysql_database", "astrbot_history"),
            pool_min_size=cfg.get("pool_min_size", 1),
            pool_max_size=cfg.get("pool_max_size", 10),
            pool_idle_timeout=cfg.get("pool_idle_timeout", 120),
            pool_timeout=cfg.get("pool_timeout", 30),
            pool_ping_cooldown=cfg.get("pool_ping_cooldown", 5),
        )

        # 初始化本地配置管理器
        self.config_mgr = ConfigManager()

        # 初始化图片清理器
        self.cleaner = ImageCleaner(self.mysql_mgr, self.config_mgr)

        # 初始化总结服务（v0.3，须在 WebAPI 之前构造，以便注入其存储实例）
        self.summary_service = SummaryService(
            context, self.config_mgr, self.mysql_mgr, self
        )

        # 初始化人物分析服务（v0.4，须在 WebAPI 之前构造，以便注入服务与其存储实例）
        self.profile_service = ProfileService(
            context, self.config_mgr, self.mysql_mgr, self
        )

        # 初始化数据分析服务（v0.5.0，须在 WebAPI 之前构造，以便注入服务实例；
        # 构造仅组装上游模块引用无 I/O，调度器在 MySQL 初始化成功后才 start）
        self.stats_service = StatsService(context, self.mysql_mgr, self.config_mgr)

        # 初始化 Web API（注入总结存储实例供总结历史端点使用，注入人物分析服务与存储实例）
        self.web_api = WebAPI(
            context,
            self.mysql_mgr,
            self.config_mgr,
            self.cleaner,
            summary_storage=self.summary_service.storage,
            summary_renderer=self.summary_service.renderer,
            profile_service=self.profile_service,
            profile_storage=self.profile_service.storage,
            profile_renderer=self.profile_service.renderer,
            stats_service=self.stats_service,
        )

        self._initialized = False
        self._init_task: asyncio.Task | None = None
        # 重试耗尽后置 True：消息直接丢弃（不再缓冲），停止一切存储活动
        self._db_gave_up = False
        # MySQL 后台初始化窗口内的消息缓冲（FIFO，溢出自动丢弃最旧记录）
        self._pending_records: deque[dict] = deque(maxlen=2000)
        self._last_drop_warn = 0.0  # 缓冲区溢出告警节流时间戳（monotonic 秒）

    async def initialize(self):
        """异步初始化：连接数据库、启动定时任务。

        MySQL 连接放入后台任务执行，避免数据库不可达时阻塞 AstrBot 启动。
        """
        # 初始化本地配置（aiosqlite 本地文件，极快，不会阻塞）
        config_ok = await self.config_mgr.initialize()
        if not config_ok:
            logger.error("[HistorySave] 本地配置初始化失败，插件功能受限")

        # MySQL 初始化放入后台，不阻塞框架启动
        self._init_task = asyncio.create_task(self._background_mysql_init())
        logger.info("[HistorySave] 插件已加载，MySQL 连接在后台初始化中")

    async def _background_mysql_init(self):
        """后台初始化 MySQL，失败时每 60 秒重试一次。

        连续失败 MAX_INIT_ATTEMPTS 次后放弃重试：关闭连接池、停用存储功能，
        避免数据库长期不可用时无限刷日志。每次尝试用 120 秒强制兜底。
        """
        retry_interval = 60
        # initialize() 内含 schema 迁移 DDL（大表 ADD INDEX 可能远超 10s），
        # 原 10s 超时会把 DDL 中途 cancel，导致连续 5 次失败后永久放弃存储；
        # 本任务为后台任务，放长超时不阻塞框架启动（F8）
        init_timeout = 120
        attempt = 0
        while not self._initialized:
            attempt += 1
            try:
                mysql_ok = await asyncio.wait_for(
                    self.mysql_mgr.initialize(), timeout=init_timeout
                )
                if mysql_ok:
                    self._initialized = True
                    # 先补录初始化窗口内缓冲的消息，再启动定时清理
                    await self._flush_pending_records()
                    await self.cleaner.start()
                    # 启动总结清理调度器（失败仅记日志，不阻断插件启动）
                    try:
                        await self.summary_service.start()
                    except Exception as e:
                        logger.error(f"[HistorySummary] 启动总结服务失败: {e}")
                    # 启动人物分析清理调度器（失败仅记日志，不阻断插件启动）
                    try:
                        await self.profile_service.start()
                    except Exception as e:
                        logger.error(f"[Profile] 启动人物分析服务失败: {e}")
                    # 启动数据分析调度器（失败仅记日志，不阻断插件启动）
                    try:
                        await self.stats_service.start()
                    except Exception as e:
                        logger.error(f"[Stats] 启动数据分析服务失败: {e}")
                    logger.info(
                        "[HistorySave] MySQL 连接成功，插件初始化完成，开始监听群消息"
                    )
                    return
                else:
                    logger.warning(
                        f"[HistorySave] MySQL 连接失败（第 {attempt} 次尝试），"
                        f"{retry_interval}s 后重试..."
                    )
            except asyncio.TimeoutError:
                logger.warning(
                    f"[HistorySave] MySQL 初始化超时（第 {attempt} 次尝试），"
                    f"{retry_interval}s 后重试..."
                )
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning(
                    f"[HistorySave] MySQL 初始化异常（第 {attempt} 次尝试）: {e}，"
                    f"{retry_interval}s 后重试..."
                )
            # 重试耗尽：放弃并停用存储功能（关闭连接池使后续操作快速失败）
            if attempt >= MAX_INIT_ATTEMPTS:
                self._db_gave_up = True
                # 明确记录被丢弃的缓冲消息条数，避免静默丢失无迹可查（F6）
                dropped = len(self._pending_records)
                logger.error(
                    f"[HistorySave] MySQL 连续 {attempt} 次连接失败，停止重试，"
                    f"消息存储功能已停用，同时丢弃启动窗口缓冲消息 {dropped} 条。"
                    f"请检查数据库配置与连通性，"
                    f"然后在插件管理中重启本插件以恢复。"
                )
                self._pending_records.clear()
                try:
                    await self.mysql_mgr.close()
                except Exception:
                    pass
                return
            # 等待后重试
            try:
                await asyncio.sleep(retry_interval)
            except asyncio.CancelledError:
                return

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def on_group_message(self, event: AstrMessageEvent):
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

    async def _flush_pending_records(self):
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

    @filter.command("history_start")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def history_start(self, event: AstrMessageEvent, group_id: str = ""):
        """开启指定群的聊天记录保存。

        用法: /history_start [群号]
        不填群号则默认为当前群。
        """
        target_group = self._resolve_group_id(event, group_id)
        if target_group is None:
            yield event.plain_result("请提供有效的群号，或在群内使用此指令。")
            return

        success = await self.config_mgr.add_group(target_group)
        if success:
            yield event.plain_result(f"已开启群 {target_group} 的聊天记录保存。")
        else:
            yield event.plain_result(f"开启群 {target_group} 记录失败，请检查日志。")

    @filter.command("history_stop")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def history_stop(self, event: AstrMessageEvent, group_id: str = ""):
        """关闭指定群的聊天记录保存。

        用法: /history_stop [群号]
        不填群号则默认为当前群。
        """
        target_group = self._resolve_group_id(event, group_id)
        if target_group is None:
            yield event.plain_result("请提供有效的群号，或在群内使用此指令。")
            return

        success = await self.config_mgr.remove_group(target_group)
        if success:
            yield event.plain_result(f"已关闭群 {target_group} 的聊天记录保存。")
        else:
            yield event.plain_result(f"关闭群 {target_group} 记录失败，请检查日志。")

    @filter.command("history_status")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def history_status(self, event: AstrMessageEvent):
        """查询聊天记录保存的状态。"""
        # 数据库连接状态
        ping = await self.mysql_mgr.ping()
        db_status = "✅ 已连接" if ping["connected"] else "❌ 未连接"
        latency = f"{ping['latency_ms']}ms" if ping["connected"] else "-"
        pool_info = ping.get("pool", {})
        pool_str = (
            f"{pool_info.get('used', 0)}活跃/"
            f"{pool_info.get('current_size', 0)}总计 "
            f"(范围 {pool_info.get('min_size', 1)}~{pool_info.get('max_size', 10)})"
        )

        # 统计信息
        stats = await self.mysql_mgr.get_stats()

        # 群列表
        groups = await self.config_mgr.get_groups()
        settings = await self.config_mgr.get_all_settings()
        all_mode = settings.get("all_mode", "false") == "true"

        enabled_groups = [g for g in groups if g["enabled"]]
        group_list = ", ".join(str(g["group_id"]) for g in enabled_groups) or "无"

        text = (
            f"📊 群聊记录存储状态\n"
            f"━━━━━━━━━━━━━━\n"
            f"数据库: {db_status} ({latency})\n"
            f"连接池: {pool_str}\n"
            f"ALL 模式: {'开启' if all_mode else '关闭'}\n"
            f"记录中的群: {group_list}\n"
            f"━━━━━━━━━━━━━━\n"
            f"今日消息: {stats.get('today_messages', 0)} 条\n"
            f"今日图片: {stats.get('today_images', 0)} 条\n"
            f"总消息: {stats.get('total_messages', 0)} 条\n"
            f"总图片: {stats.get('total_images', 0)} 条\n"
            f"━━━━━━━━━━━━━━\n"
            f"图片保留: {settings.get('image_retention_days', '3')} 天"
        )
        yield event.plain_result(text)

    @filter.command("history_clean")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def history_clean(self, event: AstrMessageEvent, days: str = ""):
        """手动清理过期的图片记录。

        用法: /history_clean [天数]
        不填天数则使用配置的默认值。
        """
        clean_days = None
        if days:
            try:
                clean_days = int(days)
                if clean_days < 1:
                    yield event.plain_result("天数不能小于 1。")
                    return
                if clean_days > 36500:
                    yield event.plain_result("天数过大（上限 36500 天）。")
                    return
            except ValueError:
                yield event.plain_result("请提供有效的天数（正整数）。")
                return

        deleted = await self.cleaner.manual_clean(clean_days)
        if deleted >= 0:
            if clean_days is not None:
                actual_days = clean_days
            else:
                # 配置值可能被篡改为非数字，兜底默认 3 天，避免指令抛异常
                try:
                    actual_days = int(
                        await self.config_mgr.get_setting("image_retention_days", "3")
                    )
                except (ValueError, TypeError):
                    actual_days = 3
            yield event.plain_result(
                f"清理完成：删除了 {deleted} 条 {actual_days} 天前的图片记录。"
            )
        else:
            yield event.plain_result("清理失败，请检查数据库连接。")

    @filter.command("消息总结", alias={"总结"})
    async def summary_count(self, event: AstrMessageEvent, arg: str = ""):
        """按条数总结群聊记录。用法: /消息总结 <数量>，如 /消息总结 512"""
        await self.summary_service.handle_count_command(event, arg)

    @filter.command("消息总结时间", alias={"总结时间"})
    async def summary_window(self, event: AstrMessageEvent, arg: str = ""):
        """按时间范围总结群聊记录。用法: /消息总结时间 <时长>，如 /消息总结时间 24h 或 1d"""
        await self.summary_service.handle_window_command(event, arg)

    @filter.command("人物分析", alias={"人物画像", "分析TA"})
    async def profile_analyze(self, event: AstrMessageEvent, arg: str = ""):
        """分析群成员发言习惯与人物画像。用法: /人物分析 [@成员 或 QQ号]"""
        await self.profile_service.handle_command(event, arg)

    @filter.command("群统计", alias={"群数据", "统计"})
    async def group_stats(self, event: AstrMessageEvent, arg: GreedyStr):
        """群/个人数据统计图片报告卡。

        用法: /群统计 [@某人 | QQ号] [时间范围]（参数顺序自由，均可省略）
        时间范围: 今日（默认）/ 昨日 / 7天 / 30天 / 全部 / 单日或区间日期

        参数注解用 ``GreedyStr``（框架内置指令同款写法）接收指令名之后的
        **全部剩余文本**：普通 ``arg: str`` 注入只给第一个空格分词，会把
        「2026-08-01 2026-08-04」空格分隔日期区间截断成单日。
        """
        try:
            # 仅群环境有效：私聊回用法提示
            group_id = event.get_group_id()
            if not group_id:
                yield event.plain_result(f"请在群内使用。\n{USAGE_TEXT}")
                return

            service = self.stats_service
            if service is None:
                yield event.plain_result("统计模块尚未就绪")
                return

            # 每群冷却（async，时长读 stats_cooldown）：冷却期内静默忽略——
            # 不回复，仅经 finally 的 stop_event 阻止指令文本流入 LLM
            if not await service.check_cooldown(str(group_id)):
                return

            # @ 目标：消息链提取（已剔除 "all"）后再剔除 bot 自身（写法同 profile）
            at_targets = extract_at_targets(event.get_messages())
            try:
                self_id = str(event.get_self_id() or "")
            except Exception:
                self_id = ""
            if self_id:
                at_targets = [qq for qq in at_targets if qq != self_id]

            # 解析指令参数（成员 + 时间范围）；失败回附带的用法文案
            try:
                member_id, time_range = parse_stats_args(arg or "", at_targets)
            except StatsParseError as e:
                yield event.plain_result(getattr(e, "usage", "") or USAGE_TEXT)
                return

            # 组装查询：top_n 读 stats_top_n 配置（typed 读取自带默认回退，
            # build_stats 内部再夹住 1–50 防御）
            query = StatsQuery(
                group_id=str(group_id),
                member_id=member_id,
                time_range=time_range,
                top_n=await self.config_mgr.get_stats_setting_typed("stats_top_n"),
            )
            try:
                data = await service.build_stats(query)
            except StatsBuildError as e:
                yield event.plain_result(str(e))
                return

            # 渲染报告卡：成功发图片；失败降级纯文本摘要
            image = await service.render(data, f"{group_id} 群聊统计")
            if image:
                yield event.image_result(image)
            else:
                yield event.plain_result(stats_fallback_text(data, time_range.label))
        except Exception as e:
            # 最外层兜底：任何异常不冒泡出 handler
            logger.error(f"[Stats] 群统计指令处理异常: {e}", exc_info=True)
            yield event.plain_result("统计失败，请稍后重试")
        finally:
            # 指令消息一律终止传播（含冷却静默路径），避免指令文本继续流入 LLM
            event.stop_event()

    def _resolve_group_id(
        self, event: AstrMessageEvent, group_id_str: str
    ) -> int | None:
        """解析目标群号：优先使用参数，否则使用当前群。"""
        if group_id_str:
            try:
                return int(group_id_str)
            except ValueError:
                return None
        # 尝试获取当前群号
        try:
            gid = event.get_group_id()
            if gid:
                return int(gid)
        except (ValueError, TypeError):
            pass
        return None

    async def terminate(self):
        """插件卸载/停用时清理资源。"""
        # 取消后台初始化任务（若仍在重试中）
        if self._init_task and not self._init_task.done():
            self._init_task.cancel()
            try:
                await self._init_task
            except asyncio.CancelledError:
                pass
        # 停止数据分析调度器（LIFO：最后启动的最先停止；吞 CancelledError 不阻塞后续清理）
        try:
            await self.stats_service.stop()
        except asyncio.CancelledError:
            pass
        # 停止总结清理调度器（与 cleaner 停止并列，吞 CancelledError 不阻塞后续清理）
        try:
            await self.summary_service.stop()
        except asyncio.CancelledError:
            pass
        # 停止人物分析清理调度器（与总结停止并列，吞 CancelledError 不阻塞后续清理）
        try:
            await self.profile_service.stop()
        except asyncio.CancelledError:
            pass
        await self.cleaner.stop()
        await self.mysql_mgr.close()
        await self.config_mgr.close()
        logger.info("[HistorySave] 插件已安全停止")
