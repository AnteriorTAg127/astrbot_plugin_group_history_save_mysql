"""数据维护操作（MaintenanceMixin）。

自根目录 db_mysql.py 拆分：purge_all / _purge_table / _truncate_table /
_reset_auto_increment 逐字迁移。
"""

from astrbot.api import logger

# DDL 超时常量（DDL_TIMEOUT_SECONDS）定义于 base.py
from .base import DDL_TIMEOUT_SECONDS


class MaintenanceMixin:
    """数据维护 Mixin：清空全部数据（TRUNCATE 优先，DELETE 回退）。"""

    async def purge_all(self) -> dict:
        """清空所有聊天记录和图片记录（不可恢复），并复位自增 ID。

        优先使用 TRUNCATE TABLE（DDL，自动复位自增 ID，性能远优于 DELETE）。
        若 MySQL 用户无 DROP 权限（TRUNCATE 需要），回退到 DELETE + ALTER 方式。

        Returns:
            dict: {"success": bool, "deleted_messages": int, "deleted_images": int, "truncated": bool}
        """
        result: dict = {
            "success": False,
            "deleted_messages": 0,
            "deleted_images": 0,
            "truncated": False,
        }
        try:
            async with self.pool.acquire() as conn:
                async with conn.cursor() as cur:
                    # 两表独立清空：一张表失败不影响另一张，
                    # 成功状态由各自的返回值汇总
                    messages_ok = await self._purge_table(
                        cur, "chat_history", result, "deleted_messages"
                    )
                    images_ok = await self._purge_table(
                        cur, "image_records", result, "deleted_images"
                    )
            result["success"] = messages_ok and images_ok
        except Exception as e:
            logger.error(f"[HistorySave] 清空所有数据失败: {e}")
        # 所有路径都输出审计日志（含部分失败情形）；
        # TRUNCATE 无 rowcount，计数取自操作前的 SELECT COUNT(*)
        if result["success"]:
            logger.warning(
                f"[HistorySave] 已清空所有数据"
                f"{' (TRUNCATE)' if result['truncated'] else ' (DELETE)'}: "
                f"聊天记录 {result['deleted_messages']} 条, "
                f"图片记录 {result['deleted_images']} 条"
            )
        else:
            logger.error(
                f"[HistorySave] 清空所有数据未完全成功: "
                f"聊天记录 {result['deleted_messages']} 条, "
                f"图片记录 {result['deleted_images']} 条"
            )
        return result

    async def _purge_table(self, cur, table: str, result: dict, count_key: str) -> bool:
        """清空单张表并把删除条数写入 result。

        TRUNCATE 是 DDL，没有 rowcount，因此操作前先 SELECT COUNT(*)
        取得条数用于审计日志。表名来自本类硬编码，无注入风险。

        Args:
            cur: 数据库游标
            table: 表名
            result: 用于写入删除条数的结果字典
            count_key: result 中删除条数的键名

        Returns:
            bool: 该表是否清空成功
        """
        try:
            # 大表 COUNT(*) 为全表扫描，可能耗时较长，用 DDL 超时档兜底
            await self._execute(
                cur,
                f"SELECT COUNT(*) FROM {table}",
                timeout=DDL_TIMEOUT_SECONDS,
            )
            row = await cur.fetchone()
            pre_count = int(row[0]) if row else 0
        except Exception as e:
            logger.error(f"[HistorySave] 预统计 {table} 行数失败: {e}")
            return False

        if await self._truncate_table(cur, table):
            result["truncated"] = True
            result[count_key] = pre_count
            return True

        try:
            await self._execute(
                cur,
                f"DELETE FROM {table}",
                timeout=DDL_TIMEOUT_SECONDS,
            )
            deleted = cur.rowcount
            # rowcount 异常（如 -1）时回退到预统计值，保证审计计数合理
            result[count_key] = deleted if deleted >= 0 else pre_count
            await self._reset_auto_increment(cur, table)
            return True
        except Exception as e:
            logger.error(f"[HistorySave] DELETE FROM {table} 失败: {e}")
            return False

    async def _truncate_table(self, cur, table: str) -> bool:
        """尝试用 TRUNCATE TABLE 清空表。

        TRUNCATE 是 DDL，自动复位自增 ID，性能远优于 DELETE。
        需要 DROP 权限，权限不足或其他异常时返回 False 供调用方回退到 DELETE。

        Args:
            cur: 数据库游标
            table: 表名（本类硬编码，无注入风险）

        Returns:
            bool: True 表示 TRUNCATE 成功，False 表示需回退到 DELETE
        """
        try:
            await self._execute(
                cur,
                f"TRUNCATE TABLE {table}",
                timeout=DDL_TIMEOUT_SECONDS,
            )
            return True
        except Exception as e:
            logger.warning(
                f"[HistorySave] TRUNCATE {table} 失败（可能无 DROP 权限），"
                f"回退到 DELETE: {e}"
            )
            return False

    async def _reset_auto_increment(self, cur, table: str) -> None:
        """表清空后将自增 ID 复位到 1。

        DELETE 不会复位 InnoDB 自增计数，需显式 ALTER。表名来自本类硬编码，无注入风险。
        权限不足等异常仅记 warning，不向上抛（清空已成功，只是 ID 未复位）。
        """
        try:
            await self._execute(
                cur,
                f"ALTER TABLE {table} AUTO_INCREMENT = 1",
                timeout=DDL_TIMEOUT_SECONDS,
            )
        except Exception as e:
            logger.warning(
                f"[HistorySave] 复位 {table} 自增 ID 失败（不影响清空）: {e}"
            )
