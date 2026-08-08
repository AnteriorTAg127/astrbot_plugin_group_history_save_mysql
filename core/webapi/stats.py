"""Web API 数据分析端点（v0.6.0 包化拆分）。

StatsMixin：数据分析数据 / 群列表 / 设置 / 推送开关 等 v0.5.0+ 端点。
"""

from datetime import datetime, timedelta

from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request

from ..db_config import ConfigManager
from ..stats.models import StatsQuery, StatsTimeRange
from .base import (
    STATS_ALL_TIME_START,
    STATS_DATE_FORMAT,
    STATS_MAX_SPAN_DAYS,
    _stats_data_to_dict,
)


class StatsMixin:
    """数据分析端点 Mixin（v0.6.0 拆分自 web_api.py）。"""

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
