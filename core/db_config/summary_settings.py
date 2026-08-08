"""总结功能配置 Mixin（SummarySettingsMixin）。

总结功能 24 项配置（SUMMARY_DEFAULTS / SUMMARY_TYPES）与群总结忽略名单的读写。
类常量随 Mixin 迁移，组装后经 MRO 仍可从 ConfigManager 直接访问
（web_api.py / summary/summarizer.py 直接引用 ConfigManager.SUMMARY_*）。
"""

import json
import sqlite3
from datetime import datetime, timezone

from astrbot.api import logger


class SummarySettingsMixin:
    """总结功能配置 Mixin：24 项配置（SUMMARY_DEFAULTS/TYPES）+ 群忽略名单。"""

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
