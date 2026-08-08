"""Web API 存储库端点（v0.6.0 包化拆分）。

StorageMixin：状态 / 群白名单 / 设置 / 每日统计 / 手动清理 / 清空验证与清空
等存储库核心端点。
"""

import time

from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request

from .base import CHALLENGE_MAX, CHALLENGE_TTL, make_challenge


class StorageMixin:
    """存储库端点 Mixin（v0.6.0 拆分自 web_api.py）。"""

    async def api_status(self):
        """获取数据库连接状态和统计概览。"""
        ping_result = await self.mysql_mgr.ping()
        stats = await self.mysql_mgr.get_stats() if ping_result["connected"] else {}
        groups = await self.config_mgr.get_groups()
        settings = await self.config_mgr.get_all_settings()

        return json_response(
            {
                "database": ping_result,
                "stats": stats,
                "enabled_groups": len([g for g in groups if g["enabled"]]),
                "total_groups": len(groups),
                "all_mode": settings.get("all_mode", "false") == "true",
            }
        )

    async def api_get_groups(self):
        """获取群白名单列表。"""
        groups = await self.config_mgr.get_groups()
        settings = await self.config_mgr.get_all_settings()
        return json_response(
            {
                "groups": groups,
                "all_mode": settings.get("all_mode", "false") == "true",
            }
        )

    async def api_toggle_group(self):
        """切换指定群的启用状态。"""
        payload = await request.json(default={})
        group_id = payload.get("group_id")
        if not group_id:
            return error_response("缺少 group_id 参数", status_code=400)
        try:
            group_id = int(group_id)
        except (ValueError, TypeError):
            return error_response("group_id 必须为数字", status_code=400)

        new_state = await self.config_mgr.toggle_group(group_id)
        if new_state is None:
            return error_response(f"群 {group_id} 不在白名单中", status_code=404)
        return json_response({"group_id": group_id, "enabled": new_state})

    async def api_add_group(self):
        """添加群到白名单。"""
        payload = await request.json(default={})
        group_id = payload.get("group_id")
        if not group_id:
            return error_response("缺少 group_id 参数", status_code=400)
        try:
            group_id = int(group_id)
        except (ValueError, TypeError):
            return error_response("group_id 必须为数字", status_code=400)

        success = await self.config_mgr.add_group(group_id)
        if success:
            return json_response({"group_id": group_id, "added": True})
        return error_response("添加群失败", status_code=500)

    async def api_remove_group(self):
        """从白名单移除群。"""
        payload = await request.json(default={})
        group_id = payload.get("group_id")
        if not group_id:
            return error_response("缺少 group_id 参数", status_code=400)
        try:
            group_id = int(group_id)
        except (ValueError, TypeError):
            return error_response("group_id 必须为数字", status_code=400)

        success = await self.config_mgr.remove_group(group_id)
        if success:
            return json_response({"group_id": group_id, "removed": True})
        return error_response("移除群失败", status_code=500)

    async def api_get_settings(self):
        """获取插件设置。"""
        settings = await self.config_mgr.get_all_settings()
        return json_response(settings)

    async def api_save_settings(self):
        """保存插件设置。

        先校验后写入：所有传入字段全部通过校验后才批量写入，
        避免靠后字段非法时靠前字段已被写入的部分生效问题。
        """
        payload = await request.json(default={})

        new_retention: str | None = None
        new_all_mode: str | None = None
        new_backfill_enabled: str | None = None
        new_backfill_hours: str | None = None

        # 验证 image_retention_days
        if "image_retention_days" in payload:
            try:
                days = int(payload["image_retention_days"])
            except (ValueError, TypeError):
                return error_response("保留天数必须为正整数", status_code=400)
            if days < 1:
                return error_response("保留天数不能小于 1", status_code=400)
            if days > 36500:
                return error_response("保留天数不能大于 36500", status_code=400)
            new_retention = str(days)

        # 验证 all_mode
        if "all_mode" in payload:
            all_mode = payload["all_mode"]
            if isinstance(all_mode, bool):
                new_all_mode = str(all_mode).lower()
            elif isinstance(all_mode, str) and all_mode in ("true", "false"):
                new_all_mode = all_mode
            else:
                return error_response("all_mode 必须为布尔值", status_code=400)

        # 验证 backfill_enabled（v0.6.0 重载自动补库开关）
        if "backfill_enabled" in payload:
            value = payload["backfill_enabled"]
            if isinstance(value, bool):
                new_backfill_enabled = str(value).lower()
            elif isinstance(value, str) and value in ("true", "false"):
                new_backfill_enabled = value
            else:
                return error_response("backfill_enabled 必须为布尔值", status_code=400)

        # 验证 backfill_hours（窗口夹取 [1,168] 与启动期一致）
        if "backfill_hours" in payload:
            try:
                hours = int(payload["backfill_hours"])
            except (ValueError, TypeError):
                return error_response("补库时长必须为整数", status_code=400)
            if hours < 1 or hours > 168:
                return error_response("补库时长必须在 1-168 小时之间", status_code=400)
            new_backfill_hours = str(hours)

        # 全部校验通过后写入
        if new_retention is not None:
            await self.config_mgr.set_setting("image_retention_days", new_retention)
        if new_all_mode is not None:
            await self.config_mgr.set_setting("all_mode", new_all_mode)
        if new_backfill_enabled is not None:
            await self.config_mgr.set_setting("backfill_enabled", new_backfill_enabled)
        if new_backfill_hours is not None:
            await self.config_mgr.set_setting("backfill_hours", new_backfill_hours)

        settings = await self.config_mgr.get_all_settings()
        return json_response({"saved": True, "settings": settings})

    async def api_daily_stats(self):
        """获取每日存储统计。

        v0.5.5：days<=31 时优先由快照口径供数（stats_service.overview_daily_stats），
        days 超出、快照不可用或 stats_service 未注入时回落 MySQL 实时路径；
        响应结构不变（{"days", "items": [{date, messages, images}]}）。
        """
        days_str = request.query.get("days") or "7"
        try:
            days = int(days_str)
        except (ValueError, TypeError):
            days = 7
        if days < 1:
            days = 7
        if days > 90:
            days = 90
        stats = None
        if self.stats_service is not None:
            stats = await self.stats_service.overview_daily_stats(days)
        if stats is None:
            stats = await self.mysql_mgr.get_daily_stats(days)
        return json_response({"days": days, "items": stats})

    async def api_clean(self):
        """手动触发图片清理。"""
        payload = await request.json(default={})
        days = payload.get("days")
        if days is not None:
            try:
                days = int(days)
                if days < 1:
                    return error_response("天数不能小于 1", status_code=400)
            except (ValueError, TypeError):
                return error_response("天数必须为正整数", status_code=400)

        deleted = await self.cleaner.manual_clean(days)
        if deleted >= 0:
            return json_response({"deleted": deleted, "days": days or "配置值"})
        return error_response("清理失败，请检查数据库连接", status_code=500)

    async def api_purge_challenge(self):
        """生成随机加减法验证题，用于清空所有数据前的二次确认。"""
        now = time.monotonic()
        # 清理过期项，防止内存泄漏
        self._purge_challenges = {
            k: v for k, v in self._purge_challenges.items() if v[1] > now
        }
        # 超过上限拒绝新建，防止高频调用导致内存膨胀
        if len(self._purge_challenges) >= CHALLENGE_MAX:
            return error_response("验证请求过于频繁，请稍后再试", status_code=429)
        challenge_id, question, answer = make_challenge()
        self._purge_challenges[challenge_id] = (answer, now + CHALLENGE_TTL)
        return json_response({"challenge_id": challenge_id, "question": question})

    async def api_purge(self):
        """校验加减法答案后清空 chat_history 与 image_records 两张表的全部数据。"""
        payload = await request.json(default={})
        challenge_id = payload.get("challenge_id") or ""
        # 一次性消费：无论对错都弹出，防止对两位数答案暴力穷举
        entry = self._purge_challenges.pop(challenge_id, None)
        if entry is None:
            return error_response("验证已失效，请重新打开弹窗再试", status_code=400)

        expected, expire_at = entry
        if time.monotonic() > expire_at:
            return error_response("验证已过期，请重新打开弹窗再试", status_code=400)

        try:
            answer = int(str(payload.get("answer", "")).strip())
        except (ValueError, TypeError):
            return error_response("答案不正确，已取消清空", status_code=400)
        if answer != expected:
            return error_response("答案不正确，已取消清空", status_code=400)

        result = await self.mysql_mgr.purge_all()
        if not result.get("success"):
            return error_response("清空数据失败，请检查数据库连接", status_code=500)

        logger.warning(
            f"[HistorySave] Web 后台执行清空所有数据"
            f"{' (TRUNCATE)' if result.get('truncated') else ' (DELETE)'}: "
            f"删除 {result['deleted_messages']} 条消息、"
            f"{result['deleted_images']} 条图片"
        )
        return json_response(result)
