"""Web API 后端。

负责注册和处理所有 Web 管理后台的 API 请求。
"""

import asyncio
import json
import random
import time
import uuid
from dataclasses import fields, is_dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from astrbot.api import logger
from astrbot.api.star import Context
from astrbot.api.web import error_response, file_response, json_response, request
from astrbot.core.utils.io import save_temp_img

from .cleaner import ImageCleaner
from .db_config import ConfigManager
from .db_mysql import MySQLManager
from .stats.models import StatsQuery, StatsTimeRange

if TYPE_CHECKING:
    from .profile.service import ProfileService
    from .profile.storage import ProfileStorage
    from .profile.t2i_render import ProfileT2IRenderer
    from .stats.service import StatsService
    from .summary.storage import SummaryStorage
    from .summary.t2i_render import T2IRenderer

PLUGIN_NAME = "astrbot_plugin_group_history_save_mysql"

CHALLENGE_TTL = 300  # 清空验证题目有效期（秒）
CHALLENGE_MAX = 1000  # 同时存留的验证题目上限，超过拒绝新建

# 人物分析 Web 触发的内部超时兜底（秒）：run_analysis 含 LLM 长耗时，
# 超时后返回结构化错误而非挂死请求（run_analysis 自身绝不抛异常，此为最外层护栏）
PROFILE_ANALYZE_TIMEOUT = 180

# 数据分析（v0.5.0）：自定义日期区间最长跨度（含首尾自然日计，
# 与 PRD §6 及 stats/parser.py 的区间校验口径一致），超限返回 400
STATS_MAX_SPAN_DAYS = 366

# 数据分析（v0.5.0）：「全部」时间预设的起点哨兵值（与 stats/parser.py
# _ALL_TIME_START 同值）；前端「全部」预设按契约发 start=2000-01-01，
# 跨度校验对其豁免，与指令侧「全部」不经跨度校验的口径对齐
STATS_ALL_TIME_START = date(2000, 1, 1)

# 数据分析（v0.5.0）：日期参数格式（严格四位年两位月日，strptime 解析后回环校验形状）
STATS_DATE_FORMAT = "%Y-%m-%d"


def make_challenge() -> tuple[str, str, int]:
    """生成一道随机加减法验证题。

    Returns:
        tuple: (challenge_id, question, answer)，如 ("ab12...", "12 + 34 = ?", 46)
    """
    a = random.randint(1, 99)
    b = random.randint(1, 99)
    if random.random() < 0.5:
        question = f"{a} + {b} = ?"
        answer = a + b
    else:
        # 减法保证结果非负
        if a < b:
            a, b = b, a
        question = f"{a} - {b} = ?"
        answer = a - b
    challenge_id = uuid.uuid4().hex
    return challenge_id, question, answer


def _to_jsonable(obj):
    """递归将对象转为 JSON 安全结构。

    - dataclass → dict（逐字段递归）
    - datetime → ISO 8601 字符串
    - tuple/list → list（逐项递归）
    - dict → dict（值递归，键字符串化）
    - None / 基本类型（str/int/float/bool）原样返回
    - 其余未知类型兜底 str()，保证结果恒可 json.dumps
    """
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, datetime):
        return obj.isoformat()
    if is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _to_jsonable(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, dict):
        return {str(key): _to_jsonable(value) for key, value in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(item) for item in obj]
    return str(obj)


def _profile_result_to_dict(result) -> dict:
    """将 ProfileResult 序列化为 JSON 安全 dict（None 安全）。

    analyze 与 history detail 端点共用，供前端直接渲染。result 为 None
    （理论不应发生，run_analysis 失败也返回降级结果）时返回空 dict。
    """
    if result is None:
        return {}
    data = _to_jsonable(result)
    return data if isinstance(data, dict) else {"value": data}


def _stats_value_to_jsonable(obj):
    """递归将数据分析统计对象转为 JSON 安全结构（v0.5.0）。

    与 _to_jsonable 同范式，区别在 datetime 按面板展示约定序列化为
    "YYYY-MM-DD HH:MM:SS"（而非 ISO 8601）：

    - dataclass → dict（逐字段递归，防御式 getattr）
    - datetime → "YYYY-MM-DD HH:MM:SS" 字符串
    - tuple/list → list（逐项递归）
    - dict → dict（值递归，键字符串化）
    - None / 基本类型（str/int/float/bool）原样返回
    - 其余未知类型兜底 str()，保证结果恒可 json.dumps
    """
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, datetime):
        return obj.strftime("%Y-%m-%d %H:%M:%S")
    if is_dataclass(obj) and not isinstance(obj, type):
        return {
            f.name: _stats_value_to_jsonable(getattr(obj, f.name, None))
            for f in fields(obj)
        }
    if isinstance(obj, dict):
        return {str(key): _stats_value_to_jsonable(value) for key, value in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_stats_value_to_jsonable(item) for item in obj]
    return str(obj)


def _stats_data_to_dict(data) -> dict:
    """将 StatsData 序列化为 JSON 安全 dict（None 安全，v0.5.0）。

    stats/data 端点使用，供前端直接渲染。data 为 None（理论不应发生，
    build_stats 失败以异常表达）时返回空 dict。
    """
    if data is None:
        return {}
    result = _stats_value_to_jsonable(data)
    return result if isinstance(result, dict) else {"value": result}


class WebAPI:
    """Web 管理后台 API 处理器。"""

    def __init__(
        self,
        context: Context,
        mysql_mgr: MySQLManager,
        config_mgr: ConfigManager,
        cleaner: ImageCleaner,
        summary_storage: "SummaryStorage | None" = None,
        summary_renderer: "T2IRenderer | None" = None,
        profile_service: "ProfileService | None" = None,
        profile_storage: "ProfileStorage | None" = None,
        profile_renderer: "ProfileT2IRenderer | None" = None,
        stats_service: "StatsService | None" = None,
    ):
        self.context = context
        self.mysql_mgr = mysql_mgr
        self.config_mgr = config_mgr
        self.cleaner = cleaner
        # v0.3 总结功能存储层，由 main.py 注入（模块 K）；
        # 未注入时总结历史相关端点返回 503
        self.summary_storage = summary_storage
        # v0.4.2 总结导出图片用 T2I 渲染器，由 main.py 注入（复用
        # SummaryService.renderer）；未注入时导出端点返回 503
        self.summary_renderer = summary_renderer
        # v0.4.0 人物分析编排层与存储层，由 main.py 注入（模块 K）；
        # 未注入时 analyze / history 相关端点返回 503
        self.profile_service = profile_service
        self.profile_storage = profile_storage
        # v0.4.2 人物分析导出图片用 T2I 渲染器，由 main.py 注入（复用
        # ProfileService.renderer）；未注入时导出端点返回 503
        self.profile_renderer = profile_renderer
        # v0.5.0 数据分析编排服务，由 main.py 注入（模块 M）；
        # 未注入时 stats/data 端点返回 503（设置类端点不依赖，正常可用）
        self.stats_service = stats_service
        self._purge_challenges: dict[str, tuple[int, float]] = {}
        self._register_routes()

    def _register_routes(self):
        """注册所有 Web API 路由。"""
        routes = [
            (f"/{PLUGIN_NAME}/status", self.api_status, ["GET"], "数据库状态"),
            (f"/{PLUGIN_NAME}/groups", self.api_get_groups, ["GET"], "获取群列表"),
            (
                f"/{PLUGIN_NAME}/groups/toggle",
                self.api_toggle_group,
                ["POST"],
                "切换群状态",
            ),
            (f"/{PLUGIN_NAME}/groups/add", self.api_add_group, ["POST"], "添加群"),
            (
                f"/{PLUGIN_NAME}/groups/remove",
                self.api_remove_group,
                ["POST"],
                "移除群",
            ),
            (f"/{PLUGIN_NAME}/settings", self.api_get_settings, ["GET"], "获取设置"),
            (
                f"/{PLUGIN_NAME}/settings/save",
                self.api_save_settings,
                ["POST"],
                "保存设置",
            ),
            (f"/{PLUGIN_NAME}/stats/daily", self.api_daily_stats, ["GET"], "每日统计"),
            (f"/{PLUGIN_NAME}/clean", self.api_clean, ["POST"], "手动清理"),
            (f"/{PLUGIN_NAME}/query", self.api_query, ["GET"], "查询聊天记录"),
            (
                f"/{PLUGIN_NAME}/purge/challenge",
                self.api_purge_challenge,
                ["GET"],
                "获取清空验证题目",
            ),
            (f"/{PLUGIN_NAME}/purge", self.api_purge, ["POST"], "清空所有数据"),
            # ---- 总结功能（v0.3） ----
            (
                f"/{PLUGIN_NAME}/summary/settings",
                self.api_summary_settings,
                ["GET"],
                "获取总结设置",
            ),
            (
                f"/{PLUGIN_NAME}/summary/settings/save",
                self.api_summary_settings_save,
                ["POST"],
                "保存总结设置",
            ),
            (
                f"/{PLUGIN_NAME}/summary/settings/reset",
                self.api_summary_settings_reset,
                ["POST"],
                "重置总结设置",
            ),
            (
                f"/{PLUGIN_NAME}/summary/providers",
                self.api_summary_providers,
                ["GET"],
                "LLM 提供商列表",
            ),
            (
                f"/{PLUGIN_NAME}/summary/ignore/groups",
                self.api_summary_ignore_groups,
                ["GET"],
                "忽略名单群列表",
            ),
            (
                f"/{PLUGIN_NAME}/summary/ignore",
                self.api_summary_ignore_list,
                ["GET"],
                "群忽略名单",
            ),
            (
                f"/{PLUGIN_NAME}/summary/ignore/add",
                self.api_summary_ignore_add,
                ["POST"],
                "添加忽略成员",
            ),
            (
                f"/{PLUGIN_NAME}/summary/ignore/remove",
                self.api_summary_ignore_remove,
                ["POST"],
                "移除忽略成员",
            ),
            (
                f"/{PLUGIN_NAME}/summary/history",
                self.api_summary_history,
                ["GET"],
                "历史总结列表",
            ),
            (
                f"/{PLUGIN_NAME}/summary/history/detail",
                self.api_summary_history_detail,
                ["GET"],
                "历史总结详情",
            ),
            (
                f"/{PLUGIN_NAME}/summary/history/export",
                self.api_summary_history_export,
                ["GET"],
                "历史总结导出图片",
            ),
            # ---- 人物分析功能（v0.4.0） ----
            (
                f"/{PLUGIN_NAME}/profile/settings",
                self.api_profile_settings,
                ["GET"],
                "获取人物分析设置",
            ),
            (
                f"/{PLUGIN_NAME}/profile/settings",
                self.api_profile_settings_save,
                ["POST"],
                "保存人物分析设置",
            ),
            (
                f"/{PLUGIN_NAME}/profile/settings/reset",
                self.api_profile_settings_reset,
                ["POST"],
                "重置人物分析设置",
            ),
            (
                f"/{PLUGIN_NAME}/profile/providers",
                self.api_profile_providers,
                ["GET"],
                "人物分析 LLM 提供商列表",
            ),
            (
                f"/{PLUGIN_NAME}/profile/groups",
                self.api_profile_groups,
                ["GET"],
                "人物分析可选群列表",
            ),
            (
                f"/{PLUGIN_NAME}/profile/analyze",
                self.api_profile_analyze,
                ["POST"],
                "触发人物分析",
            ),
            (
                f"/{PLUGIN_NAME}/profile/history",
                self.api_profile_history,
                ["GET"],
                "历史人物分析列表",
            ),
            (
                f"/{PLUGIN_NAME}/profile/history/detail",
                self.api_profile_history_detail,
                ["GET"],
                "历史人物分析详情",
            ),
            (
                f"/{PLUGIN_NAME}/profile/history/export",
                self.api_profile_history_export,
                ["GET"],
                "历史人物分析导出图片",
            ),
            (
                f"/{PLUGIN_NAME}/profile/history",
                self.api_profile_history_delete,
                ["DELETE", "POST"],
                "删除历史人物分析",
            ),
            # ---- 数据分析功能（v0.5.0） ----
            (
                f"/{PLUGIN_NAME}/stats/data",
                self.api_stats_data,
                ["GET"],
                "数据分析统计数据",
            ),
            (
                f"/{PLUGIN_NAME}/stats/groups",
                self.api_stats_groups,
                ["GET"],
                "数据分析群列表",
            ),
            (
                f"/{PLUGIN_NAME}/stats/settings",
                self.api_stats_settings,
                ["GET"],
                "获取数据分析设置",
            ),
            (
                f"/{PLUGIN_NAME}/stats/settings/save",
                self.api_stats_settings_save,
                ["POST"],
                "保存数据分析设置",
            ),
            (
                f"/{PLUGIN_NAME}/stats/settings/reset",
                self.api_stats_settings_reset,
                ["POST"],
                "重置数据分析设置",
            ),
            (
                f"/{PLUGIN_NAME}/stats/push/toggle",
                self.api_stats_push_toggle,
                ["POST"],
                "切换群推送开关",
            ),
        ]
        for route, handler, methods, desc in routes:
            self.context.register_web_api(route, handler, methods, desc)
        logger.info(f"[HistorySave] 已注册 {len(routes)} 个 Web API 路由")

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

        # 全部校验通过后写入
        if new_retention is not None:
            await self.config_mgr.set_setting("image_retention_days", new_retention)
        if new_all_mode is not None:
            await self.config_mgr.set_setting("all_mode", new_all_mode)

        settings = await self.config_mgr.get_all_settings()
        return json_response({"saved": True, "settings": settings})

    async def api_daily_stats(self):
        """获取每日存储统计。"""
        days_str = request.query.get("days") or "7"
        try:
            days = int(days_str)
        except (ValueError, TypeError):
            days = 7
        if days < 1:
            days = 7
        if days > 90:
            days = 90
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

    async def api_query(self):
        """查询聊天记录（支持多条件过滤和分页）。

        每条记录附带 ``reply_message`` 关联信息：本条回复目标消息
        （{"sender_id", "sender_name", "content"}，取不到为 None）。
        说明：at_list（被 @ 的 QQ）仅作记录存储，@ ID 无法可靠反查
        对应消息（@ 了某人不代表其某条消息与本次互动相关），故不作为
        关联上下文展示（见 v0.4.0 PRD 备注）。
        """
        group_id = request.query.get("group_id")
        sender_id = request.query.get("sender_id")
        time_start = request.query.get("time_start")
        time_end = request.query.get("time_end")
        keyword = (request.query.get("keyword") or "").strip() or None
        # 显式转换，避免框架 type=int 行为不一致
        page_str = request.query.get("page") or "1"
        try:
            page = int(page_str)
        except (ValueError, TypeError):
            page = 1
        page_size_str = request.query.get("page_size") or "50"
        try:
            page_size = int(page_size_str)
        except (ValueError, TypeError):
            page_size = 50

        # 参数校验
        if page < 1:
            page = 1
        if page_size < 1:
            page_size = 50
        if page_size > 200:
            page_size = 200

        # 群号/QQ 号为文本字段，空字符串视同未提供
        group_id = group_id or None
        sender_id = sender_id or None

        result = await self.mysql_mgr.query_messages(
            group_id=group_id,
            sender_id=sender_id,
            time_start=time_start,
            time_end=time_end,
            keyword=keyword,
            page=page,
            page_size=page_size,
        )
        await self._enrich_query_reply(result)
        return json_response(result)

    async def _enrich_query_reply(self, result: dict) -> None:
        """为查询结果批量补充回复目标消息内容。

        仅按 reply_id（消息 ID）反查——reply_id 是唯一可靠的反查锚点；
        任一关联缺失或异常仅跳过该条，不阻断整体结果。
        """
        records = result.get("records") or []
        if not records:
            return

        reply_ids = [(rec.get("reply_id") or "").strip() for rec in records]
        reply_ids = [rid for rid in reply_ids if rid]

        reply_map: dict[str, dict] = {}
        if reply_ids:
            for row in await self.mysql_mgr.get_messages_by_ids(reply_ids):
                mid = str(row.get("message_id") or "")
                if mid and mid not in reply_map:
                    reply_map[mid] = row

        for rec in records:
            rid = (rec.get("reply_id") or "").strip()
            rec["reply_message"] = reply_map.get(rid)

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

    # ========== 总结功能端点（v0.3） ==========

    async def api_summary_settings(self):
        """获取全部总结配置（附默认值与类型声明，供前端渲染表单与说明）。"""
        settings = await self.config_mgr.get_all_summary_settings()
        # 类型对象序列化为类型名（如 "bool"/"int"/"float"/"list"/"str"）
        types = {key: t.__name__ for key, t in ConfigManager.SUMMARY_TYPES.items()}
        return json_response(
            {
                "settings": settings,
                "defaults": ConfigManager.SUMMARY_DEFAULTS,
                "types": types,
            }
        )

    async def api_summary_settings_save(self):
        """批量保存总结配置。

        先校验后写入：所有传入键值全部通过 SUMMARY_TYPES 声明类型校验后才
        逐项写入，避免靠后字段非法时靠前字段已被写入的部分生效问题
        （沿用 api_save_settings 模式）。

        值归一化：bool→"true"/"false"；list/dict→JSON 序列化；其余→str()。
        """
        payload = await request.json(default={})
        incoming = payload.get("settings")
        if not isinstance(incoming, dict):
            return error_response("settings 必须为键值对象", status_code=400)

        # ① 未知键检查 + ② 值归一化为存储字符串
        normalized: dict[str, str] = {}
        for key, value in incoming.items():
            if key not in ConfigManager.SUMMARY_TYPES:
                return error_response(f"未知的配置键: {key}", status_code=400)
            if isinstance(value, bool):
                str_value = "true" if value else "false"
            elif isinstance(value, (list, dict)):
                str_value = json.dumps(value, ensure_ascii=False)
            else:
                str_value = str(value)
            normalized[key] = str_value

        # ③ 逐个按声明类型校验，任一失败整体 400、不写入任何项
        for key, str_value in normalized.items():
            try:
                ConfigManager._convert_summary_value(
                    str_value, ConfigManager.SUMMARY_TYPES[key]
                )
            except (ValueError, TypeError) as e:
                return error_response(f"配置项 {key} 的值非法: {e}", status_code=400)

        # ④ 全部校验通过后批量写入
        try:
            for key, str_value in normalized.items():
                ok = await self.config_mgr.set_summary_setting(key, str_value)
                if not ok:
                    return error_response("保存总结配置失败", status_code=500)
            settings = await self.config_mgr.get_all_summary_settings()
        except Exception:
            logger.error("[HistorySummary] 保存总结配置失败", exc_info=True)
            return error_response("保存总结配置失败", status_code=500)

        return json_response({"saved": True, "settings": settings})

    async def api_summary_settings_reset(self):
        """恢复总结配置默认值。

        keys 字段缺失或为 null → 全部重置（传 None）；
        提供列表（含空列表）→ 原样透传（空列表 = 不重置任何项）。
        """
        payload = await request.json(default={})
        keys = payload.get("keys")
        if keys is not None and not isinstance(keys, list):
            return error_response("keys 必须为配置键列表", status_code=400)

        try:
            settings = await self.config_mgr.reset_summary_settings(keys)
        except Exception:
            logger.error("[HistorySummary] 重置总结配置失败", exc_info=True)
            return error_response("重置总结配置失败", status_code=500)
        return json_response({"reset": True, "settings": settings})

    async def api_summary_providers(self):
        """获取可用 LLM（Chat Completion）提供商列表，供前端下拉选择。

        经 ``Context.get_all_providers()`` 获取（框架仅返回 CHAT_COMPLETION
        类型），从 provider_config 取 id/name。获取失败或无可用提供商时
        返回空列表并记 warning，不抛 500。
        """
        providers: list[dict] = []
        try:
            for prov in self.context.get_all_providers():
                cfg = getattr(prov, "provider_config", None) or {}
                prov_id = str(cfg.get("id") or "").strip()
                if not prov_id:
                    continue
                prov_name = str(cfg.get("name") or "").strip() or prov_id
                providers.append({"id": prov_id, "name": prov_name})
        except Exception:
            logger.warning("[HistorySummary] 获取 LLM 提供商列表失败", exc_info=True)
            providers = []
        return json_response({"providers": providers})

    async def api_summary_ignore_groups(self):
        """获取存在总结忽略记录的群列表。"""
        try:
            groups = await self.config_mgr.list_ignore_groups()
        except Exception:
            logger.error("[HistorySummary] 获取忽略群列表失败", exc_info=True)
            return error_response("获取忽略群列表失败", status_code=500)
        return json_response({"groups": groups})

    async def api_summary_ignore_list(self):
        """获取指定群的总结忽略名单。"""
        group_id = (request.query.get("group_id") or "").strip()
        if not group_id:
            return error_response("缺少 group_id 参数", status_code=400)
        if not group_id.isdigit():
            return error_response("group_id 必须为纯数字", status_code=400)

        try:
            senders = await self.config_mgr.get_ignore_senders(group_id)
        except Exception:
            logger.error("[HistorySummary] 获取忽略名单失败", exc_info=True)
            return error_response("获取忽略名单失败", status_code=500)
        return json_response({"group_id": group_id, "senders": senders})

    async def api_summary_ignore_add(self):
        """将成员加入群的总结忽略名单。"""
        payload = await request.json(default={})
        group_id = str(payload.get("group_id") or "").strip()
        sender_id = str(payload.get("sender_id") or "").strip()
        if not group_id or not sender_id:
            return error_response("缺少 group_id 或 sender_id 参数", status_code=400)
        if not group_id.isdigit():
            return error_response("group_id 必须为纯数字", status_code=400)

        try:
            added = await self.config_mgr.add_ignore_sender(group_id, sender_id)
        except Exception:
            logger.error("[HistorySummary] 添加忽略成员失败", exc_info=True)
            return error_response("添加忽略成员失败", status_code=500)
        if not added:
            return error_response("该发送者已在忽略名单中", status_code=409)
        return json_response({"added": True})

    async def api_summary_ignore_remove(self):
        """将成员移出群的总结忽略名单。"""
        payload = await request.json(default={})
        group_id = str(payload.get("group_id") or "").strip()
        sender_id = str(payload.get("sender_id") or "").strip()
        if not group_id or not sender_id:
            return error_response("缺少 group_id 或 sender_id 参数", status_code=400)
        if not group_id.isdigit():
            return error_response("group_id 必须为纯数字", status_code=400)

        try:
            removed = await self.config_mgr.remove_ignore_sender(group_id, sender_id)
        except Exception:
            logger.error("[HistorySummary] 移除忽略成员失败", exc_info=True)
            return error_response("移除忽略成员失败", status_code=500)
        if not removed:
            return error_response("该发送者不在忽略名单中", status_code=404)
        return json_response({"removed": True})

    async def api_summary_history(self):
        """分页列出历史总结记录（group_id 可选，缺省为全部群）。"""
        if self.summary_storage is None:
            return error_response("总结模块尚未初始化", status_code=503)

        # 群号为文本字段，空字符串视同未提供（None = 全部群）
        group_id = (request.query.get("group_id") or "").strip() or None
        # 显式转换，避免框架 type=int 行为不一致（参考 api_query 风格）
        page_str = request.query.get("page") or "1"
        try:
            page = int(page_str)
        except (ValueError, TypeError):
            page = 1
        page_size_str = request.query.get("page_size") or "20"
        try:
            page_size = int(page_size_str)
        except (ValueError, TypeError):
            page_size = 20

        # 边界归一
        if page < 1:
            page = 1
        if page_size < 1:
            page_size = 20
        if page_size > 200:
            page_size = 200

        try:
            result = await self.summary_storage.list_by_group(group_id, page, page_size)
        except ValueError as e:
            # group_id 非纯数字等入参错误
            return error_response(str(e), status_code=400)
        except Exception:
            logger.error("[HistorySummary] 获取历史总结列表失败", exc_info=True)
            return error_response("获取历史总结列表失败", status_code=500)
        return json_response(result)

    async def api_summary_history_detail(self):
        """读取单条历史总结详情。

        群号与文件名的白名单校验（防路径穿越）已在 SummaryStorage 层完成，
        非法或不存在统一返回 None → 404。
        """
        if self.summary_storage is None:
            return error_response("总结模块尚未初始化", status_code=503)

        group_id = (request.query.get("group_id") or "").strip()
        filename = (request.query.get("filename") or "").strip()
        if not group_id or not filename:
            return error_response("缺少 group_id 或 filename 参数", status_code=400)

        try:
            detail = await self.summary_storage.read(group_id, filename)
        except Exception:
            logger.error("[HistorySummary] 读取总结详情失败", exc_info=True)
            return error_response("读取总结详情失败", status_code=500)
        if detail is None:
            return error_response("总结记录不存在", status_code=404)
        return json_response({"detail": detail})

    async def api_summary_history_export(self):
        """导出历史总结为图片（后端 T2I 流水线渲染，与聊天端图片同模板同配置）。

        T2I 渲染耗时可能达数十秒，由前端 bridge.download 触发浏览器下载；
        渲染失败（模板缺失 / 两轮渲染全败等）统一 502，前端 toast 明确报错。
        """
        if self.summary_storage is None:
            return error_response("总结模块尚未初始化", status_code=503)

        group_id = (request.query.get("group_id") or "").strip()
        filename = (request.query.get("filename") or "").strip()
        if not group_id or not filename:
            return error_response("缺少 group_id 或 filename 参数", status_code=400)

        try:
            detail = await self.summary_storage.read(group_id, filename)
        except Exception:
            logger.error("[HistorySummary] 导出前读取总结详情失败", exc_info=True)
            return error_response("读取总结详情失败", status_code=500)
        if detail is None:
            return error_response("总结记录不存在", status_code=404)

        renderer = self.summary_renderer
        if renderer is None:
            return error_response("总结模块尚未初始化", status_code=503)
        try:
            img_bytes = await renderer.render_from_dict(detail)
        except ValueError as e:
            logger.warning(f"[HistorySummary] 导出图片渲染失败: {e}")
            return error_response("渲染失败，请稍后重试", status_code=502)
        except Exception:
            logger.error("[HistorySummary] 导出图片渲染异常", exc_info=True)
            return error_response("渲染失败，请稍后重试", status_code=502)

        try:
            path = await asyncio.to_thread(save_temp_img, img_bytes)
        except Exception:
            logger.error("[HistorySummary] 导出图片落盘失败", exc_info=True)
            return error_response("导出图片落盘失败", status_code=500)
        # filename 可能带目录前缀（如 group_123/xxx.json），用 stem 取纯文件名再拼
        # .png，避免下载名含路径分隔符与 .json.png 双后缀
        return file_response(path, filename=f"{Path(filename).stem}.png")

    # ========== 人物分析端点（v0.4.0） ==========

    async def api_profile_settings(self):
        """获取全部人物分析配置（附默认值与类型声明，供前端渲染表单）。

        settings 为逐项类型化后的完整 19 项；defaults/types 原样透出
        ``PROFILE_DEFAULTS`` / ``PROFILE_TYPES``（类型已是 "bool"/"int"/... 字符串）。
        """
        try:
            settings = await self.config_mgr.get_all_profile_settings()
        except Exception:
            logger.error("[Profile] 获取人物分析配置失败", exc_info=True)
            return error_response("获取人物分析配置失败", status_code=500)
        return json_response(
            {
                "settings": settings,
                "defaults": ConfigManager.PROFILE_DEFAULTS,
                "types": ConfigManager.PROFILE_TYPES,
            }
        )

    async def api_profile_settings_save(self):
        """批量保存人物分析配置。

        先校验后写入：所有传入键值全部通过 PROFILE_TYPES 声明类型校验后才
        交由 ``save_profile_settings`` 写入，任一非法整体 400、不写入任何项
        （沿用 api_summary_settings_save 范式）；``save_profile_settings``
        返回 False（校验未通过或数据库写入异常）→ 500。
        值归一化：bool→"true"/"false"；list/dict→JSON 序列化；其余→str()。
        """
        payload = await request.json(default={})
        incoming = payload.get("settings")
        if not isinstance(incoming, dict):
            return error_response("settings 必须为键值对象", status_code=400)

        # ① 未知键检查 + ② 值归一化为存储字符串
        normalized: dict[str, str] = {}
        for key, value in incoming.items():
            if key not in ConfigManager.PROFILE_TYPES:
                return error_response(f"未知的配置键: {key}", status_code=400)
            if isinstance(value, bool):
                str_value = "true" if value else "false"
            elif isinstance(value, (list, dict)):
                str_value = json.dumps(value, ensure_ascii=False)
            else:
                str_value = str(value)
            normalized[key] = str_value

        # ③ 逐个按声明类型校验，任一失败整体 400、不写入任何项
        #    （json.JSONDecodeError 为 ValueError 子类，已被覆盖）
        for key, str_value in normalized.items():
            try:
                ConfigManager._convert_profile_value(
                    str_value, ConfigManager.PROFILE_TYPES[key]
                )
            except (ValueError, TypeError) as e:
                return error_response(f"配置项 {key} 的值非法: {e}", status_code=400)

        # ④ 全部校验通过后写入
        try:
            ok = await self.config_mgr.save_profile_settings(normalized)
            if not ok:
                return error_response("保存设置失败（数据库写入异常）", status_code=500)
            settings = await self.config_mgr.get_all_profile_settings()
        except Exception:
            logger.error("[Profile] 保存人物分析配置失败", exc_info=True)
            return error_response("保存人物分析配置失败", status_code=500)

        return json_response({"saved": True, "settings": settings})

    async def api_profile_settings_reset(self):
        """恢复人物分析配置默认值（全部 19 项，``reset_profile_settings`` 无入参）。"""
        try:
            await self.config_mgr.reset_profile_settings()
            settings = await self.config_mgr.get_all_profile_settings()
        except Exception:
            logger.error("[Profile] 重置人物分析配置失败", exc_info=True)
            return error_response("重置人物分析配置失败", status_code=500)
        return json_response({"reset": True, "settings": settings})

    async def api_profile_providers(self):
        """获取可用 LLM（Chat Completion）提供商列表，供前端下拉选择。

        经 ``Context.get_all_providers()`` 获取（框架仅返回 CHAT_COMPLETION
        类型），从 provider_config 取 id/name。获取失败或无可用提供商时
        返回空列表并记 warning，不抛 500（与 summary providers 同源同范式）。
        """
        providers: list[dict] = []
        try:
            for prov in self.context.get_all_providers():
                cfg = getattr(prov, "provider_config", None) or {}
                prov_id = str(cfg.get("id") or "").strip()
                if not prov_id:
                    continue
                prov_name = str(cfg.get("name") or "").strip() or prov_id
                providers.append({"id": prov_id, "name": prov_name})
        except Exception:
            logger.warning("[Profile] 获取 LLM 提供商列表失败", exc_info=True)
            providers = []
        return json_response({"providers": providers})

    async def api_profile_groups(self):
        """获取已保存的群列表，供前端「发起分析」下拉选群。

        复用现有群白名单数据源 ``config_mgr.get_groups()``（与 api_get_groups
        同源），返回群对象列表（含 group_id/enabled 等字段）。
        """
        try:
            groups = await self.config_mgr.get_groups()
        except Exception:
            logger.error("[Profile] 获取群列表失败", exc_info=True)
            return error_response("获取群列表失败", status_code=500)
        return json_response({"groups": groups})

    async def api_profile_analyze(self):
        """触发人物分析（跨群分析唯一入口）。

        body ``{sender_id, group_id}``：group_id 为空或 ``"all"`` → 全局分析
        （``event=None``，不经 OneBot）。服务端以 ``asyncio.wait_for`` 包裹
        ``run_analysis`` 做内部超时兜底（``PROFILE_ANALYZE_TIMEOUT``），超时
        返回 504 结构化错误。成功返回序列化的 ProfileResult（顶层 ``result``
        键避撞桥接解包）。``run_analysis`` 自身绝不抛异常，失败返回降级结果。
        """
        if self.profile_service is None:
            return error_response("人物分析模块尚未初始化", status_code=503)

        payload = await request.json(default={})
        sender_id = str(payload.get("sender_id") or "").strip()
        group_id = str(payload.get("group_id") or "").strip()
        if not sender_id:
            return error_response("缺少 sender_id 参数", status_code=400)
        if not sender_id.isdigit():
            return error_response("sender_id 必须为纯数字 QQ 号", status_code=400)

        try:
            result = await asyncio.wait_for(
                self.profile_service.run_analysis(sender_id, group_id, event=None),
                timeout=PROFILE_ANALYZE_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.error(
                f"[Profile] Web 触发分析超时（>{PROFILE_ANALYZE_TIMEOUT}s）："
                f"sender={sender_id} group={group_id or 'all'}"
            )
            return error_response(
                f"分析超时（超过 {PROFILE_ANALYZE_TIMEOUT} 秒），请稍后重试",
                status_code=504,
            )
        except Exception:
            logger.error("[Profile] Web 触发分析异常", exc_info=True)
            return error_response("分析失败，请稍后重试", status_code=500)

        return json_response({"result": _profile_result_to_dict(result)})

    async def api_profile_history(self):
        """分页列出历史人物分析记录。

        返回 ``{"total", "profiles", "page", "page_size"}``（顶层用 profiles
        专名避撞桥接解包）。分页参数显式转换并做边界归一（参考 summary history）。
        """
        if self.profile_storage is None:
            return error_response("人物分析模块尚未初始化", status_code=503)

        page_str = request.query.get("page") or "1"
        try:
            page = int(page_str)
        except (ValueError, TypeError):
            page = 1
        page_size_str = request.query.get("page_size") or "20"
        try:
            page_size = int(page_size_str)
        except (ValueError, TypeError):
            page_size = 20

        # 边界归一
        if page < 1:
            page = 1
        if page_size < 1:
            page_size = 20
        if page_size > 200:
            page_size = 200

        try:
            result = await self.profile_storage.list_profiles(page, page_size)
        except Exception:
            logger.error("[Profile] 获取历史分析列表失败", exc_info=True)
            return error_response("获取历史分析列表失败", status_code=500)
        return json_response(result)

    async def api_profile_history_detail(self):
        """读取单条历史人物分析详情。

        filename 为 list_profiles 返回的相对名（``<scope目录>/<文件名>``）；
        路径穿越白名单校验已在 ProfileStorage 层完成，非法或不存在统一
        返回 None → 404。
        """
        if self.profile_storage is None:
            return error_response("人物分析模块尚未初始化", status_code=503)

        filename = (request.query.get("filename") or "").strip()
        if not filename:
            return error_response("缺少 filename 参数", status_code=400)

        try:
            detail = await self.profile_storage.read(filename)
        except Exception:
            logger.error("[Profile] 读取分析详情失败", exc_info=True)
            return error_response("读取分析详情失败", status_code=500)
        if detail is None:
            return error_response("分析记录不存在", status_code=404)
        return json_response({"detail": detail})

    async def api_profile_history_export(self):
        """导出历史人物分析为图片（后端 T2I 流水线渲染，与聊天端图片同模板同配置）。

        filename 为 list_profiles 返回的相对名（``<scope目录>/<文件名>``）；
        渲染失败（模板缺失 / 两轮渲染全败等）统一 502，前端 toast 明确报错。
        """
        if self.profile_storage is None:
            return error_response("人物分析模块尚未初始化", status_code=503)

        filename = (request.query.get("filename") or "").strip()
        if not filename:
            return error_response("缺少 filename 参数", status_code=400)

        try:
            detail = await self.profile_storage.read(filename)
        except Exception:
            logger.error("[Profile] 导出前读取分析详情失败", exc_info=True)
            return error_response("读取分析详情失败", status_code=500)
        if detail is None:
            return error_response("分析记录不存在", status_code=404)

        renderer = self.profile_renderer
        if renderer is None:
            return error_response("人物分析模块尚未初始化", status_code=503)
        try:
            img_bytes = await renderer.render_from_dict(detail)
        except ValueError as e:
            logger.warning(f"[Profile] 导出图片渲染失败: {e}")
            return error_response("渲染失败，请稍后重试", status_code=502)
        except Exception:
            logger.error("[Profile] 导出图片渲染异常", exc_info=True)
            return error_response("渲染失败，请稍后重试", status_code=502)

        try:
            path = await asyncio.to_thread(save_temp_img, img_bytes)
        except Exception:
            logger.error("[Profile] 导出图片落盘失败", exc_info=True)
            return error_response("导出图片落盘失败", status_code=500)
        # filename 为 <scope目录>/<文件名> 相对名，用 stem 取纯文件名再拼 .png，
        # 避免下载名含路径分隔符与 .json.png 双后缀
        return file_response(path, filename=f"{Path(filename).stem}.png")

    async def api_profile_history_delete(self):
        """删除单条历史人物分析记录。

        filename 兼容 query 与 body 两种传参（DELETE 语义下前端多走 query）；
        路径穿越防护在 ProfileStorage 层完成，不存在/删除失败 → 404。
        """
        if self.profile_storage is None:
            return error_response("人物分析模块尚未初始化", status_code=503)

        filename = (request.query.get("filename") or "").strip()
        if not filename:
            payload = await request.json(default={})
            filename = str(payload.get("filename") or "").strip()
        if not filename:
            return error_response("缺少 filename 参数", status_code=400)

        try:
            deleted = await self.profile_storage.delete(filename)
        except Exception:
            logger.error("[Profile] 删除分析记录失败", exc_info=True)
            return error_response("删除分析记录失败", status_code=500)
        if not deleted:
            return error_response("分析记录不存在或删除失败", status_code=404)
        return json_response({"deleted": True})

    # ========== 数据分析端点（v0.5.0） ==========

    async def api_stats_data(self):
        """获取数据分析统计数据（实时 SQL 聚合，三端共用 build_stats 入口）。

        查询参数：
        - group_id：群号，空/缺省 = 全部群汇总
        - sender_id：QQ 号，可选，个人维度查询
        - start/end：``YYYY-MM-DD``，均可省。均缺省默认近 7 天
          （start=今日-6 天、end=今日）；仅提供其一时另一端取同日（单日窗口）。
          显式传入时 end 同样含当日——窗口统一转为左闭右开
          ``[start 当日 00:00, end 次日 00:00)``。

        校验：日期非法/形状不符、end 早于 start、跨度 >366 天（含首尾自然日，
        「全部」预设 start=2000-01-01 豁免）均 400；stats_service 未注入 503；
        build_stats 抛异常（契约上为含友好文案的 StatsBuildError）→ 500 透出文案。
        成功返回 StatsData 完整 JSON
        （顶层键 ``stats`` 避撞桥接解包），datetime 序列化 "YYYY-MM-DD HH:MM:SS"，
        top_n 读 stats_top_n 配置并夹住 1–50。
        """
        if self.stats_service is None:
            return error_response("数据分析模块尚未初始化", status_code=503)

        group_id = (request.query.get("group_id") or "").strip() or None
        sender_id = (request.query.get("sender_id") or "").strip() or None
        start_str = (request.query.get("start") or "").strip()
        end_str = (request.query.get("end") or "").strip()

        today = datetime.now().date()
        if not start_str and not end_str:
            # 缺省窗口：近 7 天（今日-6 天 ~ 今日）
            start_date = today - timedelta(days=6)
            end_date = today
            label = "近7天"
        else:
            try:
                parsed_start = (
                    datetime.strptime(start_str, STATS_DATE_FORMAT)
                    if start_str
                    else None
                )
                parsed_end = (
                    datetime.strptime(end_str, STATS_DATE_FORMAT) if end_str else None
                )
            except ValueError:
                return error_response("日期格式非法，应为 YYYY-MM-DD", status_code=400)
            # strptime 容忍非补零形状（如 2026-8-1），回环比对保证严格 YYYY-MM-DD
            if start_str and start_str != parsed_start.strftime(STATS_DATE_FORMAT):
                return error_response("日期格式非法，应为 YYYY-MM-DD", status_code=400)
            if end_str and end_str != parsed_end.strftime(STATS_DATE_FORMAT):
                return error_response("日期格式非法，应为 YYYY-MM-DD", status_code=400)
            start_date = parsed_start.date() if parsed_start else None
            end_date = parsed_end.date() if parsed_end else None
            # 仅提供一端时另一端取同日（单日窗口）
            start_date = start_date or end_date
            end_date = end_date or start_date
            label = (
                start_date.strftime(STATS_DATE_FORMAT)
                if start_date == end_date
                else f"{start_date} ~ {end_date}"
            )

        if end_date < start_date:
            return error_response("结束日期不能早于开始日期", status_code=400)
        # 「全部」预设哨兵：start=2000-01-01 豁免跨度校验，label 归一为「全部」
        # （与指令侧 parser「全部」口径一致）
        if start_date == STATS_ALL_TIME_START:
            label = "全部"
        # 跨度含首尾自然日计（与 stats/parser.py 口径一致）
        elif (end_date - start_date).days + 1 > STATS_MAX_SPAN_DAYS:
            return error_response(
                f"日期区间不能超过 {STATS_MAX_SPAN_DAYS} 天", status_code=400
            )

        time_range = StatsTimeRange(
            start=datetime(start_date.year, start_date.month, start_date.day),
            # end 含当日：+1 天作为开区间上界
            end=datetime(end_date.year, end_date.month, end_date.day)
            + timedelta(days=1),
            label=label,
        )

        # 排行条数读配置（typed 读取已含非法回退默认），再按契约夹住 1–50
        try:
            top_n = int(await self.config_mgr.get_stats_setting_typed("stats_top_n"))
        except (TypeError, ValueError):
            top_n = 10
        top_n = max(1, min(50, top_n))

        query = StatsQuery(
            group_id=group_id,
            member_id=sender_id,
            time_range=time_range,
            top_n=top_n,
        )
        try:
            data = await self.stats_service.build_stats(query)
        except Exception as e:
            logger.error("[Stats] 构建数据分析统计失败", exc_info=True)
            message = str(e).strip() or "统计数据构建失败，请稍后重试"
            return error_response(message, status_code=500)
        return json_response({"stats": _stats_data_to_dict(data)})

    async def api_stats_settings(self):
        """获取数据分析设置（8 项 typed 配置）与群级推送开关列表。

        返回 ``{"settings": {...}, "push_groups": [...], "all_mode": bool}``。
        push_groups 模式感知（v0.5.1）：白名单模式以 group_config 为基准；
        all_mode 全局模式下白名单为空，改以 chat_history 有数据的群为基准
        （经 stats_service.resolve_push_groups，未注入时回落白名单语义）。
        """
        settings = await self.config_mgr.get_all_stats_settings()
        if self.stats_service is not None:
            push_groups = await self.stats_service.resolve_push_groups()
            all_mode = await self.stats_service.is_all_mode()
        else:
            push_groups = await self.config_mgr.get_push_groups()
            base = await self.config_mgr.get_all_settings()
            all_mode = base.get("all_mode", "false") == "true"
        return json_response(
            {"settings": settings, "push_groups": push_groups, "all_mode": all_mode}
        )

    async def api_stats_groups(self):
        """获取数据分析群下拉列表（v0.5.1，修复 all_mode 下无群可选）。

        返回 ``{"groups": [{"group_id", "enabled", "count"}], "all_mode": bool}``：
        白名单 ∪ chat_history 有数据的群（去重、消息数降序），两种记录模式下
        下拉框都能列出可查看统计的群。stats_service 未注入返 503。
        """
        if self.stats_service is None:
            return error_response("数据分析模块尚未初始化", status_code=503)
        groups = await self.stats_service.resolve_dropdown_groups()
        all_mode = await self.stats_service.is_all_mode()
        return json_response({"groups": groups, "all_mode": all_mode})

    async def api_stats_settings_save(self):
        """批量保存数据分析设置。body 为扁平 ``{key: value}`` 键值对象。

        先全量校验后写入：所有传入键值全部通过与
        ``db_config._validate_stats_setting`` 一致的校验（未知键 + 类型 +
        范围 + HH:MM 格式）后才逐项写入，任一非法整体 400 并列出全部失败键、
        不写入任何项（沿用 api_profile_settings_save 范式）；
        写入阶段 ``set_stats_setting`` 返回 False → 500。
        """
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须为键值对象", status_code=400)

        # ① 全量校验：归一化（与 set_stats_setting 一致：bool→"true"/"false"、
        #    其余 str()）后逐键走真实校验器，失败键收集不立即返回
        normalized: dict[str, str] = {}
        failed: list[str] = []
        for key, value in payload.items():
            if key not in ConfigManager.STATS_TYPES:
                failed.append(key)
                continue
            if isinstance(value, bool):
                str_value = "true" if value else "false"
            else:
                str_value = str(value)
            validated = ConfigManager._validate_stats_setting(key, str_value)
            if validated is None:
                failed.append(key)
            else:
                normalized[key] = validated

        if failed:
            return error_response(
                f"配置项取值非法: {', '.join(failed)}", status_code=400
            )

        # ② 全部校验通过后逐项写入
        for key, str_value in normalized.items():
            ok = await self.config_mgr.set_stats_setting(key, str_value)
            if not ok:
                return error_response("保存数据分析设置失败", status_code=500)

        settings = await self.config_mgr.get_all_stats_settings()
        return json_response({"saved": True, "settings": settings})

    async def api_stats_settings_reset(self):
        """重置数据分析全部 8 项配置为默认值，返回最新全量设置。"""
        await self.config_mgr.reset_stats_settings()
        settings = await self.config_mgr.get_all_stats_settings()
        return json_response({"reset": True, "settings": settings})

    async def api_stats_push_toggle(self):
        """切换群级推送开关。body ``{group_id, enabled}``。

        group_id 缺失/为空 400（须为纯数字群号）；enabled 规范化 bool：
        接受 bool 与 "true"/"false" 字符串（大小写不敏感），其余 400。
        成功返回 ``{"group_id", "enabled"}``。
        """
        payload = await request.json(default={})
        group_id = str(payload.get("group_id") or "").strip()
        if not group_id:
            return error_response("缺少 group_id 参数", status_code=400)
        if not group_id.isdigit():
            return error_response("group_id 必须为纯数字", status_code=400)

        enabled_raw = payload.get("enabled")
        if isinstance(enabled_raw, bool):
            enabled = enabled_raw
        elif isinstance(enabled_raw, str) and enabled_raw.strip().lower() in (
            "true",
            "false",
        ):
            enabled = enabled_raw.strip().lower() == "true"
        else:
            return error_response("enabled 必须为布尔值（true/false）", status_code=400)

        ok = await self.config_mgr.set_push_group(group_id, enabled)
        if not ok:
            return error_response("保存群推送开关失败", status_code=500)
        return json_response({"group_id": group_id, "enabled": enabled})
