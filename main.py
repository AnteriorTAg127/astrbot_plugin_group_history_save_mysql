"""群聊记录存储插件主入口。

监听 QQ 群消息，将文本和图片分别存入 MySQL，提供管理指令和 Web 管理后台。
"""

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .cleaner import ImageCleaner
from .db_config import ConfigManager
from .db_mysql import MySQLManager
from .web_api import WebAPI


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


@register(
    "astrbot_plugin_group_history_save_mysql",
    "AnteriorTAg127",
    "将 QQ 群聊天记录保存到 MySQL，支持 Web 管理后台",
    "0.2.1",
)
class GroupHistoryPlugin(Star):
    """群聊记录存储插件。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        # 初始化 MySQL 管理器（动态连接池）
        self.mysql_mgr = MySQLManager(
            host=config.get("mysql_host", "127.0.0.1"),
            port=config.get("mysql_port", 3306),
            user=config.get("mysql_user", "root"),
            password=config.get("mysql_password", ""),
            database=config.get("mysql_database", "astrbot_history"),
            pool_min_size=config.get("pool_min_size", 1),
            pool_max_size=config.get("pool_max_size", 10),
            pool_idle_timeout=config.get("pool_idle_timeout", 120),
            pool_timeout=config.get("pool_timeout", 30),
        )

        # 初始化本地配置管理器
        self.config_mgr = ConfigManager()

        # 初始化图片清理器
        self.cleaner = ImageCleaner(self.mysql_mgr, self.config_mgr)

        # 初始化 Web API
        self.web_api = WebAPI(context, self.mysql_mgr, self.config_mgr, self.cleaner)

        self._initialized = False

    async def initialize(self):
        """异步初始化：连接数据库、启动定时任务。"""
        # 初始化本地配置
        config_ok = await self.config_mgr.initialize()
        if not config_ok:
            logger.error("[HistorySave] 本地配置初始化失败，插件功能受限")

        # 初始化 MySQL
        mysql_ok = await self.mysql_mgr.initialize()
        if mysql_ok:
            self._initialized = True
            # 启动图片清理任务
            await self.cleaner.start()
            logger.info("[HistorySave] 插件初始化完成，开始监听群消息")
        else:
            logger.error("[HistorySave] MySQL 连接失败，聊天记录将不会被保存")

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def on_group_message(self, event: AstrMessageEvent):
        """监听群消息并保存到 MySQL。"""
        if not self._initialized:
            return

        try:
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

            # 保存文本消息
            if text_parts:
                content = "\n".join(text_parts)
                msg_type = "mixed" if image_urls else "text"
                await self.mysql_mgr.insert_chat_message(
                    group_id=group_id,
                    sender_id=sender_id,
                    sender_name=sender_name,
                    message_type=msg_type,
                    content=content,
                    message_id=message_id,
                )

            # 保存图片记录
            for url in image_urls:
                await self.mysql_mgr.insert_image_record(
                    group_id=group_id,
                    sender_id=sender_id,
                    image_url=url,
                    sender_name=sender_name,
                )

        except Exception as e:
            logger.error(f"[HistorySave] 处理群消息时出错: {e}", exc_info=True)

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
            except ValueError:
                yield event.plain_result("请提供有效的天数（正整数）。")
                return

        deleted = await self.cleaner.manual_clean(clean_days)
        if deleted >= 0:
            actual_days = clean_days or int(
                await self.config_mgr.get_setting("image_retention_days", "3")
            )
            yield event.plain_result(
                f"清理完成：删除了 {deleted} 条 {actual_days} 天前的图片记录。"
            )
        else:
            yield event.plain_result("清理失败，请检查数据库连接。")

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
        await self.cleaner.stop()
        await self.mysql_mgr.close()
        await self.config_mgr.close()
        logger.info("[HistorySave] 插件已安全停止")
