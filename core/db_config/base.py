"""本地配置存储层基础（ConfigManagerBase，aiosqlite）。

负责数据库连接管理、全部功能表建表 DDL（集中于此，属核心初始化）与
核心设置读写（plugin_settings）；群白名单 / 总结 / 人物分析 / 数据分析
配置 / 快照读写由本包各子功能 Mixin 承担。
数据文件位于 data/plugin_data/astrbot_plugin_group_history_save_mysql/config.db
"""

import asyncio

import aiosqlite

from astrbot.api import logger
from astrbot.api.star import StarTools

PLUGIN_NAME = "astrbot_plugin_group_history_save_mysql"


class ConfigManagerBase:
    """本地配置管理器基础：连接管理 + 建表 + 核心设置读写。"""

    def __init__(self):
        # 使用 StarTools.get_data_dir() 获取规范的数据目录（返回 Path，已自动创建）
        data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.db_path = str(data_dir / "config.db")
        self.db: aiosqlite.Connection | None = None
        # 连接建立串行化：防止多个协程在断线后并发重连产生多余连接
        self._connect_lock = asyncio.Lock()

    async def initialize(self) -> bool:
        """初始化数据库连接并创建表结构。

        Returns:
            bool: 初始化是否成功
        """
        try:
            await self._ensure_db()
            logger.info("[HistorySave] 本地配置数据库初始化成功")
            return True
        except Exception as e:
            logger.error(f"[HistorySave] 本地配置数据库初始化失败: {e}")
            return False

    async def _ensure_db(self) -> None:
        """确保数据库连接可用，未连接则自动建连（幂等、自愈）。

        initialize() 失败或连接意外断开后，各公开方法经由此方法自愈重连，
        而不是带着 None/坏连接永久失败。

        Raises:
            Exception: 连接或建表失败（由各公开方法自身的 except 兜底）
        """
        if self.db is not None:
            return
        async with self._connect_lock:
            # 双重检查：先抢到锁的协程可能已经完成建连
            if self.db is not None:
                return
            try:
                self.db = await aiosqlite.connect(self.db_path)
                await self._create_tables()
                await self._init_default_settings()
            except Exception:
                # 清理半成品连接并置 None，保留下次调用的自愈能力
                if self.db is not None:
                    try:
                        await self.db.close()
                    except Exception:
                        pass
                    self.db = None
                raise

    async def _create_tables(self):
        """创建群配置表、插件设置表，以及总结/人物分析/数据分析各功能表。"""
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
        # v0.3：总结功能配置表（key/value 形式，24 项配置统一落库）
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS summary_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        # v0.3：群总结忽略名单表（联合唯一约束去重，避免 SELECT-then-INSERT 竞态）
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS group_ignore_senders (
                group_id TEXT NOT NULL,
                sender_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(group_id, sender_id)
            )
        """)
        # v0.4.0：人物分析功能配置表（key/value 形式，19 项配置统一落库）
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS profile_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        # v0.5.0：数据分析功能配置表（key/value 形式，8 项配置统一落库）
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS stats_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        # v0.5.0：群级推送开关表（以 group_config 白名单为准，
        # 不在白名单的 push_group 行在 get_push_groups 中忽略）
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS push_group (
                group_id TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 0
            )
        """)
        # v0.5.0：图片统计小时级快照（群级全量），主键 (date, hour, group_id)。
        # 定时 UPSERT 覆盖写，解决 image_records 滚动清理后无法回溯统计
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS image_stats_hourly (
                date TEXT NOT NULL,
                hour INTEGER NOT NULL,
                group_id TEXT NOT NULL,
                image_count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (date, hour, group_id)
            )
        """)
        # v0.5.0：图片统计小时级快照（每小时每群个人 Top K），
        # 主键 (date, hour, group_id, sender_id)
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS image_stats_hourly_top (
                date TEXT NOT NULL,
                hour INTEGER NOT NULL,
                group_id TEXT NOT NULL,
                sender_id TEXT NOT NULL,
                sender_name TEXT NOT NULL DEFAULT '',
                image_count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (date, hour, group_id, sender_id)
            )
        """)
        # v0.5.5：分段快照体系 —— 将 v0.5.0 图片小时级快照泛化为
        # 「消息 + 图片 × 小时/日/月」三段式预计算快照（以下 6 张新表）。
        # 全部 UPSERT 覆盖写语义，与既有两张图片表一致；月层覆盖游标由上层
        # 经 plugin_settings 存取（键 snapshot_monthly_msg/image_covered）
        # 消息统计小时级快照（群级全量），主键 (date, hour, group_id)。
        # 分段快照体系小时层，淘汰保留近 7 天
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS msg_stats_hourly (
                date TEXT NOT NULL, hour INTEGER NOT NULL, group_id TEXT NOT NULL,
                msg_count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (date, hour, group_id));
        """)
        # 消息统计小时级快照（每小时每群个人 Top K，含昵称），
        # 主键 (date, hour, group_id, sender_id)。语义同 image_stats_hourly_top
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS msg_stats_hourly_top (
                date TEXT NOT NULL, hour INTEGER NOT NULL, group_id TEXT NOT NULL,
                sender_id TEXT NOT NULL, sender_name TEXT NOT NULL DEFAULT '',
                msg_count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (date, hour, group_id, sender_id));
        """)
        # 消息统计日级快照（每群每日消息总数，全量不受 Top K 截断），
        # 主键 (date, group_id)。只存完整自然日，淘汰保留上月+本月
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS msg_stats_daily (
                date TEXT NOT NULL, group_id TEXT NOT NULL,
                msg_count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (date, group_id));
        """)
        # 消息统计月级快照（每群每月消息总数，month="YYYY-MM"），
        # 主键 (month, group_id)。只存完整自然月，永久保留不淘汰
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS msg_stats_monthly (
                month TEXT NOT NULL, group_id TEXT NOT NULL,
                msg_count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (month, group_id));
        """)
        # 图片统计日级快照（每群每日图片总数），主键 (date, group_id)。
        # 只存完整自然日，淘汰保留上月+本月
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS image_stats_daily (
                date TEXT NOT NULL, group_id TEXT NOT NULL,
                image_count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (date, group_id));
        """)
        # 图片统计月级快照（每群每月图片总数，month="YYYY-MM"），
        # 主键 (month, group_id)。只存完整自然月，永久保留不淘汰
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS image_stats_monthly (
                month TEXT NOT NULL, group_id TEXT NOT NULL,
                image_count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (month, group_id));
        """)
        await self.db.commit()

    async def _init_default_settings(self):
        """初始化默认设置项（如果不存在）。"""
        defaults = {
            "image_retention_days": "3",
            "all_mode": "false",
            # v0.6.0 重载自动补库：总开关 / 时间窗口（小时，夹取 [1,168]）
            "backfill_enabled": "true",
            "backfill_hours": "12",
        }
        for key, value in defaults.items():
            await self.db.execute(
                "INSERT OR IGNORE INTO plugin_settings (key_name, value) VALUES (?, ?)",
                (key, value),
            )
        # v0.3：总结功能 24 项配置播种。INSERT OR IGNORE 仅插入缺失键，
        # 不覆盖用户已在 dashboard 修改的值（升级/自愈重连时可安全重复执行）
        for key, value in self.SUMMARY_DEFAULTS.items():
            await self.db.execute(
                "INSERT OR IGNORE INTO summary_settings (key, value) VALUES (?, ?)",
                (key, value),
            )
        # v0.4.0：人物分析功能 19 项配置播种（范式同 summary_settings）
        for key, value in self.PROFILE_DEFAULTS.items():
            await self.db.execute(
                "INSERT OR IGNORE INTO profile_settings (key, value) VALUES (?, ?)",
                (key, value),
            )
        # v0.5.0：数据分析功能 8 项配置播种（范式同 summary/profile_settings）
        for key, value in self.STATS_DEFAULTS.items():
            await self.db.execute(
                "INSERT OR IGNORE INTO stats_settings (key, value) VALUES (?, ?)",
                (key, value),
            )
        await self.db.commit()

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
            await self._ensure_db()
            async with self.db.execute(
                "SELECT value FROM plugin_settings WHERE key_name = ?", (key,)
            ) as cursor:
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
            await self._ensure_db()
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
            await self._ensure_db()
            async with self.db.execute(
                "SELECT key_name, value FROM plugin_settings"
            ) as cursor:
                rows = await cursor.fetchall()
            return {row[0]: row[1] for row in rows}
        except Exception as e:
            logger.error(f"[HistorySave] 获取所有设置失败: {e}")
            return {}

    async def close(self):
        """关闭数据库连接。"""
        if self.db:
            await self.db.close()
            # 置 None，使 _ensure_db 在需要时能够重新建连
            self.db = None
            logger.info("[HistorySave] 本地配置数据库已关闭")
