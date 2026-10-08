"""v0.5.0 模块 B MySQL 聚合仓储（stats/repository.py）冒烟脚本。

范式沿用 v0.4.0 test_db_mysql_v040.py：astrbot.* / aiomysql 全部 sys.modules stub，
FakePool/FakeConnection/FakeCursor 捕获 SQL 与参数、可编排 fetchall/fetchone 返回，
只断言 SQL 形态与结果组装，不依赖真实 MySQL。

覆盖点：
- SQL 全参数化无拼接（注入式 group_id/sender_id 不落入 SQL 文本）
- 可选过滤条件拼接正确（group_id/sender_id 为 None 时 SQL 不出现对应条件）
- 24h / 7 星期分布固定形状与缺省补 0
- 每日趋势 DATE 分组 ASC、(date, count) 元组形状、不补零
- 发言人行排行 COUNT DESC / 同数 sender_id ASC / LIMIT 参数化
- 昵称二次查询：IN 占位符参数化、关联子查询取最新行、空排行不发二查
- 群排行 GROUP BY group_id + active_senders
- 个人概览 count / active_days / 最新昵称
- 图片窗口聚合：群总量截断前全量、每群 Top K 截断、同数 sender_id ASC、昵称 IN 参数
- 超时兜底（wait_for 抛 TimeoutError）与 SQL 异常向上抛
- stats/__init__.py PEP 562 惰性导出（访问前不加载 repository）

运行方式（插件根目录，二者皆可）：
    python -m pytest "tests/v0.5.0/smoke_b_repository.py" -v
    python "tests/v0.5.0/smoke_b_repository.py"
"""

# ruff: noqa: I001

import asyncio
import os
import sys
import types
import unittest
from datetime import date, datetime


# ============================================================
# 一、astrbot.* / aiomysql stub 注入（必须在导入被测包之前）
# ============================================================


def _new_module(name: str) -> types.ModuleType:
    return types.ModuleType(name)


_astrbot = _new_module("astrbot")
_astrbot_api = _new_module("astrbot.api")


class _StubLogger:
    def __init__(self):
        self.records = {"info": [], "warning": [], "error": [], "debug": []}

    def _log(self, level, msg, args):
        try:
            text = msg % args if args else str(msg)
        except Exception:
            text = str(msg)
        self.records[level].append(text)

    def info(self, msg, *args, **kw):
        self._log("info", msg, args)

    def warning(self, msg, *args, **kw):
        self._log("warning", msg, args)

    def error(self, msg, *args, **kw):
        self._log("error", msg, args)

    def debug(self, msg, *args, **kw):
        self._log("debug", msg, args)


_astrbot_api.logger = _StubLogger()
_astrbot.api = _astrbot_api

_aiomysql = _new_module("aiomysql")
_aiomysql.connect = None
_aiomysql.DictCursor = "DictCursor"
_aiomysql.Connection = object
sys.modules["aiomysql"] = _aiomysql
sys.modules["astrbot"] = _astrbot
sys.modules["astrbot.api"] = _astrbot_api

# ---- 剔除被测包缓存（多测试文件合跑兼容）----
_PKG = "astrbot_plugin_group_history_save_mysql"
for _name in list(sys.modules):
    if _name == _PKG or _name.startswith(_PKG + "."):
        del sys.modules[_name]

_PLUGINS_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..")
)
if _PLUGINS_DIR not in sys.path:
    sys.path.insert(0, _PLUGINS_DIR)


# ============================================================
# 二、PEP 562 惰性导出验证（必须在直接 import repository 之前）
# ============================================================

import astrbot_plugin_group_history_save_mysql.core.stats as stats_pkg  # noqa: E402

assert "StatsRepository" in stats_pkg.__all__, "stats.__all__ 缺少 StatsRepository"
_repo_mod_name = _PKG + ".core.stats.repository"
assert _repo_mod_name not in sys.modules, (
    "仅 import stats 包不应急切加载 repository（会拉起 db_mysql 依赖链）"
)
assert stats_pkg.StatsRepository is not None, "惰性导出访问失败"
assert _repo_mod_name in sys.modules, "首次访问后 repository 应已加载并缓存"
import astrbot_plugin_group_history_save_mysql.core.stats.repository as REPO  # noqa: E402

assert stats_pkg.StatsRepository is REPO.StatsRepository, "导出名与模块内类不一致"
print(
    "[OK] stats/__init__.py PEP 562 惰性导出：访问前不加载 repository，访问后缓存一致"
)


def _run(coro):
    return asyncio.run(coro)


# ============================================================
# 三、Fake pool / connection / cursor
# ============================================================


class FakeCursor:
    """记录每条 execute 的 SQL 与参数；fetchall/fetchone 按各自 FIFO 队列返回。"""

    def __init__(self):
        self.executed: list[tuple[str, tuple]] = []
        self._fetchall_q: list[list] = []
        self._fetchone_q: list = []

    def queue_fetchall(self, rows):
        self._fetchall_q.append(list(rows))

    def queue_fetchone(self, row):
        self._fetchone_q.append(row)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, sql, params=None):
        self.executed.append((sql, tuple(params or ())))

    async def fetchall(self):
        return self._fetchall_q.pop(0) if self._fetchall_q else []

    async def fetchone(self):
        return self._fetchone_q.pop(0) if self._fetchone_q else None


class FakeConnection:
    def __init__(self, cursor=None):
        self._cursor = cursor or FakeCursor()
        self.closed = False

    def cursor(self, *args, **kwargs):
        return self._cursor

    def close(self):
        # 仓储层不应自行关闭连接（acquire 上下文负责）——置位供断言
        self.closed = True


class _FakeAcquire:
    def __init__(self, pool):
        self._pool = pool

    async def __aenter__(self):
        return self._pool._conn

    async def __aexit__(self, *exc):
        return False


class FakePool:
    def __init__(self, conn=None):
        self._conn = conn or FakeConnection()
        self.acquire_calls = 0

    def acquire(self):
        self.acquire_calls += 1
        return _FakeAcquire(self)


class FakeMgr:
    def __init__(self, pool):
        self.pool = pool


def _make_repo(cursor=None):
    """构造 StatsRepository + 返回 (repo, conn, pool) 供断言。"""
    conn = FakeConnection(cursor)
    pool = FakePool(conn)
    return REPO.StatsRepository(FakeMgr(pool)), conn, pool


# 通用窗口边界
START = datetime(2026, 8, 1, 0, 0, 0)
END = datetime(2026, 8, 4, 0, 0, 0)


# ============================================================
# 四、测试用例
# ============================================================


class DailyTrendTest(unittest.TestCase):
    """get_daily_trend：DATE 分组、ASC、元组形状、不补零"""

    def test_trend_shape_and_order(self):
        cur = FakeCursor()
        cur.queue_fetchall([(date(2026, 8, 1), 3), (date(2026, 8, 3), 4)])
        repo, conn, pool = _make_repo(cur)
        result = _run(repo.get_daily_trend("group_x", START, END))
        self.assertEqual(result, [("2026-08-01", 3), ("2026-08-03", 4)])
        self.assertIsInstance(result[0], tuple)
        sql, params = cur.executed[0]
        self.assertIn("DATE(timestamp)", sql)
        self.assertIn("GROUP BY DATE(timestamp)", sql)
        self.assertIn("ORDER BY DATE(timestamp) ASC", sql)
        # 8-02 无数据不出现（不补零，service 层补）
        self.assertNotIn("2026-08-02", [d for d, _ in result])
        self.assertEqual(params, (START, END, "group_x"))
        self.assertEqual(pool.acquire_calls, 1)
        self.assertFalse(conn.closed)

    def test_trend_all_groups(self):
        cur = FakeCursor()
        cur.queue_fetchall([])
        repo, _, _ = _make_repo(cur)
        self.assertEqual(_run(repo.get_daily_trend(None, START, END)), [])
        sql, params = cur.executed[0]
        self.assertNotIn("group_id =", sql)
        self.assertEqual(params, (START, END))


class DistributionsTest(unittest.TestCase):
    """24h / 星期分布：固定形状、缺省补 0、可选过滤拼接"""

    def test_hourly_zero_fill_24(self):
        cur = FakeCursor()
        cur.queue_fetchall([(0, 5), (13, 9), (23, 2)])
        repo, _, _ = _make_repo(cur)
        dist = _run(repo.get_hourly_dist("g1", None, START, END))
        self.assertEqual(len(dist), 24)
        self.assertEqual(dist[0], 5)
        self.assertEqual(dist[13], 9)
        self.assertEqual(dist[23], 2)
        self.assertEqual(sum(dist), 16)  # 其余小时全 0
        sql, params = cur.executed[0]
        self.assertIn("HOUR(timestamp)", sql)
        self.assertIn("GROUP BY HOUR(timestamp)", sql)
        self.assertNotIn("sender_id =", sql)  # sender_id=None 不加条件
        self.assertEqual(params, (START, END, "g1"))

    def test_hourly_with_sender_filter(self):
        cur = FakeCursor()
        cur.queue_fetchall([])
        repo, _, _ = _make_repo(cur)
        self.assertEqual(_run(repo.get_hourly_dist("g1", "qq1", START, END)), [0] * 24)
        sql, params = cur.executed[0]
        self.assertIn("sender_id = %s", sql)
        self.assertEqual(params, (START, END, "g1", "qq1"))

    def test_hourly_all_groups_no_filters(self):
        cur = FakeCursor()
        cur.queue_fetchall([])
        repo, _, _ = _make_repo(cur)
        _run(repo.get_hourly_dist(None, None, START, END))
        sql, params = cur.executed[0]
        self.assertNotIn("group_id =", sql)
        self.assertNotIn("sender_id =", sql)
        self.assertEqual(params, (START, END))

    def test_weekday_zero_fill_7(self):
        cur = FakeCursor()
        cur.queue_fetchall([(0, 3), (6, 4)])
        repo, _, _ = _make_repo(cur)
        dist = _run(repo.get_weekday_dist(None, None, START, END))
        self.assertEqual(len(dist), 7)
        self.assertEqual(dist[0], 3)  # MySQL WEEKDAY 0=周一
        self.assertEqual(dist[6], 4)
        self.assertEqual(dist[1], 0)
        sql, _ = cur.executed[0]
        self.assertIn("WEEKDAY(timestamp)", sql)
        self.assertIn("GROUP BY WEEKDAY(timestamp)", sql)


class GroupRankingTest(unittest.TestCase):
    """get_group_ranking：GROUP BY group_id + active_senders + LIMIT"""

    def test_group_ranking(self):
        cur = FakeCursor()
        cur.queue_fetchall([("111", 50, 5), ("222", 30, 3)])
        repo, _, _ = _make_repo(cur)
        result = _run(repo.get_group_ranking(START, END, 10))
        self.assertEqual(
            result,
            [
                {"group_id": "111", "count": 50, "active_senders": 5},
                {"group_id": "222", "count": 30, "active_senders": 3},
            ],
        )
        sql, params = cur.executed[0]
        self.assertIn("GROUP BY group_id", sql)
        self.assertIn("COUNT(DISTINCT sender_id)", sql)
        self.assertIn("ORDER BY cnt DESC, group_id ASC", sql)
        self.assertIn("LIMIT %s", sql)
        self.assertEqual(params, (START, END, 10))


class GroupSummaryTest(unittest.TestCase):
    """get_all_groups_summary（v0.5.1）：全量有数据的群，无窗口无 LIMIT"""

    def test_group_summary_shape_and_order(self):
        cur = FakeCursor()
        cur.queue_fetchall(
            [("111", 500, datetime(2026, 8, 4, 9, 0)), ("222", 30, None)]
        )
        repo, _, _ = _make_repo(cur)
        result = _run(repo.get_all_groups_summary())
        self.assertEqual(
            result,
            [
                {
                    "group_id": "111",
                    "count": 500,
                    "last_active": datetime(2026, 8, 4, 9, 0),
                },
                {"group_id": "222", "count": 30, "last_active": None},
            ],
        )
        sql, params = cur.executed[0]
        self.assertIn("GROUP BY group_id", sql)
        self.assertIn("MAX(timestamp)", sql)
        self.assertIn("ORDER BY cnt DESC, group_id ASC", sql)
        self.assertNotIn("LIMIT", sql)
        self.assertNotIn("WHERE", sql)  # 不限时间窗口
        self.assertEqual(params, ())  # FakeCursor 统一以 tuple 记录（无参为空元组）

    def test_group_summary_empty(self):
        cur = FakeCursor()
        cur.queue_fetchall([])
        repo, _, _ = _make_repo(cur)
        self.assertEqual(_run(repo.get_all_groups_summary()), [])


class ImageWindowCountsTest(unittest.TestCase):
    """get_image_window_counts：群总量全量、Top K 截断、昵称 IN 参数"""

    def _rows(self):
        # g1: s1=5, s2=3, s3=1；g2: s4=2（s5 与 s4 同数测并列排序）
        return [
            ("g1", "s1", 5),
            ("g1", "s2", 3),
            ("g1", "s3", 1),
            ("g2", "s4", 2),
            ("g2", "s5", 2),
        ]

    def test_top_k_truncation_and_totals(self):
        cur = FakeCursor()
        cur.queue_fetchall(self._rows())
        cur.queue_fetchall([("s1", "N1"), ("s4", "N4")])
        repo, conn, pool = _make_repo(cur)
        # top_k=1：g1 截掉 s2/s3；g2 上 s4 与 s5 同数，按 sender_id ASC 取 s4 截掉 s5
        result = _run(repo.get_image_window_counts(START, END, 1))

        # 群总量 = 截断前全量求和
        self.assertEqual(result["groups"], {"g1": 9, "g2": 4})
        self.assertEqual(result["senders"]["g1"], [("s1", "N1", 5)])
        self.assertEqual(result["senders"]["g2"], [("s4", "N4", 2)])

        # 第一条：聚合 SQL（image_records + 双键分组）
        sql1, params1 = cur.executed[0]
        self.assertIn("image_records", sql1)
        self.assertIn("GROUP BY group_id, sender_id", sql1)
        self.assertEqual(params1, (START, END))
        # 第二条：昵称二次查询 IN 参数化（仅上榜者，升序去重）
        sql2, params2 = cur.executed[1]
        self.assertIn("image_records", sql2)
        self.assertIn("IN (%s,%s)", sql2)
        self.assertEqual(params2, (START, END, "s1", "s4", START, END))
        self.assertEqual(pool.acquire_calls, 1)  # 单连接内两条查询
        self.assertFalse(conn.closed)

    def test_top_k_larger_than_senders_keeps_all(self):
        cur = FakeCursor()
        cur.queue_fetchall(self._rows())
        cur.queue_fetchall(
            [("s1", "N1"), ("s2", "N2"), ("s3", "N3"), ("s4", "N4"), ("s5", "N5")]
        )
        repo, _, _ = _make_repo(cur)
        result = _run(repo.get_image_window_counts(START, END, 20))
        self.assertEqual(
            result["senders"]["g1"],
            [("s1", "N1", 5), ("s2", "N2", 3), ("s3", "N3", 1)],
        )
        # g2 同数按 sender_id ASC
        self.assertEqual(result["senders"]["g2"], [("s4", "N4", 2), ("s5", "N5", 2)])

    def test_empty_window(self):
        cur = FakeCursor()
        cur.queue_fetchall([])
        repo, _, _ = _make_repo(cur)
        result = _run(repo.get_image_window_counts(START, END, 20))
        self.assertEqual(result, {"groups": {}, "senders": {}})
        self.assertEqual(len(cur.executed), 1)  # 空窗口不发昵称查询


class MsgWindowCountsTest(unittest.TestCase):
    """get_msg_window_counts（v0.5.5）：chat_history 版窗口聚合，
    结构镜像 get_image_window_counts"""

    def _rows(self):
        return [
            ("g1", "s1", 5),
            ("g1", "s2", 3),
            ("g1", "s3", 1),
            ("g2", "s4", 2),
            ("g2", "s5", 2),
        ]

    def test_chat_history_top_k_and_totals(self):
        cur = FakeCursor()
        cur.queue_fetchall(self._rows())
        cur.queue_fetchall([("s1", "N1"), ("s4", "N4")])
        repo, conn, pool = _make_repo(cur)
        result = _run(repo.get_msg_window_counts(START, END, 1))

        # 群总量 = 截断前全量求和；Top K=1 截断（同数 sender_id ASC）
        self.assertEqual(result["groups"], {"g1": 9, "g2": 4})
        self.assertEqual(result["senders"]["g1"], [("s1", "N1", 5)])
        self.assertEqual(result["senders"]["g2"], [("s4", "N4", 2)])

        sql1, params1 = cur.executed[0]
        self.assertIn("chat_history", sql1)  # 消息口径走 chat_history
        self.assertNotIn("image_records", sql1)
        self.assertIn("GROUP BY group_id, sender_id", sql1)
        self.assertEqual(params1, (START, END))
        # 昵称二查：IN 参数化（升序去重）+ 派生表 MAX(id) 等值回表
        sql2, params2 = cur.executed[1]
        self.assertIn("chat_history", sql2)
        self.assertIn("IN (%s,%s)", sql2)
        self.assertIn("MAX(t2.id)", sql2)
        self.assertEqual(params2, (START, END, "s1", "s4", START, END))
        self.assertEqual(pool.acquire_calls, 1)
        self.assertFalse(conn.closed)

    def test_empty_window_no_name_query(self):
        cur = FakeCursor()
        cur.queue_fetchall([])
        repo, _, _ = _make_repo(cur)
        result = _run(repo.get_msg_window_counts(START, END, 20))
        self.assertEqual(result, {"groups": {}, "senders": {}})
        self.assertEqual(len(cur.executed), 1)


class HourlyBatchTest(unittest.TestCase):
    """get_hourly_batch（v0.5.5）：批量小时聚合（回填/窗口重聚合专用）"""

    def _rows(self):
        # (date, hour, gid, sid, cnt)：08-01 9时 g1 两人、08-01 10时 g2 一人
        return [
            (date(2026, 8, 1), 9, "g1", "s1", 5),
            (date(2026, 8, 1), 9, "g1", "s2", 3),
            (date(2026, 8, 1), 10, "g2", "s3", 2),
        ]

    def test_hour_rows_totals_top_truncation_and_order(self):
        cur = FakeCursor()
        cur.queue_fetchall(self._rows())
        cur.queue_fetchall([("s1", "N1"), ("s3", "N3")])  # 窗口内最新昵称
        repo, _, _ = _make_repo(cur)
        hour_rows, top_rows = _run(repo.get_hourly_batch("msg", START, END, 1))

        # 群总量 = 截断前全量；按 (date, hour, group_id) 升序确定性输出
        self.assertEqual(
            hour_rows,
            [("2026-08-01", 9, "g1", 8), ("2026-08-01", 10, "g2", 2)],
        )
        # Top K=1：g1 9时截掉 s2；昵称取自窗口昵称映射（查不到空串兜底）
        self.assertEqual(
            top_rows,
            [
                ("2026-08-01", 9, "g1", "s1", "N1", 5),
                ("2026-08-01", 10, "g2", "s3", "N3", 2),
            ],
        )
        sql1, params1 = cur.executed[0]
        self.assertIn("chat_history", sql1)  # source=msg
        self.assertIn(
            "GROUP BY DATE(timestamp), HOUR(timestamp), group_id, sender_id", sql1
        )
        self.assertEqual(params1, (START, END))
        # 昵称派生表查询：无 IN 列表（窗口内全 sender GROUP BY 一次出）
        sql2, params2 = cur.executed[1]
        self.assertIn("MAX(t2.id)", sql2)
        self.assertIn("GROUP BY t2.sender_id", sql2)
        self.assertNotIn(" IN (", sql2)
        self.assertEqual(params2, (START, END, START, END))

    def test_image_source_maps_image_records(self):
        cur = FakeCursor()
        cur.queue_fetchall([])
        repo, _, _ = _make_repo(cur)
        self.assertEqual(_run(repo.get_hourly_batch("image", START, END, 20)), ([], []))
        self.assertIn("image_records", cur.executed[0][0])

    def test_empty_window_no_name_query(self):
        cur = FakeCursor()
        cur.queue_fetchall([])
        repo, _, _ = _make_repo(cur)
        self.assertEqual(_run(repo.get_hourly_batch("msg", START, END, 20)), ([], []))
        self.assertEqual(len(cur.executed), 1)

    def test_invalid_source_raises(self):
        repo, _, _ = _make_repo(FakeCursor())
        with self.assertRaises(ValueError):
            _run(repo.get_hourly_batch("bad", START, END, 20))


class DailyBatchTest(unittest.TestCase):
    """get_daily_batch（v0.5.5）：批量日聚合（日层回填专用）"""

    def test_shape_order_and_source(self):
        cur = FakeCursor()
        cur.queue_fetchall([(date(2026, 8, 1), "g2", 3), (date(2026, 8, 1), "g1", 7)])
        repo, _, _ = _make_repo(cur)
        rows = _run(repo.get_daily_batch("msg", START, END))
        self.assertEqual(rows, [("2026-08-01", "g2", 3), ("2026-08-01", "g1", 7)])
        sql, params = cur.executed[0]
        self.assertIn("chat_history", sql)
        self.assertIn("GROUP BY DATE(timestamp), group_id", sql)
        self.assertIn("ORDER BY DATE(timestamp) ASC, group_id ASC", sql)
        self.assertEqual(params, (START, END))

    def test_image_source_and_invalid(self):
        cur = FakeCursor()
        cur.queue_fetchall([])
        repo, _, _ = _make_repo(cur)
        self.assertEqual(_run(repo.get_daily_batch("image", START, END)), [])
        self.assertIn("image_records", cur.executed[0][0])
        with self.assertRaises(ValueError):
            _run(repo.get_daily_batch("xxx", START, END))


class MonthlyBatchTest(unittest.TestCase):
    """get_monthly_batch（v0.5.5）：批量月聚合（月层增量补齐专用）"""

    def test_month_zero_padded_and_source(self):
        cur = FakeCursor()
        cur.queue_fetchall([(2026, 7, "g1", 30), (2026, 12, "g2", 5)])
        repo, _, _ = _make_repo(cur)
        rows = _run(repo.get_monthly_batch("msg", START, END))
        # 月戳 "YYYY-MM" 两位补零
        self.assertEqual(rows, [("2026-07", "g1", 30), ("2026-12", "g2", 5)])
        sql, params = cur.executed[0]
        self.assertIn("YEAR(timestamp)", sql)
        self.assertIn("MONTH(timestamp)", sql)
        self.assertIn(
            "GROUP BY YEAR(timestamp), MONTH(timestamp), group_id", sql
        )
        self.assertEqual(params, (START, END))

    def test_image_source_and_invalid(self):
        cur = FakeCursor()
        cur.queue_fetchall([])
        repo, _, _ = _make_repo(cur)
        self.assertEqual(_run(repo.get_monthly_batch("image", START, END)), [])
        self.assertIn("image_records", cur.executed[0][0])
        with self.assertRaises(ValueError):
            _run(repo.get_monthly_batch("bogus", START, END))


class OverviewMetaTest(unittest.TestCase):
    """get_overview_meta（v0.5.5）：无 COUNT(*) 的总览元数据"""

    def test_meta_no_count_star(self):
        cur = FakeCursor()
        first = datetime(2026, 8, 1, 0, 0, 1)
        last = datetime(2026, 8, 3, 23, 59, 0)
        cur.queue_fetchone((7, first, last))
        repo, conn, pool = _make_repo(cur)
        result = _run(repo.get_overview_meta(None, START, END))
        self.assertEqual(
            result, {"active_senders": 7, "first": first, "last": last}
        )
        sql, params = cur.executed[0]
        self.assertIn("COUNT(DISTINCT sender_id)", sql)
        self.assertIn("MIN(timestamp)", sql)
        self.assertIn("MAX(timestamp)", sql)
        self.assertNotIn("COUNT(*)", sql)  # 总数改由快照供数
        self.assertEqual(params, (START, END))
        self.assertEqual(pool.acquire_calls, 1)
        self.assertFalse(conn.closed)

    def test_meta_with_group_filter(self):
        cur = FakeCursor()
        cur.queue_fetchone((0, None, None))
        repo, _, _ = _make_repo(cur)
        result = _run(repo.get_overview_meta("g1", START, END))
        self.assertEqual(
            result, {"active_senders": 0, "first": None, "last": None}
        )
        sql, params = cur.executed[0]
        self.assertIn("group_id = %s", sql)
        self.assertEqual(params, (START, END, "g1"))


class GroupActiveSendersTest(unittest.TestCase):
    """get_group_active_senders（v0.5.5）：群排行快照路径活跃数补齐"""

    def test_per_group_distinct_counts(self):
        cur = FakeCursor()
        cur.queue_fetchall([("111", 5), ("222", 3)])
        repo, _, _ = _make_repo(cur)
        result = _run(repo.get_group_active_senders(START, END))
        self.assertEqual(result, {"111": 5, "222": 3})
        sql, params = cur.executed[0]
        self.assertIn("COUNT(DISTINCT sender_id)", sql)
        self.assertIn("GROUP BY group_id", sql)
        self.assertEqual(params, (START, END))

    def test_empty_window(self):
        cur = FakeCursor()
        cur.queue_fetchall([])
        repo, _, _ = _make_repo(cur)
        self.assertEqual(_run(repo.get_group_active_senders(START, END)), {})


class MemberOverviewTest(unittest.TestCase):
    """get_member_overview：count / active_days / 最新昵称 + 可选群过滤"""

    def test_member_overview_with_group(self):
        cur = FakeCursor()
        cur.queue_fetchone((42, 5))
        cur.queue_fetchone(("Alice",))
        repo, _, _ = _make_repo(cur)
        result = _run(repo.get_member_overview("g1", "qq1", START, END))
        self.assertEqual(result, {"count": 42, "active_days": 5, "name": "Alice"})
        self.assertEqual(len(cur.executed), 2)
        for sql, params in cur.executed:
            self.assertIn("sender_id = %s", sql)
            self.assertIn("group_id = %s", sql)
            self.assertEqual(params, (START, END, "g1", "qq1"))
        self.assertIn("COUNT(DISTINCT DATE(timestamp))", cur.executed[0][0])
        self.assertIn("ORDER BY timestamp DESC, id DESC LIMIT 1", cur.executed[1][0])

    def test_member_overview_all_groups(self):
        cur = FakeCursor()
        cur.queue_fetchone((0, 0))
        cur.queue_fetchone(None)
        repo, _, _ = _make_repo(cur)
        result = _run(repo.get_member_overview(None, "qq1", START, END))
        self.assertEqual(result, {"count": 0, "active_days": 0, "name": ""})
        for sql, params in cur.executed:
            self.assertNotIn("group_id =", sql)
            self.assertEqual(params, (START, END, "qq1"))


class OverviewTest(unittest.TestCase):
    """get_overview：COUNT/DISTINCT/MIN/MAX + 可选群过滤"""

    def test_overview_no_group(self):
        cur = FakeCursor()
        first = datetime(2026, 8, 1, 0, 0, 1)
        last = datetime(2026, 8, 3, 23, 59, 0)
        cur.queue_fetchone((123, 7, first, last))
        repo, conn, pool = _make_repo(cur)
        result = _run(repo.get_overview(None, START, END))
        self.assertEqual(
            result, {"total": 123, "active_senders": 7, "first": first, "last": last}
        )
        sql, params = cur.executed[0]
        self.assertIn("COUNT(*)", sql)
        self.assertIn("COUNT(DISTINCT sender_id)", sql)
        self.assertIn("MIN(timestamp)", sql)
        self.assertIn("MAX(timestamp)", sql)
        self.assertIn("timestamp >= %s AND timestamp < %s", sql)
        self.assertNotIn("group_id =", sql)
        self.assertEqual(params, (START, END))
        self.assertEqual(pool.acquire_calls, 1)
        self.assertFalse(conn.closed)

    def test_overview_with_group(self):
        cur = FakeCursor()
        cur.queue_fetchone((0, 0, None, None))
        repo, _, _ = _make_repo(cur)
        result = _run(repo.get_overview("g1", START, END))
        self.assertEqual(
            result, {"total": 0, "active_senders": 0, "first": None, "last": None}
        )
        sql, params = cur.executed[0]
        self.assertIn("group_id = %s", sql)
        self.assertEqual(params, (START, END, "g1"))


class ParamSafetyTest(unittest.TestCase):
    """SQL 参数化无拼接：注入式输入只出现在参数中，不落 SQL 文本"""

    MALICIOUS_GID = "999'; DROP TABLE chat_history;--"
    MALICIOUS_SID = "1 OR 1=1"

    def test_injection_values_stay_in_params(self):
        cur = FakeCursor()
        cur.queue_fetchall([])  # hourly
        cur.queue_fetchall([])  # ranking 排行
        repo, _, _ = _make_repo(cur)
        _run(repo.get_hourly_dist(self.MALICIOUS_GID, self.MALICIOUS_SID, START, END))
        _run(repo.get_sender_ranking(self.MALICIOUS_GID, START, END, 10))
        for sql, params in cur.executed:
            self.assertNotIn(self.MALICIOUS_GID, sql)
            self.assertNotIn(self.MALICIOUS_SID, sql)
            self.assertNotIn("DROP TABLE", sql)
            self.assertIn("%s", sql)  # 一律占位符
        # 恶意值确实作为参数传入
        self.assertIn(self.MALICIOUS_GID, cur.executed[0][1])
        self.assertIn(self.MALICIOUS_SID, cur.executed[0][1])


class SenderRankingTest(unittest.TestCase):
    """get_sender_ranking：排序规则、LIMIT、昵称二次 IN 查询"""

    def test_ranking_order_limit_and_names(self):
        cur = FakeCursor()
        # 模拟 DB 已按 ORDER BY cnt DESC, sender_id ASC 返回
        cur.queue_fetchall([("222", 9), ("111", 5), ("333", 1)])
        cur.queue_fetchall([("222", "Bob"), ("111", "Alice"), ("333", "Carol")])
        repo, conn, pool = _make_repo(cur)
        result = _run(repo.get_sender_ranking("g1", START, END, 10))
        self.assertEqual(
            result,
            [
                {"sender_id": "222", "sender_name": "Bob", "count": 9},
                {"sender_id": "111", "sender_name": "Alice", "count": 5},
                {"sender_id": "333", "sender_name": "Carol", "count": 1},
            ],
        )
        # 排行 SQL：排序规则 + LIMIT 参数化
        sql1, params1 = cur.executed[0]
        self.assertIn("GROUP BY sender_id", sql1)
        self.assertIn("ORDER BY cnt DESC, sender_id ASC", sql1)
        self.assertIn("LIMIT %s", sql1)
        self.assertNotIn("LIMIT 10", sql1)
        self.assertEqual(params1, (START, END, "g1", 10))
        # 昵称二次查询：IN 占位符参数化 + 派生表 MAX(id) 等值回表（v0.5.2 起，5.7 兼容）
        sql2, params2 = cur.executed[1]
        self.assertIn("IN (%s,%s,%s)", sql2)
        self.assertIn("MAX(t2.id)", sql2)
        self.assertIn("ON m.sender_id = t.sender_id AND m.max_id = t.id", sql2)
        self.assertEqual(
            params2, ("g1", START, END, "222", "111", "333", "g1", START, END)
        )
        self.assertEqual(pool.acquire_calls, 1)  # 两条查询共用一次 acquire
        self.assertFalse(conn.closed)

    def test_ranking_empty_skips_name_query(self):
        cur = FakeCursor()
        cur.queue_fetchall([])
        repo, _, _ = _make_repo(cur)
        self.assertEqual(_run(repo.get_sender_ranking("g1", START, END, 10)), [])
        self.assertEqual(len(cur.executed), 1)  # 无上榜者不发昵称查询

    def test_ranking_missing_name_fallback_empty(self):
        cur = FakeCursor()
        cur.queue_fetchall([("222", 9), ("111", 5)])
        cur.queue_fetchall([("222", "Bob")])  # 111 昵称缺失
        repo, _, _ = _make_repo(cur)
        result = _run(repo.get_sender_ranking("g1", START, END, 10))
        self.assertEqual(result[0]["sender_name"], "Bob")
        self.assertEqual(result[1]["sender_name"], "")


class TimeoutAndErrorTest(unittest.TestCase):
    """超时兜底向上抛、SQL 异常向上抛（service 层兜底）"""

    def test_execute_timeout_raises(self):
        class HangingCursor(FakeCursor):
            async def execute(self, sql, params=None):
                self.executed.append((sql, tuple(params or ())))
                await asyncio.sleep(5)

        cur = HangingCursor()
        repo, _, _ = _make_repo(cur)
        original = REPO.QUERY_TIMEOUT_SECONDS
        REPO.QUERY_TIMEOUT_SECONDS = 0.05
        try:
            with self.assertRaises(asyncio.TimeoutError):
                _run(repo.get_overview(None, START, END))
        finally:
            REPO.QUERY_TIMEOUT_SECONDS = original

    def test_sql_error_propagates(self):
        class ExplodingCursor(FakeCursor):
            async def execute(self, sql, params=None):
                raise RuntimeError("boom: SQL 错误向上抛")

        repo, _, _ = _make_repo(ExplodingCursor())
        with self.assertRaises(RuntimeError):
            _run(repo.get_daily_trend(None, START, END))
        with self.assertRaises(RuntimeError):
            _run(repo.get_image_window_counts(START, END, 20))


if __name__ == "__main__":
    unittest.main(verbosity=2)
