# ruff: noqa: I001
"""astrbot_plugin_group_history_save_mysql v0.7.0 测试：对外查询接口 + 查询日志。

本版（v0.7.0）新增两大能力：① core/public_api.py 对外查询接口（其他插件
经 data.plugins.<本插件>.core.public_api 导入调用）；② 查询日志（MySQL
query_log 表 + Web 后台「查询日志」tab）。测试沿用 v0.6.1 范式：@async_test
装饰器（asyncio.run，无 pytest-asyncio / conftest.py）；astrbot.* / aiomysql
全部 sys.modules stub 且注入先于一切被测导入；被测包缓存剔除兼容多测试文件
合跑；核心逻辑全部为**真实模块**断言。

A 组（core/public_api.py 对外公共 API）：
- A-1 未初始化（未 register）抛 PublicAPIError，且不写日志
- A-2 成功路径：参数夹取（page<1→1、page_size>200→200）、空串归 None、
  raise_on_error=True 透传、reply 反查回填 reply_message、日志写入
  （caller/method/params/result_count/success/cost_ms 均正确）
- A-3 显式 caller 优先；缺省自动推断（__main__ 场景非 unknown）
- A-4 查询异常路径：返回空结果不抛底层异常、日志 success=False +
  error_msg 截断
- A-5 日志写失败不影响查询结果（仅 warning）

B 组（core/db_mysql/query_log.py 查询日志数据层）：
- B-1 insert_query_log：SQL 参数绑定、params JSON 序列化（中文保留）、
  success 布尔→1/0、created_at 缺省取当前时间
- B-2 概率清理：random.random 打桩 <0.05 时触发 cleanup、≥0.05 不触发
- B-3 query_query_logs：page_size 夹取 [1,200]、caller LIKE 通配符转义、
  method 精确、时间闭区间、created_at 字符串化/success 布尔化、排序
- B-4 cleanup_query_logs：rowcount 返回、异常返回 0（warning）

C 组（core/webapi/query_log.py Web API 端点）：
- C-1 默认参数（page=1、page_size=100）
- C-2 非法 int 回退默认
- C-3 空串筛选归 None、时间透传
- C-4 路由注册存在（base.py 含 query_log/list 条目）

D 组（接线与版本）：
- D-1 MySQLManager 组装含 QueryLogMixin 三方法；WebAPI 组装含 api_query_log_list
- D-2 main.py @register 版本 0.7.0、metadata.yaml v0.7.0
- D-3 前端：index.html 含 query-log tab、静态引用 ?v=0.7.0

运行方式（插件根目录，二者皆可）：
    python -m pytest "tests/v0.7.0/test_v070.py" -v
    PYTHONIOENCODING=utf-8 python "tests/v0.7.0/test_v070.py"
"""

import asyncio
import functools
import json
import sys
import types
import unittest
from unittest import mock
from datetime import datetime
from pathlib import Path


def async_test(fn):
    """装饰器：用 asyncio.run 运行异步测试函数（环境无 pytest-asyncio 依赖）。"""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        asyncio.run(fn(*args, **kwargs))

    return wrapper


_PKG = "astrbot_plugin_group_history_save_mysql"
_TEST_DIR = Path(__file__).resolve().parent
_PLUGIN_ROOT = _TEST_DIR.parents[1]  # tests/v0.7.0 → 插件根
_PLUGINS_DIR = str(_PLUGIN_ROOT.parent)  # data/plugins（包导入根）


# ============================================================
# 一、astrbot.* / aiomysql stub 注入（必须先于任何被测导入）
# ============================================================


def _new_module(name: str) -> types.ModuleType:
    return types.ModuleType(name)


# 多测试文件合跑兼容（v0.3.1 教训）：若 sys.modules 已存在其他测试文件
# 注入的 astrbot stub（如 test_v061.py 先被收集），**复用**既有对象而非覆盖——
# 覆盖会导致先加载文件中被测模块已绑定的 stub 引用与运行时对象失配。
_sys_astrbot = sys.modules.get("astrbot")
if _sys_astrbot is not None and hasattr(_sys_astrbot, "api"):
    _astrbot = _sys_astrbot
    _astrbot_api = _astrbot.api
    _astrbot_event = _astrbot.api.event
    _astrbot_star = _astrbot.api.star
    _astrbot_mc = _astrbot.api.message_components
    _astrbot_web = _astrbot.api.web
    _astrbot_core = _astrbot.core
    _astrbot_core_star = _astrbot.core.star
    _astrbot_core_star_filter = _astrbot.core.star.filter
    _astrbot_core_star_filter_command = _astrbot.core.star.filter.command
    _astrbot_core_utils = _astrbot.core.utils
    _astrbot_core_utils_io = _astrbot.core.utils.io
else:
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
    """记录各级日志（B 组断言 warning / 清理日志等）。"""

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


_STUB_LOGGER = _astrbot_api.logger if hasattr(_astrbot_api, "logger") else _StubLogger()
_astrbot_api.logger = _STUB_LOGGER
_astrbot_api.AstrBotConfig = dict

# ---- astrbot.api.web：webapi mixin 顶层 import ----
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
        return Path(__file__).resolve().parent / "tmp_data"


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
# aiomysql：复用既有 stub（合跑时 v061 已注入），缺失才新建
_sys_aiomysql = sys.modules.get("aiomysql")
if _sys_aiomysql is not None:
    _aiomysql = _sys_aiomysql
else:
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
# 二、导入真实被测模块
# ============================================================

from astrbot_plugin_group_history_save_mysql.core.db_config import ConfigManager  # noqa: E402
from astrbot_plugin_group_history_save_mysql.core.db_config.query_log import (  # noqa: E402
    QUERY_LOG_DEFAULT_RETENTION_DAYS,
    QueryLogMixin as SqliteQueryLogMixin,
)
from astrbot_plugin_group_history_save_mysql.core.db_mysql import MySQLManager  # noqa: E402
from astrbot_plugin_group_history_save_mysql.core.db_mysql.chat_history import (  # noqa: E402
    ChatHistoryMixin,
)
from astrbot_plugin_group_history_save_mysql.core import public_api  # noqa: E402
from astrbot_plugin_group_history_save_mysql.core.webapi import WebAPI  # noqa: E402
from astrbot_plugin_group_history_save_mysql.core.webapi.query_log import (  # noqa: E402
    QueryLogMixin as WebQueryLogMixin,
)

import aiosqlite  # noqa: E402  （B 组真实 SQLite 用例）

# ============================================================
# 三、共享替身（fake mysql / request / 日志断言）
# ============================================================


class _FakeCursor:
    """记录 execute SQL/参数；fetchone/fetchall 返回预设。"""

    def __init__(self, fetchone_result=None, fetchall_result=None, rowcount=0):
        self.calls: list[tuple[str, tuple]] = []
        self._fetchone_result = fetchone_result
        self._fetchall_result = fetchall_result
        self.rowcount = rowcount

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, sql, params=None):
        self.calls.append((sql, tuple(params) if params else ()))
        return 0

    async def fetchone(self):
        return self._fetchone_result

    async def fetchall(self):
        return self._fetchall_result


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def cursor(self, *a, **k):
        return self._cursor


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        # 真实 DynamicPool.acquire() 返回异步上下文管理器（非 coroutine）
        return self._conn


class _FakeMixinHost:
    """QueryLogMixin 挂载宿主：提供 pool + _execute（记录调用）+ _log_op_error。"""

    def __init__(self, cursor):
        self.pool = _FakePool(_FakeConn(cursor))
        self._execute_calls: list[tuple[str, tuple]] = []
        self._log_errors: list[str] = []

    async def _execute(self, cur, sql, params=None, timeout=None):
        self._execute_calls.append((sql, tuple(params) if params else ()))
        return await cur.execute(sql, params)

    def _log_op_error(self, key, action, exc):
        self._log_errors.append(action)


class _FakeMgr:
    """public_api 的 mysql_mgr / config_mgr 双角色替身：记录调用、可编排异常。"""

    def __init__(
        self,
        query_result=None,
        query_error=None,
        log_error=None,
        count_error=None,
    ):
        self.query_result = query_result or {
            "total": 1,
            "records": [
                {
                    "id": 1,
                    "timestamp": "2026-08-11 10:00:00",
                    "group_id": "111",
                    "sender_id": "222",
                    "sender_name": "张三",
                    "message_type": "text",
                    "content": "hello",
                    "message_id": "m1",
                    "at_list": "",
                    "reply_id": "r9",
                }
            ],
        }
        self.query_error = query_error
        self.log_error = log_error
        self.count_error = count_error
        self.query_calls: list[dict] = []
        self.log_calls: list[dict] = []
        self.count_calls: list[dict] = []

    async def query_messages(self, **kwargs):
        self.query_calls.append(kwargs)
        if self.query_error:
            raise self.query_error
        return self.query_result

    async def count_messages(self, **kwargs):
        self.count_calls.append(kwargs)
        if self.count_error:
            raise self.count_error
        sender_ids = kwargs.get("sender_ids") or []
        senders = {
            sid: {
                "in_group": 10 if kwargs.get("group_id") else None,
                "total": 20,
            }
            for sid in sender_ids
        }
        result: dict = {}
        if kwargs.get("group_id"):
            result["group_total"] = 42
        if senders:
            result["senders"] = senders
        return result

    async def get_messages_by_ids(self, message_ids):
        return [
            {
                "timestamp": "2026-08-11 09:00:00",
                "group_id": "111",
                "sender_id": "333",
                "sender_name": "李四",
                "message_type": "text",
                "content": "被回复内容",
                "message_id": "r9",
                "at_list": "",
                "reply_id": "",
            }
        ]

    async def query_query_logs(self, **kwargs):
        self.query_calls.append(kwargs)
        return {"total": 0, "records": []}

    async def insert_query_log(self, **kwargs):
        self.log_calls.append(kwargs)
        if self.log_error:
            raise self.log_error
        return True


class _FakeRequest:
    """webapi 端点用 request 替身：可编排 query 参数。"""

    query: dict = {}


# ============================================================
# A 组：core/public_api.py 对外公共 API
# ============================================================


class TestPublicAPI(unittest.TestCase):
    def setUp(self):
        self._orig_mgr = public_api._mysql_mgr
        self._orig_cfg = public_api._config_mgr

    def tearDown(self):
        public_api._mysql_mgr = self._orig_mgr
        public_api._config_mgr = self._orig_cfg

    def test_A1_uninitialized_raises(self):
        """A-1 未初始化抛 PublicAPIError（不写日志）。"""
        public_api._mysql_mgr = None
        public_api._config_mgr = None
        with self.assertRaises(public_api.PublicAPIError):
            asyncio.run(public_api.query_records())

    @async_test
    async def test_A2_success_path(self):
        """A-2 成功路径：夹取/归 None/raise_on_error 透传/reply 反查/日志。"""
        mgr = _FakeMgr()
        public_api._mysql_mgr = mgr
        public_api._config_mgr = mgr  # 查询日志存储（SQLite）同替身
        public_api._config_mgr = mgr  # 查询日志存储（SQLite）同替身
        result = await public_api.query_records(
            group_id="111", page=0, page_size=500, keyword=""
        )
        self.assertEqual(result["total"], 1)
        self.assertIsNotNone(result["records"][0].get("reply_message"))
        qc = mgr.query_calls[0]
        self.assertEqual(qc["page"], 1)  # page<1 归 1
        self.assertEqual(qc["page_size"], 200)  # >200 夹取 200
        self.assertIsNone(qc["keyword"])  # 空串归 None
        self.assertIs(qc.get("raise_on_error"), True)
        # 日志
        self.assertEqual(len(mgr.log_calls), 1)
        lc = mgr.log_calls[0]
        self.assertEqual(lc["method"], "query_records")
        self.assertIs(lc["success"], True)
        self.assertEqual(lc["result_count"], 1)
        self.assertEqual(lc["params"]["group_id"], "111")
        self.assertIsInstance(lc["cost_ms"], int)
        self.assertTrue(lc["caller"])  # 自动推断非空
        # 纯数据返回：records 中无 datetime 对象
        for rec in result["records"]:
            for v in rec.values():
                self.assertNotIsInstance(v, datetime)

    @async_test
    async def test_A3_explicit_caller(self):
        """A-3 显式 caller 优先。"""
        mgr = _FakeMgr()
        public_api._mysql_mgr = mgr
        public_api._config_mgr = mgr  # 查询日志存储（SQLite）同替身
        await public_api.query_records(caller="my_plugin")
        self.assertEqual(mgr.log_calls[0]["caller"], "my_plugin")

    def test_A3b_infer_caller_not_unknown(self):
        """A-3b 自动推断（测试脚本 __main__ 场景）非 unknown。"""
        caller = public_api._infer_caller()
        self.assertNotEqual(caller, "unknown")

    @async_test
    async def test_A4_failure_path(self):
        """A-4 查询异常：返回 _error 标记不抛底层异常 + 失败日志。"""
        mgr = _FakeMgr(query_error=RuntimeError("连接池已关闭"))
        public_api._mysql_mgr = mgr
        public_api._config_mgr = mgr  # 查询日志存储（SQLite）同替身
        result = await public_api.query_records(caller="plugin_x")
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["records"], [])
        self.assertIn("_error", result)
        self.assertIn("查询执行失败", result["_error"])
        lc = mgr.log_calls[0]
        self.assertIs(lc["success"], False)
        self.assertIn("连接池已关闭", lc["error_msg"])
        self.assertLessEqual(len(lc["error_msg"]), 500)
        self.assertEqual(lc["result_count"], 0)

    @async_test
    async def test_A4b_success_has_no_error_key(self):
        """A-4b 成功路径返回值不含 _error 键（与失败可区分）。"""
        mgr = _FakeMgr()
        public_api._mysql_mgr = mgr
        public_api._config_mgr = mgr  # 查询日志存储（SQLite）同替身
        result = await public_api.query_records(caller="plugin_x")
        self.assertNotIn("_error", result)

    @async_test
    async def test_A4c_invalid_time_format(self):
        """A-4c 时间参数格式非法：抛 PublicAPIError，不写查询日志。"""
        mgr = _FakeMgr()
        public_api._mysql_mgr = mgr
        public_api._config_mgr = mgr  # 查询日志存储（SQLite）同替身
        for bad in ("2026-08-11", "2026/08/11 10:00:00", "abc", "2026-13-99 99:99:99"):
            with self.assertRaises(public_api.PublicAPIError) as ctx:
                await public_api.query_records(caller="plugin_x", time_start=bad)
            self.assertIn("YYYY-MM-DD HH:MM:SS", str(ctx.exception))
        self.assertEqual(len(mgr.log_calls), 0, "参数错误不应写查询日志")
        # 合法格式不抛
        result = await public_api.query_records(
            caller="plugin_x", time_start="2026-08-11 10:00:00"
        )
        self.assertEqual(result["total"], 1)

    @async_test
    async def test_A5_log_failure_does_not_break_query(self):
        """A-5 日志写失败不影响查询结果。"""
        mgr = _FakeMgr(log_error=RuntimeError("日志表不可用"))
        public_api._mysql_mgr = mgr
        public_api._config_mgr = mgr  # 查询日志存储（SQLite）同替身
        result = await public_api.query_records(caller="plugin_y")
        self.assertEqual(result["total"], 1)

    @async_test
    async def test_A6_unregister(self):
        """A-6 注销（register_mysql_manager(None) + register_config_manager(None)，插件 terminate 调用）后抛 PublicAPIError。"""
        public_api.register_mysql_manager(_FakeMgr())
        public_api.register_config_manager(_FakeMgr())
        public_api.register_mysql_manager(None)
        public_api.register_config_manager(None)
        with self.assertRaises(public_api.PublicAPIError) as ctx:
            await public_api.query_records(caller="plugin_x")
        self.assertIn("尚未初始化", str(ctx.exception))


# ============================================================
# ============================================================
# B 组：core/db_config/query_log.py 查询日志数据层（真实 SQLite）
# ============================================================


class _SqliteHost(SqliteQueryLogMixin):
    """真实 aiosqlite 内存库宿主：提供 db + _ensure_db（建 query_log 两表）。

    QueryLogMixin 的 QUERY_LOG_DEFAULTS/TYPES/RANGES 类常量经 MRO 继承；
    连接登记到 _OPEN_CONNS，由 TestQueryLogMixin.tearDown 统一关闭
    （否则 aiosqlite 后台线程挂起导致测试进程不退出）。
    """

    _OPEN_CONNS: list = []

    def __init__(self):
        self.db = None

    async def close(self):
        if self.db is not None:
            await self.db.close()
            self.db = None

    async def _ensure_db(self):
        if self.db is None:
            self.db = await aiosqlite.connect(":memory:")
            self._OPEN_CONNS.append(self.db)
            await self.db.execute(
                "CREATE TABLE query_log ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " caller TEXT NOT NULL DEFAULT '',"
                " method TEXT NOT NULL DEFAULT '',"
                " params TEXT,"
                " result_count INTEGER NOT NULL DEFAULT 0,"
                " success INTEGER NOT NULL DEFAULT 1,"
                " error_msg TEXT,"
                " cost_ms INTEGER NOT NULL DEFAULT 0,"
                " created_at TEXT NOT NULL DEFAULT '')"
            )
            await self.db.execute(
                "CREATE TABLE query_log_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            await self.db.execute(
                "INSERT OR IGNORE INTO query_log_settings (key, value) VALUES (?, ?)",
                ("query_log_retention_days", "30"),
            )
            await self.db.commit()


class TestQueryLogMixin(unittest.TestCase):
    def tearDown(self):
        # 关闭本类用例创建的全部 aiosqlite 连接（防进程挂起）
        for conn in _SqliteHost._OPEN_CONNS:
            try:
                asyncio.run(conn.close())
            except Exception:
                pass
        _SqliteHost._OPEN_CONNS.clear()

    @async_test
    async def test_B1_insert_and_read(self):
        """B-1 insert：真实落库（JSON 中文保留、success→1/0、时间缺省 ISO 文本）。"""
        mixin = _SqliteHost()
        ok = await mixin.insert_query_log(
            caller="my_plugin",
            method="query_records",
            params={"group_id": "111", "keyword": "测试"},
            result_count=5,
            success=True,
            cost_ms=12,
        )
        self.assertTrue(ok)
        async with mixin.db.execute(
            "SELECT caller, method, params, result_count, success, error_msg,"
            " cost_ms, created_at FROM query_log"
        ) as cur:
            row = await cur.fetchone()
        self.assertEqual(row[0], "my_plugin")
        self.assertEqual(row[1], "query_records")
        self.assertIn("测试", row[2])  # JSON 中文原样（ensure_ascii=False）
        self.assertEqual(json.loads(row[2])["group_id"], "111")
        self.assertEqual(row[3], 5)
        self.assertEqual(row[4], 1)  # True → 1
        self.assertIsNone(row[5])
        self.assertEqual(row[6], 12)
        self.assertRegex(row[7], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

    @async_test
    async def test_B1b_insert_failure_bool(self):
        """B-1b success=False → 0、error_msg 落库。"""
        mixin = _SqliteHost()
        await mixin.insert_query_log(
            caller="x",
            method="query_records",
            params={},
            result_count=0,
            success=False,
            error_msg="boom",
            cost_ms=1,
        )
        async with mixin.db.execute("SELECT success, error_msg FROM query_log") as cur:
            row = await cur.fetchone()
        self.assertEqual(row[0], 0)
        self.assertEqual(row[1], "boom")

    @async_test
    async def test_B2_probability_cleanup(self):
        """B-2 概率清理：random<0.05 触发删除过期行、≥0.05 不触发。"""
        import random

        for prob, expect_cleanup in [(0.01, True), (0.99, False)]:
            mixin = _SqliteHost()
            await mixin._ensure_db()  # 先初始化连接再直接操作表
            # 先插一条过期行（created_at 远早于保留 30 天）
            await mixin.db.execute(
                "INSERT INTO query_log (caller, method, params, result_count,"
                " success, cost_ms, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("old", "query_records", "{}", 1, 1, 1, "2020-01-01 00:00:00"),
            )
            await mixin.db.commit()
            with mock.patch.object(random, "random", return_value=prob):
                await mixin.insert_query_log(
                    caller="x",
                    method="query_records",
                    params={},
                    result_count=1,
                    success=True,
                    cost_ms=1,
                )
            async with mixin.db.execute("SELECT COUNT(*) FROM query_log") as cur:
                total = (await cur.fetchone())[0]
            if expect_cleanup:
                self.assertEqual(total, 1, "prob=0.01 应清理过期行，剩新行")
            else:
                self.assertEqual(total, 2, "prob=0.99 不应清理")

    @async_test
    async def test_B3_query(self):
        """B-3 查询：模糊匹配/LIKE 转义/method 精确/时间闭区间/排序/布尔化。

        注意：caller 参数中的 % _ 按转义语义作**字面**匹配（与 chat_history
        query_messages 同范式）——模糊意图直接传普通子串。
        """
        mixin = _SqliteHost()
        await mixin.insert_query_log(
            caller="plugin_a",
            method="query_records",
            params={},
            result_count=1,
            success=True,
            cost_ms=2,
            created_at=datetime(2026, 8, 11, 9, 0, 0),
        )
        await mixin.insert_query_log(
            caller="plugin_b",
            method="query_records",
            params={},
            result_count=3,
            success=False,
            error_msg="boom",
            cost_ms=5,
            created_at=datetime(2026, 8, 11, 10, 0, 0),
        )
        await mixin.insert_query_log(
            caller="100%_special",
            method="query_records",
            params={},
            result_count=0,
            success=True,
            cost_ms=1,
            created_at=datetime(2026, 8, 11, 11, 0, 0),
        )
        # 模糊匹配普通子串：命中 plugin_a/plugin_b，不含 100%_special
        result = await mixin.query_query_logs(
            page=0,
            page_size=999,
            caller="plugin",
            method="query_records",
            time_start="2026-08-11 00:00:00",
            time_end="2026-08-11 23:59:59",
        )
        self.assertEqual(result["total"], 2)
        self.assertEqual(len(result["records"]), 2)
        # 排序 created_at DESC：100%_special 被时间区间排除后，plugin_b 在前
        self.assertEqual(result["records"][0]["caller"], "plugin_b")
        # 布尔化 / 字符串化
        self.assertIs(result["records"][0]["success"], False)
        self.assertIs(result["records"][1]["success"], True)
        self.assertEqual(result["records"][0]["created_at"], "2026-08-11 10:00:00")
        self.assertIsNone(result["records"][1]["error_msg"])
        self.assertEqual(result["records"][0]["cost_ms"], 5)
        # LIKE 转义：% 与 _ 按字面匹配——"100%" 命中含字面 % 的行，
        # 不命中 plugin_a/plugin_b（无字面 %）
        esc = await mixin.query_query_logs(caller="100%")
        self.assertEqual(esc["total"], 1)
        self.assertEqual(esc["records"][0]["caller"], "100%_special")

    @async_test
    async def test_B3b_query_empty_and_pagination(self):
        """B-3b 空表 + 分页 offset。"""
        mixin = _SqliteHost()
        result = await mixin.query_query_logs()
        self.assertEqual(result, {"total": 0, "records": []})
        # 造 3 条验证分页：page=2 size=1 → 取第 2 条（DESC 序）
        for i in range(3):
            await mixin.insert_query_log(
                caller="p" + str(i),
                method="query_records",
                params={},
                result_count=1,
                success=True,
                cost_ms=1,
                created_at=datetime(2026, 8, 10, 10, i, 0),
            )
        r2 = await mixin.query_query_logs(page=2, page_size=1)
        self.assertEqual(r2["total"], 3)
        self.assertEqual(len(r2["records"]), 1)
        self.assertEqual(r2["records"][0]["caller"], "p1")  # DESC: p2, p1, p0

    @async_test
    async def test_B4_cleanup(self):
        """B-4 cleanup：删除过期行返回删除数；异常返回 0（warning）。

        v0.9.0 测试漂移同步：insert_query_log 自带 5% 概率顺带清理
        （B-2 已专测），不打桩时本用例存在 ~5% 概率被顺带清理抢先删除
        过期行导致 cleanup 返回 0 的偶发假失败——显式关闭概率路径，
        使断言确定化（仅测试侧修复，不改生产代码）。
        """
        import random as _stdlib_random

        mixin = _SqliteHost()
        with mock.patch.object(_stdlib_random, "random", return_value=0.99):
            await mixin.insert_query_log(
                caller="old",
                method="query_records",
                params={},
                result_count=1,
                success=True,
                cost_ms=1,
                created_at=datetime(2020, 1, 1),
            )
            await mixin.insert_query_log(
                caller="new",
                method="query_records",
                params={},
                result_count=1,
                success=True,
                cost_ms=1,
                created_at=datetime(2026, 8, 11),
            )
        n = await mixin.cleanup_query_logs("2026-01-01 00:00:00")
        self.assertEqual(n, 1)
        async with mixin.db.execute("SELECT caller FROM query_log") as cur:
            rows = await cur.fetchall()
        self.assertEqual([r[0] for r in rows], ["new"])

        # 异常路径：_ensure_db 抛异常 → 返回 0 + warning
        class _BadHost(SqliteQueryLogMixin):
            async def _ensure_db(self):
                raise RuntimeError("db down")

        _STUB_LOGGER.clear()
        n2 = await _BadHost().cleanup_query_logs("2026-01-01 00:00:00")
        self.assertEqual(n2, 0)
        self.assertTrue(
            any("清理过期查询日志失败" in w for w in _STUB_LOGGER.records["warning"])
        )

    @async_test
    async def test_B5_retention_settings(self):
        """B-5 保留天数：默认 30、set 生效、越界夹取。"""
        mixin = _SqliteHost()
        self.assertEqual(await mixin.get_query_log_retention_days(), 30)
        self.assertTrue(await mixin.set_query_log_retention_days(7))
        self.assertEqual(await mixin.get_query_log_retention_days(), 7)
        # 越界夹取
        self.assertTrue(await mixin.set_query_log_retention_days(99999))
        self.assertEqual(await mixin.get_query_log_retention_days(), 3650)
        self.assertTrue(await mixin.set_query_log_retention_days(0))
        self.assertEqual(await mixin.get_query_log_retention_days(), 1)
        # 默认值常量（类属性）
        self.assertEqual(
            SqliteQueryLogMixin.QUERY_LOG_DEFAULTS["query_log_retention_days"], "30"
        )
        self.assertEqual(QUERY_LOG_DEFAULT_RETENTION_DAYS, 30)

    @async_test
    async def test_B6_query_error_returns_empty(self):
        """B-6 查询异常返回空结果并记 error（不向上抛）。"""

        class _BadHost(SqliteQueryLogMixin):
            async def _ensure_db(self):
                raise RuntimeError("db down")

        _STUB_LOGGER.clear()
        result = await _BadHost().query_query_logs()
        self.assertEqual(result, {"total": 0, "records": []})
        self.assertTrue(
            any("查询查询日志失败" in e for e in _STUB_LOGGER.records["error"])
        )


# C 组：core/webapi/query_log.py Web API 端点
# ============================================================


class TestWebQueryLog(unittest.TestCase):
    def _make_endpoint(self, query: dict, mgr=None):
        """构造 api_query_log_list 可直接调用的宿主。

        query_log.py 顶层 `from astrbot.api.web import request` 绑定的是导入时
        的 request 对象，需直接 patch 模块属性（真实运行时由框架绑定全局单例）。
        v0.7.0 起端点经 config_mgr（SQLite 查询日志存储）读数据。
        """
        import astrbot_plugin_group_history_save_mysql.core.webapi.query_log as ql_mod

        orig_request = ql_mod.request
        ql_mod.request = _FakeRequest()
        ql_mod.request.query = dict(query)

        class Host(WebQueryLogMixin):
            def __init__(self, mgr):
                self.config_mgr = mgr

        host = Host(mgr or _FakeMgr())
        self._restore = lambda: setattr(ql_mod, "request", orig_request)
        return host

    @async_test
    async def test_C1_defaults(self):
        """C-1 默认参数：page=1、page_size=100。"""
        mgr = _FakeMgr()
        host = self._make_endpoint({}, mgr)
        resp = await host.api_query_log_list()
        self._restore()
        self.assertIn("total", resp["json"])
        self.assertEqual(mgr.query_calls[0]["page"], 1)
        self.assertEqual(mgr.query_calls[0]["page_size"], 100)
        self.assertIsNone(mgr.query_calls[0]["caller"])

    @async_test
    async def test_C2_invalid_int_fallback(self):
        """C-2 非法 int 回退默认。"""
        mgr = _FakeMgr()
        host = self._make_endpoint({"page": "abc", "page_size": "xyz"}, mgr)
        await host.api_query_log_list()
        self._restore()
        self.assertEqual(mgr.query_calls[0]["page"], 1)
        self.assertEqual(mgr.query_calls[0]["page_size"], 100)

    @async_test
    async def test_C2b_clamp(self):
        """C-2b page<1 归 1、page_size 上限 200。"""
        mgr = _FakeMgr()
        host = self._make_endpoint({"page": "-5", "page_size": "999"}, mgr)
        await host.api_query_log_list()
        self._restore()
        self.assertEqual(mgr.query_calls[0]["page"], 1)
        self.assertEqual(mgr.query_calls[0]["page_size"], 200)

    @async_test
    async def test_C3_filters(self):
        """C-3 空串归 None、时间透传。"""
        mgr = _FakeMgr()
        host = self._make_endpoint(
            {
                "caller": "  plugin  ",
                "method": "",
                "time_start": "2026-08-11 00:00:00",
                "time_end": "",
            },
            mgr,
        )
        await host.api_query_log_list()
        self._restore()
        qc = mgr.query_calls[0]
        self.assertEqual(qc["caller"], "plugin")  # strip 后传
        self.assertIsNone(qc["method"])  # 空串归 None
        self.assertEqual(qc["time_start"], "2026-08-11 00:00:00")
        self.assertEqual(qc["time_end"], "")  # 透传（底层空串不生效）

    def test_C4_route_registered(self):
        """C-4 路由注册存在（列表 + 设置读写）。"""
        base_src = (_PLUGIN_ROOT / "core" / "webapi" / "base.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("query_log/list", base_src)
        self.assertIn("api_query_log_list", base_src)
        self.assertIn("query_log/settings", base_src)
        self.assertIn("api_query_log_settings_get", base_src)
        self.assertIn("api_query_log_settings_save", base_src)
        # WebAPI 组装含该 Mixin
        self.assertTrue(hasattr(WebAPI, "api_query_log_list"))
        self.assertTrue(hasattr(WebAPI, "api_query_log_settings_get"))
        self.assertTrue(hasattr(WebAPI, "api_query_log_settings_save"))

    @async_test
    async def test_C5_settings_get(self):
        """C-5 设置读取端点：返回 retention_days。"""

        class _SettingsMgr(_FakeMgr):
            def __init__(self, days):
                super().__init__()
                self.days = days
                self.saved_days = None

            async def get_query_log_retention_days(self):
                return self.days

            async def set_query_log_retention_days(self, days):
                self.saved_days = days
                return True

        mgr = _SettingsMgr(45)
        host = self._make_endpoint({}, mgr)
        resp = await host.api_query_log_settings_get()
        self._restore()
        self.assertEqual(resp["json"]["retention_days"], 45)

    @async_test
    async def test_C6_settings_save(self):
        """C-6 设置保存端点：读 JSON、委托 set、返回 success；非法值拒绝。"""
        import astrbot_plugin_group_history_save_mysql.core.webapi.query_log as ql_mod

        class _SettingsMgr(_FakeMgr):
            def __init__(self, days):
                super().__init__()
                self.days = days
                self.saved_days = None

            async def get_query_log_retention_days(self):
                return self.days

            async def set_query_log_retention_days(self, days):
                self.saved_days = days
                return True

        mgr = _SettingsMgr(30)

        async def _fake_json(default=None):
            return {"retention_days": 7}

        orig_json = getattr(ql_mod.request, "json", None)
        host = self._make_endpoint({}, mgr)
        ql_mod.request.json = _fake_json
        resp = await host.api_query_log_settings_save()
        self._restore()
        if orig_json is not None:
            ql_mod.request.json = orig_json
        self.assertTrue(resp["json"]["success"])
        self.assertEqual(mgr.saved_days, 7)

        # 非法值：非整数 → success=False，不写入
        async def _bad_json(default=None):
            return {"retention_days": "abc"}

        host2 = self._make_endpoint({}, mgr)
        ql_mod.request.json = _bad_json
        resp2 = await host2.api_query_log_settings_save()
        self._restore()
        if orig_json is not None:
            ql_mod.request.json = orig_json
        self.assertFalse(resp2["json"]["success"])
        self.assertEqual(mgr.saved_days, 7, "非法值不应写入")


# ============================================================
# D 组：接线与版本
# ============================================================


class TestWiring(unittest.TestCase):
    def test_D1_config_manager_assembly(self):
        """D-1 查询日志迁移 SQLite：ConfigManager 组装含全部查询日志方法。"""
        for method in (
            "insert_query_log",
            "query_query_logs",
            "cleanup_query_logs",
            "get_query_log_retention_days",
            "set_query_log_retention_days",
        ):
            self.assertTrue(hasattr(ConfigManager, method), method)

    def test_D1b_mysql_manager_no_query_log(self):
        """D-1b MySQLManager 不再含查询日志方法（已迁移 SQLite）。"""
        for method in ("insert_query_log", "query_query_logs", "cleanup_query_logs"):
            self.assertFalse(hasattr(MySQLManager, method), method)

    def test_D1c_webapi_assembly(self):
        """D-1c WebAPI 组装含查询日志端点。"""
        self.assertTrue(hasattr(WebAPI, "api_query_log_list"))
        self.assertTrue(hasattr(WebAPI, "api_query_log_settings_get"))
        self.assertTrue(hasattr(WebAPI, "api_query_log_settings_save"))

    def test_D2_version(self):
        """D-2 版本号：main.py @register 与 metadata.yaml 同步当前版本。

        v0.9.0 漂移同步：版本文本断言随 @register / metadata.yaml 演进
        （0.7.0 → 0.9.0），测的是「两处版本一致」这一接线事实。
        """
        main_src = (_PLUGIN_ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn('"0.9.0"', main_src)
        meta = (_PLUGIN_ROOT / "metadata.yaml").read_text(encoding="utf-8")
        self.assertIn("version: v0.9.0", meta)

    def test_D3_frontend(self):
        """D-3 前端：query-log tab、保留天数设置、?v= 静态引用。

        v0.9.0 漂移同步：缓存版本参数随前端升版（?v=0.7.0 → ?v=0.9.0）。
        """
        html = (_PLUGIN_ROOT / "pages" / "dashboard" / "index.html").read_text(
            encoding="utf-8"
        )
        self.assertIn('data-tab="query-log"', html)
        self.assertIn("?v=0.9.0", html)
        self.assertNotIn("?v=0.7.0", html)
        # 保留天数设置控件
        self.assertIn('id="qlRetentionDays"', html)
        self.assertIn('id="qlSaveSettingsBtn"', html)
        js = (_PLUGIN_ROOT / "pages" / "dashboard" / "storage.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("query_log/list", js)
        self.assertIn("query_log/settings", js)
        self.assertIn("saveQueryLogSettings", js)

    def test_D4_query_messages_raise_on_error_contract(self):
        """D-4 query_messages 新增 raise_on_error 参数存在（默认 False）。"""
        import inspect as _inspect

        sig = _inspect.signature(ChatHistoryMixin.query_messages)
        self.assertIn("raise_on_error", sig.parameters)
        self.assertIs(sig.parameters["raise_on_error"].default, False)


# ============================================================
# E 组：count_messages 消息统计接口（数据层 + 对外 API）
# ============================================================


class TestCountMessages(unittest.TestCase):
    """E 组：public_api.count_messages 对外接口全分支。"""

    def setUp(self):
        self._orig_mgr = public_api._mysql_mgr
        self._orig_cfg = public_api._config_mgr

    def tearDown(self):
        public_api._mysql_mgr = self._orig_mgr
        public_api._config_mgr = self._orig_cfg

    @async_test
    async def test_E1_uninitialized_raises(self):
        """E-1 未初始化抛 PublicAPIError。"""
        public_api._mysql_mgr = None
        public_api._config_mgr = None
        with self.assertRaises(public_api.PublicAPIError):
            await public_api.count_messages(group_id="111")

    @async_test
    async def test_E2_missing_args(self):
        """E-2 group_id 与 sender_ids 都未提供 → PublicAPIError。"""
        mgr = _FakeMgr()
        public_api._mysql_mgr = mgr
        public_api._config_mgr = mgr
        with self.assertRaises(public_api.PublicAPIError) as ctx:
            await public_api.count_messages()
        self.assertIn("至少提供", str(ctx.exception))
        self.assertEqual(len(mgr.log_calls), 0, "参数错误不应写日志")

    @async_test
    async def test_E3_sender_limit(self):
        """E-3 sender_ids 超 500 → PublicAPIError。"""
        mgr = _FakeMgr()
        public_api._mysql_mgr = mgr
        public_api._config_mgr = mgr
        with self.assertRaises(public_api.PublicAPIError) as ctx:
            await public_api.count_messages(sender_ids=[str(i) for i in range(501)])
        self.assertIn("500", str(ctx.exception))

    @async_test
    async def test_E4_invalid_time(self):
        """E-4 时间格式非法 → PublicAPIError，不写日志。"""
        mgr = _FakeMgr()
        public_api._mysql_mgr = mgr
        public_api._config_mgr = mgr
        with self.assertRaises(public_api.PublicAPIError):
            await public_api.count_messages(group_id="111", time_start="2026/08/01")
        self.assertEqual(len(mgr.log_calls), 0)

    @async_test
    async def test_E5_success_batch(self):
        """E-5 成功路径（批量）：sender_ids 归一、参数透传、日志写入。"""
        mgr = _FakeMgr()
        public_api._mysql_mgr = mgr
        public_api._config_mgr = mgr
        result = await public_api.count_messages(
            caller="my_plugin",
            group_id="111",
            sender_ids=["a", "b", ""],  # 空值过滤
            time_start="2026-08-01 00:00:00",
            time_end="2026-08-31 23:59:59",
        )
        self.assertEqual(result["group_total"], 42)
        self.assertIn("senders", result)
        cc = mgr.count_calls[0]
        self.assertEqual(cc["group_id"], "111")
        self.assertEqual(cc["sender_ids"], ["a", "b"])  # 空值已过滤
        self.assertEqual(cc["time_start"], "2026-08-01 00:00:00")
        # 日志
        lc = mgr.log_calls[0]
        self.assertEqual(lc["method"], "count_messages")
        self.assertIs(lc["success"], True)
        self.assertEqual(lc["caller"], "my_plugin")
        self.assertEqual(lc["result_count"], 2)
        self.assertEqual(lc["params"]["sender_ids_count"], 2)
        self.assertEqual(lc["params"]["sender_ids_sample"], ["a", "b"])
        self.assertIsInstance(lc["cost_ms"], int)

    @async_test
    async def test_E5b_success_single_str(self):
        """E-5b 单人（str sender_ids）自动转列表。"""
        mgr = _FakeMgr()
        public_api._mysql_mgr = mgr
        public_api._config_mgr = mgr
        result = await public_api.count_messages(sender_ids="a")
        self.assertIn("senders", result)
        self.assertEqual(mgr.count_calls[0]["sender_ids"], ["a"])
        # 未给 group_id：数据层返回不含 group_total
        self.assertNotIn("group_total", result)

    @async_test
    async def test_E6_failure_path(self):
        """E-6 查询失败：返回 _error + 失败日志。"""
        mgr = _FakeMgr(count_error=RuntimeError("连接池已关闭"))
        public_api._mysql_mgr = mgr
        public_api._config_mgr = mgr
        result = await public_api.count_messages(group_id="111", sender_ids=["a"])
        self.assertEqual(result["group_total"], 0)
        self.assertEqual(result["senders"], {})
        self.assertIn("_error", result)
        lc = mgr.log_calls[0]
        self.assertIs(lc["success"], False)
        self.assertIn("连接池已关闭", lc["error_msg"])

    @async_test
    async def test_E7_log_failure_degrades(self):
        """E-7 日志写失败不影响统计结果。"""
        mgr = _FakeMgr(log_error=RuntimeError("日志表不可用"))
        public_api._mysql_mgr = mgr
        public_api._config_mgr = mgr
        result = await public_api.count_messages(group_id="111", sender_ids=["a"])
        self.assertEqual(result["group_total"], 42)


class TestCountMessagesDataLayer(unittest.TestCase):
    """E 组数据层：ChatHistoryMixin.count_messages 聚合与 SQL。"""

    @async_test
    async def test_E8_sql_and_assembly(self):
        """E-8 三组计数并行、GROUP BY 单条、结果组装。"""
        from astrbot_plugin_group_history_save_mysql.core.db_mysql.chat_history import (
            ChatHistoryMixin,
        )

        # 并发安全 stub：每次 acquire 返回独立 cursor（真实 MySQL 同语义），
        # 各 cursor 按 SQL 特征返回对应数据，无共享 iter 竞争
        shared_calls: list = []

        class _Cur:
            def __init__(self, shared):
                self.calls = shared
                self._data = None

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def execute(self, sql, params=None):
                self.calls.append((sql, tuple(params) if params else ()))
                if "COUNT(*)" in sql and "GROUP BY" not in sql:
                    self._data = ("one", (42,))  # 真实 MySQL fetchone 返回元组
                elif "GROUP BY" in sql:
                    if "group_id = %s" in sql:
                        self._data = ("all", [("a", "10"), ("b", "3")])
                    else:
                        self._data = ("all", [("a", "30"), ("b", "7")])
                return 0

            async def fetchone(self):
                kind, val = self._data
                return val if kind == "one" else None

            async def fetchall(self):
                kind, val = self._data
                return val if kind == "all" else []

        class _Conn:
            def __init__(self, cur):
                self._cur = cur

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def cursor(self, *a, **k):
                return self._cur

        class _Pool:
            def __init__(self, shared):
                self._shared = shared

            def acquire(self):
                return _Conn(_Cur(self._shared))

        class Host(ChatHistoryMixin):
            def __init__(self, shared):
                self.pool = _Pool(shared)

            async def _execute(self, cur, sql, params=None, timeout=None):
                await cur.execute(sql, params)
                return 0

        host = Host(shared_calls)
        result = await host.count_messages(
            group_id="111",
            sender_ids=["a", "b"],
            time_start="2026-08-01 00:00:00",
            time_end="2026-08-31 23:59:59",
        )
        self.assertEqual(result["group_total"], 42)
        self.assertEqual(result["senders"]["a"], {"in_group": 10, "total": 30})
        self.assertEqual(result["senders"]["b"], {"in_group": 3, "total": 7})
        # SQL 断言：3 条查询，GROUP BY 单条聚合（无 N+1 循环）
        sqls = [c[0] for c in shared_calls]
        self.assertEqual(len(sqls), 3)
        self.assertIn("SELECT COUNT(*) FROM chat_history WHERE group_id = %s", sqls[0])
        self.assertIn("GROUP BY sender_id", sqls[1])
        self.assertIn("GROUP BY sender_id", sqls[2])
        self.assertIn("sender_id IN (%s,%s)", sqls[1])
        # 时间条件参数化透传
        self.assertIn("2026-08-01 00:00:00", shared_calls[0][1])

    @async_test
    async def test_E9_group_only(self):
        """E-9 仅群维度：只跑群总计 COUNT。

        v0.9.0 测试漂移同步：原缺失 @async_test 装饰器导致 pytest/unittest
        下协程从未被 await（RuntimeWarning），断言实际未执行——补齐装饰器
        使本用例真实生效。
        """
        from astrbot_plugin_group_history_save_mysql.core.db_mysql.chat_history import (
            ChatHistoryMixin,
        )

        shared_calls: list = []

        class _Cur:
            def __init__(self, shared):
                self.calls = shared
                self._data = None

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def execute(self, sql, params=None):
                self.calls.append((sql, tuple(params) if params else ()))
                self._data = ("one", (99,))  # 真实 MySQL fetchone 返回元组
                return 0

            async def fetchone(self):
                kind, val = self._data
                return val if kind == "one" else None

            async def fetchall(self):
                return []

        class _Conn:
            def __init__(self, cur):
                self._cur = cur

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def cursor(self, *a, **k):
                return self._cur

        class _Pool:
            def __init__(self, shared):
                self._shared = shared

            def acquire(self):
                return _Conn(_Cur(self._shared))

        class Host(ChatHistoryMixin):
            def __init__(self, shared):
                self.pool = _Pool(shared)

            async def _execute(self, cur, sql, params=None, timeout=None):
                await cur.execute(sql, params)
                return 0

        host = Host(shared_calls)
        result = await host.count_messages(group_id="111")
        self.assertEqual(result["group_total"], 99)
        self.assertNotIn("senders", result)
        self.assertEqual(len(shared_calls), 1, "仅群维度只发一条 COUNT")


if __name__ == "__main__":
    unittest.main(verbosity=2)
