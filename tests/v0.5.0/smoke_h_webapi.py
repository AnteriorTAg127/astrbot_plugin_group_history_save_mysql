# ruff: noqa: I001
"""v0.5.0 模块 H Web API 扩展冒烟脚本（web_api.py 数据分析 5 端点）。

覆盖点（对照分工契约「Web API（模块 H）」与任务书）：

- GET /stats/data：
  - 缺省近 7 天窗口计算（start=今日-6 天 00:00、end=今日次日 00:00，label=近7天）
  - 显式日期转左闭右开（end +1 天为开区间上界）/ 仅传一端按单日窗口回退
  - 跨度恰 366 天通过、>366 天 400、end 早于 start 400
  - 非法日期 400（2026-13-40 / 2026-02-30 / 形状不符 2026-8-1 / 非日期文本）
  - group_id 空→None、sender_id→member_id 映射
  - top_n 读 stats_top_n 配置并夹住 1–50
  - 顶层键 stats（避撞桥接解包）；datetime→"YYYY-MM-DD HH:MM:SS"、
    dataclass 嵌套递归、tuple→list、None 字段安全、整体 json.dumps 可序列化
  - stats_service 未注入 503；build_stats 抛异常（StatsBuildError 契约）500 透出文案
- GET /stats/settings：{"settings", "push_groups"} 结构透出
- POST /stats/settings/save：先全量校验后写入（非法键在首个合法键已校验时
  也不写入——断言写入调用集合为空）、归一化（"9:00"→"09:00"、bool→"true"）、
  400 列出全部失败键、写入失败 500、非 dict body 400
- POST /stats/settings/reset：重置后返回最新全量
- POST /stats/push/toggle：enabled 规范化（bool / "true" / "false" 大小写不敏感）、
  group_id 数字串化、缺参/非法 400、写入失败 500
- 路由注册表含全部 5 个 stats 端点
- 模块级 _stats_data_to_dict / _stats_value_to_jsonable 辅助函数

运行方式（插件根目录，二者皆可）：
    python -m pytest "tests/v0.5.0/smoke_h_webapi.py" -v
    python "tests/v0.5.0/smoke_h_webapi.py"

范式沿用 tests/v0.4.0/test_profile_webapi.py（Agent-K stub 隔离技巧）：
astrbot.* 全部 sys.modules stub；**db_mysql / cleaner 因依赖 aiomysql（离线环境缺失）
以 fake 模块注入**；db_config 导入真实 ConfigManager（stats 配置校验规则以真实
STATS_TYPES / _validate_stats_setting 为准）；stats.models 真实导入（纯 stdlib
数据模型）；stats.service（模块 G）并行开发未交付，运行期以 Fake 实例替代，
不拉起依赖链。
"""

import asyncio
import json
import os
import sys
import types
import unittest
from datetime import datetime, timedelta


# ============================================================
# 一、astrbot.* stub 注入（必须在任何被测包 import 之前）
# ============================================================


def _new_module(name: str) -> types.ModuleType:
    return types.ModuleType(name)


_astrbot = _new_module("astrbot")
_astrbot_api = _new_module("astrbot.api")
_astrbot_star = _new_module("astrbot.api.star")
_astrbot_event = _new_module("astrbot.api.event")
_astrbot_web = _new_module("astrbot.api.web")


# ---- astrbot.api: logger（静默）----
class _StubLogger:
    def _noop(self, *args, **kwargs):
        pass

    info = warning = error = debug = _noop


_astrbot_api.logger = _StubLogger()


# ---- astrbot.api.star: Context / Star / StarTools ----
class _StubContext:
    pass


class _StubStar:
    pass


class _StubStarTools:
    @classmethod
    def get_data_dir(cls, plugin_name=None):
        import tempfile
        from pathlib import Path

        return Path(tempfile.gettempdir()) / (plugin_name or "test")


_astrbot_star.Context = _StubContext
_astrbot_star.Star = _StubStar
_astrbot_star.StarTools = _StubStarTools


# ---- astrbot.api.event（轻量占位）----
class _StubAstrMessageEvent:
    pass


_astrbot_event.AstrMessageEvent = _StubAstrMessageEvent


# ---- astrbot.api.web: json_response / error_response / file_response / request ----
# 形状对齐真实实现（astrbot/api/web.py）：json_response 承载业务 body；
# error_response 承载 message/status_code（桥接据此 reject）。测试以类型断言区分。
class JsonResp:
    def __init__(self, data=None, status_code=200):
        self.data = {} if data is None else data
        self.status_code = status_code


class ErrResp:
    def __init__(self, message, status_code=400, data=None):
        self.message = message
        self.status_code = status_code
        self.data = data


def _json_response(data=None, *, status_code=200, headers=None):
    return JsonResp(data, status_code)


def _error_response(message, *, status_code=400, data=None, headers=None):
    return ErrResp(message, status_code, data)


def _file_response(path, filename=None, content_type=None):
    return {"__file__": True, "path": path, "filename": filename}


class _StubQuery:
    """模拟 PluginMultiDict：get(key, default, type) 行为对齐。"""

    def __init__(self, pairs=None):
        self._d = dict(pairs or {})

    def get(self, key, default=None, type=None):  # noqa: A002 - 对齐框架签名
        if key not in self._d:
            return default
        value = self._d[key]
        if type is None:
            return value
        try:
            return type(value)
        except (TypeError, ValueError):
            return default


class _StubRequest:
    """模拟模块级 request 代理：query 可配置，json() 返回注入的 body。"""

    def __init__(self):
        self.query = _StubQuery()
        self._json = {}
        self.method = "GET"

    async def json(self, default=None):
        return self._json if self._json is not None else default


_STUB_REQUEST = _StubRequest()
_astrbot_web.json_response = _json_response
_astrbot_web.error_response = _error_response
_astrbot_web.file_response = _file_response
_astrbot_web.request = _STUB_REQUEST

# ---- astrbot.core.utils.io: save_temp_img（web_api.py 顶层导入）----
_astrbot_core = _new_module("astrbot.core")
_astrbot_core_utils = _new_module("astrbot.core.utils")
_astrbot_core_utils_io = _new_module("astrbot.core.utils.io")
_astrbot_core_utils_io.save_temp_img = lambda data: ""

# ---- 多测试文件合跑兼容：剔除被测包缓存（Agent-K stub 隔离技巧）----
_PKG = "astrbot_plugin_group_history_save_mysql"
for _name in list(sys.modules):
    if _name == _PKG or _name.startswith(_PKG + "."):
        del sys.modules[_name]

# ---- 注入 sys.modules（astrbot.*）----
sys.modules["astrbot"] = _astrbot
sys.modules["astrbot.api"] = _astrbot_api
sys.modules["astrbot.api.star"] = _astrbot_star
sys.modules["astrbot.api.event"] = _astrbot_event
sys.modules["astrbot.api.web"] = _astrbot_web
sys.modules["astrbot.core"] = _astrbot_core
sys.modules["astrbot.core.utils"] = _astrbot_core_utils
sys.modules["astrbot.core.utils.io"] = _astrbot_core_utils_io

# ---- 让被测包可被导入：<plugins 目录> 加入 sys.path ----
_PLUGINS_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..")
)
if _PLUGINS_DIR not in sys.path:
    sys.path.insert(0, _PLUGINS_DIR)


# ============================================================
# 二、fake 内部模块注入（db_mysql / cleaner 依赖 aiomysql，离线缺失）
#      必须在 import web_api 之前，使 web_api.py 的 from .db_mysql/.cleaner 可解析。
#      db_config / stats.models 保留真实导入（校验规则与数据模型以真实实现为准）。
# ============================================================

import importlib  # noqa: E402

_pkg_mod = importlib.import_module(_PKG)

_db_mysql_mod = _new_module(f"{_PKG}.core.db_mysql")


class _StubMySQLManager:
    pass


_db_mysql_mod.MySQLManager = _StubMySQLManager
_db_mysql_mod.QUERY_TIMEOUT_SECONDS = 30.0
sys.modules[f"{_PKG}.core.db_mysql"] = _db_mysql_mod

_cleaner_mod = _new_module(f"{_PKG}.core.cleaner")


class _StubImageCleaner:
    pass


_cleaner_mod.ImageCleaner = _StubImageCleaner
sys.modules[f"{_PKG}.core.cleaner"] = _cleaner_mod


# ============================================================
# 三、导入被测代码（stub 已就位）
# ============================================================

import astrbot_plugin_group_history_save_mysql.core.webapi as WEB  # noqa: E402
from astrbot_plugin_group_history_save_mysql.core.db_config import (  # noqa: E402
    ConfigManager,
)
from astrbot_plugin_group_history_save_mysql.core.stats.models import (  # noqa: E402
    GroupRankItem,
    MemberStats,
    SenderRankItem,
    StatsData,
    StatsQuery,
    StatsTimeRange,
)
from astrbot_plugin_group_history_save_mysql.core.webapi.base import (  # noqa: E402
    PLUGIN_NAME,
    StatsFacade,
    _stats_data_to_dict,
    _stats_value_to_jsonable,
)

# v0.6.0 拆分后包级 __init__ 不再导出常量/私有 helper 与 request：
# 以 base.py 同名值补齐包属性，保持既有 WEB.* 引用零改动（request 为共享
# stub 实例，mixin 模块 import 时绑定同一对象，改其属性即生效）。
WEB.PLUGIN_NAME = PLUGIN_NAME
WEB._stats_data_to_dict = _stats_data_to_dict
WEB._stats_value_to_jsonable = _stats_value_to_jsonable
WEB.request = _STUB_REQUEST


class StatsBuildError(Exception):
    """与模块 G 契约同名异常（stats/service.py 并行开发未交付，本地定义替代）。"""


# ============================================================
# 四、fake 依赖（context / config_mgr / stats_service）
# ============================================================


class FakeContext:
    def __init__(self):
        self.registered = []  # register_web_api 记录

    def register_web_api(self, route, handler, methods, desc):
        self.registered.append((route, handler, methods, desc))


# get_all_stats_settings 返回的 typed 默认值：由真实 ConfigManager 的
# STATS_DEFAULTS / STATS_TYPES / _convert_stats_value 推导，保证与契约同源
TYPED_DEFAULTS = {
    key: ConfigManager._convert_stats_value(
        raw, ConfigManager.STATS_TYPES.get(key, "str")
    )
    for key, raw in ConfigManager.STATS_DEFAULTS.items()
}


class FakeConfigMgr:
    """stats 相关方法 fake；值校验规则走真实 ConfigManager 类方法（非本 fake）。"""

    def __init__(self, settings=None, push_groups=None):
        self.settings = dict(TYPED_DEFAULTS)
        self.settings.update(settings or {})
        self.push_groups = list(push_groups or [])
        self.set_calls = []  # [(key, value)] 按写入顺序记录
        self.reset_calls = 0
        self.push_calls = []  # [(group_id, enabled)]
        self.fail_set = False  # set_stats_setting 恒返 False（模拟写入失败）
        self.fail_push = False  # set_push_group 恒返 False

    async def get_all_stats_settings(self):
        return dict(self.settings)

    async def get_stats_setting_typed(self, key):
        return self.settings.get(key)

    async def set_stats_setting(self, key, value):
        if self.fail_set:
            return False
        self.set_calls.append((key, value))
        self.settings[key] = value
        return True

    async def reset_stats_settings(self):
        self.reset_calls += 1
        self.settings = dict(TYPED_DEFAULTS)

    async def get_push_groups(self):
        return list(self.push_groups)

    async def get_all_settings(self):
        return {"all_mode": getattr(self, "all_mode", "false")}

    async def set_push_group(self, group_id, enabled):
        if self.fail_push:
            return False
        self.push_calls.append((group_id, enabled))
        return True


class FakeStatsService:
    """build_stats 记录入参 query，可编排返回值或抛异常（模拟 StatsBuildError）。"""

    def __init__(self, data=None):
        self.data = data
        self.calls = []
        self.raise_exc = None
        self.all_mode = False
        self.push_groups_view = []
        self.dropdown_groups = []
        self.daily_stats_result = None  # v0.5.5：overview_daily_stats 返回值
        self.daily_stats_calls = []  # v0.5.5：overview_daily_stats 入参记录

    async def build_stats(self, query):
        self.calls.append(query)
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.data

    async def overview_daily_stats(self, days, now=None):
        """v0.5.5 契约：快照口径供数；默认返回 None 使既有端点用例（如有）
        走 MySQL 回落路径。快照优先/回落两态新用例见 test_v055.py。"""
        self.daily_stats_calls.append(days)
        return self.daily_stats_result

    async def is_all_mode(self):
        return self.all_mode

    async def resolve_push_groups(self):
        return list(self.push_groups_view)

    async def resolve_dropdown_groups(self):
        return list(self.dropdown_groups)


# ============================================================
# 五、工具：构造真实 StatsData / 配置 request / 运行协程
# ============================================================


def _make_stats_data(**overrides):
    """构造字段齐备的真实 StatsData（含嵌套 dataclass 与 datetime 字段）。"""
    query = StatsQuery(
        group_id="9001",
        member_id=None,
        time_range=StatsTimeRange(
            start=datetime(2026, 7, 29),
            end=datetime(2026, 8, 5),
            label="近7天",
        ),
        top_n=10,
    )
    data = StatsData(
        query=query,
        total_messages=123,
        total_images=45,
        active_senders=7,
        peak_hour=21,
        first_msg_time=datetime(2026, 7, 29, 8, 15, 30),
        last_msg_time=datetime(2026, 8, 4, 23, 59, 59),
        daily_trend=[{"date": "2026-07-29", "count": 10}],
        hourly_dist=[0] * 24,
        weekday_dist=[0] * 7,
        sender_ranking=[
            SenderRankItem(
                sender_id="123", sender_name="Alice", count=50, image_count=5
            )
        ],
        group_ranking=[
            GroupRankItem(group_id="9001", count=123, image_count=45, active_senders=7)
        ],
        member=MemberStats(
            sender_id="123",
            sender_name="Alice",
            count=50,
            image_count=5,
            ratio=0.4,
            rank=1,
            active_days=6,
            avg_per_day=8.3,
            hourly_dist=[0] * 24,
            weekday_dist=[0] * 7,
        ),
        generated_at=datetime(2026, 8, 4, 15, 30, 45),
    )
    for key, value in overrides.items():
        setattr(data, key, value)
    return data


def _set_request(query=None, body=None, method="GET"):
    WEB.request.query = _StubQuery(query or {})
    WEB.request._json = body if body is not None else {}
    WEB.request.method = method


def _run(coro):
    return asyncio.run(coro)


def _build_api(service=None, config=None, context=None):
    """组装 WebAPI（mysql_mgr/cleaner 传 None，stats 端点不依赖）。"""
    ctx = context if context is not None else FakeContext()
    cfg = config if config is not None else FakeConfigMgr()
    return WEB.WebAPI(ctx, None, cfg, None, stats=StatsFacade(service=service))


# ============================================================
# 六、测试用例
# ============================================================


class RouteRegistrationTest(unittest.TestCase):
    def test_all_six_stats_routes_registered(self):
        ctx = FakeContext()
        _build_api(context=ctx)
        pairs = {(route, tuple(methods)) for route, _, methods, _ in ctx.registered}
        prefix = f"/{WEB.PLUGIN_NAME}/stats"
        expected = {
            (f"{prefix}/data", ("GET",)),
            (f"{prefix}/groups", ("GET",)),
            (f"{prefix}/settings", ("GET",)),
            (f"{prefix}/settings/save", ("POST",)),
            (f"{prefix}/settings/reset", ("POST",)),
            (f"{prefix}/push/toggle", ("POST",)),
        }
        self.assertTrue(expected.issubset(pairs), f"缺失: {expected - pairs}")


class StatsDataEndpointTest(unittest.TestCase):
    def test_default_window_last_7_days(self):
        service = FakeStatsService(data=_make_stats_data())
        api = _build_api(service=service)
        _set_request(query={})
        resp = _run(api.api_stats_data())
        self.assertIsInstance(resp, JsonResp)
        self.assertEqual(len(service.calls), 1)
        q = service.calls[0]
        # 缺省窗口：start=今日-6 天 00:00、end=今日次日 00:00（左闭右开）
        today = datetime.now().date()
        exp_start = datetime(today.year, today.month, today.day) - timedelta(days=6)
        exp_end = datetime(today.year, today.month, today.day) + timedelta(days=1)
        self.assertEqual(q.time_range.start, exp_start)
        self.assertEqual(q.time_range.end, exp_end)
        self.assertEqual(q.time_range.label, "近7天")
        self.assertIsNone(q.group_id)
        self.assertIsNone(q.member_id)
        self.assertEqual(q.top_n, 10)

    def test_explicit_range_end_exclusive_next_day(self):
        service = FakeStatsService(data=_make_stats_data())
        api = _build_api(service=service)
        _set_request(
            query={
                "group_id": "9001",
                "sender_id": "12345",
                "start": "2026-08-01",
                "end": "2026-08-04",
            }
        )
        resp = _run(api.api_stats_data())
        self.assertIsInstance(resp, JsonResp)
        q = service.calls[0]
        self.assertEqual(q.time_range.start, datetime(2026, 8, 1))
        # end 含当日：+1 天为开区间上界
        self.assertEqual(q.time_range.end, datetime(2026, 8, 5))
        self.assertEqual(q.time_range.label, "2026-08-01 ~ 2026-08-04")
        self.assertEqual(q.group_id, "9001")
        self.assertEqual(q.member_id, "12345")

    def test_single_day_when_only_start_given(self):
        service = FakeStatsService(data=_make_stats_data())
        api = _build_api(service=service)
        _set_request(query={"start": "2026-08-01"})
        resp = _run(api.api_stats_data())
        self.assertIsInstance(resp, JsonResp)
        q = service.calls[0]
        self.assertEqual(q.time_range.start, datetime(2026, 8, 1))
        self.assertEqual(q.time_range.end, datetime(2026, 8, 2))
        self.assertEqual(q.time_range.label, "2026-08-01")

    def test_single_day_when_only_end_given(self):
        service = FakeStatsService(data=_make_stats_data())
        api = _build_api(service=service)
        _set_request(query={"end": "2026-08-04"})
        resp = _run(api.api_stats_data())
        self.assertIsInstance(resp, JsonResp)
        q = service.calls[0]
        self.assertEqual(q.time_range.start, datetime(2026, 8, 4))
        self.assertEqual(q.time_range.end, datetime(2026, 8, 5))
        self.assertEqual(q.time_range.label, "2026-08-04")

    def test_span_exactly_366_days_ok(self):
        service = FakeStatsService(data=_make_stats_data())
        api = _build_api(service=service)
        # 2024 闰年：01-01 ~ 12-31 含首尾共 366 个自然日，恰在上限内
        _set_request(query={"start": "2024-01-01", "end": "2024-12-31"})
        resp = _run(api.api_stats_data())
        self.assertIsInstance(resp, JsonResp)

    def test_span_over_366_days_400(self):
        service = FakeStatsService(data=_make_stats_data())
        api = _build_api(service=service)
        _set_request(query={"start": "2024-01-01", "end": "2025-01-01"})
        resp = _run(api.api_stats_data())
        self.assertIsInstance(resp, ErrResp)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(service.calls, [])

    def test_all_time_preset_exempts_span_check(self):
        # 「全部」预设（前端契约 start=2000-01-01）豁免跨度校验，
        # 与指令侧 parser「全部」口径对齐（v0.5.0 H/I 契约冲突修复）
        service = FakeStatsService(data=_make_stats_data())
        api = _build_api(service=service)
        end_today = datetime.now().date().strftime("%Y-%m-%d")
        _set_request(query={"start": "2000-01-01", "end": end_today})
        resp = _run(api.api_stats_data())
        self.assertIsInstance(resp, JsonResp)
        self.assertEqual(len(service.calls), 1)
        q = service.calls[0]
        self.assertEqual(q.time_range.start, datetime(2000, 1, 1))
        self.assertEqual(q.time_range.label, "全部")

    def test_all_time_start_but_not_preset_still_span_checked(self):
        # 仅当 start 恰为哨兵值 2000-01-01 才豁免；
        # 2000-01-02 起 >366 天仍应 400（防止豁免面扩大）
        service = FakeStatsService(data=_make_stats_data())
        api = _build_api(service=service)
        _set_request(query={"start": "2000-01-02", "end": "2026-08-04"})
        resp = _run(api.api_stats_data())
        self.assertIsInstance(resp, ErrResp)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(service.calls, [])

    def test_end_before_start_400(self):
        service = FakeStatsService(data=_make_stats_data())
        api = _build_api(service=service)
        _set_request(query={"start": "2026-08-04", "end": "2026-08-01"})
        resp = _run(api.api_stats_data())
        self.assertIsInstance(resp, ErrResp)
        self.assertEqual(resp.status_code, 400)

    def test_invalid_date_400(self):
        api = _build_api(service=FakeStatsService(data=_make_stats_data()))
        bad_pairs = [
            ("2026-13-40", None),  # 月日越界
            ("2026-02-30", None),  # 不存在的日期
            ("abc", None),  # 非日期文本
            ("2026-8-1", None),  # 形状不符（未补零）
            (None, "2026-02-30"),
        ]
        for start, end in bad_pairs:
            service = FakeStatsService(data=_make_stats_data())
            api = _build_api(service=service)
            query = {}
            if start:
                query["start"] = start
            if end:
                query["end"] = end
            _set_request(query=query)
            resp = _run(api.api_stats_data())
            self.assertIsInstance(resp, ErrResp, f"start={start} end={end}")
            self.assertEqual(resp.status_code, 400, f"start={start} end={end}")
            self.assertEqual(service.calls, [], f"start={start} end={end}")

    def test_top_level_stats_key_and_serialization(self):
        service = FakeStatsService(data=_make_stats_data())
        api = _build_api(service=service)
        _set_request(query={"group_id": "9001"})
        resp = _run(api.api_stats_data())
        self.assertIsInstance(resp, JsonResp)
        # 顶层键 stats 避撞桥接解包（不得用 data）
        self.assertIn("stats", resp.data)
        self.assertNotIn("data", resp.data)
        stats = resp.data["stats"]
        # datetime → "YYYY-MM-DD HH:MM:SS"
        self.assertEqual(stats["generated_at"], "2026-08-04 15:30:45")
        self.assertEqual(stats["first_msg_time"], "2026-07-29 08:15:30")
        # 嵌套 dataclass 递归序列化（query → time_range）
        self.assertEqual(stats["query"]["group_id"], "9001")
        self.assertEqual(stats["query"]["time_range"]["start"], "2026-07-29 00:00:00")
        self.assertEqual(stats["query"]["time_range"]["end"], "2026-08-05 00:00:00")
        self.assertEqual(stats["query"]["time_range"]["label"], "近7天")
        # 排行 dataclass 列表逐项序列化
        self.assertEqual(
            stats["sender_ranking"][0],
            {"sender_id": "123", "sender_name": "Alice", "count": 50, "image_count": 5},
        )
        self.assertEqual(stats["group_ranking"][0]["active_senders"], 7)
        # member 嵌套分布列表保形
        self.assertEqual(len(stats["member"]["hourly_dist"]), 24)
        self.assertEqual(len(stats["member"]["weekday_dist"]), 7)
        # 整体 JSON 安全
        json.dumps(resp.data)

    def test_none_fields_serialization_safe(self):
        data = _make_stats_data(
            peak_hour=None, first_msg_time=None, last_msg_time=None, member=None
        )
        api = _build_api(service=FakeStatsService(data=data))
        _set_request(query={})
        resp = _run(api.api_stats_data())
        self.assertIsInstance(resp, JsonResp)
        stats = resp.data["stats"]
        self.assertIsNone(stats["peak_hour"])
        self.assertIsNone(stats["first_msg_time"])
        self.assertIsNone(stats["last_msg_time"])
        self.assertIsNone(stats["member"])
        json.dumps(resp.data)

    def test_service_not_injected_503(self):
        api = _build_api(service=None)
        _set_request(query={})
        resp = _run(api.api_stats_data())
        self.assertIsInstance(resp, ErrResp)
        self.assertEqual(resp.status_code, 503)

    def test_build_error_500_message_passthrough(self):
        service = FakeStatsService(data=_make_stats_data())
        service.raise_exc = StatsBuildError("数据库连接失败，无法构建统计数据")
        api = _build_api(service=service)
        _set_request(query={})
        resp = _run(api.api_stats_data())
        self.assertIsInstance(resp, ErrResp)
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.message, "数据库连接失败，无法构建统计数据")

    def test_top_n_from_config(self):
        cfg = FakeConfigMgr(settings={"stats_top_n": 25})
        service = FakeStatsService(data=_make_stats_data())
        api = _build_api(service=service, config=cfg)
        _set_request(query={})
        resp = _run(api.api_stats_data())
        self.assertIsInstance(resp, JsonResp)
        self.assertEqual(service.calls[0].top_n, 25)

    def test_top_n_clamped_to_1_50(self):
        for stored, expected in ((999, 50), (0, 1), (-5, 1)):
            cfg = FakeConfigMgr(settings={"stats_top_n": stored})
            service = FakeStatsService(data=_make_stats_data())
            api = _build_api(service=service, config=cfg)
            _set_request(query={})
            resp = _run(api.api_stats_data())
            self.assertIsInstance(resp, JsonResp, f"stored={stored}")
            self.assertEqual(service.calls[0].top_n, expected, f"stored={stored}")


class StatsSettingsEndpointsTest(unittest.TestCase):
    def test_get_settings_structure(self):
        push_groups = [
            {"group_id": "9001", "enabled": True},
            {"group_id": "9002", "enabled": False},
        ]
        cfg = FakeConfigMgr(push_groups=push_groups)
        api = _build_api(config=cfg)
        _set_request()
        resp = _run(api.api_stats_settings())
        self.assertIsInstance(resp, JsonResp)
        # v0.5.1：未注入 service 时回落白名单语义 + all_mode 键
        self.assertEqual(set(resp.data.keys()), {"settings", "push_groups", "all_mode"})
        self.assertEqual(resp.data["settings"], dict(TYPED_DEFAULTS))
        self.assertEqual(resp.data["push_groups"], push_groups)
        self.assertIs(resp.data["all_mode"], False)

    def test_get_settings_mode_aware_via_service(self):
        # v0.5.1：注入 service 后 push_groups/all_mode 走模式感知解析
        service = FakeStatsService()
        service.all_mode = True
        service.push_groups_view = [
            {"group_id": "7001", "enabled": True, "count": 1234}
        ]
        api = _build_api(service=service)
        _set_request()
        resp = _run(api.api_stats_settings())
        self.assertIsInstance(resp, JsonResp)
        self.assertIs(resp.data["all_mode"], True)
        self.assertEqual(
            resp.data["push_groups"],
            [{"group_id": "7001", "enabled": True, "count": 1234}],
        )

    def test_stats_groups_endpoint(self):
        service = FakeStatsService()
        service.dropdown_groups = [
            {"group_id": "101", "enabled": True, "count": 500},
            {"group_id": "102", "enabled": True, "count": 30},
        ]
        api = _build_api(service=service)
        _set_request()
        resp = _run(api.api_stats_groups())
        self.assertIsInstance(resp, JsonResp)
        self.assertEqual(resp.data["groups"], service.dropdown_groups)
        self.assertIs(resp.data["all_mode"], False)

    def test_stats_groups_not_injected_503(self):
        api = _build_api()
        _set_request()
        resp = _run(api.api_stats_groups())
        self.assertIsInstance(resp, ErrResp)
        self.assertEqual(resp.status_code, 503)

    def test_save_valid_normalizes_and_writes_in_order(self):
        cfg = FakeConfigMgr()
        api = _build_api(config=cfg)
        _set_request(
            body={
                "push_daily_time": "9:00",  # HH:MM 归一化两位补零
                "stats_top_n": 20,
                "push_weekly_enabled": True,  # bool → "true"
            }
        )
        resp = _run(api.api_stats_settings_save())
        self.assertIsInstance(resp, JsonResp)
        self.assertTrue(resp.data["saved"])
        self.assertIn("settings", resp.data)
        # 写入顺序与归一化值
        self.assertEqual(
            cfg.set_calls,
            [
                ("push_daily_time", "09:00"),
                ("stats_top_n", "20"),
                ("push_weekly_enabled", "true"),
            ],
        )

    def test_save_all_validated_before_any_write(self):
        cfg = FakeConfigMgr()
        api = _build_api(config=cfg)
        # 首个键合法、第二个键超范围：全量校验后整体 400，任何项都不得写入
        _set_request(body={"stats_top_n": 20, "stats_cooldown": 9999})
        resp = _run(api.api_stats_settings_save())
        self.assertIsInstance(resp, ErrResp)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("stats_cooldown", resp.message)
        self.assertEqual(cfg.set_calls, [])

    def test_save_unknown_key_400_no_write(self):
        cfg = FakeConfigMgr()
        api = _build_api(config=cfg)
        _set_request(body={"bogus_key": 1})
        resp = _run(api.api_stats_settings_save())
        self.assertIsInstance(resp, ErrResp)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("bogus_key", resp.message)
        self.assertEqual(cfg.set_calls, [])

    def test_save_lists_all_failed_keys(self):
        cfg = FakeConfigMgr()
        api = _build_api(config=cfg)
        # 两个非法键 + 一个合法键：失败键全部列出
        _set_request(
            body={"stats_top_n": 0, "push_weekly_weekday": 9, "stats_cooldown": 30}
        )
        resp = _run(api.api_stats_settings_save())
        self.assertIsInstance(resp, ErrResp)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("stats_top_n", resp.message)
        self.assertIn("push_weekly_weekday", resp.message)
        self.assertNotIn("stats_cooldown", resp.message)
        self.assertEqual(cfg.set_calls, [])

    def test_save_invalid_time_format_400(self):
        cfg = FakeConfigMgr()
        api = _build_api(config=cfg)
        _set_request(body={"push_daily_time": "25:99"})
        resp = _run(api.api_stats_settings_save())
        self.assertIsInstance(resp, ErrResp)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(cfg.set_calls, [])

    def test_save_write_failure_500(self):
        cfg = FakeConfigMgr()
        cfg.fail_set = True
        api = _build_api(config=cfg)
        _set_request(body={"stats_top_n": 20})
        resp = _run(api.api_stats_settings_save())
        self.assertIsInstance(resp, ErrResp)
        self.assertEqual(resp.status_code, 500)

    def test_save_non_dict_body_400(self):
        api = _build_api(config=FakeConfigMgr())
        _set_request(body=[1, 2])
        resp = _run(api.api_stats_settings_save())
        self.assertIsInstance(resp, ErrResp)
        self.assertEqual(resp.status_code, 400)

    def test_save_empty_body_ok(self):
        cfg = FakeConfigMgr()
        api = _build_api(config=cfg)
        _set_request(body={})
        resp = _run(api.api_stats_settings_save())
        self.assertIsInstance(resp, JsonResp)
        self.assertTrue(resp.data["saved"])
        self.assertEqual(cfg.set_calls, [])

    def test_reset_returns_full_settings(self):
        cfg = FakeConfigMgr(settings={"stats_top_n": 42})
        api = _build_api(config=cfg)
        _set_request(body={})
        resp = _run(api.api_stats_settings_reset())
        self.assertIsInstance(resp, JsonResp)
        self.assertTrue(resp.data["reset"])
        self.assertEqual(cfg.reset_calls, 1)
        # 重置后返回默认全量（fake 已复位为 TYPED_DEFAULTS）
        self.assertEqual(resp.data["settings"], dict(TYPED_DEFAULTS))


class PushToggleEndpointTest(unittest.TestCase):
    def test_toggle_bool_true(self):
        cfg = FakeConfigMgr()
        api = _build_api(config=cfg)
        _set_request(body={"group_id": "9001", "enabled": True})
        resp = _run(api.api_stats_push_toggle())
        self.assertIsInstance(resp, JsonResp)
        self.assertEqual(resp.data, {"group_id": "9001", "enabled": True})
        self.assertEqual(cfg.push_calls, [("9001", True)])

    def test_toggle_string_values_normalized(self):
        cfg = FakeConfigMgr()
        api = _build_api(config=cfg)
        _set_request(body={"group_id": "9001", "enabled": "false"})
        resp = _run(api.api_stats_push_toggle())
        self.assertIsInstance(resp, JsonResp)
        self.assertEqual(resp.data["enabled"], False)
        # 大小写不敏感
        _set_request(body={"group_id": "9001", "enabled": "TRUE"})
        resp = _run(api.api_stats_push_toggle())
        self.assertIsInstance(resp, JsonResp)
        self.assertEqual(resp.data["enabled"], True)
        self.assertEqual(cfg.push_calls, [("9001", False), ("9001", True)])

    def test_toggle_int_group_id_stringified(self):
        cfg = FakeConfigMgr()
        api = _build_api(config=cfg)
        _set_request(body={"group_id": 9001, "enabled": True})
        resp = _run(api.api_stats_push_toggle())
        self.assertIsInstance(resp, JsonResp)
        self.assertEqual(cfg.push_calls, [("9001", True)])

    def test_toggle_invalid_enabled_400(self):
        cfg = FakeConfigMgr()
        api = _build_api(config=cfg)
        for body in (
            {"group_id": "9001", "enabled": "yes"},
            {"group_id": "9001"},  # enabled 缺失
            {"group_id": "9001", "enabled": 2},
        ):
            _set_request(body=body)
            resp = _run(api.api_stats_push_toggle())
            self.assertIsInstance(resp, ErrResp, f"body={body}")
            self.assertEqual(resp.status_code, 400, f"body={body}")
        self.assertEqual(cfg.push_calls, [])

    def test_toggle_missing_group_id_400(self):
        cfg = FakeConfigMgr()
        api = _build_api(config=cfg)
        for body in ({}, {"enabled": True}, {"group_id": "", "enabled": True}):
            _set_request(body=body)
            resp = _run(api.api_stats_push_toggle())
            self.assertIsInstance(resp, ErrResp, f"body={body}")
            self.assertEqual(resp.status_code, 400, f"body={body}")
        self.assertEqual(cfg.push_calls, [])

    def test_toggle_non_digit_group_id_400(self):
        cfg = FakeConfigMgr()
        api = _build_api(config=cfg)
        _set_request(body={"group_id": "90a1", "enabled": True})
        resp = _run(api.api_stats_push_toggle())
        self.assertIsInstance(resp, ErrResp)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(cfg.push_calls, [])

    def test_toggle_set_failure_500(self):
        cfg = FakeConfigMgr()
        cfg.fail_push = True
        api = _build_api(config=cfg)
        _set_request(body={"group_id": "9001", "enabled": True})
        resp = _run(api.api_stats_push_toggle())
        self.assertIsInstance(resp, ErrResp)
        self.assertEqual(resp.status_code, 500)


class SerializerHelperTest(unittest.TestCase):
    def test_stats_data_to_dict_none_safe(self):
        self.assertEqual(WEB._stats_data_to_dict(None), {})

    def test_stats_value_to_jsonable_tuple_unknown_and_primitives(self):
        class _Weird:
            def __str__(self):
                return "weird!"

        out = WEB._stats_value_to_jsonable(
            {"a": (1, 2), "b": [_Weird()], "c": None, "d": True, "e": 3.5}
        )
        self.assertEqual(out["a"], [1, 2])  # tuple → list
        self.assertEqual(out["b"], ["weird!"])  # 未知类型兜底 str
        self.assertIsNone(out["c"])
        self.assertIs(out["d"], True)
        self.assertEqual(out["e"], 3.5)
        json.dumps(out)

    def test_stats_value_to_jsonable_datetime_format(self):
        out = WEB._stats_value_to_jsonable(datetime(2026, 8, 4, 9, 5, 3))
        self.assertEqual(out, "2026-08-04 09:05:03")


if __name__ == "__main__":
    unittest.main(verbosity=2)
