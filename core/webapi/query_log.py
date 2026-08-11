"""Web API 查询日志端点（v0.7.0）。

QueryLogMixin：对外查询日志列表（分页 + 调用方/方法/时间筛选）与
保留天数设置读写。v0.7.0 起查询日志存内置 SQLite（config.db），
由 db_config/query_log.py 读写。
"""

from astrbot.api.web import json_response, request


class QueryLogMixin:
    """查询日志端点 Mixin（v0.7.0）。"""

    async def api_query_log_list(self):
        """查询日志列表（分页 + 筛选）。

        caller 模糊匹配、method 精确匹配、time_start/time_end 闭区间，
        筛选与分页逻辑委托 config_mgr.query_query_logs（见
        core/db_config/query_log.py）；默认取最近 100 条（上限 200）。
        """
        # 显式转换，避免框架 type=int 行为不一致
        page_str = request.query.get("page") or "1"
        try:
            page = int(page_str)
        except (ValueError, TypeError):
            page = 1
        page_size_str = request.query.get("page_size") or "100"
        try:
            page_size = int(page_size_str)
        except (ValueError, TypeError):
            page_size = 100

        # 参数校验
        if page < 1:
            page = 1
        if page_size < 1:
            page_size = 100
        if page_size > 200:
            page_size = 200

        # 筛选条件：调用方模糊/方法精确均为文本字段，空字符串视同未提供；
        # 时间起止透传底层（空串底层不生效，等价未提供）
        caller = (request.query.get("caller") or "").strip() or None
        method = (request.query.get("method") or "").strip() or None
        time_start = request.query.get("time_start")
        time_end = request.query.get("time_end")

        result = await self.config_mgr.query_query_logs(
            page=page,
            page_size=page_size,
            caller=caller,
            method=method,
            time_start=time_start,
            time_end=time_end,
        )
        return json_response(result)

    async def api_query_log_settings_get(self):
        """获取查询日志设置（当前仅保留天数）。"""
        days = await self.config_mgr.get_query_log_retention_days()
        return json_response({"retention_days": days})

    async def api_query_log_settings_save(self):
        """保存查询日志设置（保留天数，夹取 [1, 3650]）。"""
        payload = await request.json(default={})
        raw = payload.get("retention_days")
        try:
            days = int(raw)
        except (ValueError, TypeError):
            return json_response({"success": False, "message": "保留天数必须为整数"})
        ok = await self.config_mgr.set_query_log_retention_days(days)
        if ok:
            return json_response({"success": True, "retention_days": days})
        return json_response({"success": False, "message": "保存失败，请查看日志"})
