"""Web API 后端核心（v0.6.0 包化拆分）。

base：路由注册基类 WebAPIBase + 域依赖 Facade + 公共 helper 纯函数 + 模块常量。
子功能 Mixin（storage/query/summary/profile/stats）经 __init__.py
多继承组装出单一公开类名 WebAPI，对外调用零改动。

v0.8.2 R4 依赖收敛与路由分组（纯重构零行为变化）：
- 构造依赖 10 → 7：4 个核心依赖（context/mysql_mgr/config_mgr/cleaner，位置不变）
  + 3 个按域打包的 Facade（summary/profile/stats），老平铺属性名保留为
  只读兼容 property，各 Mixin 与外部读取路径零改动。
- _register_routes 拆为 5 个分组表方法，路由路径/方法/回调/描述逐条不变，
  注册顺序与 v0.6.0 单表一致。
"""

import random
import uuid
from dataclasses import dataclass, fields, is_dataclass
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


# ---------------------------------------------------------------------------
# v0.8.2 R4：域依赖 Facade（按域打包的小 dataclass，替代 6 个平铺注入参数）。
# 字段缺省 None，「未注入 → 相关端点 503」的原语义逐字段保持（WebAPIBase 总
# 会构造出非 None 的 Facade 实例，端点判空仍看各字段）。
# ---------------------------------------------------------------------------


@dataclass
class SummaryFacade:
    """总结域依赖（v0.3 历史存储 + v0.4.2 导出渲染器）。"""

    storage: "SummaryStorage | None" = None
    renderer: "T2IRenderer | None" = None


@dataclass
class ProfileFacade:
    """人物分析域依赖（v0.4.0 编排服务与存储 + v0.4.2 导出渲染器）。"""

    service: "ProfileService | None" = None
    storage: "ProfileStorage | None" = None
    renderer: "ProfileT2IRenderer | None" = None


@dataclass
class StatsFacade:
    """数据分析域依赖（v0.5.0 编排服务；设置类端点不依赖，正常可用）。"""

    service: "StatsService | None" = None


class WebAPIBase:
    """Web 管理后台 API 处理器。

    构造依赖（v0.8.2 R4 收敛后 7 个）：context / mysql_mgr / config_mgr /
    cleaner 四个核心依赖保持老位置参数顺序（兼容既有调用），域依赖经
    summary / profile / stats 三个 Facade 注入；老平铺属性名
    （summary_storage / summary_renderer / profile_service / profile_storage /
    profile_renderer / stats_service）保留为只读兼容 property，各 Mixin
    端点与外部读取零改动。
    """

    def __init__(
        self,
        context: Context,
        mysql_mgr: MySQLManager,
        config_mgr: ConfigManager,
        cleaner: ImageCleaner,
        summary: SummaryFacade | None = None,
        profile: ProfileFacade | None = None,
        stats: StatsFacade | None = None,
    ):
        self.context = context
        self.mysql_mgr = mysql_mgr
        self.config_mgr = config_mgr
        self.cleaner = cleaner
        # v0.3 总结域（存储层 + v0.4.2 导出渲染器），由 bootstrap 注入（模块 K）；
        # 未注入时总结历史/导出相关端点返回 503
        self.summary = summary or SummaryFacade()
        # v0.4.0 人物分析域（编排服务 + 存储层 + v0.4.2 导出渲染器），
        # 由 bootstrap 注入（模块 K）；未注入时 analyze / history 端点返回 503
        self.profile = profile or ProfileFacade()
        # v0.5.0 数据分析域（编排服务），由 bootstrap 注入（模块 M）；
        # 未注入时 stats/data 端点返回 503（设置类端点不依赖，正常可用）
        self.stats = stats or StatsFacade()
        self._purge_challenges: dict[str, tuple[int, float]] = {}
        self._register_routes()

    # ---- v0.8.2 R4 兼容只读属性：老平铺依赖名 → 新 Facade 字段 ----

    @property
    def summary_storage(self) -> "SummaryStorage | None":
        """总结存储层（兼容：原 self.summary_storage → self.summary.storage）。"""
        return self.summary.storage

    @property
    def summary_renderer(self) -> "T2IRenderer | None":
        """总结导出渲染器（兼容：原 self.summary_renderer → self.summary.renderer）。"""
        return self.summary.renderer

    @property
    def profile_service(self) -> "ProfileService | None":
        """人物分析编排服务（兼容：原 self.profile_service → self.profile.service）。"""
        return self.profile.service

    @property
    def profile_storage(self) -> "ProfileStorage | None":
        """人物分析存储层（兼容：原 self.profile_storage → self.profile.storage）。"""
        return self.profile.storage

    @property
    def profile_renderer(self) -> "ProfileT2IRenderer | None":
        """人物分析导出渲染器（兼容：原 self.profile_renderer → self.profile.renderer）。"""
        return self.profile.renderer

    @property
    def stats_service(self) -> "StatsService | None":
        """数据分析编排服务（兼容：原 self.stats_service → self.stats.service）。"""
        return self.stats.service

    # ---- 路由注册（v0.8.2 R4：按域分组；条目内容/注册顺序与 v0.6.0 单表一致） ----

    def _register_routes(self):
        """注册所有 Web API 路由（分组表拼接后统一注册）。

        分组拼接顺序即 v0.6.0 单表的历史顺序（清空维护两条在查询域之后，
        保持注册次序逐条不变）。
        """
        routes = [
            *self._register_storage_routes(),
            *self._register_query_routes(),
            *self._register_maintenance_routes(),
            *self._register_summary_routes(),
            *self._register_profile_routes(),
            *self._register_stats_routes(),
        ]
        for route, handler, methods, desc in routes:
            self.context.register_web_api(route, handler, methods, desc)
        logger.info(f"[HistorySave] 已注册 {len(routes)} 个 Web API 路由")

    def _register_storage_routes(self) -> list:
        """存储库域路由表：状态 / 群白名单 / 设置 / 每日统计 / 手动清理。"""
        return [
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
        ]

    def _register_query_routes(self) -> list:
        """查询域路由表：聊天记录查询 + 查询日志（v0.7.0）列表与设置。"""
        return [
            (f"/{PLUGIN_NAME}/query", self.api_query, ["GET"], "查询聊天记录"),
            (
                f"/{PLUGIN_NAME}/query_log/list",
                self.api_query_log_list,
                ["GET"],
                "查询日志列表",
            ),
            (
                f"/{PLUGIN_NAME}/query_log/settings",
                self.api_query_log_settings_get,
                ["GET"],
                "查询日志设置",
            ),
            (
                f"/{PLUGIN_NAME}/query_log/settings/save",
                self.api_query_log_settings_save,
                ["POST"],
                "保存查询日志设置",
            ),
        ]

    def _register_maintenance_routes(self) -> list:
        """清空维护域路由表：二次验证题目 + 清空所有数据。

        （历史上紧随查询日志之后注册，为保持注册次序逐条不变单列一组。）
        """
        return [
            (
                f"/{PLUGIN_NAME}/purge/challenge",
                self.api_purge_challenge,
                ["GET"],
                "获取清空验证题目",
            ),
            (f"/{PLUGIN_NAME}/purge", self.api_purge, ["POST"], "清空所有数据"),
        ]

    def _register_summary_routes(self) -> list:
        """总结域路由表（v0.3）：设置 / 提供商 / 忽略名单 / 历史总结。"""
        return [
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
        ]

    def _register_profile_routes(self) -> list:
        """人物分析域路由表（v0.4.0）：设置 / 提供商 / 群 / 分析 / 历史。"""
        return [
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
        ]

    def _register_stats_routes(self) -> list:
        """数据分析域路由表（v0.5.0）：数据 / 群列表 / 设置 / 推送开关。"""
        return [
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
