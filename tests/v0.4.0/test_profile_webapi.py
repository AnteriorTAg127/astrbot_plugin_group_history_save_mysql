# ruff: noqa: I001
"""astrbot_plugin_group_history_save_mysql v0.4.0 Module K 离线单元测试。

覆盖「人物分析」Web API 层（web_api.py 新增 profile 端点）：

- settings 读（settings/defaults/types 透出）/ 存（先全量校验后写入，非法拒 400）/ 重置
- providers（context.get_all_providers，缺 id 跳过，异常降级空列表）
- groups（复用 config_mgr.get_groups 数据源，异常 500）
- analyze 触发：单群 / 全局（group_id 空/"all"）返回序列化 ProfileResult；
  sender_id 缺失/非数字 400；service 未注入 503；内部超时兜底 504；异常 500
- history 列表（顶层 profiles 专名避撞桥接解包）/ 详情（命中/缺失/非法 404/400）/ 删除（query 与 body 双路）
- 未注入 profile_storage 时 history 各端点 503
- _profile_result_to_dict 对 datetime/tuple/None 的 JSON 安全性（json.dumps 不抛）
- 路由注册表含全部 9 个 profile 端点（GET/POST 同路径分立）

运行方式（插件根目录，二者皆可）：
    python -m pytest "tests/v0.4.0/test_profile_webapi.py" -v
    python "tests/v0.4.0/test_profile_webapi.py"

范式沿用 tests/v0.4.0/test_profile_service.py（Agent-C stub 隔离技巧）：
astrbot.* 全部 sys.modules stub；**db_mysql / cleaner 因依赖 aiomysql（离线环境缺失）
以 fake 模块注入**；db_config 导入真实 ConfigManager（校验规则以真实 PROFILE_TYPES /
_convert_profile_value 为准）；profile.service / profile.storage 仅 TYPE_CHECKING 引用，
运行期以 Fake 实例替代，不拉起重依赖链。
"""

import asyncio
import json
import os
import sys
import types
import unittest
from datetime import datetime


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
    def info(self, msg, *a, **k):
        pass

    def warning(self, msg, *a, **k):
        pass

    def error(self, msg, *a, **k):
        pass

    def debug(self, msg, *a, **k):
        pass


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


# ---- astrbot.api.event（db_config/service 类型引用，轻量占位）----
class _StubAstrMessageEvent:
    pass


_astrbot_event.AstrMessageEvent = _StubAstrMessageEvent


# ---- astrbot.api.web: json_response / error_response / request ----
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
_astrbot_web.request = _STUB_REQUEST


def _file_response(path, filename=None, content_type=None):
    return {"__file__": True, "path": path, "filename": filename}


# v0.4.2 起 web_api.py 顶层导入 file_response（导出图片端点）
_astrbot_web.file_response = _file_response

# ---- astrbot.core.utils.io: save_temp_img（v0.4.2 起 web_api.py 顶层导入）----
_astrbot_core = _new_module("astrbot.core")
_astrbot_core_utils = _new_module("astrbot.core.utils")
_astrbot_core_utils_io = _new_module("astrbot.core.utils.io")
_astrbot_core_utils_io.save_temp_img = lambda data: ""


# ---- 多测试文件合跑兼容：剔除被测包缓存（Agent-C stub 隔离技巧）----
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
#      db_config 保留真实导入（校验规则以真实 ConfigManager 为准）。
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
from astrbot_plugin_group_history_save_mysql.core.profile.models import (  # noqa: E402
    ProfileResult,
    ProfileStats,
    ProfileTarget,
)
from astrbot_plugin_group_history_save_mysql.core.webapi.base import (  # noqa: E402
    PLUGIN_NAME,
    PROFILE_ANALYZE_TIMEOUT,
    ProfileFacade,
    SummaryFacade,
    _profile_result_to_dict,
)

# v0.6.0 拆分后包级 __init__ 不再导出常量/私有 helper 与 request：
# 以 base.py 同名值补齐包属性，保持既有 WEB.* 引用零改动（request 为共享
# stub 实例，mixin 模块 import 时绑定同一对象，改其属性即生效）。
WEB.PLUGIN_NAME = PLUGIN_NAME
WEB.PROFILE_ANALYZE_TIMEOUT = PROFILE_ANALYZE_TIMEOUT
WEB._profile_result_to_dict = _profile_result_to_dict
WEB.request = _STUB_REQUEST


# ============================================================
# 四、fake 依赖（context / config_mgr / profile_service / profile_storage）
# ============================================================


class FakeContext:
    def __init__(self, providers=None, fail_providers=False):
        self._providers = providers or []
        self._fail = fail_providers
        self.registered = []  # register_web_api 记录

    def register_web_api(self, route, handler, methods, desc):
        self.registered.append((route, handler, methods, desc))

    def get_all_providers(self):
        if self._fail:
            raise RuntimeError("boom")
        return self._providers


class FakeConfigMgr:
    """实例方法 fake；类级 PROFILE_TYPES/DEFAULTS/_convert_profile_value 走真实 ConfigManager。"""

    def __init__(self, settings=None, groups=None, all_mode=False, fail_all=False):
        self.settings = dict(settings or {})
        self.groups = list(groups or [])
        self.saved = None  # 最近一次 save_profile_settings 入参
        self.reset_calls = 0
        self.fail_get_all = False
        self.fail_save = False
        self.fail_reset = False
        self.fail_groups = False
        self.all_mode = all_mode
        self.fail_all_settings = fail_all

    async def get_all_profile_settings(self):
        if self.fail_get_all:
            raise RuntimeError("boom")
        return dict(self.settings)

    async def save_profile_settings(self, settings):
        if self.fail_save:
            raise RuntimeError("boom")
        self.saved = dict(settings)
        self.settings.update(settings)
        # v0.4.5 契约：返回 bool（成功 True），端点据此区分 500 与假成功
        return True

    async def reset_profile_settings(self):
        if self.fail_reset:
            raise RuntimeError("boom")
        self.reset_calls += 1

    async def get_groups(self):
        if self.fail_groups:
            raise RuntimeError("boom")
        return list(self.groups)

    async def get_all_settings(self):
        if self.fail_all_settings:
            raise RuntimeError("boom")
        return {"all_mode": "true" if self.all_mode else "false"}


class FakeProfileService:
    def __init__(self, result=None, launch_groups=None, all_mode=False):
        self.result = result
        self.calls = []  # [(sender_id, group_id, event)]
        self.delay = 0.0
        self.raise_exc = None
        self.launch_groups = list(launch_groups or [])
        self.all_mode = all_mode
        self.fail_launch_groups = False

    async def run_analysis(self, sender_id, group_id, event=None):
        self.calls.append((sender_id, group_id, event))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.result

    async def resolve_launch_groups(self):
        if self.fail_launch_groups:
            raise RuntimeError("launch groups boom")
        return [dict(g) for g in self.launch_groups]

    async def is_all_mode(self):
        return self.all_mode


class FakeProfileStorage:
    def __init__(self, list_result=None, read_result=None, delete_result=True):
        self.list_result = list_result or {
            "total": 0,
            "profiles": [],
            "page": 1,
            "page_size": 20,
        }
        self.read_result = read_result
        self.delete_result = delete_result
        self.list_calls = []
        self.read_calls = []
        self.delete_calls = []
        self.fail_list = False
        self.fail_read = False
        self.fail_delete = False

    async def list_profiles(self, page, page_size):
        self.list_calls.append((page, page_size))
        if self.fail_list:
            raise RuntimeError("boom")
        return self.list_result

    async def read(self, filename):
        self.read_calls.append(filename)
        if self.fail_read:
            raise RuntimeError("boom")
        return self.read_result

    async def delete(self, filename):
        self.delete_calls.append(filename)
        if self.fail_delete:
            raise RuntimeError("boom")
        return self.delete_result


# ============================================================
# 五、工具：构造真实 ProfileResult / 配置 request / 运行协程
# ============================================================


def _make_result(scope="group", group_id="9001"):
    """构造一个字段齐备的真实 ProfileResult（含 datetime 与 tuple 字段）。"""
    target = ProfileTarget(
        sender_id="12345",
        sender_name="Alice",
        scope=scope,
        group_id=group_id,
    )
    stats = ProfileStats(
        total=10,
        group_count=1 if scope == "group" else 2,
        group_breakdown=[(group_id or "9001", 10)],  # tuple 列表
        time_start=datetime(2026, 8, 1, 10, 0, 0),  # datetime
        time_end=datetime(2026, 8, 2, 12, 30, 45),  # datetime
        active_days=2,
        hour_dist=[0] * 24,
        weekday_dist=[0] * 7,
        peak_hour=22,
        peak_weekday=4,
        avg_length=12.5,
        total_chars=125,
        emoji_ratio=0.1,
        question_ratio=0.2,
        top_partners=[("777", "Bob", 5)],  # tuple 列表
        truncated=False,
    )
    return ProfileResult(
        target=target,
        stats=stats,
        sections=[("发言习惯", "内容 A"), ("性格分析", "内容 B")],  # tuple 列表
        raw_llm_text="raw",
        provider_id="prov-1",
        messages_used=10,
        sources={"mysql": 10},
        relation_context_complete=True,
        scope_desc=f"群 {group_id}" if scope == "group" else "全部已保存群（2 个）",
        created_at="2026-08-02 10:30:45",
    )


def _set_request(query=None, body=None, method="GET"):
    WEB.request.query = _StubQuery(query or {})
    WEB.request._json = body if body is not None else {}
    WEB.request.method = method


def _run(coro):
    return asyncio.run(coro)


def _build_api(service=None, storage=None, config=None, context=None):
    """组装 WebAPI（mysql_mgr/cleaner 传 None，profile 端点不依赖）。

    v0.8.2 R4 构造形态同步：域依赖按 ProfileFacade/SummaryFacade 打包注入。
    """
    ctx = context if context is not None else FakeContext()
    cfg = config if config is not None else FakeConfigMgr()
    return WEB.WebAPI(
        ctx,
        None,
        cfg,
        None,
        summary=SummaryFacade(),
        profile=ProfileFacade(service=service, storage=storage),
    )


# ============================================================
# 六、测试用例
# ============================================================


class RouteRegistrationTest(unittest.TestCase):
    def test_all_nine_profile_routes_registered(self):
        ctx = FakeContext()
        _build_api(context=ctx)
        pairs = {(route, tuple(methods)) for route, _, methods, _ in ctx.registered}
        prefix = f"/{WEB.PLUGIN_NAME}/profile"
        expected = {
            (f"{prefix}/settings", ("GET",)),
            (f"{prefix}/settings", ("POST",)),
            (f"{prefix}/settings/reset", ("POST",)),
            (f"{prefix}/providers", ("GET",)),
            (f"{prefix}/groups", ("GET",)),
            (f"{prefix}/analyze", ("POST",)),
            (f"{prefix}/history", ("GET",)),
            (f"{prefix}/history/detail", ("GET",)),
            # L2 验收发现浏览器无法发出 DELETE → 主 agent 将 methods 扩为
            # ["DELETE", "POST"]，前端以 POST 携带 filename 参数删除（2026-08-03）
            (f"{prefix}/history", ("DELETE", "POST")),
        }
        self.assertTrue(expected.issubset(pairs), f"缺失: {expected - pairs}")


class SettingsEndpointsTest(unittest.TestCase):
    def test_get_settings_returns_settings_defaults_types(self):
        cfg = FakeConfigMgr(settings={"profile_enabled": True})
        api = _build_api(config=cfg)
        _set_request()
        resp = _run(api.api_profile_settings())
        self.assertIsInstance(resp, JsonResp)
        self.assertEqual(resp.data["settings"], {"profile_enabled": True})
        self.assertEqual(resp.data["defaults"], ConfigManager.PROFILE_DEFAULTS)
        self.assertEqual(resp.data["types"], ConfigManager.PROFILE_TYPES)

    def test_get_settings_failure_500(self):
        cfg = FakeConfigMgr()
        cfg.fail_get_all = True
        api = _build_api(config=cfg)
        _set_request()
        resp = _run(api.api_profile_settings())
        self.assertIsInstance(resp, ErrResp)
        self.assertEqual(resp.status_code, 500)

    def test_save_settings_valid_normalizes_and_writes(self):
        cfg = FakeConfigMgr(settings={"profile_enabled": True})
        api = _build_api(config=cfg)
        _set_request(
            body={
                "settings": {
                    "profile_enabled": False,
                    "profile_max_count": 100,
                    "profile_fallback_providers": ["a", "b"],
                }
            }
        )
        resp = _run(api.api_profile_settings_save())
        self.assertIsInstance(resp, JsonResp)
        self.assertTrue(resp.data["saved"])
        # bool→"false"；int→"100"；list→JSON 串
        self.assertEqual(cfg.saved["profile_enabled"], "false")
        self.assertEqual(cfg.saved["profile_max_count"], "100")
        self.assertEqual(
            json.loads(cfg.saved["profile_fallback_providers"]), ["a", "b"]
        )
        self.assertIn("settings", resp.data)

    def test_save_settings_rejects_unknown_key(self):
        cfg = FakeConfigMgr()
        api = _build_api(config=cfg)
        _set_request(body={"settings": {"bogus_key": 1}})
        resp = _run(api.api_profile_settings_save())
        self.assertIsInstance(resp, ErrResp)
        self.assertEqual(resp.status_code, 400)
        self.assertIsNone(cfg.saved)  # 未写入

    def test_save_settings_rejects_invalid_bool(self):
        cfg = FakeConfigMgr()
        api = _build_api(config=cfg)
        _set_request(body={"settings": {"profile_enabled": "notabool"}})
        resp = _run(api.api_profile_settings_save())
        self.assertIsInstance(resp, ErrResp)
        self.assertEqual(resp.status_code, 400)
        self.assertIsNone(cfg.saved)

    def test_save_settings_rejects_invalid_int(self):
        cfg = FakeConfigMgr()
        api = _build_api(config=cfg)
        _set_request(body={"settings": {"profile_max_count": "abc"}})
        resp = _run(api.api_profile_settings_save())
        self.assertIsInstance(resp, ErrResp)
        self.assertEqual(resp.status_code, 400)
        self.assertIsNone(cfg.saved)

    def test_save_settings_rejects_non_dict(self):
        api = _build_api(config=FakeConfigMgr())
        _set_request(body={"settings": [1, 2]})
        resp = _run(api.api_profile_settings_save())
        self.assertIsInstance(resp, ErrResp)
        self.assertEqual(resp.status_code, 400)

    def test_save_settings_write_failure_500(self):
        cfg = FakeConfigMgr()
        cfg.fail_save = True
        api = _build_api(config=cfg)
        _set_request(body={"settings": {"profile_enabled": True}})
        resp = _run(api.api_profile_settings_save())
        self.assertIsInstance(resp, ErrResp)
        self.assertEqual(resp.status_code, 500)

    def test_reset_settings(self):
        cfg = FakeConfigMgr(settings={"profile_enabled": True})
        api = _build_api(config=cfg)
        _set_request(body={})
        resp = _run(api.api_profile_settings_reset())
        self.assertIsInstance(resp, JsonResp)
        self.assertTrue(resp.data["reset"])
        self.assertEqual(cfg.reset_calls, 1)
        self.assertIn("settings", resp.data)

    def test_reset_settings_failure_500(self):
        cfg = FakeConfigMgr()
        cfg.fail_reset = True
        api = _build_api(config=cfg)
        _set_request(body={})
        resp = _run(api.api_profile_settings_reset())
        self.assertIsInstance(resp, ErrResp)
        self.assertEqual(resp.status_code, 500)


class ProvidersGroupsEndpointsTest(unittest.TestCase):
    def test_providers_normal_and_skip_blank_id(self):
        provs = [
            types.SimpleNamespace(provider_config={"id": "p1", "name": "Provider 1"}),
            types.SimpleNamespace(provider_config={"id": "", "name": "no-id"}),  # 跳过
            types.SimpleNamespace(provider_config={"id": "p2"}),  # name 回退 id
        ]
        api = _build_api(context=FakeContext(providers=provs))
        _set_request()
        resp = _run(api.api_profile_providers())
        self.assertIsInstance(resp, JsonResp)
        self.assertEqual(
            resp.data["providers"],
            [{"id": "p1", "name": "Provider 1"}, {"id": "p2", "name": "p2"}],
        )

    def test_providers_failure_degrades_empty(self):
        api = _build_api(context=FakeContext(fail_providers=True))
        _set_request()
        resp = _run(api.api_profile_providers())
        self.assertIsInstance(resp, JsonResp)
        self.assertEqual(resp.data["providers"], [])

    def test_groups_fallback_config_groups(self):
        groups = [{"group_id": "9001", "enabled": 1}]
        api = _build_api(config=FakeConfigMgr(groups=groups))
        _set_request()
        resp = _run(api.api_profile_groups())
        self.assertIsInstance(resp, JsonResp)
        self.assertEqual(resp.data["groups"], groups)
        self.assertFalse(resp.data["all_mode"])

    def test_groups_failure_500(self):
        cfg = FakeConfigMgr()
        cfg.fail_groups = True
        api = _build_api(config=cfg)
        _set_request()
        resp = _run(api.api_profile_groups())
        self.assertIsInstance(resp, ErrResp)
        self.assertEqual(resp.status_code, 500)

    def test_groups_service_injected_uses_launch_groups(self):
        # v0.5.6：模式感知——服务注入时以 resolve_launch_groups 为源
        groups = [{"group_id": "9001", "enabled": True, "count": 5}]
        service = FakeProfileService(launch_groups=groups, all_mode=True)
        api = _build_api(service=service)
        _set_request()
        resp = _run(api.api_profile_groups())
        self.assertIsInstance(resp, JsonResp)
        self.assertEqual(resp.data["groups"], groups)
        self.assertTrue(resp.data["all_mode"])

    def test_groups_service_launch_groups_failure_500(self):
        service = FakeProfileService(all_mode=False)
        service.fail_launch_groups = True
        api = _build_api(service=service)
        _set_request()
        resp = _run(api.api_profile_groups())
        self.assertIsInstance(resp, ErrResp)
        self.assertEqual(resp.status_code, 500)


class AnalyzeEndpointTest(unittest.TestCase):
    def test_analyze_single_group_returns_serialized_result(self):
        service = FakeProfileService(result=_make_result("group", "9001"))
        api = _build_api(service=service)
        _set_request(body={"sender_id": "12345", "group_id": "9001"})
        resp = _run(api.api_profile_analyze())
        self.assertIsInstance(resp, JsonResp)
        # 调用参数：(sender_id, group_id, event=None)
        self.assertEqual(service.calls, [("12345", "9001", None)])
        result = resp.data["result"]
        self.assertEqual(result["target"]["sender_id"], "12345")
        self.assertEqual(result["target"]["scope"], "group")
        self.assertEqual(result["provider_id"], "prov-1")
        # datetime → ISO 字符串；tuple → list
        self.assertEqual(result["stats"]["time_start"], "2026-08-01T10:00:00")
        self.assertEqual(result["stats"]["group_breakdown"], [["9001", 10]])
        self.assertEqual(result["stats"]["top_partners"], [["777", "Bob", 5]])
        self.assertEqual(
            result["sections"], [["发言习惯", "内容 A"], ["性格分析", "内容 B"]]
        )
        # 结果 JSON 安全
        json.dumps(result)

    def test_analyze_global_empty_group_id(self):
        service = FakeProfileService(result=_make_result("all", ""))
        api = _build_api(service=service)
        _set_request(body={"sender_id": "12345", "group_id": ""})
        resp = _run(api.api_profile_analyze())
        self.assertIsInstance(resp, JsonResp)
        self.assertEqual(service.calls, [("12345", "", None)])
        self.assertEqual(resp.data["result"]["target"]["scope"], "all")

    def test_analyze_global_literal_all(self):
        service = FakeProfileService(result=_make_result("all", ""))
        api = _build_api(service=service)
        _set_request(body={"sender_id": "12345", "group_id": "all"})
        _run(api.api_profile_analyze())
        self.assertEqual(service.calls, [("12345", "all", None)])

    def test_analyze_missing_sender_id_400(self):
        service = FakeProfileService(result=_make_result())
        api = _build_api(service=service)
        _set_request(body={"group_id": "9001"})
        resp = _run(api.api_profile_analyze())
        self.assertIsInstance(resp, ErrResp)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(service.calls, [])

    def test_analyze_non_digit_sender_id_400(self):
        service = FakeProfileService(result=_make_result())
        api = _build_api(service=service)
        _set_request(body={"sender_id": "12a", "group_id": "9001"})
        resp = _run(api.api_profile_analyze())
        self.assertIsInstance(resp, ErrResp)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(service.calls, [])

    def test_analyze_service_not_injected_503(self):
        api = _build_api(service=None)
        _set_request(body={"sender_id": "12345", "group_id": "9001"})
        resp = _run(api.api_profile_analyze())
        self.assertIsInstance(resp, ErrResp)
        self.assertEqual(resp.status_code, 503)

    def test_analyze_timeout_504(self):
        service = FakeProfileService(result=_make_result())
        service.delay = 0.5
        api = _build_api(service=service)
        _set_request(body={"sender_id": "12345", "group_id": "9001"})
        # v0.6.0 拆分后 api_profile_analyze 位于 core/webapi/profile.py：
        # PROFILE_ANALYZE_TIMEOUT 以模块全局绑定，须 patch 该模块而非包属性
        import astrbot_plugin_group_history_save_mysql.core.webapi.profile as _profile_api_mod

        original = _profile_api_mod.PROFILE_ANALYZE_TIMEOUT
        _profile_api_mod.PROFILE_ANALYZE_TIMEOUT = 0.05
        try:
            resp = _run(api.api_profile_analyze())
        finally:
            _profile_api_mod.PROFILE_ANALYZE_TIMEOUT = original
        self.assertIsInstance(resp, ErrResp)
        self.assertEqual(resp.status_code, 504)

    def test_analyze_service_raises_500(self):
        service = FakeProfileService(result=_make_result())
        service.raise_exc = RuntimeError("boom")
        api = _build_api(service=service)
        _set_request(body={"sender_id": "12345", "group_id": "9001"})
        resp = _run(api.api_profile_analyze())
        self.assertIsInstance(resp, ErrResp)
        self.assertEqual(resp.status_code, 500)


class HistoryEndpointsTest(unittest.TestCase):
    def test_history_list_top_level_profiles_key(self):
        storage = FakeProfileStorage(
            list_result={
                "total": 2,
                "profiles": [
                    {"filename": "all/x.json"},
                    {"filename": "group_1/y.json"},
                ],
                "page": 1,
                "page_size": 20,
            }
        )
        api = _build_api(storage=storage)
        _set_request(query={"page": "1", "page_size": "20"})
        resp = _run(api.api_profile_history())
        self.assertIsInstance(resp, JsonResp)
        # 顶层用 profiles 专名避撞桥接解包（不得用 data）
        self.assertIn("profiles", resp.data)
        self.assertNotIn("data", resp.data)
        self.assertEqual(resp.data["total"], 2)
        self.assertEqual(storage.list_calls, [(1, 20)])

    def test_history_list_pagination_normalization(self):
        storage = FakeProfileStorage()
        api = _build_api(storage=storage)
        _set_request(query={"page": "abc", "page_size": "999"})
        resp = _run(api.api_profile_history())
        self.assertIsInstance(resp, JsonResp)
        # page 非法→1；page_size 超上限→200
        self.assertEqual(storage.list_calls, [(1, 200)])

    def test_history_list_storage_not_injected_503(self):
        api = _build_api(storage=None)
        _set_request()
        resp = _run(api.api_profile_history())
        self.assertIsInstance(resp, ErrResp)
        self.assertEqual(resp.status_code, 503)

    def test_history_list_failure_500(self):
        storage = FakeProfileStorage()
        storage.fail_list = True
        api = _build_api(storage=storage)
        _set_request()
        resp = _run(api.api_profile_history())
        self.assertIsInstance(resp, ErrResp)
        self.assertEqual(resp.status_code, 500)

    def test_history_detail_hit(self):
        storage = FakeProfileStorage(read_result={"target": {"sender_id": "1"}})
        api = _build_api(storage=storage)
        fname = "group_9001/20260802_103045_12345.json"
        _set_request(query={"filename": fname})
        resp = _run(api.api_profile_history_detail())
        self.assertIsInstance(resp, JsonResp)
        self.assertEqual(resp.data["detail"], {"target": {"sender_id": "1"}})
        self.assertEqual(storage.read_calls, [fname])

    def test_history_detail_missing_param_400(self):
        api = _build_api(storage=FakeProfileStorage())
        _set_request(query={})
        resp = _run(api.api_profile_history_detail())
        self.assertIsInstance(resp, ErrResp)
        self.assertEqual(resp.status_code, 400)

    def test_history_detail_not_found_404(self):
        storage = FakeProfileStorage(read_result=None)
        api = _build_api(storage=storage)
        _set_request(query={"filename": "all/20260802_103045_1.json"})
        resp = _run(api.api_profile_history_detail())
        self.assertIsInstance(resp, ErrResp)
        self.assertEqual(resp.status_code, 404)

    def test_history_detail_storage_not_injected_503(self):
        api = _build_api(storage=None)
        _set_request(query={"filename": "all/20260802_103045_1.json"})
        resp = _run(api.api_profile_history_detail())
        self.assertIsInstance(resp, ErrResp)
        self.assertEqual(resp.status_code, 503)

    def test_history_delete_via_query(self):
        storage = FakeProfileStorage(delete_result=True)
        api = _build_api(storage=storage)
        fname = "all/20260802_103045_1.json"
        _set_request(query={"filename": fname}, method="DELETE")
        resp = _run(api.api_profile_history_delete())
        self.assertIsInstance(resp, JsonResp)
        self.assertTrue(resp.data["deleted"])
        self.assertEqual(storage.delete_calls, [fname])

    def test_history_delete_via_body_fallback(self):
        storage = FakeProfileStorage(delete_result=True)
        api = _build_api(storage=storage)
        fname = "group_5/20260802_103045_9.json"
        _set_request(query={}, body={"filename": fname}, method="DELETE")
        resp = _run(api.api_profile_history_delete())
        self.assertIsInstance(resp, JsonResp)
        self.assertEqual(storage.delete_calls, [fname])

    def test_history_delete_missing_param_400(self):
        api = _build_api(storage=FakeProfileStorage())
        _set_request(query={}, body={}, method="DELETE")
        resp = _run(api.api_profile_history_delete())
        self.assertIsInstance(resp, ErrResp)
        self.assertEqual(resp.status_code, 400)

    def test_history_delete_not_found_404(self):
        storage = FakeProfileStorage(delete_result=False)
        api = _build_api(storage=storage)
        _set_request(query={"filename": "all/20260802_103045_1.json"}, method="DELETE")
        resp = _run(api.api_profile_history_delete())
        self.assertIsInstance(resp, ErrResp)
        self.assertEqual(resp.status_code, 404)

    def test_history_delete_storage_not_injected_503(self):
        api = _build_api(storage=None)
        _set_request(query={"filename": "all/20260802_103045_1.json"}, method="DELETE")
        resp = _run(api.api_profile_history_delete())
        self.assertIsInstance(resp, ErrResp)
        self.assertEqual(resp.status_code, 503)


class SerializationHelperTest(unittest.TestCase):
    def test_result_to_dict_json_safe_and_conversions(self):
        data = WEB._profile_result_to_dict(_make_result("group", "9001"))
        # datetime → ISO 字符串
        self.assertIsInstance(data["stats"]["time_start"], str)
        self.assertEqual(data["stats"]["time_start"], "2026-08-01T10:00:00")
        # tuple → list（嵌套）
        self.assertIsInstance(data["stats"]["group_breakdown"], list)
        self.assertIsInstance(data["stats"]["group_breakdown"][0], list)
        self.assertIsInstance(data["sections"], list)
        self.assertIsInstance(data["sections"][0], list)
        # None 时间安全
        result = _make_result()
        result.stats.time_start = None
        result.stats.time_end = None
        data2 = WEB._profile_result_to_dict(result)
        self.assertIsNone(data2["stats"]["time_start"])
        # 整体可 json.dumps（不抛）
        json.dumps(data)
        json.dumps(data2)

    def test_result_to_dict_none_safe(self):
        self.assertEqual(WEB._profile_result_to_dict(None), {})

    def test_to_jsonable_nested_and_unknown_fallback(self):
        class _Weird:
            def __str__(self):
                return "weird!"

        out = WEB._to_jsonable({"a": (1, 2), "b": [_Weird()], "c": None, "d": True})
        self.assertEqual(out["a"], [1, 2])
        self.assertEqual(out["b"], ["weird!"])
        self.assertIsNone(out["c"])
        self.assertIs(out["d"], True)
        json.dumps(out)


class _EnrichMysql:
    """api_query 关联富化用的 fake MySQLManager。"""

    def __init__(self, rows_by_id=None):
        # message_id → row
        self.rows_by_id = dict(rows_by_id or {})
        self.by_id_calls = []

    async def get_messages_by_ids(self, message_ids):
        self.by_id_calls.append(list(message_ids))
        return [
            self.rows_by_id[mid] for mid in message_ids if mid in self.rows_by_id
        ]


class QueryRelationsTest(unittest.TestCase):
    """api_query 的回复关联富化（_enrich_query_reply）。

    说明：at_list（被 @ 的 QQ）仅作记录存储，@ ID 无法可靠反查对应消息，
    故不参与关联富化（见 v0.4.0 PRD 备注）。
    """

    def _api(self, mysql):
        # v0.8.2 R4 构造形态同步：域依赖按 Facade 打包注入
        return WEB.WebAPI(
            FakeContext(),
            mysql,
            FakeConfigMgr(),
            None,
            summary=SummaryFacade(),
            profile=ProfileFacade(),
        )

    def test_reply_enriched(self):
        mysql = _EnrichMysql(
            rows_by_id={
                "msg-999": {
                    "timestamp": "2026-08-01 10:00:00", "group_id": "1",
                    "sender_id": "777", "sender_name": "Bob",
                    "message_type": "text", "content": "被回复的内容",
                    "message_id": "msg-999", "at_list": "", "reply_id": "",
                }
            }
        )
        api = self._api(mysql)
        result = {
            "total": 1,
            "records": [
                {
                    "timestamp": "2026-08-01 10:30:00", "group_id": "1",
                    "sender_id": "123", "sender_name": "Alice",
                    "message_type": "text", "content": "回复",
                    "message_id": "msg-100", "at_list": "888", "reply_id": "msg-999",
                }
            ],
        }
        _run(api._enrich_query_reply(result))
        rec = result["records"][0]
        self.assertEqual(rec["reply_message"]["sender_id"], "777")
        self.assertEqual(rec["reply_message"]["content"], "被回复的内容")
        # 批量反查仅一次（at_list 不触发任何查询）
        self.assertEqual(mysql.by_id_calls, [["msg-999"]])

    def test_no_relations_no_queries(self):
        mysql = _EnrichMysql()
        api = self._api(mysql)
        result = {
            "total": 1,
            "records": [
                {
                    "timestamp": "2026-08-01 10:30:00", "group_id": "1",
                    "sender_id": "123", "sender_name": "Alice",
                    "message_type": "text", "content": "无关联",
                    "message_id": "msg-100", "at_list": "", "reply_id": "",
                }
            ],
        }
        _run(api._enrich_query_reply(result))
        self.assertIsNone(result["records"][0]["reply_message"])
        self.assertEqual(mysql.by_id_calls, [])

    def test_empty_records_returns_early(self):
        mysql = _EnrichMysql()
        api = self._api(mysql)
        result = {"total": 0, "records": []}
        _run(api._enrich_query_reply(result))
        self.assertEqual(mysql.by_id_calls, [])

    def test_missing_reply_tolerated(self):
        mysql = _EnrichMysql()  # 无任何命中
        api = self._api(mysql)
        result = {
            "total": 1,
            "records": [
                {
                    "timestamp": "2026-08-01 10:30:00", "group_id": "1",
                    "sender_id": "123", "sender_name": "Alice",
                    "message_type": "text", "content": "回复不存在",
                    "message_id": "msg-100", "at_list": "999", "reply_id": "msg-missing",
                }
            ],
        }
        _run(api._enrich_query_reply(result))
        # 反查不到 → reply_message 为 None，不报错
        self.assertIsNone(result["records"][0]["reply_message"])

    def test_at_list_not_queried(self):
        # at_list 仅存储，不触发任何反查（@ ID 无法可靠定位消息）
        mysql = _EnrichMysql()
        api = self._api(mysql)
        result = {
            "total": 1,
            "records": [
                {
                    "timestamp": "2026-08-01 10:30:00", "group_id": "1",
                    "sender_id": "123", "sender_name": "Alice",
                    "message_type": "text", "content": "@ 但无反查",
                    "message_id": "msg-100", "at_list": "888,777", "reply_id": "",
                }
            ],
        }
        _run(api._enrich_query_reply(result))
        rec = result["records"][0]
        self.assertIsNone(rec["reply_message"])
        self.assertEqual(mysql.by_id_calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
