"""查询日志 Mixin（QueryLogMixin，v0.7.0 迁移至内置 SQLite）。

对外查询接口（core/public_api.py）每次调用写入一条 query_log 记录
（调用方/方法/参数/结果条数/成败/耗时/时间），供 Web 后台「查询日志」
tab 审计与排查。v0.7.0 起由 MySQL 迁移至内置 SQLite（config.db）：
查询日志属插件自身审计数据，与聊天记录存储解耦——MySQL 不可用时
审计仍可用；保留天数经 query_log_settings 配置（Web 可调，默认 30 天），
写入时按 5% 概率顺带清理过期行（清理失败仅 warning，不影响写入）。

时间一律存 ISO 定宽文本 "YYYY-MM-DD HH:MM:SS"（与 group_config 等表
同范式）：定宽格式下字符串比较即时间序比较，time_start/time_end 筛选
与过期清理直接字符串比较即可。
"""

import json
import random
from datetime import datetime, timedelta
from typing import Any

from astrbot.api import logger

# 查询日志保留天数默认值（Web 可调，范围 [1, 3650]）
QUERY_LOG_DEFAULT_RETENTION_DAYS = 30


class QueryLogMixin:
    """查询日志 Mixin：写入、分页筛选查询、过期清理、保留天数配置。"""

    # ========== 查询日志设置常量（类属性，范式同 summary/profile/stats_settings） ==========

    # 查询日志设置默认值（保留天数，Web 可调，范围 [1, 3650]）
    QUERY_LOG_DEFAULTS: dict[str, str] = {
        "query_log_retention_days": "30",
    }
    QUERY_LOG_TYPES: dict[str, str] = {
        "query_log_retention_days": "int",
    }
    QUERY_LOG_RANGES: dict[str, tuple[int, int]] = {
        "query_log_retention_days": (1, 3650),
    }

    # ========== 保留天数配置 ==========

    async def get_query_log_retention_days(self) -> int:
        """读取查询日志保留天数（类型化）。

        缺失/非法值回退 QUERY_LOG_DEFAULT_RETENTION_DAYS；读取失败记
        error 后同样回退默认（防御性契约，不向上抛）。

        Returns:
            int: 保留天数（[1, 3650]）
        """
        try:
            await self._ensure_db()
            async with self.db.execute(
                "SELECT value FROM query_log_settings WHERE key = ?",
                ("query_log_retention_days",),
            ) as cursor:
                row = await cursor.fetchone()
            if row is not None:
                days = int(row[0])
                lo, hi = self.QUERY_LOG_RANGES["query_log_retention_days"]
                return max(lo, min(days, hi))
            return QUERY_LOG_DEFAULT_RETENTION_DAYS
        except Exception as e:
            logger.error(f"[HistorySave] 读取查询日志保留天数失败: {e}")
            return QUERY_LOG_DEFAULT_RETENTION_DAYS

    async def set_query_log_retention_days(self, days: int) -> bool:
        """设置查询日志保留天数（夹取 [1, 3650]）。

        Args:
            days: 目标天数；越界值夹取到合法范围

        Returns:
            bool: 是否写入成功
        """
        try:
            lo, hi = self.QUERY_LOG_RANGES["query_log_retention_days"]
            days = max(lo, min(int(days), hi))
            await self._ensure_db()
            await self.db.execute(
                "INSERT OR REPLACE INTO query_log_settings (key, value) VALUES (?, ?)",
                ("query_log_retention_days", str(days)),
            )
            await self.db.commit()
            return True
        except Exception as e:
            logger.error(f"[HistorySave] 设置查询日志保留天数失败: {e}")
            return False

    # ========== 日志写入 ==========

    async def insert_query_log(
        self,
        caller: str,
        method: str,
        params: dict,
        result_count: int,
        success: bool,
        error_msg: str | None = None,
        cost_ms: int = 0,
        created_at: datetime | None = None,
    ) -> bool:
        """写入一条查询日志。

        params 经 json.dumps(ensure_ascii=False) 序列化后存 TEXT 列
        （中文原样保留，便于 Web 后台直接展示）；写入成功后按 5% 概率
        顺带清理超过保留天数（Web 可配）的过期行——清理失败仅 warning
        不影响本次写入。

        Args:
            caller: 调用方标识（如 "data.plugins.xxx" 或 "unknown"）
            method: 调用方法名（如 "query_records"）
            params: 查询参数字典，序列化为 JSON 存储
            result_count: 返回的结果条数
            success: 本次查询是否成功
            error_msg: 失败原因；成功时为 None
            cost_ms: 本次查询耗时（毫秒）
            created_at: 记录时间；None 取当前时间

        Returns:
            bool: 是否写入成功（写入失败记 error 日志并返回 False）
        """
        try:
            await self._ensure_db()
            if created_at is None:
                created_at = datetime.now()
            created_at_str = created_at.strftime("%Y-%m-%d %H:%M:%S")
            params_json = (
                json.dumps(params, ensure_ascii=False) if params is not None else None
            )
            await self.db.execute(
                """INSERT INTO query_log
                   (caller, method, params, result_count, success,
                    error_msg, cost_ms, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    caller,
                    method,
                    params_json,
                    result_count,
                    1 if success else 0,
                    error_msg,
                    cost_ms,
                    created_at_str,
                ),
            )
            await self.db.commit()
            # 5% 概率顺带清理过期日志：保留天数经 Web 配置读取
            # （清理失败仅 warning，不影响本次写入结果）
            if random.random() < 0.05:
                retention = await self.get_query_log_retention_days()
                older_than = (datetime.now() - timedelta(days=retention)).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                deleted = await self.cleanup_query_logs(older_than)
                if deleted > 0:
                    logger.info(f"[HistorySave] 顺带清理过期查询日志 {deleted} 条")
            return True
        except Exception as e:
            logger.error(f"[HistorySave] 写入查询日志失败: {e}")
            return False

    # ========== 日志查询 ==========

    async def query_query_logs(
        self,
        page: int = 1,
        page_size: int = 100,
        caller: str | None = None,
        method: str | None = None,
        time_start: str | None = None,
        time_end: str | None = None,
    ) -> dict:
        """分页筛选查询查询日志。

        记录按 created_at DESC, id DESC 排序（同秒写入时 id 大的新）；
        caller 模糊匹配（LIKE 通配符转义同 chat_history.query_messages
        范式，用户输入的 % _ 按字面匹配）、method 精确匹配；
        page < 1 归 1，page_size 夹取 [1, 200]；异常记 _log_op_error
        后返回空结果（防御性契约，不向上抛）。

        Args:
            page: 页码（从 1 开始）
            page_size: 每页条数（夹取 [1, 200]）
            caller: 调用方模糊匹配（可选）
            method: 方法名精确匹配（可选）
            time_start: 开始时间（格式 YYYY-MM-DD HH:MM:SS，闭区间）
            time_end: 结束时间（格式 YYYY-MM-DD HH:MM:SS，闭区间）

        Returns:
            dict: {"total": int, "records": list[dict]}，records 中
                created_at 为 ISO 文本、success 已布尔化
        """
        result: dict[str, Any] = {"total": 0, "records": []}
        try:
            await self._ensure_db()
            if page < 1:
                page = 1
            page_size = max(1, min(page_size, 200))
            conditions = []
            params: list = []

            if caller:
                # 转义 LIKE 通配符，避免调用方名中的 % _ 干扰匹配
                kw = (
                    caller.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                )
                conditions.append("caller LIKE ? ESCAPE '\\'")
                params.append(f"%{kw}%")
            if method:
                conditions.append("method = ?")
                params.append(method)
            if time_start:
                conditions.append("created_at >= ?")
                params.append(time_start)
            if time_end:
                conditions.append("created_at <= ?")
                params.append(time_end)

            where_clause = " AND ".join(conditions) if conditions else "1=1"
            offset = (page - 1) * page_size

            async with self.db.execute(
                f"SELECT COUNT(*) as total FROM query_log WHERE {where_clause}",
                params,
            ) as cursor:
                row = await cursor.fetchone()
                result["total"] = row[0] if row else 0

            async with self.db.execute(
                f"""SELECT id, caller, method, params, result_count,
                           success, error_msg, cost_ms, created_at
                    FROM query_log WHERE {where_clause}
                    ORDER BY created_at DESC, id DESC
                    LIMIT ? OFFSET ?""",
                params + [page_size, offset],
            ) as cursor:
                rows = await cursor.fetchall()
                for row in rows:
                    result["records"].append(
                        {
                            "id": row[0],
                            "caller": row[1],
                            "method": row[2],
                            "params": row[3],
                            "result_count": row[4],
                            "success": bool(row[5]),
                            "error_msg": row[6],
                            "cost_ms": row[7],
                            "created_at": row[8],
                        }
                    )
        except Exception as e:
            logger.error(f"[HistorySave] 查询查询日志失败: {e}")
        return result

    # ========== 过期清理 ==========

    async def cleanup_query_logs(self, older_than: str) -> int:
        """删除 created_at 早于 older_than 的过期查询日志行。

        异常记 warning 并返回 0（不影响调用方写入结果）。

        Args:
            older_than: 保留边界（ISO 文本 "YYYY-MM-DD HH:MM:SS"，
                定宽格式下字符串比较即时间序比较）

        Returns:
            int: 删除的行数；异常时返回 0
        """
        try:
            await self._ensure_db()
            cursor = await self.db.execute(
                "DELETE FROM query_log WHERE created_at < ?", (older_than,)
            )
            await self.db.commit()
            return cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
        except Exception as e:
            logger.warning(f"[HistorySave] 清理过期查询日志失败: {e}")
            return 0
