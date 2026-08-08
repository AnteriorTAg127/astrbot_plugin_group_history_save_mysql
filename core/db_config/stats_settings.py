"""数据分析功能配置 Mixin（StatsSettingsMixin）。

数据分析功能 8 项配置（STATS_DEFAULTS / STATS_TYPES / STATS_RANGES /
STATS_TIME_KEYS）与群推送开关的读写。类常量随 Mixin 迁移，组装后经 MRO
仍可从 ConfigManager 直接访问（web_api.py 直接引用 ConfigManager.STATS_TYPES）。
"""

import re
from typing import Any

from astrbot.api import logger


class StatsSettingsMixin:
    """数据分析功能配置 Mixin：8 项配置（STATS_DEFAULTS/TYPES/RANGES/TIME_KEYS）+ 群推送开关。"""

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
