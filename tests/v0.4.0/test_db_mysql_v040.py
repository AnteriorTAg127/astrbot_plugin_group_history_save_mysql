# ruff: noqa: I001
"""astrbot_plugin_group_history_save_mysql v0.4.0 Module B 存储增强离线测试（阶段 6-7 测试 Agent 新增）。

覆盖 db_mysql.py 的 v0.4.0 增量改动（chat_history 新增 at_list/reply_id 两列）：

- _migrate_schema：幂等迁移 —— 缺列时 ADD COLUMN、已存在时跳过（重复执行不重复 ALTER）
- insert_chat_message：at_list/reply_id 默认空串透传（新参不破坏旧调用）
- query_messages：SELECT 列表含 at_list/reply_id；旧行 NULL 由下游 .get 兜底
- get_messages_by_ids：空入参/全空串返回 []；非空生成 IN 占位符全参数化 SQL；
  DictCursor 记录返回 + timestamp 字符串化；异常降级 []（不抛）

运行方式（插件根目录，二者皆可）：
    python -m pytest "tests/v0.4.0/test_db_mysql_v040.py" -v
    python "tests/v0.4.0/test_db_mysql_v040.py"

范式：astrbot.* 与 aiomysql 全部 sys.modules stub；用 FakePool/FakeConnection/
FakeCursor 捕获 SQL 与参数，断言 SQL 形态而不依赖真实 MySQL 服务。
"""

import asyncio
import os
import sys
import types
import unittest


# ============================================================
# 一、astrbot.* / aiomysql stub 注入（必须在 import db_mysql 之前）
# ============================================================


def _new_module(name: str) -> types.ModuleType:
    return types.ModuleType(name)


_astrbot = _new_module("astrbot")
_astrbot_api = _new_module("astrbot.api")


class _StubLogger:
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
_astrbot.api = _astrbot_api

_aiomysql = _new_module("aiomysql")


class _FakeDictCursor:
    """模拟 aiomysql DictCursor：记录 execute 并可由测试注入 fetch 结果。"""

    def __init__(self, fetch_map=None):
        self.executed: list[tuple[str, tuple]] = []
        self.fetch_map = fetch_map or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, sql, params=None):
        self.executed.append((sql, tuple(params or [])))

    async def fetchone(self):
        sql = self.executed[-1][0] if self.executed else ""
        return self.fetch_map.get(sql, None)

    async def fetchall(self):
        sql = self.executed[-1][0] if self.executed else ""
        return self.fetch_map.get(sql, [])


class _FakeCursor(_FakeDictCursor):
    """普通 cursor（fetchone 返回 tuple 形状，如 DATA_TYPE 检查）。"""


class _FakeConnection:
    def __init__(self, cursor=None):
        self._cursor = cursor or _FakeCursor()
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def cursor(self, *a, **k):
        return self._cursor

    async def ping(self, reconnect=False):
        return True

    def close(self):
        self.closed = True


class _FakeAcquire:
    """模拟 aiomysql.Pool.acquire 的返回值：既可 async with 也可 await。

    真实 aiomysql 的 acquire() 返回一个同时实现 __await__ 与 __aenter__/
    __aexit__ 的对象；被测代码统一用 `async with pool.acquire() as conn`。
    """

    def __init__(self, pool):
        self.pool = pool

    def __await__(self):
        async def _get():
            return self.pool._conn

        return _get().__await__()

    async def __aenter__(self):
        return self.pool._conn

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    """池子替身：acquire 返回 _FakeAcquire（连接内部记录 SQL）。"""

    def __init__(self, conn=None):
        self._conn = conn or _FakeConnection()
        self.closed = False

    def acquire(self):
        return _FakeAcquire(self)

    async def initialize(self):
        return None

    async def close(self):
        self.closed = True


_aiomysql.connect = None
_aiomysql.DictCursor = "DictCursor"
_aiomysql.Connection = object
sys.modules["aiomysql"] = _aiomysql

# ---- 剔除被测包缓存（多测试文件合跑兼容）----
_PKG = "astrbot_plugin_group_history_save_mysql"
for _name in list(sys.modules):
    if _name == _PKG or _name.startswith(_PKG + "."):
        del sys.modules[_name]

sys.modules["astrbot"] = _astrbot
sys.modules["astrbot.api"] = _astrbot_api

_PLUGINS_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..")
)
if _PLUGINS_DIR not in sys.path:
    sys.path.insert(0, _PLUGINS_DIR)

# ============================================================
# 二、导入被测代码（stub 已就位；DynamicPool 仅构造不入网）
# ============================================================

import astrbot_plugin_group_history_save_mysql.core.db_mysql as DB  # noqa: E402


def _make_manager(pool=None):
    """构造 MySQLManager 并替换其 pool 为 FakePool。"""
    mgr = DB.MySQLManager(
        host="h", port=3306, user="u", password="p", database="d"
    )
    mgr.pool = pool or _FakePool()
    return mgr


def _run(coro):
    return asyncio.run(coro)


# ============================================================
# 三、测试用例
# ============================================================


class _SchemaCursor(_FakeCursor):
    """按列名模拟 INFORMATION_SCHEMA 探测结果的 cursor。

    fetchone 对 INFORMATION_SCHEMA 查询：列存在返回 ("varchar",)，不存在返回
    None（触发 ALTER）。执行中记录所有 chat_history 的 ADD COLUMN。
    v0.5.2 起 _migrate_schema 数据驱动补建 5 个索引（复合索引 + idx_message_id
    + idx_timestamp），STATISTICS 分支改为按被查索引名应答：复合索引与
    idx_timestamp 视为存量表已存在（v0.4 起 DDL 即含），idx_message_id 由
    has_msg_index 门控（早期版本迁移仅补它）。
    """

    def __init__(self, existing_cols: list[str], has_msg_index: bool = False):
        super().__init__()
        self.existing_cols = set(existing_cols)
        self.has_msg_index = has_msg_index
        self.chat_adds: list[str] = []

    async def execute(self, sql, params=None):
        await super().execute(sql, params)
        params = params or ()
        if sql.startswith("ALTER TABLE"):
            if "chat_history" in sql:
                self.chat_adds.append(sql)

    async def fetchone(self):
        sql = self.executed[-1][0] if self.executed else ""
        if "INFORMATION_SCHEMA.COLUMNS" in sql:
            params = self.executed[-1][1]
            col = params[1] if len(params) > 1 else params[0]
            return ("varchar",) if col in self.existing_cols else None
        if "INFORMATION_SCHEMA.STATISTICS" in sql:
            params = self.executed[-1][1]
            index_name = params[1] if len(params) > 1 else params[0]
            if index_name == "idx_message_id" and not self.has_msg_index:
                return None
            return (index_name,)
        return ("varchar",)


class MigrationIdempotentTest(unittest.TestCase):
    """B1：_migrate_schema 幂等迁移（缺列 ADD / 已存在跳过）"""

    def _run_migration(
        self, existing_cols: list[str], has_msg_index: bool = False
    ):
        cur = _SchemaCursor(existing_cols, has_msg_index=has_msg_index)
        conn = _FakeConnection(cur)
        mgr = _make_manager(_FakePool(conn))
        _run(mgr._migrate_schema())
        return cur.chat_adds

    def test_adds_missing_columns(self):
        # 存量库：无 at_list/reply_id → chat_history 应 ADD 两列
        adds = self._run_migration(
            ["group_id", "sender_id"], has_msg_index=True
        )
        self.assertEqual(len(adds), 2)
        self.assertTrue(any("ADD COLUMN at_list" in a for a in adds))
        self.assertTrue(any("ADD COLUMN reply_id" in a for a in adds))

    def test_skips_when_columns_exist(self):
        # 已迁移库：at_list/reply_id 已存在 → 不再 ALTER（幂等可重入）
        adds = self._run_migration(
            ["group_id", "sender_id", "at_list", "reply_id"],
            has_msg_index=True,
        )
        self.assertEqual(adds, [])

    def test_partial_migration_one_missing(self):
        # 部分迁移：只缺 reply_id → 仅 ADD reply_id
        adds = self._run_migration(
            ["group_id", "sender_id", "at_list"], has_msg_index=True
        )
        self.assertEqual(len(adds), 1)
        self.assertIn("reply_id", adds[0])
        self.assertNotIn("at_list", adds[0])

    def test_adds_missing_message_id_index(self):
        # 存量库：列齐全但缺 idx_message_id 索引 → 仅 ADD INDEX
        adds = self._run_migration(
            ["group_id", "sender_id", "at_list", "reply_id"]
        )
        self.assertEqual(len(adds), 1)
        self.assertIn("ADD INDEX idx_message_id", adds[0])

    def test_skips_when_message_id_index_exists(self):
        # 已迁移库：索引已存在 → 不再 ALTER（幂等可重入）
        adds = self._run_migration(
            ["group_id", "sender_id", "at_list", "reply_id"],
            has_msg_index=True,
        )
        self.assertEqual(adds, [])


class InsertChatMessageTest(unittest.TestCase):
    """B2：insert_chat_message 新参透传（at_list/reply_id）"""

    def _insert(self, **extra):
        cur = _FakeCursor()
        conn = _FakeConnection(cur)
        mgr = _make_manager(_FakePool(conn))
        kwargs = dict(
            group_id="9001",
            sender_id="123",
            sender_name="A",
            message_type="text",
            content="hi",
            message_id="m1",
        )
        kwargs.update(extra)
        ok = _run(mgr.insert_chat_message(**kwargs))
        return ok, cur.executed[0]

    def test_insert_with_at_reply(self):
        ok, (sql, params) = self._insert(at_list="777,888", reply_id="r1")
        self.assertTrue(ok)
        self.assertIn("at_list", sql)
        self.assertIn("reply_id", sql)
        self.assertEqual(params[-2], "777,888")
        self.assertEqual(params[-1], "r1")

    def test_insert_without_at_reply_defaults(self):
        # 旧调用（不带新参）→ 默认空串，SQL 仍含两列
        ok, (sql, params) = self._insert()
        self.assertTrue(ok)
        self.assertIn("at_list", sql)
        self.assertIn("reply_id", sql)
        self.assertEqual(params[-2], "")
        self.assertEqual(params[-1], "")


class QueryMessagesColumnsTest(unittest.TestCase):
    """B3：query_messages SELECT 含 at_list/reply_id"""

    def test_select_contains_new_columns(self):
        rows = [
            {
                "id": 1,
                "timestamp": "2026-08-01 10:00:00",
                "group_id": "9001",
                "sender_id": "123",
                "sender_name": "A",
                "message_type": "text",
                "content": "hi",
                "message_id": "m1",
                "at_list": "777",
                "reply_id": "r1",
            }
        ]

        class DictCursor(_FakeDictCursor):
            async def fetchall(self):
                sql = self.executed[-1][0] if self.executed else ""
                if "COUNT(*)" in sql:
                    return []
                return rows

        cur = DictCursor()
        conn = _FakeConnection(cur)
        mgr = _make_manager(_FakePool(conn))
        result = _run(mgr.query_messages(page_size=50))
        # COUNT 未注入 → total 0；records 返回注入行
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["records"], rows)
        # SELECT 主查询含两新列且带分页占位符
        select_sql = [s for s, _ in cur.executed if s.startswith("SELECT id")][0]
        self.assertIn("at_list", select_sql)
        self.assertIn("reply_id", select_sql)
        self.assertIn("LIMIT %s OFFSET %s", select_sql)


class GetMessagesByIdsTest(unittest.TestCase):
    """B4：get_messages_by_ids（空入参 / IN 占位符 / 异常降级）"""

    def test_empty_input_returns_empty_no_sql(self):
        cur = _FakeCursor()
        conn = _FakeConnection(cur)
        mgr = _make_manager(_FakePool(conn))
        self.assertEqual(_run(mgr.get_messages_by_ids([])), [])
        self.assertEqual(_run(mgr.get_messages_by_ids(["", None, ""])), [])
        self.assertEqual(cur.executed, [])  # 未发 SQL

    def test_in_placeholders_parameterized(self):
        class DictCursor(_FakeDictCursor):
            async def fetchall(self):
                return [
                    {
                        "timestamp": "2026-08-01 10:00:00",
                        "group_id": "9001",
                        "sender_id": "456",
                        "sender_name": "Bob",
                        "message_type": "text",
                        "content": "yo",
                        "message_id": "r1",
                        "at_list": "",
                        "reply_id": "",
                    }
                ]

        cur = DictCursor()
        conn = _FakeConnection(cur)
        mgr = _make_manager(_FakePool(conn))
        rows = _run(mgr.get_messages_by_ids(["r1", "r2", ""]))
        self.assertEqual(len(rows), 1)
        sql, params = cur.executed[0]
        self.assertIn("WHERE message_id IN (%s,%s)", sql)
        self.assertEqual(params, ("r1", "r2"))  # 空串已过滤
        self.assertIsInstance(rows[0]["timestamp"], str)  # 字符串化

    def test_exception_degrades_empty(self):
        class BoomPool(_FakePool):
            def acquire(self):
                raise RuntimeError("conn lost")

        mgr = _make_manager(BoomPool())
        self.assertEqual(_run(mgr.get_messages_by_ids(["r1"])), [])  # 不抛


if __name__ == "__main__":
    unittest.main(verbosity=2)
