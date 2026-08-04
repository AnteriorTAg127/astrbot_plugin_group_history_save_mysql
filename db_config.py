"""本地配置存储层（aiosqlite）。

负责管理群白名单、插件设置、总结/人物分析/数据分析配置、群推送开关与
统计快照（v0.5.0 图片小时级、v0.5.5 分段快照体系）的本地持久化存储。
数据文件位于 data/plugin_data/astrbot_plugin_group_history_save_mysql/config.db
"""

import asyncio
import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from astrbot.api import logger
from astrbot.api.star import StarTools

PLUGIN_NAME = "astrbot_plugin_group_history_save_mysql"


class ConfigManager:
    """本地 aiosqlite 配置管理器。"""

    # ========== 总结功能配置常量（v0.3） ==========

    # 总结功能 24 项配置的默认值。值一律字符串存储；列表类型 JSON 序列化。
    # 同时作为初始化播种目标与 dashboard「恢复默认」的目标值。
    SUMMARY_DEFAULTS: dict[str, str] = {
        "summary_enabled": "true",
        "summary_whitelist_mode": "whitelist",
        "summary_group_whitelist": "[]",
        "summary_user_cooldown": "60",
        "summary_group_cooldown": "120",
        "summary_max_count": "1000",
        "summary_max_hours": "168",
        "summary_min_mysql_ratio": "0.8",
        "summary_gap_tolerance_minutes": "30",
        "summary_onebot_max_fetch": "200",
        "summary_provider_id": "",
        # 备用总结模型 provider id 列表（JSON 存储）；主选失败后按序尝试，全失败兜底会话模型
        "summary_fallback_providers": "[]",
        "summary_output_mode": "forward",
        # 触发反馈模式：reaction 贴表情 / text 文字提示 / none 关闭
        "summary_feedback_mode": "reaction",
        # 文字反馈文案（reaction 失败降级时同用）
        "summary_feedback_text": "📝 收到！正在总结中，请稍候…",
        "summary_rank_top_n": "5",
        "summary_max_prompt_chars": "60000",
        "summary_retention_days": "30",
        # 默认提示词模板（内置 4 板块），支持占位符
        # {group_id} {time_range} {stats} {messages} {format_constraint}，由总结引擎渲染
        "summary_prompt": """你是一个 QQ 群聊记录总结助手。请阅读下面的群聊记录，严格按板块输出中文总结。

【群号】{group_id}
【时间范围】{time_range}
【消息统计】
{stats}

【聊天记录】
{messages}

【总结要求】
1. 严格按以下四个板块输出，板块标题原样保留（含 emoji）：
📢 重要通知与结论
💬 讨论要点 / 争议
🎉 有趣片段
✅ TODO / 待跟进
2. 每个条目尽量注明参与者（使用昵称）与大致时间（如「下午 3 点左右」）；
3. 语言简洁客观，不编造聊天记录中不存在的内容；
4. 某板块无对应内容时写「无」；
5. {format_constraint}""",
        # ========== v0.3.2 T2I 图片渲染配置 ==========
        # 主题模式：auto 按时段自动切换 / light 强制浅色 / dark 强制深色
        "summary_t2i_theme_mode": "auto",
        # 深色时段起点（HH:MM，24 小时制）
        "summary_t2i_dark_start": "22:00",
        # 浅色时段起点（HH:MM，24 小时制）
        "summary_t2i_light_start": "08:00",
        # 单轮渲染超时秒数（合法范围 5–300，R2 轮双倍兜底）
        "summary_t2i_timeout": "30",
        # CDN 节点尝试顺序（JSON 存储），国内镜像优先，单节点失败自动切换
        "summary_t2i_cdn_providers": '["bootcdn", "npmmirror", "staticfile", "jsdelivr", "unpkg"]',
    }

    # 各配置键的值类型声明，供 get_summary_setting_typed() 类型化读取与
    # web_api 服务端校验使用（bool/int/float/list/str）
    SUMMARY_TYPES: dict[str, type] = {
        "summary_enabled": bool,
        "summary_whitelist_mode": str,
        "summary_group_whitelist": list,
        "summary_user_cooldown": int,
        "summary_group_cooldown": int,
        "summary_max_count": int,
        "summary_max_hours": int,
        "summary_min_mysql_ratio": float,
        "summary_gap_tolerance_minutes": int,
        "summary_onebot_max_fetch": int,
        "summary_provider_id": str,
        "summary_fallback_providers": list,
        "summary_output_mode": str,
        "summary_feedback_mode": str,
        "summary_feedback_text": str,
        "summary_rank_top_n": int,
        "summary_max_prompt_chars": int,
        "summary_retention_days": int,
        "summary_prompt": str,
        # v0.3.2 T2I 渲染：主题模式/双时段/超时/CDN 节点序
        "summary_t2i_theme_mode": str,
        "summary_t2i_dark_start": str,
        "summary_t2i_light_start": str,
        "summary_t2i_timeout": int,
        "summary_t2i_cdn_providers": list,
    }

    # ========== 人物分析功能配置常量（v0.4.0） ==========

    # 人物分析功能 19 项配置的默认值（见 PRD §5）。值一律字符串存储；列表类型 JSON 序列化。
    # 同时作为初始化播种目标与 dashboard「恢复默认」的目标值；default 一律非 null。
    PROFILE_DEFAULTS: dict[str, str] = {
        # 人物分析总开关
        "profile_enabled": "true",
        # 指令权限：admin 仅管理员 / all 所有人
        "profile_permission": "admin",
        # 输出模式：forward 合并转发 / image 图片 / text 纯文本
        "profile_output_mode": "forward",
        # 主选 LLM provider id（空=使用当前会话模型）
        "profile_provider": "",
        # 备用 provider 降级列表（JSON 存储）；主选失败后按序尝试，全失败兜底会话模型
        "profile_fallback_providers": "[]",
        # 单次分析最大消息条数
        "profile_max_count": "2000",
        # 送入 LLM 的素材长度预算（字符数，截断保最近、统计仍全量）
        "profile_max_prompt_chars": "60000",
        # 关系上下文开关：开启后双向识别目标↔他人的 @/回复互动对象
        "profile_relation_context": "true",
        # 互动对象上下文最大人数（Top N）
        "profile_relation_max_partners": "10",
        # 分析维度开关（五项，关闭的维度不进 prompt、不渲染）
        "profile_dim_habits": "true",
        "profile_dim_activity": "true",
        "profile_dim_personality": "true",
        "profile_dim_hobbies": "true",
        "profile_dim_relations": "true",
        # 触发冷却（秒）：用户 / 群双维度限流
        "profile_user_cooldown": "60",
        "profile_group_cooldown": "30",
        # 触发反馈模式：reaction 贴表情 / text 文字提示 / none 关闭
        "profile_feedback_mode": "reaction",
        # 文字反馈文案（reaction 失败降级时同用）
        "profile_feedback_text": "正在生成人物画像，请稍候…",
        # 历史分析保留天数（清理调度用）
        "profile_keep_days": "30",
    }

    # 各配置键的值类型声明（字符串形式 "bool"/"int"/"str"/"list"），供
    # get_profile_setting_typed() 类型化读取与 save_profile_settings() 全量校验使用
    PROFILE_TYPES: dict[str, str] = {
        "profile_enabled": "bool",
        "profile_permission": "str",
        "profile_output_mode": "str",
        "profile_provider": "str",
        "profile_fallback_providers": "list",
        "profile_max_count": "int",
        "profile_max_prompt_chars": "int",
        "profile_relation_context": "bool",
        "profile_relation_max_partners": "int",
        "profile_dim_habits": "bool",
        "profile_dim_activity": "bool",
        "profile_dim_personality": "bool",
        "profile_dim_hobbies": "bool",
        "profile_dim_relations": "bool",
        "profile_user_cooldown": "int",
        "profile_group_cooldown": "int",
        "profile_feedback_mode": "str",
        "profile_feedback_text": "str",
        "profile_keep_days": "int",
    }

    # ========== 数据分析功能配置常量（v0.5.0） ==========

    # 数据分析功能 8 项配置的默认值（见 PRD §3）。值一律字符串存储。
    # 同时作为初始化播种目标与 dashboard「恢复默认」的目标值。
    STATS_DEFAULTS: dict[str, str] = {
        # 排行展示条数（合法范围 1–50）
        "stats_top_n": "10",
        # /群统计 指令每群冷却秒数（合法范围 0–600）
        "stats_cooldown": "30",
        # 图片快照每小时每群个人 Top K（合法范围 1–100）
        "stats_image_top_k": "20",
        # 日报全局总开关（仍需群级开关同时开启才推送）
        "push_daily_enabled": "true",
        # 日报推送时间（HH:MM，24 小时制）
        "push_daily_time": "21:00",
        # 周报开关（可选功能，默认关）
        "push_weekly_enabled": "false",
        # 周报推送星期（1=周一 … 7=周日）
        "push_weekly_weekday": "1",
        # 周报推送时间（HH:MM，24 小时制）
        "push_weekly_time": "09:00",
    }

    # 各配置键的值类型声明（字符串形式 "bool"/"int"/"str"），供
    # get_stats_setting_typed() 类型化读取与 set_stats_setting() 写入校验使用
    STATS_TYPES: dict[str, str] = {
        "stats_top_n": "int",
        "stats_cooldown": "int",
        "stats_image_top_k": "int",
        "push_daily_enabled": "bool",
        "push_daily_time": "str",
        "push_weekly_enabled": "bool",
        "push_weekly_weekday": "int",
        "push_weekly_time": "str",
    }

    # int 型配置合法范围（闭区间），超范围的写入请求整体拒绝
    STATS_RANGES: dict[str, tuple[int, int]] = {
        "stats_top_n": (1, 50),
        "stats_cooldown": (0, 600),
        "stats_image_top_k": (1, 100),
        "push_weekly_weekday": (1, 7),
    }

    # HH:MM 时间格式配置键，写入时校验并归一化为两位补零形式
    STATS_TIME_KEYS: tuple[str, ...] = ("push_daily_time", "push_weekly_time")

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

    # ========== 总结功能配置（v0.3） ==========

    @staticmethod
    def _convert_summary_value(value: str, target: type):
        """将配置字符串按声明类型转换，失败时抛出异常由调用方兜底。

        Args:
            value: 数据库中存储的原始字符串
            target: SUMMARY_TYPES 声明的目标类型（bool/int/float/list/str）

        Returns:
            转换后的值

        Raises:
            ValueError: 值无法解析为目标类型（如 bool 串非法、JSON 不是列表）
            json.JSONDecodeError: list 类型 JSON 解析失败
        """
        if target is bool:
            # 不能直接 bool(value)（"false" 也是真值），必须显式匹配字符串
            lowered = value.strip().lower()
            if lowered == "true":
                return True
            if lowered == "false":
                return False
            raise ValueError(f"无法识别的布尔值: {value!r}")
        if target is int:
            return int(value)
        if target is float:
            return float(value)
        if target is list:
            parsed = json.loads(value)
            if not isinstance(parsed, list):
                raise ValueError(f"JSON 值不是列表: {value!r}")
            return parsed
        return value

    async def get_summary_setting(self, key: str, default: str | None = None) -> str:
        """获取总结功能配置值（字符串形式）。

        键在表中缺失时自动以规范默认值播种（INSERT OR IGNORE，绝不覆盖并发/已存值）；
        default 为 None 时回退 SUMMARY_DEFAULTS.get(key, "")。

        Args:
            key: 配置键（SUMMARY_DEFAULTS 24 项之一）
            default: 自定义回退值；为 None 时使用 SUMMARY_DEFAULTS 中的默认值

        Returns:
            str: 配置值（字符串形式）
        """
        fallback = self.SUMMARY_DEFAULTS.get(key, "")
        try:
            await self._ensure_db()
            async with self.db.execute(
                "SELECT value FROM summary_settings WHERE key = ?", (key,)
            ) as cursor:
                row = await cursor.fetchone()
            if row is not None:
                return row[0]
            # 缺失键自动播种：OR IGNORE 保证并发与重复调用下不覆盖已插入值
            if key in self.SUMMARY_DEFAULTS:
                await self.db.execute(
                    "INSERT OR IGNORE INTO summary_settings (key, value) VALUES (?, ?)",
                    (key, fallback),
                )
                await self.db.commit()
            return default if default is not None else fallback
        except Exception as e:
            logger.error(f"[HistorySummary] 获取总结配置失败: {e}")
            return default if default is not None else fallback

    async def set_summary_setting(self, key: str, value: str) -> bool:
        """保存总结功能配置值（UPSERT：键存在则更新，不存在则插入）。

        Args:
            key: 配置键
            value: 配置值（统一字符串存储；列表值需调用方先 JSON 序列化）

        Returns:
            bool: 是否成功
        """
        try:
            await self._ensure_db()
            await self.db.execute(
                "INSERT OR REPLACE INTO summary_settings (key, value) VALUES (?, ?)",
                (key, value),
            )
            await self.db.commit()
            return True
        except Exception as e:
            logger.error(f"[HistorySummary] 保存总结配置失败: {e}")
            return False

    async def get_all_summary_settings(self) -> dict[str, str]:
        """获取全部总结功能配置（完整 24 项，缺失以默认值补全）。

        Returns:
            dict[str, str]: 按 SUMMARY_DEFAULTS 顺序的完整配置字典，值均为字符串
        """
        try:
            await self._ensure_db()
            async with self.db.execute(
                "SELECT key, value FROM summary_settings"
            ) as cursor:
                rows = await cursor.fetchall()
            stored = {row[0]: row[1] for row in rows}
            # 以 SUMMARY_DEFAULTS 为骨架补全缺失项，保证返回恒为完整 24 项
            return {
                key: stored.get(key, value)
                for key, value in self.SUMMARY_DEFAULTS.items()
            }
        except Exception as e:
            logger.error(f"[HistorySummary] 获取所有总结配置失败: {e}")
            return {}

    async def reset_summary_settings(
        self, keys: list[str] | None = None
    ) -> dict[str, str]:
        """将总结功能配置重置为默认值。

        Args:
            keys: 需要重置的键列表；为 None 时重置全部 24 项；未知键跳过并告警

        Returns:
            dict[str, str]: 重置后的全量配置（失败时返回空字典）
        """
        try:
            await self._ensure_db()
            targets = list(self.SUMMARY_DEFAULTS.keys()) if keys is None else keys
            for key in targets:
                if key not in self.SUMMARY_DEFAULTS:
                    logger.warning(f"[HistorySummary] 忽略未知配置键的重置请求: {key}")
                    continue
                await self.db.execute(
                    "INSERT OR REPLACE INTO summary_settings (key, value) VALUES (?, ?)",
                    (key, self.SUMMARY_DEFAULTS[key]),
                )
            await self.db.commit()
            return await self.get_all_summary_settings()
        except Exception as e:
            logger.error(f"[HistorySummary] 重置总结配置失败: {e}")
            return {}

    async def get_summary_setting_typed(
        self, key: str
    ) -> int | float | bool | list | str:
        """获取总结配置并按 SUMMARY_TYPES 声明的类型转换。

        bool 识别 "true"/"false"（大小写不敏感）；list 用 json.loads 解析。
        转换失败时记录 warning 并回退默认值的同类型结果。

        Args:
            key: 配置键

        Returns:
            声明类型的配置值（未在 SUMMARY_TYPES 中声明的键按字符串原样返回）
        """
        raw = await self.get_summary_setting(key)
        target = self.SUMMARY_TYPES.get(key, str)
        try:
            return self._convert_summary_value(raw, target)
        except Exception as e:
            logger.warning(
                f"[HistorySummary] 配置 {key} 的值 {raw!r} 类型转换失败，回退默认值: {e}"
            )
            default_raw = self.SUMMARY_DEFAULTS.get(key, "")
            try:
                return self._convert_summary_value(default_raw, target)
            except Exception:
                # 默认值构造上保证合法，此分支仅为最终兜底
                return default_raw

    # ========== 人物分析配置（v0.4.0） ==========

    @staticmethod
    def _convert_profile_value(value: str, target: str):
        """将配置字符串按声明类型字符串转换，失败时抛出异常由调用方兜底。

        Args:
            value: 数据库中存储的原始字符串
            target: PROFILE_TYPES 声明的目标类型字符串（"bool"/"int"/"str"/"list"）

        Returns:
            转换后的值

        Raises:
            ValueError: 值无法解析为目标类型（如 bool 串非法、JSON 不是列表）
            json.JSONDecodeError: list 类型 JSON 解析失败
        """
        if target == "bool":
            # 不能直接 bool(value)（"false" 也是真值），必须显式匹配字符串
            lowered = value.strip().lower()
            if lowered in ("true", "1"):
                return True
            if lowered in ("false", "0"):
                return False
            raise ValueError(f"无法识别的布尔值: {value!r}")
        if target == "int":
            return int(value)
        if target == "list":
            parsed = json.loads(value)
            if not isinstance(parsed, list):
                raise ValueError(f"JSON 值不是列表: {value!r}")
            return parsed
        return value

    async def get_profile_setting(self, key: str) -> str:
        """获取人物分析配置值（字符串形式）。

        键在表中缺失时自动以规范默认值播种（INSERT OR IGNORE，绝不覆盖并发/已存值）；
        键不在 PROFILE_DEFAULTS 中时返回空串。

        Args:
            key: 配置键（PROFILE_DEFAULTS 19 项之一）

        Returns:
            str: 配置值（字符串形式）
        """
        fallback = self.PROFILE_DEFAULTS.get(key, "")
        try:
            await self._ensure_db()
            async with self.db.execute(
                "SELECT value FROM profile_settings WHERE key = ?", (key,)
            ) as cursor:
                row = await cursor.fetchone()
            if row is not None:
                return row[0]
            # 缺失键自动播种：OR IGNORE 保证并发与重复调用下不覆盖已插入值
            if key in self.PROFILE_DEFAULTS:
                await self.db.execute(
                    "INSERT OR IGNORE INTO profile_settings (key, value) VALUES (?, ?)",
                    (key, fallback),
                )
                await self.db.commit()
            return fallback
        except Exception as e:
            logger.error(f"[Profile] 获取人物分析配置失败: {e}")
            return fallback

    async def get_profile_setting_typed(self, key: str) -> Any:
        """获取人物分析配置并按 PROFILE_TYPES 声明的类型转换。

        bool 识别 "true"/"false"/"1"/"0"（大小写不敏感）；list 用 json.loads 解析。
        转换失败时记录 warning 并回退默认值的同类型结果。

        Args:
            key: 配置键

        Returns:
            声明类型的配置值（未在 PROFILE_TYPES 中声明的键按字符串原样返回）
        """
        raw = await self.get_profile_setting(key)
        target = self.PROFILE_TYPES.get(key, "str")
        try:
            return self._convert_profile_value(raw, target)
        except Exception as e:
            logger.warning(
                f"[Profile] 配置 {key} 的值 {raw!r} 类型转换失败，回退默认值: {e}"
            )
            default_raw = self.PROFILE_DEFAULTS.get(key, "")
            try:
                return self._convert_profile_value(default_raw, target)
            except Exception:
                # 默认值构造上保证合法，此分支仅为最终兜底
                return default_raw

    async def get_all_profile_settings(self) -> dict[str, Any]:
        """获取全部人物分析配置（完整 19 项，缺失以默认值补全，逐项类型化）。

        各值按 PROFILE_TYPES 声明类型转换；单项非法值回退该项默认值的同类型结果。

        Returns:
            dict[str, Any]: 按 PROFILE_DEFAULTS 顺序的完整 typed 配置字典（失败时返回空字典）
        """
        try:
            await self._ensure_db()
            async with self.db.execute(
                "SELECT key, value FROM profile_settings"
            ) as cursor:
                rows = await cursor.fetchall()
            stored = {row[0]: row[1] for row in rows}
            # 以 PROFILE_DEFAULTS 为骨架补全缺失项，保证返回恒为完整 19 项
            result: dict[str, Any] = {}
            for key, default_raw in self.PROFILE_DEFAULTS.items():
                raw = stored.get(key, default_raw)
                target = self.PROFILE_TYPES.get(key, "str")
                try:
                    result[key] = self._convert_profile_value(raw, target)
                except Exception:
                    # 非法值回退默认值的同类型结果（默认值构造上保证合法）
                    try:
                        result[key] = self._convert_profile_value(default_raw, target)
                    except Exception:
                        result[key] = default_raw
            return result
        except Exception as e:
            logger.error(f"[Profile] 获取所有人物分析配置失败: {e}")
            return {}

    async def save_profile_settings(self, settings: dict[str, Any]) -> bool:
        """批量保存人物分析配置。

        先全量校验后写入：所有传入键值全部通过 PROFILE_TYPES 声明类型校验后才
        逐项写入，任一非法则整体不写入（校验范式同 web_api summary save）。
        值归一化：bool→"true"/"false"；list/dict→JSON 序列化；其余→str()。
        未知键仅告警跳过，不阻断其余合法项的校验与写入。

        Args:
            settings: 配置键 → 值（任意可归一化类型）的字典

        Returns:
            bool: 是否保存成功（校验未通过或数据库写入异常时返回 False）
        """
        # ① 未知键检查 + ② 值归一化为存储字符串
        normalized: dict[str, str] = {}
        for key, value in settings.items():
            if key not in self.PROFILE_TYPES:
                logger.warning(f"[Profile] 忽略未知配置键: {key}")
                continue
            if isinstance(value, bool):
                str_value = "true" if value else "false"
            elif isinstance(value, (list, dict)):
                str_value = json.dumps(value, ensure_ascii=False)
            else:
                str_value = str(value)
            normalized[key] = str_value

        # ③ 逐个按声明类型校验，任一失败整体放弃写入
        for key, str_value in normalized.items():
            try:
                self._convert_profile_value(str_value, self.PROFILE_TYPES[key])
            except (ValueError, TypeError) as e:
                logger.warning(
                    f"[Profile] 配置项 {key} 的值 {str_value!r} 非法，本次整体不写入: {e}"
                )
                return False

        # ④ 全部校验通过后批量写入
        try:
            await self._ensure_db()
            for key, str_value in normalized.items():
                await self.db.execute(
                    "INSERT OR REPLACE INTO profile_settings (key, value) VALUES (?, ?)",
                    (key, str_value),
                )
            await self.db.commit()
            return True
        except Exception as e:
            logger.error(f"[Profile] 保存人物分析配置失败: {e}")
            return False

    async def reset_profile_settings(self) -> None:
        """将人物分析功能全部 19 项配置重置为 PROFILE_DEFAULTS 默认值。"""
        try:
            await self._ensure_db()
            for key, value in self.PROFILE_DEFAULTS.items():
                await self.db.execute(
                    "INSERT OR REPLACE INTO profile_settings (key, value) VALUES (?, ?)",
                    (key, value),
                )
            await self.db.commit()
        except Exception as e:
            logger.error(f"[Profile] 重置人物分析配置失败: {e}")

    # ========== 总结忽略名单（v0.3） ==========

    async def get_ignore_senders(self, group_id: str) -> list[dict]:
        """获取指定群的总结忽略名单。

        Args:
            group_id: 群号（内部统一字符串化）

        Returns:
            list[dict]: [{"sender_id": str, "created_at": str}]，按 created_at 升序
        """
        try:
            await self._ensure_db()
            # created_at 为秒级精度，同秒插入的记录以 rowid 兜底保证按插入顺序稳定排序
            async with self.db.execute(
                "SELECT sender_id, created_at FROM group_ignore_senders "
                "WHERE group_id = ? ORDER BY created_at ASC, rowid ASC",
                (str(group_id),),
            ) as cursor:
                rows = await cursor.fetchall()
            return [{"sender_id": row[0], "created_at": row[1]} for row in rows]
        except Exception as e:
            logger.error(f"[HistorySummary] 获取忽略名单失败: {e}")
            return []

    async def add_ignore_sender(self, group_id: str, sender_id: str) -> bool:
        """将成员加入群的总结忽略名单。

        依赖 UNIQUE(group_id, sender_id) 去重：重复添加捕获 IntegrityError 返回 False，
        不抛异常（消除 SELECT-then-INSERT 竞态）。created_at 使用 ISO 格式 UTC 时间。

        Args:
            group_id: 群号
            sender_id: 成员 QQ 号

        Returns:
            bool: 新增成功 True；重复或出错 False
        """
        try:
            await self._ensure_db()
            await self.db.execute(
                "INSERT INTO group_ignore_senders (group_id, sender_id, created_at) "
                "VALUES (?, ?, ?)",
                (
                    str(group_id),
                    str(sender_id),
                    datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                ),
            )
            await self.db.commit()
            return True
        except sqlite3.IntegrityError:
            # 唯一约束命中 = 重复添加，幂等返回 False
            return False
        except Exception as e:
            logger.error(f"[HistorySummary] 添加忽略成员失败: {e}")
            return False

    async def remove_ignore_sender(self, group_id: str, sender_id: str) -> bool:
        """从群的总结忽略名单移除成员。

        Args:
            group_id: 群号
            sender_id: 成员 QQ 号

        Returns:
            bool: 实际删除到记录返回 True；记录不存在或出错返回 False（按 rowcount 判断）
        """
        try:
            await self._ensure_db()
            async with self.db.execute(
                "DELETE FROM group_ignore_senders WHERE group_id = ? AND sender_id = ?",
                (str(group_id), str(sender_id)),
            ) as cursor:
                deleted = cursor.rowcount
            await self.db.commit()
            return deleted > 0
        except Exception as e:
            logger.error(f"[HistorySummary] 移除忽略成员失败: {e}")
            return False

    async def list_ignore_groups(self) -> list[str]:
        """列出所有存在总结忽略记录的群号。

        Returns:
            list[str]: 去重后的群号列表（字符串，升序）
        """
        try:
            await self._ensure_db()
            async with self.db.execute(
                "SELECT DISTINCT group_id FROM group_ignore_senders "
                "ORDER BY group_id ASC"
            ) as cursor:
                rows = await cursor.fetchall()
            return [row[0] for row in rows]
        except Exception as e:
            logger.error(f"[HistorySummary] 获取忽略群列表失败: {e}")
            return []

    # ========== 数据分析配置（v0.5.0） ==========

    @staticmethod
    def _convert_stats_value(value: str, target: str):
        """将配置字符串按声明类型字符串转换，失败时抛出异常由调用方兜底。

        Args:
            value: 数据库中存储的原始字符串
            target: STATS_TYPES 声明的目标类型字符串（"bool"/"int"/"str"）

        Returns:
            转换后的值

        Raises:
            ValueError: 值无法解析为目标类型（如 bool 串非法、int 串非数字）
        """
        if target == "bool":
            # 不能直接 bool(value)（"false" 也是真值），必须显式匹配字符串
            lowered = value.strip().lower()
            if lowered in ("true", "1"):
                return True
            if lowered in ("false", "0"):
                return False
            raise ValueError(f"无法识别的布尔值: {value!r}")
        if target == "int":
            return int(value)
        return value

    @classmethod
    def _validate_stats_setting(cls, key: str, str_value: str) -> str | None:
        """按类型 + 范围 + HH:MM 格式校验配置值。

        Args:
            key: 配置键（必须在 STATS_TYPES 中声明）
            str_value: 已归一化为字符串的待写入值

        Returns:
            str | None: 合法时返回归一化后的存储字符串（int 去前导零、
            bool 归一 "true"/"false"、时间两位补零）；
            类型/范围/格式任一不合法时返回 None
        """
        target = cls.STATS_TYPES[key]
        try:
            converted = cls._convert_stats_value(str_value, target)
        except (ValueError, TypeError):
            return None
        if target == "bool":
            # 归一化存储，避免 "1"/"0" 与 "true"/"false" 混存
            return "true" if converted else "false"
        # int 键范围校验（STATS_RANGES 声明的闭区间）
        if key in cls.STATS_RANGES:
            low, high = cls.STATS_RANGES[key]
            if not low <= converted <= high:
                return None
            return str(converted)
        # HH:MM 时间键格式校验（容忍一位小时数，归一化为两位补零）
        if key in cls.STATS_TIME_KEYS:
            match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", str_value.strip())
            if match is None:
                return None
            return f"{int(match.group(1)):02d}:{match.group(2)}"
        return str_value

    async def get_stats_setting(self, key: str) -> str:
        """获取数据分析配置值（字符串形式）。

        键在表中缺失时自动以规范默认值播种（INSERT OR IGNORE，绝不覆盖并发/已存值）；
        键不在 STATS_DEFAULTS 中时返回空串。

        Args:
            key: 配置键（STATS_DEFAULTS 8 项之一）

        Returns:
            str: 配置值（字符串形式）
        """
        fallback = self.STATS_DEFAULTS.get(key, "")
        try:
            await self._ensure_db()
            async with self.db.execute(
                "SELECT value FROM stats_settings WHERE key = ?", (key,)
            ) as cursor:
                row = await cursor.fetchone()
            if row is not None:
                return row[0]
            # 缺失键自动播种：OR IGNORE 保证并发与重复调用下不覆盖已插入值
            if key in self.STATS_DEFAULTS:
                await self.db.execute(
                    "INSERT OR IGNORE INTO stats_settings (key, value) VALUES (?, ?)",
                    (key, fallback),
                )
                await self.db.commit()
            return fallback
        except Exception as e:
            logger.error(f"[Stats] 获取数据分析配置失败: {e}")
            return fallback

    async def get_stats_setting_typed(self, key: str) -> Any:
        """获取数据分析配置并按 STATS_TYPES 声明的类型转换。

        bool 识别 "true"/"false"/"1"/"0"（大小写不敏感）。
        转换失败时记录 warning 并回退默认值的同类型结果。

        Args:
            key: 配置键

        Returns:
            声明类型的配置值（未在 STATS_TYPES 中声明的键按字符串原样返回）
        """
        raw = await self.get_stats_setting(key)
        target = self.STATS_TYPES.get(key, "str")
        try:
            return self._convert_stats_value(raw, target)
        except Exception as e:
            logger.warning(
                f"[Stats] 配置 {key} 的值 {raw!r} 类型转换失败，回退默认值: {e}"
            )
            default_raw = self.STATS_DEFAULTS.get(key, "")
            try:
                return self._convert_stats_value(default_raw, target)
            except Exception:
                # 默认值构造上保证合法，此分支仅为最终兜底
                return default_raw

    async def set_stats_setting(self, key: str, value: Any) -> bool:
        """保存单项数据分析配置（先校验后写入，非法值拒绝写入）。

        校验链：未知键 → 类型（STATS_TYPES）→ 范围（STATS_RANGES 闭区间）→
        HH:MM 格式（STATS_TIME_KEYS，归一化两位补零）。
        值归一化：bool→"true"/"false"；其余→str()。

        Args:
            key: 配置键
            value: 配置值（任意可归一化类型）

        Returns:
            bool: 是否保存成功（键未知、值非法或数据库写入异常时返回 False）
        """
        if key not in self.STATS_TYPES:
            logger.warning(f"[Stats] 忽略未知配置键: {key}")
            return False
        # 值归一化为存储字符串（范式同 save_profile_settings）
        if isinstance(value, bool):
            str_value = "true" if value else "false"
        else:
            str_value = str(value)
        normalized = self._validate_stats_setting(key, str_value)
        if normalized is None:
            logger.warning(
                f"[Stats] 配置项 {key} 的值 {str_value!r} 非法（类型/范围/格式），"
                "本次不写入"
            )
            return False
        try:
            await self._ensure_db()
            await self.db.execute(
                "INSERT OR REPLACE INTO stats_settings (key, value) VALUES (?, ?)",
                (key, normalized),
            )
            await self.db.commit()
            return True
        except Exception as e:
            logger.error(f"[Stats] 保存数据分析配置失败: {e}")
            return False

    async def get_all_stats_settings(self) -> dict[str, Any]:
        """获取全部数据分析配置（完整 8 项，缺失以默认值补全，逐项类型化）。

        各值按 STATS_TYPES 声明类型转换；单项非法值回退该项默认值的同类型结果。

        Returns:
            dict[str, Any]: 按 STATS_DEFAULTS 顺序的完整 typed 配置字典
            （数据库异常时返回空字典）
        """
        try:
            await self._ensure_db()
            async with self.db.execute(
                "SELECT key, value FROM stats_settings"
            ) as cursor:
                rows = await cursor.fetchall()
            stored = {row[0]: row[1] for row in rows}
            # 以 STATS_DEFAULTS 为骨架补全缺失项，保证返回恒为完整 8 项
            result: dict[str, Any] = {}
            for key, default_raw in self.STATS_DEFAULTS.items():
                raw = stored.get(key, default_raw)
                target = self.STATS_TYPES.get(key, "str")
                try:
                    result[key] = self._convert_stats_value(raw, target)
                except Exception:
                    # 非法值回退默认值的同类型结果（默认值构造上保证合法）
                    try:
                        result[key] = self._convert_stats_value(default_raw, target)
                    except Exception:
                        result[key] = default_raw
            return result
        except Exception as e:
            logger.error(f"[Stats] 获取所有数据分析配置失败: {e}")
            return {}

    async def reset_stats_settings(self) -> None:
        """将数据分析功能全部 8 项配置重置为 STATS_DEFAULTS 默认值。"""
        try:
            await self._ensure_db()
            for key, value in self.STATS_DEFAULTS.items():
                await self.db.execute(
                    "INSERT OR REPLACE INTO stats_settings (key, value) VALUES (?, ?)",
                    (key, value),
                )
            await self.db.commit()
        except Exception as e:
            logger.error(f"[Stats] 重置数据分析配置失败: {e}")

    # ========== 群推送开关（v0.5.0） ==========

    async def get_push_groups(self) -> list[dict]:
        """获取所有白名单群的推送开关状态。

        以 group_config 白名单表为准 LEFT JOIN push_group：不在白名单的
        push_group 行忽略；白名单群无 push_group 行时默认 False。

        Returns:
            list[dict]: [{"group_id": str, "enabled": bool}]，按群号升序
        """
        try:
            await self._ensure_db()
            # group_config.group_id 为 INTEGER、push_group.group_id 为 TEXT，
            # 连接键需 CAST 统一为字符串比较
            async with self.db.execute(
                "SELECT gc.group_id, pg.enabled "
                "FROM group_config AS gc "
                "LEFT JOIN push_group AS pg "
                "ON CAST(gc.group_id AS TEXT) = pg.group_id "
                "ORDER BY gc.group_id ASC"
            ) as cursor:
                rows = await cursor.fetchall()
            # LEFT JOIN 未命中时 enabled 为 NULL，bool(None) == False 即默认关
            return [{"group_id": str(row[0]), "enabled": bool(row[1])} for row in rows]
        except Exception as e:
            logger.error(f"[Stats] 获取群推送开关失败: {e}")
            return []

    async def set_push_group(self, group_id: str, enabled: bool) -> bool:
        """UPSERT 群级推送开关（新增或覆盖，幂等）。

        Args:
            group_id: 群号（内部统一字符串化）
            enabled: 推送开关目标状态

        Returns:
            bool: 是否成功
        """
        try:
            await self._ensure_db()
            await self.db.execute(
                "INSERT INTO push_group (group_id, enabled) VALUES (?, ?) "
                "ON CONFLICT(group_id) DO UPDATE SET enabled = excluded.enabled",
                (str(group_id), 1 if enabled else 0),
            )
            await self.db.commit()
            return True
        except Exception as e:
            logger.error(f"[Stats] 保存群推送开关失败: {e}")
            return False

    async def get_push_flags(self, group_ids: list[str]) -> dict[str, bool]:
        """批量读取指定群号的推送开关状态（v0.5.1，all_mode 列表源配套）。

        与 get_push_groups 以白名单为基准不同，本方法对调用方给出的任意
        群号列表取 push_group 行：无行的群默认 False（开关默认关）。

        Args:
            group_ids: 群号列表（内部统一字符串化）；空列表直接返回 {}

        Returns:
            dict[str, bool]: {group_id: enabled}；异常仅记日志返回 {}
        """
        if not group_ids:
            return {}
        try:
            await self._ensure_db()
            placeholders = ",".join("?" for _ in group_ids)
            async with self.db.execute(
                f"SELECT group_id, enabled FROM push_group "
                f"WHERE group_id IN ({placeholders})",
                [str(g) for g in group_ids],
            ) as cursor:
                rows = await cursor.fetchall()
            # 请求的群号全部出现在结果中：无行的群默认 False（开关默认关）
            flags = {str(g): False for g in group_ids}
            flags.update({str(row[0]): bool(row[1]) for row in rows})
            return flags
        except Exception as e:
            logger.error(f"[Stats] 读取推送开关标志失败: {e}")
            return {}

    # ========== 图片统计快照（v0.5.0） ==========

    async def snapshot_upsert(
        self, hour_rows: list[tuple], top_rows: list[tuple]
    ) -> None:
        """UPSERT 图片统计小时级快照行（同主键覆盖写，可重复执行）。

        Args:
            hour_rows: (date, hour, group_id, image_count) 行列表（群级全量）
            top_rows: (date, hour, group_id, sender_id, sender_name, image_count)
                行列表（每小时每群个人 Top K，覆盖时同步更新 sender_name）

        两列表均为空时直接返回；异常仅记日志不抛出（定时任务调用方依赖）。
        """
        if not hour_rows and not top_rows:
            return
        try:
            await self._ensure_db()
            if hour_rows:
                await self.db.executemany(
                    "INSERT INTO image_stats_hourly "
                    "(date, hour, group_id, image_count) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(date, hour, group_id) "
                    "DO UPDATE SET image_count = excluded.image_count",
                    hour_rows,
                )
            if top_rows:
                await self.db.executemany(
                    "INSERT INTO image_stats_hourly_top "
                    "(date, hour, group_id, sender_id, sender_name, image_count) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(date, hour, group_id, sender_id) "
                    "DO UPDATE SET image_count = excluded.image_count, "
                    "sender_name = excluded.sender_name",
                    top_rows,
                )
            await self.db.commit()
        except Exception as e:
            logger.error(f"[Stats] 写入图片统计快照失败: {e}")

    async def snapshot_query(
        self, start: datetime, end: datetime, group_id: str | None = None
    ) -> dict:
        """按 (date, hour) 半开区间 [start, end) 聚合查询图片统计快照。

        区间展开：date > start_date 或 (date == start_date 且 hour >= start.hour)；
        end 不含：date < end_date 或 (date == end_date 且 hour < end.hour)。
        total/by_group 取自 image_stats_hourly（群级全量口径），
        by_sender 取自 image_stats_hourly_top（Top K 个人口径）。

        Args:
            start: 起始时间（含）
            end: 结束时间（不含）
            group_id: 可选群过滤；None 时聚合全部群

        Returns:
            dict: {"total": int, "by_group": {gid: int},
                   "by_sender": {(gid, sender_id): int}}；异常返回全零结构
        """
        empty = {"total": 0, "by_group": {}, "by_sender": {}}
        try:
            await self._ensure_db()
            start_date = start.strftime("%Y-%m-%d")
            end_date = end.strftime("%Y-%m-%d")
            # (date, hour) 半开区间条件，比较值全部参数化绑定
            time_cond = (
                "((date > ?) OR (date = ? AND hour >= ?)) AND "
                "((date < ?) OR (date = ? AND hour < ?))"
            )
            params: tuple = (
                start_date,
                start_date,
                start.hour,
                end_date,
                end_date,
                end.hour,
            )
            group_cond = ""
            if group_id is not None:
                group_cond = " AND group_id = ?"
                params = params + (str(group_id),)
            result: dict = {"total": 0, "by_group": {}, "by_sender": {}}
            # 群级全量：总图片数与分群聚合
            async with self.db.execute(
                "SELECT group_id, SUM(image_count) FROM image_stats_hourly "
                f"WHERE {time_cond}{group_cond} GROUP BY group_id",
                params,
            ) as cursor:
                for gid, count in await cursor.fetchall():
                    result["by_group"][gid] = count
                    result["total"] += count
            # 个人 Top K：(group_id, sender_id) 维度聚合
            async with self.db.execute(
                "SELECT group_id, sender_id, SUM(image_count) "
                "FROM image_stats_hourly_top "
                f"WHERE {time_cond}{group_cond} "
                "GROUP BY group_id, sender_id",
                params,
            ) as cursor:
                for gid, sid, count in await cursor.fetchall():
                    result["by_sender"][(gid, sid)] = count
            return result
        except Exception as e:
            logger.error(f"[Stats] 查询图片统计快照失败: {e}")
            return empty

    # ========== 分段快照体系（v0.5.5） ==========

    async def snapshot_upsert_msg_hour(
        self, hour_rows: list[tuple], top_rows: list[tuple]
    ) -> None:
        """UPSERT 消息统计小时级快照行（同主键覆盖写，可重复执行）。

        v0.5.5 分段快照体系消息侧小时层，与图片侧 snapshot_upsert 语义对称：
        hour_rows 写入 msg_stats_hourly（群级全量），top_rows 写入
        msg_stats_hourly_top（每小时每群个人 Top K）。

        Args:
            hour_rows: (date, hour, group_id, msg_count) 行列表（群级全量）
            top_rows: (date, hour, group_id, sender_id, sender_name, msg_count)
                行列表（每小时每群个人 Top K，覆盖时同步更新 sender_name）

        两列表均为空时直接返回；异常仅记日志不抛出（定时任务调用方依赖）。
        """
        if not hour_rows and not top_rows:
            return
        try:
            await self._ensure_db()
            if hour_rows:
                await self.db.executemany(
                    "INSERT INTO msg_stats_hourly "
                    "(date, hour, group_id, msg_count) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(date, hour, group_id) "
                    "DO UPDATE SET msg_count = excluded.msg_count",
                    hour_rows,
                )
            if top_rows:
                await self.db.executemany(
                    "INSERT INTO msg_stats_hourly_top "
                    "(date, hour, group_id, sender_id, sender_name, msg_count) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(date, hour, group_id, sender_id) "
                    "DO UPDATE SET msg_count = excluded.msg_count, "
                    "sender_name = excluded.sender_name",
                    top_rows,
                )
            await self.db.commit()
        except Exception as e:
            logger.error(f"[Stats] 写入消息统计快照失败: {e}")

    async def snapshot_upsert_daily(
        self, msg_rows: list[tuple], image_rows: list[tuple]
    ) -> None:
        """UPSERT 消息/图片统计日级快照行（同主键覆盖写，可重复执行）。

        v0.5.5 分段快照体系日层：行形如 (date, group_id, count)，为群级每日
        总数全量（不受 Top K 截断），只存完整自然日（今天的数据由小时层承载）；
        msg_rows 写入 msg_stats_daily，image_rows 写入 image_stats_daily。

        Args:
            msg_rows: (date, group_id, count) 行列表（写入 msg_stats_daily）
            image_rows: (date, group_id, count) 行列表（写入 image_stats_daily）

        两列表均为空时直接返回；异常仅记日志不抛出（定时任务调用方依赖）。
        """
        if not msg_rows and not image_rows:
            return
        try:
            await self._ensure_db()
            if msg_rows:
                await self.db.executemany(
                    "INSERT INTO msg_stats_daily (date, group_id, msg_count) "
                    "VALUES (?, ?, ?) "
                    "ON CONFLICT(date, group_id) "
                    "DO UPDATE SET msg_count = excluded.msg_count",
                    msg_rows,
                )
            if image_rows:
                await self.db.executemany(
                    "INSERT INTO image_stats_daily (date, group_id, image_count) "
                    "VALUES (?, ?, ?) "
                    "ON CONFLICT(date, group_id) "
                    "DO UPDATE SET image_count = excluded.image_count",
                    image_rows,
                )
            await self.db.commit()
        except Exception as e:
            logger.error(f"[Stats] 写入统计日级快照失败: {e}")

    async def snapshot_upsert_monthly(
        self, msg_rows: list[tuple], image_rows: list[tuple]
    ) -> None:
        """UPSERT 消息/图片统计月级快照行（同主键覆盖写，可重复执行）。

        v0.5.5 分段快照体系月层：行形如 (month, group_id, count)，month 为
        "YYYY-MM" 格式，只存完整自然月，永久保留不淘汰；msg_rows 写入
        msg_stats_monthly，image_rows 写入 image_stats_monthly。

        Args:
            msg_rows: (month, group_id, count) 行列表（写入 msg_stats_monthly）
            image_rows: (month, group_id, count) 行列表（写入 image_stats_monthly）

        两列表均为空时直接返回；异常仅记日志不抛出（定时任务调用方依赖）。
        """
        if not msg_rows and not image_rows:
            return
        try:
            await self._ensure_db()
            if msg_rows:
                await self.db.executemany(
                    "INSERT INTO msg_stats_monthly (month, group_id, msg_count) "
                    "VALUES (?, ?, ?) "
                    "ON CONFLICT(month, group_id) "
                    "DO UPDATE SET msg_count = excluded.msg_count",
                    msg_rows,
                )
            if image_rows:
                await self.db.executemany(
                    "INSERT INTO image_stats_monthly "
                    "(month, group_id, image_count) VALUES (?, ?, ?) "
                    "ON CONFLICT(month, group_id) "
                    "DO UPDATE SET image_count = excluded.image_count",
                    image_rows,
                )
            await self.db.commit()
        except Exception as e:
            logger.error(f"[Stats] 写入统计月级快照失败: {e}")

    async def snapshot_monthly_totals(
        self,
        source: str,
        month_start: str,
        month_end: str,
        group_id: str | None = None,
    ) -> dict[str, int]:
        """按群累计月级快照总数（month 闭区间）。

        v0.5.5 分段快照体系查询原语：读取 *_stats_monthly 表，按 "YYYY-MM"
        闭区间 [month_start, month_end] 对每群 SUM 累计。

        Args:
            source: 快照来源，"msg"（消息）或 "image"（图片）
            month_start: 月份起点 "YYYY-MM"（含）
            month_end: 月份终点 "YYYY-MM"（含）
            group_id: 可选群过滤；None 时聚合全部群

        Returns:
            dict[str, int]: {group_id: 累计数}；source 非法或异常时返回空
            dict（仅记日志不抛出）
        """
        # source → (表名, 计数列名) 白名单映射：表名/列名仅从白名单取用，
        # 杜绝非法 source 值拼接进 SQL 的注入风险
        source_map = {
            "msg": ("msg_stats_monthly", "msg_count"),
            "image": ("image_stats_monthly", "image_count"),
        }
        entry = source_map.get(source)
        if entry is None:
            logger.warning(
                f"[Stats] snapshot_monthly_totals 收到非法 source: {source!r}"
            )
            return {}
        table, count_col = entry
        try:
            await self._ensure_db()
            # 闭区间比较值全部参数化绑定
            params: tuple = (month_start, month_end)
            group_cond = ""
            if group_id is not None:
                group_cond = " AND group_id = ?"
                params = params + (str(group_id),)
            async with self.db.execute(
                f"SELECT group_id, SUM({count_col}) FROM {table} "
                f"WHERE month >= ? AND month <= ?{group_cond} "
                "GROUP BY group_id",
                params,
            ) as cursor:
                rows = await cursor.fetchall()
            return {row[0]: row[1] for row in rows}
        except Exception as e:
            logger.error(f"[Stats] 查询月级快照失败: {e}")
            return {}

    async def snapshot_daily_rows(
        self,
        source: str,
        date_start: str,
        date_end: str,
        group_id: str | None = None,
    ) -> dict[tuple[str, str], int]:
        """读取日级快照原始行（date 闭区间）。

        v0.5.5 分段快照体系查询原语：读取 *_stats_daily 表，取 "YYYY-MM-DD"
        闭区间 [date_start, date_end] 内的各 (date, group_id) 原始行。

        Args:
            source: 快照来源，"msg"（消息）或 "image"（图片）
            date_start: 日期起点 "YYYY-MM-DD"（含）
            date_end: 日期终点 "YYYY-MM-DD"（含）
            group_id: 可选群过滤；None 时取全部群

        Returns:
            dict[tuple[str, str], int]: {(date, group_id): count}；source 非法
            或异常时返回空 dict（仅记日志不抛出）
        """
        # source → (表名, 计数列名) 白名单映射：表名/列名仅从白名单取用，
        # 杜绝非法 source 值拼接进 SQL 的注入风险
        source_map = {
            "msg": ("msg_stats_daily", "msg_count"),
            "image": ("image_stats_daily", "image_count"),
        }
        entry = source_map.get(source)
        if entry is None:
            logger.warning(f"[Stats] snapshot_daily_rows 收到非法 source: {source!r}")
            return {}
        table, count_col = entry
        try:
            await self._ensure_db()
            params: tuple = (date_start, date_end)
            group_cond = ""
            if group_id is not None:
                group_cond = " AND group_id = ?"
                params = params + (str(group_id),)
            async with self.db.execute(
                f"SELECT date, group_id, {count_col} FROM {table} "
                f"WHERE date >= ? AND date <= ?{group_cond}",
                params,
            ) as cursor:
                rows = await cursor.fetchall()
            return {(row[0], row[1]): row[2] for row in rows}
        except Exception as e:
            logger.error(f"[Stats] 查询日级快照失败: {e}")
            return {}

    async def snapshot_hourly_date_totals(
        self,
        source: str,
        date_start: str,
        date_end: str,
        group_id: str | None = None,
    ) -> dict[tuple[str, str], int]:
        """小时级快照按 (date, group_id) SUM 聚合（date 闭区间）。

        v0.5.5 分段快照体系查询原语：读取 *_stats_hourly 群级全量表
        （不含 Top K 表），按 "YYYY-MM-DD" 闭区间 [date_start, date_end]
        逐 (date, group_id) SUM。某 (date, group_id) 无行即不出现在结果中
        （存在性语义由调用方使用）。

        Args:
            source: 快照来源，"msg"（消息）或 "image"（图片）
            date_start: 日期起点 "YYYY-MM-DD"（含）
            date_end: 日期终点 "YYYY-MM-DD"（含）
            group_id: 可选群过滤；None 时聚合全部群

        Returns:
            dict[tuple[str, str], int]: {(date, group_id): count}；source 非法
            或异常时返回空 dict（仅记日志不抛出）
        """
        # source → (表名, 计数列名) 白名单映射：表名/列名仅从白名单取用，
        # 杜绝非法 source 值拼接进 SQL 的注入风险
        source_map = {
            "msg": ("msg_stats_hourly", "msg_count"),
            "image": ("image_stats_hourly", "image_count"),
        }
        entry = source_map.get(source)
        if entry is None:
            logger.warning(
                f"[Stats] snapshot_hourly_date_totals 收到非法 source: {source!r}"
            )
            return {}
        table, count_col = entry
        try:
            await self._ensure_db()
            params: tuple = (date_start, date_end)
            group_cond = ""
            if group_id is not None:
                group_cond = " AND group_id = ?"
                params = params + (str(group_id),)
            async with self.db.execute(
                f"SELECT date, group_id, SUM({count_col}) FROM {table} "
                f"WHERE date >= ? AND date <= ?{group_cond} "
                "GROUP BY date, group_id",
                params,
            ) as cursor:
                rows = await cursor.fetchall()
            return {(row[0], row[1]): row[2] for row in rows}
        except Exception as e:
            logger.error(f"[Stats] 查询小时级快照日期聚合失败: {e}")
            return {}

    async def snapshot_evict(
        self,
        hourly_cutoff: str,
        msg_top_cutoff: str,
        image_top_cutoff: str,
        daily_cutoff: str,
    ) -> bool:
        """淘汰过期快照行（DELETE WHERE date < ?，monthly 两表不动）。

        v0.5.5 分段快照体系淘汰（PRD F4.3）：
        - msg_stats_hourly 与 image_stats_hourly 使用 hourly_cutoff；
        - msg_stats_hourly_top 使用 msg_top_cutoff；
        - image_stats_hourly_top 使用 image_top_cutoff；
        - msg_stats_daily 与 image_stats_daily 使用 daily_cutoff；
        - msg_stats_monthly / image_stats_monthly 永不淘汰，此处不触碰。

        Args:
            hourly_cutoff: 小时层淘汰阈值 "YYYY-MM-DD"（删除 date 小于该值的行）
            msg_top_cutoff: 消息小时 Top K 表淘汰阈值 "YYYY-MM-DD"
            image_top_cutoff: 图片小时 Top K 表淘汰阈值 "YYYY-MM-DD"
            daily_cutoff: 日层淘汰阈值 "YYYY-MM-DD"

        Returns:
            bool: 全部删除成功并 commit 后返回 True；任一异常仅记日志返回
            False（不阻断统计主流程）
        """
        try:
            await self._ensure_db()
            await self.db.execute(
                "DELETE FROM msg_stats_hourly WHERE date < ?", (hourly_cutoff,)
            )
            await self.db.execute(
                "DELETE FROM image_stats_hourly WHERE date < ?", (hourly_cutoff,)
            )
            await self.db.execute(
                "DELETE FROM msg_stats_hourly_top WHERE date < ?",
                (msg_top_cutoff,),
            )
            await self.db.execute(
                "DELETE FROM image_stats_hourly_top WHERE date < ?",
                (image_top_cutoff,),
            )
            await self.db.execute(
                "DELETE FROM msg_stats_daily WHERE date < ?", (daily_cutoff,)
            )
            await self.db.execute(
                "DELETE FROM image_stats_daily WHERE date < ?", (daily_cutoff,)
            )
            await self.db.commit()
            return True
        except Exception as e:
            logger.error(f"[Stats] 淘汰快照失败: {e}")
            return False
