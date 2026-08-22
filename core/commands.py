"""群管理/清理/补库 指令执行器（v0.8.1 自 main.py 迁出）。

承接 main.py ``GroupHistoryPlugin`` 迁出的历史管理类指令函数体（纯搬移，
零行为变化；每个方法返回用户可见的回复文案，由 main.py 薄 handler 统一
``yield event.plain_result(...)``）：

- :meth:`GroupCommands.group_start`：原 ``history_start`` 函数体
- :meth:`GroupCommands.group_stop`：原 ``history_stop`` 函数体
- :meth:`GroupCommands.group_status`：原 ``history_status`` 函数体
- :meth:`GroupCommands.group_clean`：原 ``history_clean`` 函数体
- :meth:`GroupCommands.group_backfill`：原 ``force_backfill`` 函数体

群号解析（原 ``_resolve_group_id``）迁至 :mod:`core.parsing` 的
:func:`resolve_group_id`。
"""

from astrbot.api import logger

from .parsing import resolve_group_id


class GroupCommands:
    """历史管理类指令执行器：开启/关闭/状态/清理/强制补库。

    各方法返回待发送的回复文案；纯函数式组织，供 main.py 薄 handler
    委托后 ``yield event.plain_result(reply)``。
    """

    def __init__(self, mysql_mgr, config_mgr, cleaner, backfill):
        self.mysql_mgr = mysql_mgr
        self.config_mgr = config_mgr
        self.cleaner = cleaner
        self.backfill = backfill

    async def group_start(self, event, group_id: str = "") -> str:
        """开启指定群的聊天记录保存。返回回复文案。"""
        target_group = resolve_group_id(event, group_id)
        if target_group is None:
            return "请提供有效的群号，或在群内使用此指令。"

        success = await self.config_mgr.add_group(target_group)
        if success:
            return f"已开启群 {target_group} 的聊天记录保存。"
        return f"开启群 {target_group} 记录失败，请检查日志。"

    async def group_stop(self, event, group_id: str = "") -> str:
        """关闭指定群的聊天记录保存。返回回复文案。"""
        target_group = resolve_group_id(event, group_id)
        if target_group is None:
            return "请提供有效的群号，或在群内使用此指令。"

        success = await self.config_mgr.remove_group(target_group)
        if success:
            return f"已关闭群 {target_group} 的聊天记录保存。"
        return f"关闭群 {target_group} 记录失败，请检查日志。"

    async def group_status(self, event) -> str:
        """查询聊天记录保存的状态。返回回复文案。"""
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
        return text

    async def group_clean(self, event, days: str = "") -> str:
        """手动清理过期的图片记录。返回回复文案。"""
        clean_days = None
        if days:
            try:
                clean_days = int(days)
                if clean_days < 1:
                    return "天数不能小于 1。"
                if clean_days > 36500:
                    return "天数过大（上限 36500 天）。"
            except ValueError:
                return "请提供有效的天数（正整数）。"

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
            return f"清理完成：删除了 {deleted} 条 {actual_days} 天前的图片记录。"
        return "清理失败，请检查数据库连接。"

    async def group_backfill(
        self, event, group_id: str = "", hours: str = ""
    ) -> str:
        """强制对指定群补库（管理员，可随时触发一次）。返回回复文案。

        用法: /补库 [群号] [小时数]
        不填群号默认当前群；不填小时数按「该群最后记录时间 − 5 分钟」窗口补，
        填小时数则强制回补最近 N 小时（如 /补库 123456 24）。
        """
        target_group = resolve_group_id(event, group_id)
        if target_group is None:
            return "请提供有效的群号，或在群内使用此指令。"
        hours_val = None
        if hours.strip():
            try:
                hours_val = int(hours)
                if hours_val < 1:
                    return "小时数必须为正整数（1~168）。"
            except ValueError:
                return "小时数必须为正整数。"
        try:
            started = await self.backfill.force_backfill(
                str(target_group), hours=hours_val
            )
        except Exception as e:
            logger.error(f"[HistorySave] 强制补库失败: {e}", exc_info=True)
            return "强制补库启动失败，请查看日志。"
        if started:
            return f"已开始对群 {target_group} 强制补库，进度请查看日志。"
        return "补库未启动：该群补库可能正在执行，或存储尚未就绪。"


__all__ = ["GroupCommands"]
