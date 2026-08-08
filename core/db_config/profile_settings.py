"""人物分析功能配置 Mixin（ProfileSettingsMixin）。

人物分析功能 19 项配置（PROFILE_DEFAULTS / PROFILE_TYPES）的读写与全量校验。
类常量随 Mixin 迁移，组装后经 MRO 仍可从 ConfigManager 直接访问
（web_api.py 直接引用 ConfigManager.PROFILE_*）。
"""

import json
from typing import Any

from astrbot.api import logger


class ProfileSettingsMixin:
    """人物分析功能配置 Mixin：19 项配置（PROFILE_DEFAULTS/TYPES）+ 批量保存校验。"""

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
