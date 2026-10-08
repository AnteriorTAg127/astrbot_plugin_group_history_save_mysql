# ruff: noqa: I001
"""astrbot_plugin_group_history_save_mysql v0.7.1 测试：Web 查询导出。

本版（v0.7.1）新增：Web 管理后台「查询」页导出——数据层
``ChatHistoryMixin.export_messages``（复用抽取的 ``_build_chat_history_where``，
LIMIT 上限）、端点 ``QueryMixin.api_query_export``（CSV/JSON）、前端导出按钮。

沿用 v0.7.0 测试范式：@async_test（asyncio.run）；astrbot.* / aiomysql 全部
sys.modules stub 且注入先于被测导入；核心逻辑用真实模块断言。

A 组（core/db_mysql/chat_history.py 数据层）：
- A-1 _build_chat_history_where：各条件组合 + keyword LIKE 转义
- A-2 _build_chat_history_where：无条件返回 "1=1"
- A-3 export_messages：WHERE + LIMIT 参数、timestamp 字符串化
- A-4 export_messages：limit<=0 返回 []（不触碰 pool）
- A-5 export_messages：异常返回 [] 并记 error 日志
- A-6 query_messages 回归：COUNT + LIMIT/OFFSET 参数（WHERE 重构后行为不变）

B 组（core/webapi/query.py 端点）：
- B-1 _records_to_csv：BOM/列序/None 空串/reply 扁平/逗号引号换行转义
- B-2 api_query_export：format 非法 400
- B-3 api_query_export：CSV 成功路径（file_response 文件名/content_type）
- B-4 api_query_export：JSON 成功路径 + limit 夹取 [1, 500000]

C 组（接线与版本）：
- C-1 base.py 路由含 query/export、WebAPI 组装含 api_query_export
- C-2 metadata.yaml version=v0.7.1
- C-3 前端 index.html 含导出按钮 + ?v=0.7.1

运行方式（插件根目录）：
    python -m pytest "tests/v0.7.1/test_v071.py" -v
    PYTHONIOENCODING=utf-8 python "tests/v0.7.1/test_v071.py"
"""

import asyncio
import csv
import functools
import io
import sys
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock


def async_test(fn):
    """装饰器：用 asyncio.run 运行异步测试函数（环境无 pytest-asyncio 依赖）。"""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


_PKG = "astrbot_plugin_group_history_save_mysql"
_TEST_DIR = Path(__file__).resolve().parent
_PLUGIN_ROOT = _TEST_DIR.parents[1]  # tests/v0.7.1 → 插件根
_PLUGINS_DIR = str(_PLUGIN_ROOT.parent)  # data/plugins（包导入根）


def _new_module(name: str) -> types.ModuleType:
    return types.ModuleType(name)


# ============================================================
# 一、astrbot.* / aiomysql stub 注入（必须先于任何被测导入）
# ============================================================
_astrbot = _new_module("astrbot")
_astrbot_api = _new_module("astrbot.api")
_astrbot_star = _new_module("astrbot.api.star")
_astrbot_web = _new_module("astrbot.api.web")
_astrbot_core = _new_module("astrbot.core")
_astrbot_core_utils = _new_module("astrbot.core.utils")
_astrbot_core_utils_astrbot_path = _new_module("astrbot.core.utils.astrbot_path")
_astrbot_core_utils_io = _new_module("astrbot.core.utils.io")


class _StubLogger:
    """记录各级日志。"""

    def __init__(self):
        self.records = {"info": [], "warning": [], "error": [], "debug": []}

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


_astrbot_api.logger = _StubLogger()


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
        return _PLUGIN_ROOT / "data" / "tmp_data"


_astrbot_star.Context = _StubContext
_astrbot_star.Star = _StubStar
_astrbot_star.register = _stub_register
_astrbot_star.StarTools = _StubStarTools

_astrbot_core_utils_io.save_temp_img = lambda *a, **k: ""


class _StubFileTokenService:
    """astrbot.core.file_token_service 替身：记录注册/消费、返回固定 token。"""

    def __init__(self):
        self.registered: list[tuple[str, object]] = []
        self.handled: list[str] = []

    async def register_file(self, file_path, timeout=None):
        self.registered.append((file_path, timeout))
        return "tok_123"

    async def handle_file(self, file_token):
        self.handled.append(file_token)
        raise KeyError("Invalid or expired file token")


_astrbot_core.file_token_service = _StubFileTokenService()

_astrbot_web.error_response = lambda msg="", **k: {
    "error": msg,
    "status": k.get("status_code"),
}
_astrbot_web.json_response = lambda obj=None, **k: {"json": obj}
_astrbot_web.file_response = lambda path, **k: {
    "file": path,
    "filename": k.get("filename"),
    "content_type": k.get("content_type"),
}
_astrbot_web.request = None  # 模块顶层绑定，测试内 patch

_astrbot_core_utils_astrbot_path.get_astrbot_temp_path = lambda: str(
    _PLUGIN_ROOT / "data" / "tmp_export"
)

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
sys.modules["astrbot.api.star"] = _astrbot_star
sys.modules["astrbot.api.web"] = _astrbot_web
sys.modules["astrbot.core"] = _astrbot_core
sys.modules["astrbot.core.utils"] = _astrbot_core_utils
sys.modules["astrbot.core.utils.astrbot_path"] = _astrbot_core_utils_astrbot_path
sys.modules["astrbot.core.utils.io"] = _astrbot_core_utils_io
sys.modules["aiomysql"] = _aiomysql

if _PLUGINS_DIR not in sys.path:
    sys.path.insert(0, _PLUGINS_DIR)

# ============================================================
# 二、导入真实被测模块
# ============================================================
from astrbot_plugin_group_history_save_mysql.core.db_mysql.chat_history import (  # noqa: E402
    EXPORT_LIMIT_DEFAULT,
    EXPORT_LIMIT_MAX,
    ChatHistoryMixin,
)
from astrbot_plugin_group_history_save_mysql.core.webapi import WebAPI  # noqa: E402
from astrbot_plugin_group_history_save_mysql.core.webapi import query as web_query  # noqa: E402


# ============================================================
# 三、共享替身（fake cursor / conn / pool）
# ============================================================


class _FakeCursor:
    """记录 execute SQL/参数；fetchone/fetchall 返回预设。"""

    def __init__(self, fetchone_result=None, fetchall_result=None):
        self.calls: list[tuple[str, tuple]] = []
        self._fetchone_result = fetchone_result
        self._fetchall_result = fetchall_result

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
        return self._conn


class _ChatHistoryHost(ChatHistoryMixin):
    """ChatHistoryMixin 挂载宿主：提供 pool + _execute + _log_op_error。"""

    def __init__(self, cursor):
        self.pool = _FakePool(_FakeConn(cursor))
        self._execute_calls: list[tuple[str, tuple]] = []
        self._log_errors: list[str] = []

    async def _execute(self, cur, sql, params=None, timeout=None):
        self._execute_calls.append((sql, tuple(params) if params else ()))
        return await cur.execute(sql, params)

    def _log_op_error(self, key, action, exc):
        self._log_errors.append(action)


class _FakeRequest:
    """webapi 端点用 request 替身。"""

    query: dict = {}


def _make_endpoint(query: dict):
    """构造 api_query_export 可直接调用的宿主，并 patch 模块 request。"""
    req = _FakeRequest()
    req.query = dict(query)
    web_query.request = req

    class Host(web_query.QueryMixin):
        def __init__(self, mgr):
            self.mysql_mgr = mgr

    return Host, req


# ============================================================
# A 组：数据层
# ============================================================


class TestWhereBuilder(unittest.TestCase):
    def test_A1_all_conditions_and_keyword_escape(self):
        """A-1 全条件组合 + keyword LIKE 转义。"""
        where, params = ChatHistoryMixin._build_chat_history_where(
            group_id="111",
            sender_id="222",
            time_start="2026-08-01 00:00:00",
            time_end="2026-08-13 23:59:59",
            keyword="a%b_c\\d",
        )
        self.assertEqual(
            where,
            "group_id = %s AND sender_id = %s AND timestamp >= %s "
            "AND timestamp <= %s AND (content LIKE %s OR sender_name LIKE %s)",
        )
        # 转义后：a\%b\_c\\d（% _ 前加反斜杠、原反斜杠翻倍）
        self.assertEqual(params[-2], "%a\\%b\\_c\\\\d%")
        self.assertEqual(params[-2], params[-1])
        self.assertEqual(
            params[:-2],
            ["111", "222", "2026-08-01 00:00:00", "2026-08-13 23:59:59"],
        )

    def test_A2_no_conditions(self):
        """A-2 无条件返回 1=1。"""
        where, params = ChatHistoryMixin._build_chat_history_where(
            None, None, None, None, None
        )
        self.assertEqual(where, "1=1")
        self.assertEqual(params, [])


class TestExportMessages(unittest.TestCase):
    def _make_host(self, fetchone=None, fetchall=None):
        return _ChatHistoryHost(_FakeCursor(fetchone, fetchall))

    @async_test
    async def test_A3_where_and_limit(self):
        """A-3 export_messages：WHERE + LIMIT 参数、timestamp 字符串化。"""
        row = {
            "id": 1,
            "timestamp": datetime(2026, 8, 13, 14, 30, 0),
            "group_id": "111",
            "sender_id": "222",
            "sender_name": "张三",
            "message_type": "text",
            "content": "hi",
            "message_id": "m1",
            "at_list": "",
            "reply_id": "r9",
        }
        host = self._make_host(fetchall=[row])
        records = await host.export_messages(
            group_id="111",
            keyword="a%b",
            limit=10,
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["timestamp"], "2026-08-13 14:30:00")
        # 仅一次 execute：WHERE + LIMIT（无 COUNT/OFFSET）
        self.assertEqual(len(host._execute_calls), 1)
        sql, params = host._execute_calls[0]
        self.assertIn("LIMIT %s", sql)
        self.assertNotIn("OFFSET", sql)
        self.assertEqual(params, ("111", "%a\\%b%", "%a\\%b%", 10))

    @async_test
    async def test_A4_non_positive_limit(self):
        """A-4 limit<=0 返回 []，不触碰 pool。"""
        host = self._make_host()
        records = await host.export_messages(limit=0)
        self.assertEqual(records, [])
        self.assertEqual(host._execute_calls, [])

    @async_test
    async def test_A5_error_returns_empty(self):
        """A-5 异常返回 [] 并记 error 日志。"""
        host = self._make_host(fetchall=[{"id": 1, "timestamp": "x"}])
        # 让 cursor.execute 抛异常
        host.pool = _FakePool(_FakeConn(_BoomCursor()))
        records = await host.export_messages()
        self.assertEqual(records, [])
        self.assertIn("导出聊天记录", host._log_errors)

    @async_test
    async def test_A6_query_messages_regression(self):
        """A-6 query_messages 回归：COUNT + LIMIT/OFFSET 参数不变。"""
        host = self._make_host(fetchone={"total": 1}, fetchall=[])
        await host.query_messages(group_id="111", page=2, page_size=25)
        self.assertEqual(len(host._execute_calls), 2)
        count_sql, count_params = host._execute_calls[0]
        self.assertIn("COUNT(*)", count_sql)
        self.assertEqual(count_params, ("111",))
        page_sql, page_params = host._execute_calls[1]
        self.assertIn("LIMIT %s OFFSET %s", page_sql)
        self.assertEqual(page_params, ("111", 25, 25))  # page=2 → offset 25


class _BoomCursor(_FakeCursor):
    async def execute(self, sql, params=None):
        raise RuntimeError("boom")


class _SeqCursor(_FakeCursor):
    """按 execute 顺序依次返回预设的 fetchall 结果（分块查询测试用）。"""

    def __init__(self, fetchall_results):
        super().__init__(fetchall_result=None)
        self._results = list(fetchall_results)
        self._i = 0

    async def fetchall(self):
        result = self._results[self._i] if self._i < len(self._results) else []
        self._i += 1
        return result


class TestGetMessagesByIds(unittest.TestCase):
    @async_test
    async def test_A7_chunked(self):
        """A-7 get_messages_by_ids 分块：1200 id → 3 次 execute，结果合并。"""
        ids = [f"m{i}" for i in range(1200)]
        cursor = _SeqCursor(
            [
                [
                    {
                        "message_id": f"m{i}",
                        "timestamp": "2026-08-13 10:00:00",
                        "content": f"c{i}",
                    }
                    for i in range(500)
                ],
                [
                    {
                        "message_id": f"m{i}",
                        "timestamp": "2026-08-13 10:00:00",
                        "content": f"c{i}",
                    }
                    for i in range(500, 1000)
                ],
                [
                    {
                        "message_id": f"m{i}",
                        "timestamp": "2026-08-13 10:00:00",
                        "content": f"c{i}",
                    }
                    for i in range(1000, 1200)
                ],
            ]
        )
        host = _ChatHistoryHost(cursor)
        records = await host.get_messages_by_ids(ids)
        self.assertEqual(len(records), 1200)
        self.assertEqual(len(host._execute_calls), 3)
        for sql, params in host._execute_calls:
            self.assertLessEqual(len(params), 500)
            self.assertIn("message_id IN", sql)
        self.assertEqual(host._execute_calls[0][1][0], "m0")
        self.assertEqual(host._execute_calls[0][1][-1], "m499")
        self.assertEqual(host._execute_calls[1][1][0], "m500")
        self.assertEqual(host._execute_calls[2][1][-1], "m1199")
        self.assertEqual(records[0]["timestamp"], "2026-08-13 10:00:00")

    @async_test
    async def test_A8_empty(self):
        """A-8 空/全空串入参返回 []，不触碰 pool。"""
        host = _ChatHistoryHost(_FakeCursor())
        self.assertEqual(await host.get_messages_by_ids([]), [])
        self.assertEqual(await host.get_messages_by_ids(["", None]), [])
        self.assertEqual(host._execute_calls, [])


# ============================================================
# B 组：webapi 端点
# ============================================================


class TestRecordsToCsv(unittest.TestCase):
    def test_B1_csv_shape_and_escaping(self):
        """B-1 BOM/列序/None 空串/reply 扁平/逗号引号换行转义。"""
        records = [
            {
                "timestamp": "2026-08-13 14:30:00",
                "group_id": "111",
                "sender_id": "222",
                "sender_name": '张,三"号',
                "message_type": "text",
                "content": "hello\nworld, ok",
                "message_id": "m1",
                "at_list": None,
                "reply_id": "r9",
                "reply_message": {
                    "sender_id": "333",
                    "sender_name": "李四",
                    "content": "原话",
                },
            }
        ]
        text = web_query._records_to_csv(records)
        self.assertTrue(text.startswith("\ufeff"))
        # 用 csv.reader 回读验证列数与转义还原
        rows = list(csv.reader(io.StringIO(text.lstrip("\ufeff"))))
        self.assertEqual(len(rows), 2)  # 表头 + 1 数据行
        self.assertEqual(
            rows[0],
            [
                "timestamp",
                "group_id",
                "sender_id",
                "sender_name",
                "message_type",
                "content",
                "message_id",
                "at_list",
                "reply_id",
                "reply_sender_id",
                "reply_sender_name",
                "reply_content",
            ],
        )
        self.assertEqual(rows[1][3], '张,三"号')  # 逗号/引号还原
        self.assertEqual(rows[1][5], "hello\nworld, ok")  # 换行/逗号还原
        self.assertEqual(rows[1][7], "None")  # None → "None"
        self.assertEqual(rows[1][9], "333")  # reply 扁平
        self.assertEqual(rows[1][10], "李四")
        self.assertEqual(rows[1][11], "原话")

    def test_B1b_none_reply(self):
        """B-1b reply_message 为 None 时三列写 "None"；空串仍写空。"""
        text = web_query._records_to_csv(
            [
                {
                    "timestamp": "t",
                    "group_id": "g",
                    "sender_id": "s",
                    "sender_name": "n",
                    "message_type": "text",
                    "content": "c",
                    "message_id": "m",
                    "at_list": "",
                    "reply_id": "",
                    "reply_message": None,
                }
            ]
        )
        rows = list(csv.reader(io.StringIO(text.lstrip("\ufeff"))))
        self.assertEqual(rows[1][7], "")  # 空串仍写空
        self.assertEqual(rows[1][9:], ["None", "None", "None"])  # reply 缺失 → None


class _FakeExportMgr:
    def __init__(self, records=None, error=None):
        self.records = records if records is not None else []
        self.error = error
        self.calls: list[dict] = []
        self.reply_ids: list[str] = []

    async def export_messages(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.records

    async def get_messages_by_ids(self, ids):
        self.reply_ids.extend(ids)
        return []


class TestApiQueryExport(unittest.TestCase):
    def setUp(self):
        self._orig_request = web_query.request

    def tearDown(self):
        web_query.request = self._orig_request

    @async_test
    async def test_B2_invalid_format(self):
        """B-2 format 非法返回 400。"""
        Host, req = _make_endpoint({"format": "xlsx"})
        host = Host(_FakeExportMgr())
        resp = await host.api_query_export()
        self.assertEqual(resp["status"], 400)
        self.assertIn("csv", resp["error"])

    @async_test
    async def test_B3_csv_success(self):
        """B-3 CSV 成功路径：file_response 文件名/content_type + _save_export_temp 落盘。"""
        mgr = _FakeExportMgr(
            records=[
                {
                    "timestamp": "2026-08-13 14:30:00",
                    "group_id": "111",
                    "sender_id": "222",
                    "sender_name": "张三",
                    "message_type": "text",
                    "content": "hi",
                    "message_id": "m1",
                    "at_list": "",
                    "reply_id": "",
                }
            ]
        )
        Host, req = _make_endpoint({"format": "csv", "limit": "10"})
        host = Host(mgr)
        with mock.patch.object(
            web_query, "_save_export_temp", return_value="/tmp/x.csv"
        ) as sv:
            resp = await host.api_query_export()
        # 返回免鉴权一次性下载地址；注册的路径与 TTL 正确
        self.assertEqual(resp["json"]["token"], "tok_123")
        self.assertEqual(web_query.file_token_service.registered[-1][0], "/tmp/x.csv")
        self.assertEqual(
            web_query.file_token_service.registered[-1][1], web_query.EXPORT_TOKEN_TTL
        )
        # limit 透传；写入文本含 BOM
        self.assertEqual(mgr.calls[0]["limit"], 10)
        written = sv.call_args[0][0]
        self.assertTrue(written.startswith("\ufeff"))

    @async_test
    async def test_B4_json_success_and_clamp(self):
        """B-4 JSON 成功路径 + limit 夹取 [1, EXPORT_LIMIT_MAX]。"""
        mgr = _FakeExportMgr(records=[{"timestamp": "t", "content": "你好"}])
        Host, req = _make_endpoint({"format": "json", "limit": "99999999"})
        host = Host(mgr)
        with mock.patch.object(
            web_query, "_save_export_temp", return_value="/tmp/x.json"
        ) as sv:
            resp = await host.api_query_export()
        self.assertEqual(resp["json"]["token"], "tok_123")
        self.assertEqual(mgr.calls[0]["limit"], EXPORT_LIMIT_MAX)
        written = sv.call_args[0][0]
        parsed = __import__("json").loads(written)
        # _enrich_query_reply 会为每条记录补 reply_message（无关联则为 None）
        self.assertEqual(
            parsed, [{"timestamp": "t", "content": "你好", "reply_message": None}]
        )

    @async_test
    async def test_B5_defaults(self):
        """B-5 缺省 format=csv、limit=EXPORT_LIMIT_DEFAULT；空串 group/sender 归 None。"""
        mgr = _FakeExportMgr()
        Host, req = _make_endpoint(
            {"group_id": "  ", "sender_id": "  ", "keyword": "  "}
        )
        host = Host(mgr)
        with mock.patch.object(
            web_query, "_save_export_temp", return_value="/tmp/x.csv"
        ):
            resp = await host.api_query_export()
        self.assertEqual(resp["json"]["token"], "tok_123")
        call = mgr.calls[0]
        self.assertIsNone(call["group_id"])
        self.assertIsNone(call["sender_id"])
        self.assertIsNone(call["keyword"])
        self.assertEqual(call["limit"], EXPORT_LIMIT_DEFAULT)

    @async_test
    async def test_B6_download_ok(self):
        """B-6 api_query_export_download：消费令牌返回文件流。"""
        Host, req = _make_endpoint({"token": "tok_123"})
        host = Host(_FakeExportMgr())
        with mock.patch.object(
            web_query.file_token_service,
            "handle_file",
            mock.AsyncMock(return_value="/tmp/x.csv"),
        ):
            resp = await host.api_query_export_download()
        self.assertEqual(resp["file"], "/tmp/x.csv")
        self.assertTrue(resp["filename"].startswith("chat_history_"))
        self.assertTrue(resp["filename"].endswith(".csv"))
        self.assertEqual(resp["content_type"], "text/csv; charset=utf-8")

    @async_test
    async def test_B6b_download_json(self):
        """B-6b JSON 文件 content_type。"""
        Host, req = _make_endpoint({"token": "tok_123"})
        host = Host(_FakeExportMgr())
        with mock.patch.object(
            web_query.file_token_service,
            "handle_file",
            mock.AsyncMock(return_value="/tmp/x.json"),
        ):
            resp = await host.api_query_export_download()
        self.assertTrue(resp["filename"].endswith(".json"))
        self.assertEqual(resp["content_type"], "application/json; charset=utf-8")

    @async_test
    async def test_B6c_download_missing_token(self):
        """B-6c 缺 token → 400。"""
        Host, req = _make_endpoint({})
        host = Host(_FakeExportMgr())
        resp = await host.api_query_export_download()
        self.assertEqual(resp["status"], 400)

    @async_test
    async def test_B6d_download_invalid_token(self):
        """B-6d 令牌无效/过期 → 404。"""
        Host, req = _make_endpoint({"token": "bad"})
        host = Host(_FakeExportMgr())
        with mock.patch.object(
            web_query.file_token_service,
            "handle_file",
            mock.AsyncMock(side_effect=KeyError("expired")),
        ):
            resp = await host.api_query_export_download()
        self.assertEqual(resp["status"], 404)


# ============================================================
# C 组：接线与版本
# ============================================================


class TestWiring(unittest.TestCase):
    def test_C1_route_and_assembly(self):
        """C-1 base.py 路由含 query/export、WebAPI 组装含 api_query_export。"""
        base_src = (_PLUGIN_ROOT / "core" / "webapi" / "base.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("query/export", base_src)
        self.assertIn("api_query_export", base_src)
        self.assertTrue(hasattr(WebAPI, "api_query_export"))

    def test_C2_metadata_version(self):
        """C-2 metadata.yaml version=v0.7.1。"""
        md = (_PLUGIN_ROOT / "metadata.yaml").read_text(encoding="utf-8")
        self.assertIn("version: v0.7.1", md)

    def test_C3_frontend(self):
        """C-3 前端 index.html 含导出按钮 + ?v=0.7.1。"""
        html = (_PLUGIN_ROOT / "pages" / "dashboard" / "index.html").read_text(
            encoding="utf-8"
        )
        self.assertIn('id="exportCsvBtn"', html)
        self.assertIn('id="exportJsonBtn"', html)
        self.assertIn("?v=0.7.1", html)
        self.assertNotIn("?v=0.7.0", html)

    def test_C4_constants(self):
        """C-4 导出上限常量：默认 10 万、最大 50 万。"""
        self.assertEqual(EXPORT_LIMIT_DEFAULT, 100000)
        self.assertEqual(EXPORT_LIMIT_MAX, 500000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
