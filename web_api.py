"""Web API 后端。

负责注册和处理所有 Web 管理后台的 API 请求。
"""

from astrbot.api import logger
from astrbot.api.star import Context
from astrbot.api.web import error_response, json_response, request

from .cleaner import ImageCleaner
from .db_config import ConfigManager
from .db_mysql import MySQLManager

PLUGIN_NAME = "astrbot_plugin_group_history_save_mysql"


class WebAPI:
    """Web 管理后台 API 处理器。"""

    def __init__(
        self,
        context: Context,
        mysql_mgr: MySQLManager,
        config_mgr: ConfigManager,
        cleaner: ImageCleaner,
    ):
        self.context = context
        self.mysql_mgr = mysql_mgr
        self.config_mgr = config_mgr
        self.cleaner = cleaner
        self._register_routes()

    def _register_routes(self):
        """注册所有 Web API 路由。"""
        routes = [
            (f"/{PLUGIN_NAME}/status", self.api_status, ["GET"], "数据库状态"),
            (f"/{PLUGIN_NAME}/groups", self.api_get_groups, ["GET"], "获取群列表"),
            (
                f"/{PLUGIN_NAME}/groups/toggle",
                self.api_toggle_group,
                ["POST"],
                "切换群状态",
            ),
            (f"/{PLUGIN_NAME}/groups/add", self.api_add_group, ["POST"], "添加群"),
            (
                f"/{PLUGIN_NAME}/groups/remove",
                self.api_remove_group,
                ["POST"],
                "移除群",
            ),
            (f"/{PLUGIN_NAME}/settings", self.api_get_settings, ["GET"], "获取设置"),
            (
                f"/{PLUGIN_NAME}/settings/save",
                self.api_save_settings,
                ["POST"],
                "保存设置",
            ),
            (f"/{PLUGIN_NAME}/stats/daily", self.api_daily_stats, ["GET"], "每日统计"),
            (f"/{PLUGIN_NAME}/clean", self.api_clean, ["POST"], "手动清理"),
            (f"/{PLUGIN_NAME}/query", self.api_query, ["GET"], "查询聊天记录"),
        ]
        for route, handler, methods, desc in routes:
            self.context.register_web_api(route, handler, methods, desc)
        logger.info(f"[HistorySave] 已注册 {len(routes)} 个 Web API 路由")

    async def api_status(self):
        """获取数据库连接状态和统计概览。"""
        ping_result = await self.mysql_mgr.ping()
        stats = await self.mysql_mgr.get_stats() if ping_result["connected"] else {}
        groups = await self.config_mgr.get_groups()
        settings = await self.config_mgr.get_all_settings()

        return json_response(
            {
                "database": ping_result,
                "stats": stats,
                "enabled_groups": len([g for g in groups if g["enabled"]]),
                "total_groups": len(groups),
                "all_mode": settings.get("all_mode", "false") == "true",
            }
        )

    async def api_get_groups(self):
        """获取群白名单列表。"""
        groups = await self.config_mgr.get_groups()
        settings = await self.config_mgr.get_all_settings()
        return json_response(
            {
                "groups": groups,
                "all_mode": settings.get("all_mode", "false") == "true",
            }
        )

    async def api_toggle_group(self):
        """切换指定群的启用状态。"""
        payload = await request.json(default={})
        group_id = payload.get("group_id")
        if not group_id:
            return error_response("缺少 group_id 参数", status_code=400)
        try:
            group_id = int(group_id)
        except (ValueError, TypeError):
            return error_response("group_id 必须为数字", status_code=400)

        new_state = await self.config_mgr.toggle_group(group_id)
        if new_state is None:
            return error_response(f"群 {group_id} 不在白名单中", status_code=404)
        return json_response({"group_id": group_id, "enabled": new_state})

    async def api_add_group(self):
        """添加群到白名单。"""
        payload = await request.json(default={})
        group_id = payload.get("group_id")
        if not group_id:
            return error_response("缺少 group_id 参数", status_code=400)
        try:
            group_id = int(group_id)
        except (ValueError, TypeError):
            return error_response("group_id 必须为数字", status_code=400)

        success = await self.config_mgr.add_group(group_id)
        if success:
            return json_response({"group_id": group_id, "added": True})
        return error_response("添加群失败", status_code=500)

    async def api_remove_group(self):
        """从白名单移除群。"""
        payload = await request.json(default={})
        group_id = payload.get("group_id")
        if not group_id:
            return error_response("缺少 group_id 参数", status_code=400)
        try:
            group_id = int(group_id)
        except (ValueError, TypeError):
            return error_response("group_id 必须为数字", status_code=400)

        success = await self.config_mgr.remove_group(group_id)
        if success:
            return json_response({"group_id": group_id, "removed": True})
        return error_response("移除群失败", status_code=500)

    async def api_get_settings(self):
        """获取插件设置。"""
        settings = await self.config_mgr.get_all_settings()
        return json_response(settings)

    async def api_save_settings(self):
        """保存插件设置。"""
        payload = await request.json(default={})

        # 验证 image_retention_days
        if "image_retention_days" in payload:
            try:
                days = int(payload["image_retention_days"])
                if days < 1:
                    return error_response("保留天数不能小于 1", status_code=400)
                await self.config_mgr.set_setting("image_retention_days", str(days))
            except (ValueError, TypeError):
                return error_response("保留天数必须为正整数", status_code=400)

        # 验证 all_mode
        if "all_mode" in payload:
            all_mode = payload["all_mode"]
            if isinstance(all_mode, bool):
                await self.config_mgr.set_setting("all_mode", str(all_mode).lower())
            elif isinstance(all_mode, str) and all_mode in ("true", "false"):
                await self.config_mgr.set_setting("all_mode", all_mode)
            else:
                return error_response("all_mode 必须为布尔值", status_code=400)

        settings = await self.config_mgr.get_all_settings()
        return json_response({"saved": True, "settings": settings})

    async def api_daily_stats(self):
        """获取每日存储统计。"""
        days = request.query.get("days", 7, type=int)
        if days < 1:
            days = 7
        if days > 90:
            days = 90
        stats = await self.mysql_mgr.get_daily_stats(days)
        return json_response({"days": days, "data": stats})

    async def api_clean(self):
        """手动触发图片清理。"""
        payload = await request.json(default={})
        days = payload.get("days")
        if days is not None:
            try:
                days = int(days)
                if days < 1:
                    return error_response("天数不能小于 1", status_code=400)
            except (ValueError, TypeError):
                return error_response("天数必须为正整数", status_code=400)

        deleted = await self.cleaner.manual_clean(days)
        if deleted >= 0:
            return json_response({"deleted": deleted, "days": days or "配置值"})
        return error_response("清理失败，请检查数据库连接", status_code=500)

    async def api_query(self):
        """查询聊天记录（支持多条件过滤和分页）。"""
        group_id = request.query.get("group_id", type=int)
        sender_id = request.query.get("sender_id", type=int)
        time_start = request.query.get("time_start")
        time_end = request.query.get("time_end")
        page = request.query.get("page", 1, type=int)
        page_size = request.query.get("page_size", 50, type=int)

        # 参数校验
        if page < 1:
            page = 1
        if page_size < 1:
            page_size = 50
        if page_size > 200:
            page_size = 200

        result = await self.mysql_mgr.query_messages(
            group_id=group_id,
            sender_id=sender_id,
            time_start=time_start,
            time_end=time_end,
            page=page,
            page_size=page_size,
        )
        return json_response(result)
