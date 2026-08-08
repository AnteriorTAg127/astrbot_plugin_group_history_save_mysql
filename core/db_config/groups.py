"""群白名单 Mixin（GroupMixin）。

群白名单的增删改查与启用状态切换；all_mode 全局模式判定见 is_group_enabled。
"""

from datetime import datetime

from astrbot.api import logger


class GroupMixin:
    """群白名单管理 Mixin：get_groups / add_group / remove_group / toggle_group / is_group_enabled。"""

    # ========== 群管理 ==========

    async def get_groups(self) -> list[dict]:
        """获取所有已配置的群列表。

        Returns:
            list[dict]: 群配置列表 [{"group_id": int, "enabled": bool, "created_at": str}]
        """
        try:
            await self._ensure_db()
            async with self.db.execute(
                "SELECT group_id, enabled, created_at FROM group_config ORDER BY created_at DESC"
            ) as cursor:
                rows = await cursor.fetchall()
                return [
                    {"group_id": row[0], "enabled": bool(row[1]), "created_at": row[2]}
                    for row in rows
                ]
        except Exception as e:
            logger.error(f"[HistorySave] 获取群列表失败: {e}")
            return []

    async def add_group(self, group_id: int) -> bool:
        """添加群到白名单。

        Args:
            group_id: 群号

        Returns:
            bool: 是否成功
        """
        try:
            await self._ensure_db()
            # UPSERT：重复添加时强制 enabled=1，但不重置 created_at（保留首次加入时间）
            await self.db.execute(
                "INSERT INTO group_config (group_id, enabled, created_at) "
                "VALUES (?, 1, ?) "
                "ON CONFLICT(group_id) DO UPDATE SET enabled = 1",
                (group_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )
            await self.db.commit()
            return True
        except Exception as e:
            logger.error(f"[HistorySave] 添加群失败: {e}")
            return False

    async def remove_group(self, group_id: int) -> bool:
        """从白名单移除群。

        Args:
            group_id: 群号

        Returns:
            bool: 是否成功
        """
        try:
            await self._ensure_db()
            await self.db.execute(
                "DELETE FROM group_config WHERE group_id = ?", (group_id,)
            )
            await self.db.commit()
            return True
        except Exception as e:
            logger.error(f"[HistorySave] 移除群失败: {e}")
            return False

    async def toggle_group(self, group_id: int) -> bool | None:
        """切换群的启用状态。

        Args:
            group_id: 群号

        Returns:
            bool: 切换后的状态，群不存在返回 None
        """
        try:
            await self._ensure_db()
            # 单条 UPDATE 消除 SELECT-then-UPDATE 竞态，rowcount 判断群是否存在
            async with self.db.execute(
                "UPDATE group_config SET enabled = 1 - enabled WHERE group_id = ?",
                (group_id,),
            ) as cursor:
                updated = cursor.rowcount
            await self.db.commit()
            if updated == 0:
                return None
            async with self.db.execute(
                "SELECT enabled FROM group_config WHERE group_id = ?", (group_id,)
            ) as cursor:
                row = await cursor.fetchone()
            return bool(row[0]) if row else None
        except Exception as e:
            logger.error(f"[HistorySave] 切换群状态失败: {e}")
            return None

    async def is_group_enabled(self, group_id: int) -> bool:
        """检查指定群是否启用记录。

        如果 all_mode 为 true，则所有群都启用。

        Args:
            group_id: 群号

        Returns:
            bool: 是否启用
        """
        try:
            await self._ensure_db()
            # 先检查 all_mode
            all_mode = await self.get_setting("all_mode", "false")
            if all_mode == "true":
                return True
            # 再检查群白名单
            async with self.db.execute(
                "SELECT enabled FROM group_config WHERE group_id = ?", (group_id,)
            ) as cursor:
                row = await cursor.fetchone()
            return bool(row[0]) if row else False
        except Exception as e:
            logger.error(f"[HistorySave] 检查群状态失败: {e}")
            return False
