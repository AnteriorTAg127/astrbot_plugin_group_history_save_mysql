# ruff: noqa: I001
"""astrbot_plugin_group_history_save_mysql v0.6.0 模块 G 测试：重构回归 + 重载自动补库。

A 组（重构回归）：v0.6.0 包化拆分（db_mysql / db_config / web_api 由根目录
单文件迁入 core/ 子包，main.py 变薄）后，对外门面类的方法集、类常量与
常量导出保持完整，包可导入性（core/summary 急切链、core/profile / core/stats
惰性链）不受影响——全部为**真实模块**断言（无 fake 注入），失败即重构缺斤短两。

- A-1 MySQLManager 方法面 + DynamicPool + 常量导出（QUERY_TIMEOUT_SECONDS 等，
  含 stats/repository 同款 ``from ..db_mysql import QUERY_TIMEOUT_SECONDS``）
- A-2 ConfigManager 方法面 + SUMMARY/PROFILE/STATS 三组 DEFAULTS/TYPES 类常量
- A-3 WebAPI 方法面 + make_challenge / _to_jsonable 导出
- A-4 core/summary、core/profile、core/stats 包可导入性与惰性导出解析
- A-5 main.py 顶层导入（全真实链）可导入性
- A-6 api_save_settings 新增 backfill_enabled / backfill_hours 校验与写入
  （补库配置 Web 设置入口：全量归一写入 / 非法布尔与越界小时 400 且零写入）

B 组（重载自动补库，v0.6.0 新增 core/backfill.py + core/parsing.py）：

- B-1 parse_onebot_raw_message 六态（纯文本 / 纯图片 / 文本+图片 / at+回复 /
  file 字段 http 链接 / 无文本无图片）；另附结构异常 → None
- B-2 ReloadBackfill.start 开关关闭跳过（不建任务）
- B-3 start 窗口小时夹取 [1,168] 与非法回退 12，window_start 形如 now-hours
- B-4 _backfill_group 全链路：翻页拉取 → 窗口过滤 → 双去重 → 文本/图片入库；
  _run_all 群清单 all_mode 感知（全群模式以 chat_history 有数据的群为准，
  白名单空也补库；清单查询失败降级跳过）
- B-5 message_id 已存在跳过（skipped 计数）与图片 URL 批内/群内去重
- B-6 单群失败不阻断其他群（_run_all 循环隔离）
- B-7 get_existing_message_ids 分块（每块 ≤500）与空入参
- B-8 start/stop 生命周期（幂等、取消安全、可重启）

范式沿用既有测试：@async_test 装饰器（asyncio.run，无 pytest-asyncio /
conftest.py）；astrbot.* / aiomysql 全部 sys.modules stub 且注入先于一切
被测导入；被测包缓存剔除兼容多测试文件合跑。

运行方式（插件根目录，二者皆可）：
    python -m pytest "tests/v0.6.0/test_v060.py" -v
    PYTHONIOENCODING=utf-8 python "tests/v0.6.0/test_v060.py"
"""

import asyncio
import functools
import importlib
import inspect
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path


def async_test(fn):
    """装饰器：用 asyncio.run 运行异步测试函数（环境无 pytest-asyncio 依赖）。"""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        asyncio.run(fn(*args, **kwargs))

    return wrapper


_PKG = "astrbot_plugin_group_history_save_mysql"
_TEST_DIR = Path(__file__).resolve().parent
_PLUGIN_ROOT = _TEST_DIR.parents[1]  # tests/v0.6.0 → 插件根
_PLUGINS_DIR = str(_PLUGIN_ROOT.parent)  # data/plugins（包导入根）


# ============================================================
# 一、astrbot.* / aiomysql stub 注入（必须先于任何被测导入）
# ============================================================


def _new_module(name: str) -> types.ModuleType:
    return types.ModuleType(name)


_astrbot = _new_module("astrbot")
_astrbot_api = _new_module("astrbot.api")
_astrbot_event = _new_module("astrbot.api.event")
_astrbot_star = _new_module("astrbot.api.star")
_astrbot_mc = _new_module("astrbot.api.message_components")
_astrbot_web = _new_module("astrbot.api.web")
_astrbot_core = _new_module("astrbot.core")
_astrbot_core_star = _new_module("astrbot.core.star")
_astrbot_core_star_filter = _new_module("astrbot.core.star.filter")
_astrbot_core_star_filter_command = _new_module("astrbot.core.star.filter.command")
_astrbot_core_utils = _new_module("astrbot.core.utils")
_astrbot_core_utils_io = _new_module("astrbot.core.utils.io")


class _StubLogger:
    """记录各级日志（B 组断言 warning 跳过 / 汇总计数等）。"""

    def __init__(self):
        self.records = {"info": [], "warning": [], "error": [], "debug": []}

    def clear(self):
        for lines in self.records.values():
            lines.clear()

    def _fmt(self, msg, args):
        try:
            return msg % args if args else str(msg)
        except Exception:
            return str(msg)

    def info(self, msg, *args, **kwargs):
        self.records["info"].append(self._fmt(msg, args))

    def warning(self, msg, *args, **kwargs):
        self.records["warning"].append(self._fmt(msg, args))

    def error(self, msg, *args, **kwargs):
        self.records["error"].append(self._fmt(msg, args))

    def debug(self, msg, *args, **kwargs):
        self.records["debug"].append(self._fmt(msg, args))


_STUB_LOGGER = _StubLogger()
_astrbot_api.logger = _STUB_LOGGER
_astrbot_api.AstrBotConfig = dict

# ---- astrbot.api.web：WebAPI mixin 顶层 import ----
_astrbot_web.error_response = lambda *a, **k: {"error": a[0] if a else ""}
_astrbot_web.json_response = lambda *a, **k: {"json": a[0] if a else None}
_astrbot_web.file_response = lambda *a, **k: {"file": a[0] if a else None}
_astrbot_web.request = None  # 各 mixin 模块顶层绑定 request（仅导入不调用）

# ---- astrbot.core：main.py 顶层 import GreedyStr ----


class _StubGreedyStr(str):
    """指令剩余文本参数标记（真实实现为 str 子类，stub 同形）。"""


_astrbot_core_star_filter_command.GreedyStr = _StubGreedyStr

# ---- astrbot.core.utils.io：webapi profile/summary mixin ----
_astrbot_core_utils_io.save_temp_img = lambda *a, **k: ""


class _StubFilter:
    EventMessageType = types.SimpleNamespace(GROUP_MESSAGE="group_message")
    PlatformAdapterType = types.SimpleNamespace(AIOCQHTTP="aiocqhttp")
    PermissionType = types.SimpleNamespace(ADMIN="admin")

    @staticmethod
    def _deco(fn):
        return fn

    event_message_type = staticmethod(lambda *a, **k: _StubFilter._deco)
    platform_adapter_type = staticmethod(lambda *a, **k: _StubFilter._deco)
    permission_type = staticmethod(lambda *a, **k: _StubFilter._deco)

    @staticmethod
    def command(*a, **k):
        return _StubFilter._deco


class _StubAstrMessageEvent:
    pass


class _StubMessageChain:
    def __init__(self, chain=None):
        self.chain = list(chain or [])


_astrbot_event.filter = _StubFilter
_astrbot_event.EventMessageType = _StubFilter.EventMessageType
_astrbot_event.PlatformAdapterType = _StubFilter.PlatformAdapterType
_astrbot_event.PermissionType = _StubFilter.PermissionType
_astrbot_event.AstrMessageEvent = _StubAstrMessageEvent
_astrbot_event.MessageChain = _StubMessageChain


class _StubContext:
    def __init__(self, *a, **k):
        pass


class _StubStar:
    def __init__(self, *a, **k):
        pass


def _stub_register(name, author, desc, version):
    def deco(cls):
        cls._registered = (name, author, desc, version)
        return cls

    return deco


class _StubStarTools:
    @classmethod
    def get_data_dir(cls, plugin_name=None):
        return Path(tempfile.gettempdir()) / (plugin_name or "astrbot_test")


_astrbot_star.Context = _StubContext
_astrbot_star.Star = _StubStar
_astrbot_star.register = _stub_register
_astrbot_star.StarTools = _StubStarTools


class _StubPlain:
    def __init__(self, text=""):
        self.text = text


class _StubImage:
    def __init__(self, url=None, file=None):
        self.url = url
        self.file = file


class _StubAt:
    def __init__(self, qq="", name=""):
        self.qq = qq
        self.name = name


class _StubNodes:
    def __init__(self, nodes=None):
        self.nodes = nodes or []


class _StubNode:
    def __init__(self, uin="0", name="", content=None):
        self.uin = uin
        self.name = name
        self.content = content or []


_astrbot_mc.Plain = _StubPlain
_astrbot_mc.Image = _StubImage
_astrbot_mc.At = _StubAt
_astrbot_mc.AtAll = _StubAt
_astrbot_mc.Reply = _StubAt
_astrbot_mc.Nodes = _StubNodes
_astrbot_mc.Node = _StubNode

# ---- aiomysql：db_mysql 系列模块顶层 import（不联网）----
_aiomysql = _new_module("aiomysql")
_aiomysql.connect = None
_aiomysql.DictCursor = "DictCursor"
_aiomysql.Connection = object

# ---- 剔除非被测缓存（多测试文件合跑兼容）----
for _name in list(sys.modules):
    if _name == _PKG or _name.startswith(_PKG + "."):
        del sys.modules[_name]

sys.modules["astrbot"] = _astrbot
sys.modules["astrbot.api"] = _astrbot_api
sys.modules["astrbot.api.event"] = _astrbot_event
sys.modules["astrbot.api.star"] = _astrbot_star
sys.modules["astrbot.api.message_components"] = _astrbot_mc
sys.modules["astrbot.api.web"] = _astrbot_web
sys.modules["astrbot.core"] = _astrbot_core
sys.modules["astrbot.core.star"] = _astrbot_core_star
sys.modules["astrbot.core.star.filter"] = _astrbot_core_star_filter
sys.modules["astrbot.core.star.filter.command"] = _astrbot_core_star_filter_command
sys.modules["astrbot.core.utils"] = _astrbot_core_utils
sys.modules["astrbot.core.utils.io"] = _astrbot_core_utils_io
sys.modules["aiomysql"] = _aiomysql

if _PLUGINS_DIR not in sys.path:
    sys.path.insert(0, _PLUGINS_DIR)

# ============================================================
# 二、导入真实被测模块（A 组断言对象；B 组沿用）
# ============================================================

from astrbot_plugin_group_history_save_mysql.core.db_mysql import (  # noqa: E402
    CREATE_RETRY_BACKOFF_SECONDS,
    DDL_TIMEOUT_SECONDS,
    QUERY_TIMEOUT_SECONDS,
    RESET_PENDING_WAIT_SECONDS,
    DynamicPool,
    MySQLManager,
)
from astrbot_plugin_group_history_save_mysql.core.db_config import ConfigManager  # noqa: E402
from astrbot_plugin_group_history_save_mysql.core.webapi import (  # noqa: E402
    WebAPI,
    _to_jsonable,
    make_challenge,
)
from astrbot_plugin_group_history_save_mysql.core.parsing import (  # noqa: E402
    parse_onebot_raw_message,
)
from astrbot_plugin_group_history_save_mysql.core.backfill import (  # noqa: E402
    BACKFILL_HOURS_DEFAULT,
    BACKFILL_HOURS_MAX,
    BACKFILL_HOURS_MIN,
    ReloadBackfill,
)

# ============================================================
# 三、A 组：重构回归（方法面 / 类常量 / 常量导出 / 包可导入性）
# ============================================================


class TestRefactorRegression(unittest.TestCase):
    """A-1~A-5：v0.6.0 core/ 包化拆分后公开面完整（真实模块断言）。"""

    def test_a01_mysql_manager_method_surface_and_constants(self):
        self.assertTrue(issubclass(MySQLManager, object))
        expected = {
            # base
            "initialize",
            "ping",
            "close",
            # chat_history
            "insert_chat_message",
            "query_messages",
            "get_messages_by_ids",
            "get_existing_message_ids",
            # images
            "insert_image_record",
            "get_existing_image_urls",
            "clean_old_images",
            # stats
            "get_stats",
            "get_daily_stats",
            # maintenance
            "purge_all",
        }
        missing = {m for m in expected if not callable(getattr(MySQLManager, m, None))}
        self.assertEqual(missing, set(), f"MySQLManager 缺失方法: {missing}")
        # DynamicPool 仍经包导出（pool.py 拆分后对外名不变）
        self.assertTrue(inspect.isclass(DynamicPool))
        # 常量导出（stats/repository 的 from ..db_mysql import QUERY_TIMEOUT_SECONDS 同款）
        for const in (
            QUERY_TIMEOUT_SECONDS,
            DDL_TIMEOUT_SECONDS,
            CREATE_RETRY_BACKOFF_SECONDS,
            RESET_PENDING_WAIT_SECONDS,
        ):
            self.assertIsInstance(const, float)
            self.assertGreater(const, 0)
        pkg = importlib.import_module(_PKG + ".core.db_mysql")
        for name in (
            "MySQLManager",
            "DynamicPool",
            "QUERY_TIMEOUT_SECONDS",
            "DDL_TIMEOUT_SECONDS",
            "CREATE_RETRY_BACKOFF_SECONDS",
            "RESET_PENDING_WAIT_SECONDS",
        ):
            self.assertIn(name, pkg.__all__, f"db_mysql.__all__ 缺 {name}")

    def test_a02_config_manager_method_surface_and_class_attrs(self):
        expected = {
            # base
            "initialize",
            "get_setting",
            "set_setting",
            "get_all_settings",
            "close",
            # groups
            "get_groups",
            "add_group",
            "remove_group",
            "toggle_group",
            "is_group_enabled",
            # summary_settings
            "get_summary_setting",
            "set_summary_setting",
            "get_all_summary_settings",
            "reset_summary_settings",
            "get_summary_setting_typed",
            "get_ignore_senders",
            "add_ignore_sender",
            "remove_ignore_sender",
            "list_ignore_groups",
            # profile_settings
            "get_profile_setting",
            "get_profile_setting_typed",
            "get_all_profile_settings",
            "save_profile_settings",
            "reset_profile_settings",
            # stats_settings
            "get_stats_setting",
            "get_stats_setting_typed",
            "set_stats_setting",
            "get_all_stats_settings",
            "reset_stats_settings",
            "get_push_groups",
            "set_push_group",
            "get_push_flags",
            # snapshots
            "snapshot_upsert",
            "snapshot_query",
            "snapshot_upsert_msg_hour",
            "snapshot_upsert_daily",
            "snapshot_upsert_monthly",
            "snapshot_monthly_totals",
            "snapshot_daily_rows",
            "snapshot_hourly_date_totals",
            "snapshot_evict",
        }
        missing = {m for m in expected if not callable(getattr(ConfigManager, m, None))}
        self.assertEqual(missing, set(), f"ConfigManager 缺失方法: {missing}")
        # 三组功能配置的 DEFAULTS/TYPES 类常量（Web 表单渲染与 typed 读写依赖）
        for prefix, count in (
            ("SUMMARY", 24),
            ("PROFILE", 19),
            ("STATS", 8),
        ):
            defaults = getattr(ConfigManager, f"{prefix}_DEFAULTS")
            types_map = getattr(ConfigManager, f"{prefix}_TYPES")
            self.assertIsInstance(defaults, dict)
            self.assertIsInstance(types_map, dict)
            self.assertEqual(len(defaults), count, f"{prefix}_DEFAULTS 应为 {count} 项")
            self.assertEqual(
                set(defaults),
                set(types_map),
                f"{prefix}_DEFAULTS 与 {prefix}_TYPES 键集合应一致",
            )

    def test_a03_webapi_method_surface_and_helpers(self):
        expected = {
            # storage
            "api_status",
            "api_get_groups",
            "api_toggle_group",
            "api_add_group",
            "api_remove_group",
            "api_get_settings",
            "api_save_settings",
            "api_daily_stats",
            "api_clean",
            "api_purge_challenge",
            "api_purge",
            # query
            "api_query",
            # summary
            "api_summary_settings",
            "api_summary_settings_save",
            "api_summary_settings_reset",
            "api_summary_providers",
            "api_summary_ignore_groups",
            "api_summary_ignore_list",
            "api_summary_ignore_add",
            "api_summary_ignore_remove",
            "api_summary_history",
            "api_summary_history_detail",
            "api_summary_history_export",
            # profile
            "api_profile_settings",
            "api_profile_settings_save",
            "api_profile_settings_reset",
            "api_profile_providers",
            "api_profile_groups",
            "api_profile_analyze",
            "api_profile_history",
            "api_profile_history_detail",
            "api_profile_history_export",
            "api_profile_history_delete",
            # stats
            "api_stats_data",
            "api_stats_settings",
            "api_stats_groups",
            "api_stats_settings_save",
            "api_stats_settings_reset",
            "api_stats_push_toggle",
        }
        missing = {m for m in expected if not callable(getattr(WebAPI, m, None))}
        self.assertEqual(missing, set(), f"WebAPI 缺失方法: {missing}")
        self.assertTrue(callable(make_challenge))
        self.assertTrue(callable(_to_jsonable))
        pkg = importlib.import_module(_PKG + ".core.webapi")
        self.assertEqual(pkg.__all__, ["WebAPI", "make_challenge", "_to_jsonable"])

    def test_a04_summary_profile_stats_package_importability(self):
        # core/summary 为急切导出（service 链在 import 时拉起）
        summary = importlib.import_module(_PKG + ".core.summary")
        self.assertEqual(summary.__all__, ["SummaryService"])
        self.assertTrue(callable(summary.SummaryService))
        # core/profile 惰性导出：轻导入 + 首次访问才解析
        profile = importlib.import_module(_PKG + ".core.profile")
        for name in (
            "ProfileService",
            "ProfileTarget",
            "ProfileMessage",
            "ProfileStats",
            "ProfileResult",
            "ProfileFetchOutcome",
        ):
            self.assertIn(name, profile.__all__)
        self.assertTrue(callable(getattr(profile, "ProfileService")))
        # core/stats 惰性导出（v0.5.5 起含快照类）
        stats = importlib.import_module(_PKG + ".core.stats")
        for name in (
            "StatsRepository",
            "StatsService",
            "StatsBuildError",
            "SnapshotManager",
            "ImageSnapshotManager",
        ):
            self.assertIn(name, stats.__all__)
            self.assertTrue(callable(getattr(stats, name)))
        # 常量跨界引用（stats/repository 的 from ..db_mysql import QUERY_TIMEOUT_SECONDS）
        repo = importlib.import_module(_PKG + ".core.stats.repository")
        self.assertEqual(repo.QUERY_TIMEOUT_SECONDS, QUERY_TIMEOUT_SECONDS)

    def test_a05_main_importability(self):
        main = importlib.import_module(_PKG + ".main")
        self.assertTrue(hasattr(main, "GroupHistoryPlugin"))
        # v0.6.0 瘦身：消息解析/保存逻辑已迁出 core/parsing 与 core/saver
        self.assertFalse(hasattr(main, "extract_image_urls"))
        parsing = importlib.import_module(_PKG + ".core.parsing")
        self.assertTrue(callable(parsing.extract_image_urls))
        self.assertTrue(callable(parsing.stats_fallback_text))
        self.assertTrue(callable(parsing.parse_onebot_raw_message))


class TestSaveSettingsBackfillApi(unittest.TestCase):
    """A-6：api_save_settings 新增 backfill_enabled / backfill_hours 校验与写入
    （v0.6.0 补库配置的 Web 设置入口，先全量校验后写入）。"""

    def setUp(self):
        self.storage_mod = importlib.import_module(_PKG + ".core.webapi.storage")
        self.writes: dict[str, str] = {}

        class _Cfg:
            def __init__(self, writes):
                self.writes = writes

            async def set_setting(self, key, value):
                self.writes[key] = value

            async def get_all_settings(self):
                return dict(self.writes)

        class _Ctx:
            def register_web_api(self, *a, **k):
                pass

        self.api = WebAPI(_Ctx(), None, _Cfg(self.writes), None)

    def tearDown(self):
        self.storage_mod.request = None

    def _set_payload(self, payload):
        class _Req:
            def __init__(self, payload):
                self._payload = payload

            async def json(self, default=None):
                return self._payload

        self.storage_mod.request = _Req(payload)

    @async_test
    async def test_a06_save_all_settings_normalized(self):
        self._set_payload(
            {
                "image_retention_days": 7,
                "all_mode": True,
                "backfill_enabled": True,
                "backfill_hours": 24,
            }
        )
        resp = await self.api.api_save_settings()
        self.assertIn("json", resp)
        self.assertEqual(
            self.writes,
            {
                "image_retention_days": "7",
                "all_mode": "true",
                "backfill_enabled": "true",
                "backfill_hours": "24",
            },
        )

    @async_test
    async def test_a06_save_only_backfill_keeps_strings(self):
        self._set_payload({"backfill_enabled": "false", "backfill_hours": "1"})
        resp = await self.api.api_save_settings()
        self.assertIn("json", resp)
        self.assertEqual(
            self.writes, {"backfill_enabled": "false", "backfill_hours": "1"}
        )

    @async_test
    async def test_a06_reject_invalid_backfill_enabled(self):
        for bad in ("yes", 1, None):
            self._set_payload({"backfill_enabled": bad})
            resp = await self.api.api_save_settings()
            self.assertIn("error", resp)
        self.assertEqual(self.writes, {})

    @async_test
    async def test_a06_reject_invalid_backfill_hours(self):
        for bad in ("abc", 0, 169, -3):
            self._set_payload({"backfill_hours": bad})
            resp = await self.api.api_save_settings()
            self.assertIn("error", resp)
        self.assertEqual(self.writes, {})


# ============================================================
# 四、B 组：重载自动补库（parse_onebot_raw_message 六态 + ReloadBackfill）
# ============================================================


class TestParseOnebotRawMessage(unittest.TestCase):
    """B-1：parse_onebot_raw_message 六态 + 异常兜底。"""

    @staticmethod
    def _mk(segments, message_id="m1", when=1700000000, sender=None):
        return {
            "time": when,
            "message_id": message_id,
            "sender": sender or {"user_id": 123, "nickname": "甲"},
            "message": segments,
        }

    def test_b01_state1_text_only(self):
        item = parse_onebot_raw_message(
            self._mk([{"type": "text", "data": {"text": "  你好  "}}]), "111"
        )
        self.assertIsNotNone(item)
        self.assertEqual(item["text"], "你好")  # strip 后 join
        self.assertEqual(item["image_urls"], [])
        self.assertEqual(item["message_id"], "m1")
        self.assertEqual(item["sender_id"], "123")
        self.assertEqual(item["sender_name"], "甲")
        self.assertEqual(item["at_list"], "")
        self.assertEqual(item["reply_id"], "")
        self.assertIsInstance(item["timestamp"], datetime)

    def test_b01_state2_image_only(self):
        item = parse_onebot_raw_message(
            self._mk(
                [{"type": "image", "data": {"url": "https://a.com/1.jpg"}}], "m2"
            ),
            "111",
        )
        self.assertIsNotNone(item)
        self.assertEqual(item["text"], "")
        self.assertEqual(item["image_urls"], ["https://a.com/1.jpg"])

    def test_b01_state3_text_and_image_mixed(self):
        item = parse_onebot_raw_message(
            self._mk(
                [
                    {"type": "text", "data": {"text": "看这图"}},
                    {"type": "image", "data": {"url": "https://a.com/2.jpg"}},
                ],
                "m3",
            ),
            "111",
        )
        self.assertEqual(item["text"], "看这图")
        self.assertEqual(item["image_urls"], ["https://a.com/2.jpg"])

    def test_b01_state4_at_and_reply(self):
        item = parse_onebot_raw_message(
            self._mk(
                [
                    {"type": "at", "data": {"qq": "777"}},
                    {"type": "at", "data": {"qq": "all"}},  # 伪目标剔除
                    {"type": "reply", "data": {"id": "r1"}},
                    {"type": "text", "data": {"text": "回复"}},
                ],
                "m4",
            ),
            "111",
        )
        self.assertEqual(item["at_list"], "777")
        self.assertEqual(item["reply_id"], "r1")
        self.assertEqual(item["text"], "回复")

    def test_b01_state5_file_field_http_url(self):
        # 部分协议端（如 Lagrange）把链接放 data.file
        item = parse_onebot_raw_message(
            self._mk(
                [{"type": "image", "data": {"file": "https://f.com/x.png"}}], "m5"
            ),
            "111",
        )
        self.assertEqual(item["image_urls"], ["https://f.com/x.png"])

    def test_b01_state6_no_text_no_image_returns_none(self):
        # 本地文件路径（非 http）不收集 → 无文本无图片 → None
        item = parse_onebot_raw_message(
            self._mk([{"type": "image", "data": {"file": "/tmp/local.png"}}], "m6"),
            "111",
        )
        self.assertIsNone(item)
        # 空消息段 → None
        self.assertIsNone(parse_onebot_raw_message(self._mk([]), "111"))

    def test_b01_malformed_returns_none(self):
        # 缺 time / message 非列表 / 顶层非 dict 均不抛，返回 None
        self.assertIsNone(parse_onebot_raw_message({"message_id": 1}, "111"))
        self.assertIsNone(
            parse_onebot_raw_message(self._mk("not-a-list", when="bad"), "111")
        )
        self.assertIsNone(parse_onebot_raw_message(None, "111"))
        self.assertIsNone(parse_onebot_raw_message("str", "111"))


# ---- B 组共享替身 ----


class _FakeConfigMgr:
    """ReloadBackfill 配置替身：可编排 backfill_enabled / backfill_hours / all_mode / 白名单。"""

    def __init__(self, enabled="true", hours="12", groups=None, all_mode="false"):
        self._enabled = enabled
        self._hours = hours
        self._all_mode = all_mode
        self._groups = groups or []

    async def get_setting(self, key, default=""):
        if key == "backfill_enabled":
            return self._enabled
        if key == "backfill_hours":
            return self._hours
        if key == "all_mode":
            return self._all_mode
        return default

    async def get_groups(self):
        return [dict(g) for g in self._groups]


class _FakeMySQLMgr:
    """MySQLManager 替身：记录调用，可编排已存在集合与插入失败。"""

    def __init__(self, existing_ids=None, existing_urls=None):
        self.existing_ids = set(existing_ids or [])
        self.existing_urls = set(existing_urls or [])
        self.chat_calls: list[dict] = []
        self.img_calls: list[dict] = []
        self.id_lookups: list[tuple] = []
        self.url_lookups: list[tuple] = []
        self.fail_chat = False
        self.all_group_ids: list[str] | None = None  # None → get_all_group_ids 抛异常

    async def get_existing_message_ids(self, group_id, ids):
        self.id_lookups.append((group_id, list(ids)))
        return {i for i in ids if i in self.existing_ids}

    async def get_existing_image_urls(self, group_id, urls):
        self.url_lookups.append((group_id, list(urls)))
        return {u for u in urls if u in self.existing_urls}

    async def get_all_group_ids(self):
        if self.all_group_ids is None:
            raise RuntimeError("boom")
        return list(self.all_group_ids)

    async def insert_chat_message(self, **kwargs):
        self.chat_calls.append(kwargs)
        return not self.fail_chat

    async def insert_image_record(self, **kwargs):
        self.img_calls.append(kwargs)
        return True


class _FakeClientAPI:
    """协议端 call_action 替身：按 message_seq 返回固定页。"""

    def __init__(self, pages: dict):
        self.pages = pages  # {message_seq: [raw, ...]}
        self.calls: list[dict] = []

    async def call_action(self, action, **kwargs):
        self.calls.append(kwargs)
        assert action == "get_group_msg_history"
        seq = kwargs.get("message_seq", 0)
        return {"messages": self.pages.get(seq, [])}


class _FakeInst:
    def __init__(self, client):
        self._client = client

    def meta(self):
        return types.SimpleNamespace(name="aiocqhttp")

    def get_client(self):
        return self._client


class _FakeContext:
    def __init__(self, insts=None):
        self._insts = insts or []
        self.platform_manager = types.SimpleNamespace(
            get_insts=lambda: self._insts
        )


def _raw_text(mid, text, when, seq=1, user="u1", nick="甲"):
    return {
        "time": int(when.timestamp()),
        "message_id": mid,
        "message_seq": seq,
        "sender": {"user_id": user, "nickname": nick},
        "message": [{"type": "text", "data": {"text": text}}],
    }


def _raw_image(mid, url, when, seq=1, user="u1", nick="甲"):
    return {
        "time": int(when.timestamp()),
        "message_id": mid,
        "message_seq": seq,
        "sender": {"user_id": user, "nickname": nick},
        "message": [{"type": "image", "data": {"url": url}}],
    }


class TestBackfillStartStop(unittest.TestCase):
    """B-2/3/8：start 开关、窗口夹取、生命周期。"""

    def _make(self, config: _FakeConfigMgr):
        return ReloadBackfill(_FakeContext(), _FakeMySQLMgr(), config)

    @async_test
    async def test_b02_start_switch_off_skips(self):
        bf = self._make(_FakeConfigMgr(enabled="false"))
        await bf.start()
        self.assertIsNone(bf._task, "开关关闭后不应创建后台任务")
        self.assertTrue(
            any("开关未开启" in m for m in _STUB_LOGGER.records["info"]),
            "应记跳过日志",
        )

    @async_test
    async def test_b03_start_hours_clamp_and_window(self):
        for hours, expected_hours in (
            ("5", 5),  # 正常
            ("0", BACKFILL_HOURS_MIN),  # 低于下限 → 1
            ("999", BACKFILL_HOURS_MAX),  # 高于上限 → 168
            ("abc", BACKFILL_HOURS_DEFAULT),  # 非法 → 12
        ):
            _STUB_LOGGER.clear()
            captured = {}

            class _CaptureWindow(ReloadBackfill):
                async def _run_all(self, window_start):
                    captured["window_start"] = window_start

            bf = _CaptureWindow(_FakeContext(), _FakeMySQLMgr(), _FakeConfigMgr(hours=hours))
            await bf.start()
            self.assertIsNotNone(bf._task, f"hours={hours} 应创建任务")
            # await 让 _run_all 有机会执行（其立即返回，确定性拿到 window_start）
            await bf._task
            ws = captured.get("window_start")
            self.assertIsNotNone(ws, f"hours={hours} 应执行 _run_all")
            expected = datetime.now() - timedelta(hours=expected_hours)
            self.assertLess(
                abs((ws - expected).total_seconds()), 5, f"hours={hours} 窗口窗口偏差过大"
            )
            await bf.stop()

    @async_test
    async def test_b08_start_idempotent_stop_cancels_restart(self):
        bf = self._make(_FakeConfigMgr())
        await bf.start()
        task1 = bf._task
        self.assertIsNotNone(task1)
        await bf.start()  # 运行中重复 start → 幂等跳过
        self.assertIs(task1, bf._task)
        await bf.stop()  # cancel + await + 吞 CancelledError
        self.assertIsNone(bf._task)
        self.assertTrue(task1.done())
        await bf.start()  # 停止后可重启
        task2 = bf._task
        self.assertIsNotNone(task2)
        self.assertIsNot(task1, task2)
        await bf.stop()


class TestBackfillGroup(unittest.TestCase):
    """B-4/5：_backfill_group 全链路（窗口过滤 / 双去重 / 入库）。"""

    @async_test
    async def test_b04_window_filter_and_inserts(self):
        window_start = datetime(2026, 8, 1, 12, 0)
        old = _raw_text("m_old", "旧消息", datetime(2026, 8, 1, 10, 0), seq=3)
        ok1 = _raw_text("m_ok1", "窗口内文本", datetime(2026, 8, 1, 13, 0), seq=2)
        ok2 = _raw_image("m_ok2", "https://a.com/3.jpg", datetime(2026, 8, 1, 14, 0), seq=1)
        client = _FakeClientAPI({0: [old, ok1, ok2]})
        ctx = _FakeContext([_FakeInst(types.SimpleNamespace(api=client))])
        mysql = _FakeMySQLMgr()
        bf = ReloadBackfill(ctx, mysql, _FakeConfigMgr())

        result = await bf._backfill_group({"group_id": 111, "enabled": True}, window_start)

        # old 早于窗口被过滤；ok1/ok2 进入去重流程
        self.assertEqual(result["pulled"], 2)
        self.assertEqual(result["inserted_text"], 1)
        self.assertEqual(result["inserted_images"], 1)
        self.assertEqual(result["skipped"], 0)
        # 文本入库透传群号与 at/reply 空串（同 insert_chat_message 契约）
        self.assertEqual(len(mysql.chat_calls), 1)
        call = mysql.chat_calls[0]
        self.assertEqual(call["group_id"], "111")
        self.assertEqual(call["message_id"], "m_ok1")
        self.assertEqual(call["content"], "窗口内文本")
        self.assertEqual(call["at_list"], "")
        self.assertEqual(call["reply_id"], "")
        # 图片入库透传 URL 与真实时间
        self.assertEqual(len(mysql.img_calls), 1)
        self.assertEqual(mysql.img_calls[0]["image_url"], "https://a.com/3.jpg")
        self.assertEqual(mysql.img_calls[0]["timestamp"], datetime(2026, 8, 1, 14, 0))
        # 群号字符串化传入协议端
        self.assertEqual(client.calls[0]["group_id"], 111)

    @async_test
    async def test_b05_message_id_dedup_and_url_dedup(self):
        window_start = datetime(2026, 8, 1, 12, 0)
        dup = _raw_text("m_dup", "已存在", datetime(2026, 8, 1, 13, 0), seq=2)
        img1 = _raw_image("m_img1", "https://a.com/dup.jpg", datetime(2026, 8, 1, 13, 5), seq=1)
        img2 = _raw_image("m_img2", "https://a.com/dup.jpg", datetime(2026, 8, 1, 13, 10), seq=0)
        client = _FakeClientAPI({0: [dup, img1, img2]})
        ctx = _FakeContext([_FakeInst(types.SimpleNamespace(api=client))])
        mysql = _FakeMySQLMgr(existing_ids=["m_dup"], existing_urls=["https://a.com/dup.jpg"])
        bf = ReloadBackfill(ctx, mysql, _FakeConfigMgr())

        result = await bf._backfill_group({"group_id": 111, "enabled": True}, window_start)

        self.assertEqual(result["pulled"], 3)
        self.assertEqual(result["skipped"], 1)  # m_dup 已在库
        self.assertEqual(result["inserted_text"], 0)
        self.assertEqual(result["inserted_images"], 0)  # 群内 URL 已存在/批内重复
        self.assertEqual(mysql.chat_calls, [])
        self.assertEqual(mysql.img_calls, [])

    @async_test
    async def test_b05_within_batch_url_dedup(self):
        # 同一批两条消息携带同一新 URL → 仅入库一次
        window_start = datetime(2026, 8, 1, 12, 0)
        a = _raw_image("m_a", "https://a.com/new.jpg", datetime(2026, 8, 1, 13, 0), seq=1)
        b = _raw_image("m_b", "https://a.com/new.jpg", datetime(2026, 8, 1, 13, 5), seq=0)
        client = _FakeClientAPI({0: [a, b]})
        ctx = _FakeContext([_FakeInst(types.SimpleNamespace(api=client))])
        mysql = _FakeMySQLMgr()
        bf = ReloadBackfill(ctx, mysql, _FakeConfigMgr())

        result = await bf._backfill_group({"group_id": 111, "enabled": True}, window_start)

        self.assertEqual(result["inserted_images"], 1)
        self.assertEqual(len(mysql.img_calls), 1)

    @async_test
    async def test_b04_no_client_returns_zero_counts(self):
        bf = ReloadBackfill(_FakeContext(insts=[]), _FakeMySQLMgr(), _FakeConfigMgr())
        result = await bf._backfill_group({"group_id": 111, "enabled": True}, datetime(2026, 8, 1))
        self.assertEqual(result, {"pulled": 0, "inserted_text": 0, "inserted_images": 0, "skipped": 0})
        self.assertTrue(
            any("取不到 aiocqhttp" in m for m in _STUB_LOGGER.records["warning"])
        )


class TestBackfillRunAll(unittest.TestCase):
    """B-6：单群失败不阻断其他群。"""

    @async_test
    async def test_b06_single_group_failure_does_not_abort(self):
        class _RaisingBackfill(ReloadBackfill):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                self.attempted: list[str] = []
                self.fail_group: str | None = None

            async def _backfill_group(self, group, window_start):
                gid = str(group.get("group_id"))
                self.attempted.append(gid)
                if gid == self.fail_group:
                    raise RuntimeError("boom")
                return {"pulled": 1, "inserted_text": 1, "inserted_images": 0, "skipped": 0}

        cfg = _FakeConfigMgr(
            groups=[
                {"group_id": 111, "enabled": True},
                {"group_id": 222, "enabled": True},
            ]
        )
        _STUB_LOGGER.clear()
        bf = _RaisingBackfill(_FakeContext(), _FakeMySQLMgr(), cfg)
        bf.fail_group = "111"
        await bf._run_all(datetime(2026, 8, 1))
        # 两个群都被尝试：111 失败后 222 仍执行
        self.assertEqual(bf.attempted, ["111", "222"])
        self.assertTrue(
            any("群 111 补库失败" in m for m in _STUB_LOGGER.records["warning"])
        )
        # 汇总日志反映 222 成功（111 失败不计入）
        self.assertTrue(
            any("全部完成" in m and "共 2 个群" in m and "拉取 1 条" in m
                for m in _STUB_LOGGER.records["info"]),
            f"应输出含 222 成功计数的汇总日志: {_STUB_LOGGER.records['info']}",
        )

    @async_test
    async def test_b06_empty_whitelist_skips(self):
        bf = ReloadBackfill(
            _FakeContext(), _FakeMySQLMgr(), _FakeConfigMgr(groups=[])
        )
        await bf._run_all(datetime(2026, 8, 1))
        self.assertTrue(
            any("白名单为空" in m for m in _STUB_LOGGER.records["info"])
        )


class TestBackfillAllMode(unittest.TestCase):
    """B-4：all_mode 全群模式下补库群清单 = chat_history 有数据的群（白名单无意义）。"""

    @async_test
    async def test_b04_all_mode_uses_data_groups(self):
        window_start = datetime(2026, 8, 1, 12, 0)
        _STUB_LOGGER.clear()
        mysql = _FakeMySQLMgr()
        mysql.all_group_ids = ["222"]
        client = _FakeClientAPI(
            {
                0: [
                    _raw_text(
                        "m_222", "全群模式文本", datetime(2026, 8, 1, 13, 0), seq=1
                    )
                ]
            }
        )
        ctx = _FakeContext([_FakeInst(types.SimpleNamespace(api=client))])
        bf = ReloadBackfill(ctx, mysql, _FakeConfigMgr(all_mode="true"))
        await bf._run_all(window_start)
        # 白名单为空但 all_mode 开 → 以 chat_history 群清单补库
        self.assertEqual(len(mysql.chat_calls), 1)
        self.assertEqual(mysql.chat_calls[0]["group_id"], "222")
        self.assertEqual(mysql.chat_calls[0]["content"], "全群模式文本")
        self.assertTrue(
            any(
                "全部完成" in m and "共 1 个群" in m and "拉取 1 条" in m
                for m in _STUB_LOGGER.records["info"]
            ),
            f"应输出 all_mode 汇总日志: {_STUB_LOGGER.records['info']}",
        )

    @async_test
    async def test_b04_all_mode_group_list_failure_skips(self):
        window_start = datetime(2026, 8, 1, 12, 0)
        _STUB_LOGGER.clear()
        mysql = _FakeMySQLMgr()  # all_group_ids=None → get_all_group_ids 抛异常
        bf = ReloadBackfill(_FakeContext(), mysql, _FakeConfigMgr(all_mode="true"))
        await bf._run_all(window_start)
        self.assertEqual(mysql.chat_calls, [])
        self.assertTrue(
            any("读取群清单失败" in m for m in _STUB_LOGGER.records["warning"])
        )

    @async_test
    async def test_b04_all_mode_off_keeps_whitelist(self):
        # all_mode 关：仍以白名单为准，白名单空 → 跳过（不触达 get_all_group_ids）
        _STUB_LOGGER.clear()
        mysql = _FakeMySQLMgr()
        bf = ReloadBackfill(
            _FakeContext(), mysql, _FakeConfigMgr(all_mode="false", groups=[])
        )
        await bf._run_all(datetime(2026, 8, 1))
        self.assertTrue(
            any("白名单为空" in m for m in _STUB_LOGGER.records["info"])
        )
        self.assertIsNone(mysql.all_group_ids)  # 未触发枚举（若触发会因 None 抛错）


# ---- B-7：get_existing_message_ids 分块（真实 MySQLManager + fake pool）----


class _ChunkCursor:
    """记录 execute 的游标；fetchall 返回该块全部 id 命中行。"""

    def __init__(self):
        self.executed: list[tuple] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, sql, params=None):
        self.executed.append((sql, tuple(params or [])))

    async def fetchall(self):
        params = self.executed[-1][1]
        return [(p,) for p in params[1:]]  # params[0] 为 group_id


class _ChunkPool:
    def __init__(self, cursor):
        self._cursor = cursor

    def acquire(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def cursor(self, *a, **k):
        return self._cursor


class TestExistingMessageIdsChunking(unittest.TestCase):
    """B-7：get_existing_message_ids 分块 + 空入参 + 异常降级。"""

    @async_test
    async def test_b07_chunks_at_500(self):
        mgr = MySQLManager(host="h", port=3306, user="u", password="p", database="d")
        cur = _ChunkCursor()
        mgr.pool = _ChunkPool(cur)
        ids = [f"mid-{i}" for i in range(1200)]
        existing = await mgr.get_existing_message_ids("9001", ids)
        # 1200 个 id → 3 块（500/500/200）
        self.assertEqual(len(cur.executed), 3)
        chunk_sizes = [len(params) - 1 for _sql, params in cur.executed]
        self.assertEqual(chunk_sizes, [500, 500, 200])
        self.assertEqual(existing, set(ids))
        # 每块 SQL 均带 group_id 参数与 IN 占位符
        for sql, params in cur.executed:
            self.assertIn("WHERE group_id = %s AND message_id IN", sql)
            self.assertEqual(params[0], "9001")

    @async_test
    async def test_b07_empty_inputs_no_sql(self):
        mgr = MySQLManager(host="h", port=3306, user="u", password="p", database="d")
        cur = _ChunkCursor()
        mgr.pool = _ChunkPool(cur)
        self.assertEqual(await mgr.get_existing_message_ids("9001", []), set())
        self.assertEqual(await mgr.get_existing_message_ids("9001", ["", None, ""]), set())
        self.assertEqual(cur.executed, [])

    @async_test
    async def test_b07_exception_degrades_empty(self):
        class _BoomPool:
            def acquire(self):
                raise RuntimeError("conn lost")

        mgr = MySQLManager(host="h", port=3306, user="u", password="p", database="d")
        mgr.pool = _BoomPool()
        self.assertEqual(await mgr.get_existing_message_ids("9001", ["m1"]), set())


if __name__ == "__main__":
    unittest.main(verbosity=2)
