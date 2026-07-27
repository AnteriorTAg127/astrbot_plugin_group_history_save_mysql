"""本地配置存储层（aiosqlite）。

负责管理群白名单和插件设置的本地持久化存储。
数据文件位于 data/plugin_data/astrbot_plugin_group_history_save_mysql/config.db
"""

from datetime import datetime
from pathlib import Path

import aiosqlite

from astrbot.api import logger
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

PLUGIN_NAME = "astrbot_plugin_group_history_save_mysql"


class ConfigManager:
    """本地 aiosqlite 配置管理器。"""

    def __init__(self):
        data_dir = Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME
        data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = str(data_dir / "config.db")
        self.db: aiosqlite.Connection | None = None

    async def initialize(self) -> bool:
        """初始化数据库连接并创建表结构。

        Returns:
            bool: 初始化是否成功
        """
        try:
            self.db = await aiosqlite.connect(self.db_path)
            await self._create_tables()
            await self._init_default_settings()
            logger.info("[HistorySave] 本地配置数据库初始化成功")
            return True
        except Exception as e:
            logger.error(f"[HistorySave] 本地配置数据库初始化失败: {e}")
            return False

    async def _create_tables(self):
        """创建群配置表和插件设置表。"""
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS group_config (
                group_id INTEGER PRIMARY KEY,
                enabled INTEGER DEFAULT 1,
                created_at TEXT DEFAULT ''
            )
        """)
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS plugin_settings (
                key_name TEXT PRIMARY KEY,
                value TEXT DEFAULT ''
            )
        """)
        await self.db.commit()

    async def _init_default_settings(self):
        """初始化默认设置项（如果不存在）。"""
        defaults = {
            "image_retention_days": "3",
            "all_mode": "false",
        }
        for key, value in defaults.items():
            await self.db.execute(
                "INSERT OR IGNORE INTO plugin_settings (key_name, value) VALUES (?, ?)",
                (key, value),
            )
        await self.db.commit()

    # ========== 群管理 ==========

    async def get_groups(self) -> list[dict]:
        """获取所有已配置的群列表。

        Returns:
            list[dict]: 群配置列表 [{"group_id": int, "enabled": bool, "created_at": str}]
        """
        try:
            cursor = await self.db.execute(
                "SELECT group_id, enabled, created_at FROM group_config ORDER BY created_at DESC"
            )
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
            await self.db.execute(
                "INSERT OR REPLACE INTO group_config (group_id, enabled, created_at) VALUES (?, 1, ?)",
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
            cursor = await self.db.execute(
                "SELECT enabled FROM group_config WHERE group_id = ?", (group_id,)
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            new_state = 0 if row[0] else 1
            await self.db.execute(
                "UPDATE group_config SET enabled = ? WHERE group_id = ?",
                (new_state, group_id),
            )
            await self.db.commit()
            return bool(new_state)
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
            # 先检查 all_mode
            all_mode = await self.get_setting("all_mode", "false")
            if all_mode == "true":
                return True
            # 再检查群白名单
            cursor = await self.db.execute(
                "SELECT enabled FROM group_config WHERE group_id = ?", (group_id,)
            )
            row = await cursor.fetchone()
            return bool(row[0]) if row else False
        except Exception as e:
            logger.error(f"[HistorySave] 检查群状态失败: {e}")
            return False

    # ========== 插件设置 ==========

    async def get_setting(self, key: str, default: str = "") -> str:
        """获取设置值。

        Args:
            key: 设置键名
            default: 默认值

        Returns:
            str: 设置值
        """
        try:
            cursor = await self.db.execute(
                "SELECT value FROM plugin_settings WHERE key_name = ?", (key,)
            )
            row = await cursor.fetchone()
            return row[0] if row else default
        except Exception as e:
            logger.error(f"[HistorySave] 获取设置失败: {e}")
            return default

    async def set_setting(self, key: str, value: str) -> bool:
        """设置配置值。

        Args:
            key: 设置键名
            value: 设置值

        Returns:
            bool: 是否成功
        """
        try:
            await self.db.execute(
                "INSERT OR REPLACE INTO plugin_settings (key_name, value) VALUES (?, ?)",
                (key, value),
            )
            await self.db.commit()
            return True
        except Exception as e:
            logger.error(f"[HistorySave] 保存设置失败: {e}")
            return False

    async def get_all_settings(self) -> dict:
        """获取所有设置。

        Returns:
            dict: 设置字典
        """
        try:
            cursor = await self.db.execute(
                "SELECT key_name, value FROM plugin_settings"
            )
            rows = await cursor.fetchall()
            return {row[0]: row[1] for row in rows}
        except Exception as e:
            logger.error(f"[HistorySave] 获取所有设置失败: {e}")
            return {}

    async def close(self):
        """关闭数据库连接。"""
        if self.db:
            await self.db.close()
            logger.info("[HistorySave] 本地配置数据库已关闭")
