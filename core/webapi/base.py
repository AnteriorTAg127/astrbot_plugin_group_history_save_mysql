"""Web API 后端核心（v0.6.0 包化拆分）。

base：路由注册基类 WebAPIBase + 公共 helper 纯函数 + 模块常量。
子功能 Mixin（storage/query/summary/profile/stats）经 __init__.py
多继承组装出单一公开类名 WebAPI，对外调用零改动。
"""

import random
import uuid
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING

from astrbot.api import logger
from astrbot.api.star import Context

from ..cleaner import ImageCleaner
from ..db_config import ConfigManager
from ..db_mysql import MySQLManager

if TYPE_CHECKING:
    from ..profile.service import ProfileService
    from ..profile.storage import ProfileStorage
    from ..profile.t2i_render import ProfileT2IRenderer
    from ..stats.service import StatsService
    from ..summary.storage import SummaryStorage
    from ..summary.t2i_render import T2IRenderer

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


class WebAPIBase:
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
