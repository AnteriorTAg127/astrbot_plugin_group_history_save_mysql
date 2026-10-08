# ruff: noqa: I001
"""astrbot_plugin_group_history_save_mysql v0.4.0 Module D 离线单元测试。

覆盖「人物分析」数据获取层 profile/fetcher.py（fake mysql_mgr / config_mgr /
event + 脚本化 OneBot 协议端，不依赖 AstrBot 运行时与真实数据库）：

- OneBot 原始消息解析 ``_onebot_raw_to_profile_message``（text 拼接 / at 剔除
  all 去重保序 / reply_id / 非文本与脏结构返 None）；
- 单群拉取：分页（DESC 多页）反转升序、超上限保留最近、message_id + 退化键去重、
  非文本过滤；
- 全局拉取：group_id=None 跨群、即使不足也不走 OneBot（event 有无均不）；
- 不足 → OneBot 补齐：筛目标发送者、at_list/reply_id 保留、与 MySQL 去重、
  协议端异常降级（onebot_error 记录、不抛）；
- 关系开关开：partners 三路识别（at 聚合 + reply 反查 + 他人→目标扫描 + OneBot
  实时补强且与库内去重）、name 取最近昵称、Top N 截断、context 拉取升序去重；
- 关系开关关：partners / context 为空、不反查不扫描不拉 OneBot；
- 各环节异常降级不抛、relation_context_complete=False。

运行方式（插件根目录，二者皆可）：
    python -m pytest "tests/v0.4.0/test_profile_fetcher.py" -v
    python "tests/v0.4.0/test_profile_fetcher.py"

stub 隔离范式沿用 test_models_capture.py：astrbot.* 全量 sys.modules stub 且
先于被测包导入；多文件合跑时仅剔除 profile 子模块缓存（不动 summary.* 兄弟子包，
避免破坏其他版本测试的 isinstance 断言）。
"""

import asyncio
import os
import sys
import types
import unittest
from datetime import datetime


# ============================================================
# 一、stub 注入（必须在任何 from astrbot_plugin_group_history_save_mysql... import 之前）
# ============================================================


def _new_module(name: str) -> types.ModuleType:
    return types.ModuleType(name)


_astrbot = _new_module("astrbot")
_astrbot_api = _new_module("astrbot.api")


class _StubLogger:
    """记录各级日志内容，便于断言降级 warning。"""

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

    def joined(self, level: str) -> str:
        return "\n".join(self.records[level])


_STUB_LOGGER = _StubLogger()
_astrbot_api.logger = _STUB_LOGGER

# ---- aiomysql（db_mysql.py 顶层导入；fetcher 经 QUERY_TIMEOUT_SECONDS 引入）----
_aiomysql = _new_module("aiomysql")
_aiomysql.connect = None
_aiomysql.DictCursor = "DictCursor"
_aiomysql.Connection = object

# ---- 多测试文件合跑兼容：仅剔除 profile 子模块（而非整包或 summary.*）----
for _name in list(sys.modules):
    if _name.startswith("astrbot_plugin_group_history_save_mysql.core.profile"):
        del sys.modules[_name]

sys.modules["astrbot"] = _astrbot
sys.modules["astrbot.api"] = _astrbot_api
sys.modules["aiomysql"] = _aiomysql

# 让被测包可被导入：<plugins 目录> 加入 sys.path
_PLUGINS_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..")
)
if _PLUGINS_DIR not in sys.path:
    sys.path.insert(0, _PLUGINS_DIR)


# ============================================================
# 二、导入被测代码（stub 已就位）
# ============================================================

from astrbot_plugin_group_history_save_mysql.core.db_mysql.stats import StatsMixin  # noqa: E402
from astrbot_plugin_group_history_save_mysql.core.profile import fetcher as fetcher_mod  # noqa: E402
from astrbot_plugin_group_history_save_mysql.core.profile.fetcher import (  # noqa: E402
    ProfileFetcher,
    _onebot_raw_to_profile_message,
)
from astrbot_plugin_group_history_save_mysql.core.profile.models import ProfileTarget  # noqa: E402


def _run(coro):
    """以独立事件循环驱动协程（无 pytest-asyncio 依赖，沿用 v0.2 范式）。"""
    return asyncio.run(coro)


# ============================================================
# 三、时间戳 / 数据构造辅助
# ============================================================


def _ts(minute: int) -> str:
    """分钟偏移 → "YYYY-MM-DD HH:MM:SS"（基准 2026-08-01 10:00:00）。"""
    return f"2026-08-01 {10 + minute // 60:02d}:{minute % 60:02d}:00"


def _unix(minute: int) -> int:
    """分钟偏移 → unix 秒（与 _ts 同一基准，供 OneBot raw.time 使用）。"""
    return int(datetime(2026, 8, 1, 10 + minute // 60, minute % 60).timestamp())


def _row(
    mid,
    sid,
    name,
    minute,
    content="hello",
    gid="9001",
    at_list="",
    reply_id="",
    mtype="text",
):
    """构造 query_messages 返回的 chat_history 行。"""
    return {
        "id": 1,
        "timestamp": _ts(minute),
        "group_id": gid,
        "sender_id": sid,
        "sender_name": name,
        "message_type": mtype,
        "content": content,
        "message_id": mid,
        "at_list": at_list,
        "reply_id": reply_id,
    }


def _raw(mid, uid, nick, minute, text="hey", at=None, reply=None, seq=None):
    """构造 get_group_msg_history 返回的单条原始消息。"""
    segs = []
    if reply:
        segs.append({"type": "reply", "data": {"id": reply}})
    for qq in at or []:
        segs.append({"type": "at", "data": {"qq": qq}})
    if text is not None:
        segs.append({"type": "text", "data": {"text": text}})
    r = {
        "time": _unix(minute),
        "message_id": mid,
        "sender": {"user_id": uid, "nickname": nick},
        "message": segs,
    }
    if seq is not None:
        r["message_seq"] = seq
    return r


# ============================================================
# 四、fake 依赖（mysql_mgr / config_mgr / event）
# ============================================================


class FakeMySQL:
    """内存版 MySQLManager：真实过滤 + DESC 分页，可注入故障。"""

    def __init__(self, rows=None):
        self.rows = rows or []
        self.query_calls = []
        self.by_ids_calls = []
        self.fail_query = None  # 每次 query_messages 抛出的异常
        self.fail_scan = False  # 仅「有 group_id 无 sender_id」的扫描池查询抛出
        self.fail_by_ids = None  # get_messages_by_ids 抛出的异常

    async def query_messages(
        self,
        group_id=None,
        sender_id=None,
        time_start=None,
        time_end=None,
        keyword=None,
        page=1,
        page_size=50,
    ):
        self.query_calls.append(
            {
                "group_id": group_id,
                "sender_id": sender_id,
                "page": page,
                "page_size": page_size,
            }
        )
        if self.fail_query is not None:
            raise self.fail_query
        if self.fail_scan and group_id and not sender_id:
            raise RuntimeError("scan boom")
        filtered = [
            r
            for r in self.rows
            if (not group_id or r["group_id"] == group_id)
            and (not sender_id or r["sender_id"] == sender_id)
        ]
        filtered.sort(key=lambda r: r["timestamp"], reverse=True)  # 固定 DESC
        total = len(filtered)
        start = (page - 1) * page_size
        return {
            "total": total,
            "records": [dict(r) for r in filtered[start : start + page_size]],
        }

    async def get_messages_by_ids(self, message_ids):
        ids = list(message_ids or [])
        self.by_ids_calls.append(ids)
        if self.fail_by_ids is not None:
            raise self.fail_by_ids
        wanted = {mid for mid in ids if mid}
        return [dict(r) for r in self.rows if r.get("message_id") in wanted]


class FakePool:
    """迷你连接池 fake：get_all_groups_summary 专用（acquire→cursor→execute→fetchall）。"""

    def __init__(self, rows=None, fail=False):
        self.rows = list(rows or [])  # [(group_id, count, last_active)]
        self.executed = []  # 记录 execute 的 SQL
        self.fail = fail

    def acquire(self):
        # 与 aiomysql.Pool.acquire 同构：普通方法返回异步上下文管理器（非协程）
        return _FakeConn(self)


class _FakeConn:
    def __init__(self, pool):
        self._pool = pool

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def cursor(self):
        return _FakeCur(self._pool)


class _FakeCur:
    def __init__(self, pool):
        self._pool = pool

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, sql, params=None):
        self._pool.executed.append(sql)
        if self._pool.fail:
            raise RuntimeError("pool boom")

    async def fetchall(self):
        # 模拟 SQL 的 ORDER BY cnt DESC, group_id ASC（输出确定性）
        rows = list(self._pool.rows)
        rows.sort(key=lambda r: (-(int(r[1] or 0)), str(r[0])))
        return rows


class FakeConfig:
    """内存版 ConfigManager：缺失键抛 KeyError（fetcher 应兜底默认值）。"""

    def __init__(self, settings=None):
        self.settings = dict(settings or {})

    async def get_profile_setting_typed(self, key):
        if key not in self.settings:
            raise KeyError(key)
        return self.settings[key]


class FakeApi:
    """脚本化 OneBot 协议端 api：按队列返回响应或统一抛异常，并记录调用。"""

    def __init__(self):
        self.calls = []
        self.queue = []  # FIFO 响应（dict）
        self.exc = None  # 非 None 时每次 call_action 抛出

    async def call_action(self, action, **payloads):
        self.calls.append((action, payloads))
        if self.exc is not None:
            raise self.exc
        if self.queue:
            return self.queue.pop(0)
        return {"messages": []}


class FakeBot:
    def __init__(self, api):
        self.api = api


class FakeEvent:
    def __init__(self, api=None):
        self.bot = FakeBot(api) if api is not None else None


# ============================================================
# 五、关系上下文测试共享数据集
# ============================================================
#
# 目标 T=100（群 g1）：
#   tm1 @200；tm2 @200,300；tm3 回复 rB（300 的消息）；tm4 回复 rC（400 的消息）
# 同群扫描池：s1 200 @100；s2 500 回复 tm1；s3 600 无互动；s4 200 @100
# OneBot 近期池：o1 200 @100（新消息）；o2 与 s4 同 message_id（去重）；o3 200 无互动
# 期望 partners：200=5（at2 + 扫描2 + onebot1，o2 去重）/ 300=2（at1 + 回复1）
#                / 400=1（回复）/ 500=1（扫描），600 不计入


def _relation_rows():
    rows = [
        _row("tm1", "100", "T", 0, at_list="200"),
        _row("tm2", "100", "T", 1, at_list="200,300"),
        _row("tm3", "100", "T", 2, reply_id="rB"),
        _row("tm4", "100", "T", 3, reply_id="rC"),
        # 被回复的消息放 g0：供 get_messages_by_ids 反查，且不污染 g1 扫描池/上下文
        _row("rB", "300", "Bob", 0, gid="9000"),
        _row("rC", "400", "Carol", 1, gid="9000"),
        # 同群扫描池
        _row("s1", "200", "Alice", 40, at_list="100"),
        _row("s2", "500", "Dave", 41, reply_id="tm1"),
        _row("s3", "600", "Eve", 42),
        _row("s4", "200", "Alice", 43, at_list="100"),
        # 各互动对象的上下文消息（g1）
        _row("cA1", "200", "Alice", 44, content="ctxA"),
        _row("cB1", "300", "Bob", 45, content="ctxB"),
        _row("cC1", "400", "Carol", 46, content="ctxC"),
        _row("cD1", "500", "Dave", 47, content="ctxD"),
    ]
    return rows


def _relation_onebot_response():
    return {
        "messages": [
            _raw("o1", "200", "AliceNew", 50, at=["100"]),
            _raw("s4", "200", "Alice", 43, at=["100"]),  # 与库内 s4 同 id → 去重
            _raw("o3", "200", "AliceNew", 51),
        ]
    }


def _relation_fetcher(api=None, settings=None):
    cfg = {
        "profile_max_count": 4,
        "profile_relation_context": True,
    }  # 4 条恰好不触发目标补齐
    cfg.update(settings or {})
    mysql = FakeMySQL(_relation_rows())
    return ProfileFetcher(mysql, FakeConfig(cfg)), mysql, FakeEvent(api)


_GROUP_TARGET = ProfileTarget(
    sender_id="100", sender_name="T", scope="group", group_id="9001"
)


# ============================================================
# 六、测试用例
# ============================================================


class TestOnebotRawParse(unittest.TestCase):
    """_onebot_raw_to_profile_message 解析。"""

    def test_text_segments_concat(self):
        raw = _raw("p1", 7, "N", 0, text=None)
        raw["message"] = [
            {"type": "text", "data": {"text": "a"}},
            {"type": "image", "data": {"file": "x"}},
            {"type": "text", "data": {"text": "b"}},
        ]
        msg = _onebot_raw_to_profile_message(raw, "9001")
        self.assertEqual(msg.content, "ab")
        self.assertEqual(msg.sender_id, "7")
        self.assertEqual(msg.source, "onebot")
        self.assertEqual(msg.group_id, "9001")

    def test_at_extract_exclude_all_dedup(self):
        raw = _raw("p2", 7, "N", 0, at=["1", "all", "2", "1", "ALL"])
        msg = _onebot_raw_to_profile_message(raw, "9001")
        self.assertEqual(msg.at_list, ["1", "2"])

    def test_reply_id_first_wins(self):
        raw = _raw("p3", 7, "N", 0, reply="r1")
        raw["message"].append({"type": "reply", "data": {"id": "r2"}})
        msg = _onebot_raw_to_profile_message(raw, "9001")
        self.assertEqual(msg.reply_id, "r1")

    def test_no_text_returns_none(self):
        raw = _raw("p4", 7, "N", 0, text=None)
        raw["message"] = [{"type": "image", "data": {"file": "x"}}]
        self.assertIsNone(_onebot_raw_to_profile_message(raw, "9001"))

    def test_missing_time_returns_none(self):
        raw = _raw("p5", 7, "N", 0)
        del raw["time"]
        self.assertIsNone(_onebot_raw_to_profile_message(raw, "9001"))

    def test_missing_message_returns_none(self):
        self.assertIsNone(_onebot_raw_to_profile_message({"time": _unix(0)}, "9001"))

    def test_garbage_segments_skipped(self):
        raw = _raw("p6", 7, "N", 0, text=None)
        raw["message"] = ["junk", None, {"type": "text", "data": {"text": "ok"}}]
        msg = _onebot_raw_to_profile_message(raw, "9001")
        self.assertEqual(msg.content, "ok")

    def test_missing_sender_defaults_empty_id(self):
        raw = _raw("p7", 7, "N", 0)
        del raw["sender"]
        msg = _onebot_raw_to_profile_message(raw, "9001")
        self.assertEqual(msg.sender_id, "")
        self.assertEqual(msg.sender_name, "")


class TestGroupFetch(unittest.TestCase):
    """单群拉取：分页 DESC→升序、上限、去重、非文本过滤。"""

    def test_pagination_desc_to_asc_and_cap(self):
        rows = [_row(f"m{i:02d}", "100", "T", i, f"msg-{i}") for i in range(12)]
        mysql = FakeMySQL(rows)
        cfg = FakeConfig({"profile_max_count": 8, "profile_relation_context": False})
        f = ProfileFetcher(mysql, cfg)
        old_page_size = fetcher_mod._PAGE_SIZE
        fetcher_mod._PAGE_SIZE = 5  # 强制多页：5 + 5 = 10 ≥ 8 止于第 2 页
        try:
            out = _run(f.fetch(_GROUP_TARGET, FakeEvent()))
        finally:
            fetcher_mod._PAGE_SIZE = old_page_size
        msgs = out.target_messages
        self.assertEqual(len(msgs), 8)  # 超上限保留最近 8 条
        self.assertEqual([m.content for m in msgs], [f"msg-{i}" for i in range(4, 12)])
        tss = [m.timestamp for m in msgs]
        self.assertEqual(tss, sorted(tss))  # 时间升序
        self.assertEqual([c["page"] for c in mysql.query_calls], [1, 2])
        self.assertTrue(all(c["sender_id"] == "100" for c in mysql.query_calls))
        self.assertEqual(out.sources, {"mysql": 8, "onebot": 0})

    def test_dedup_by_message_id_and_fallback_key(self):
        rows = [
            _row("m1", "100", "T", 0, "aaa"),
            _row("m1", "100", "T", 0, "aaa"),  # message_id 重复
            _row("m2", "100", "T", 1, "bbb"),
            # 复读消息：秒级时间戳+content 撞退化键但 message_id 不同 →
            # 有合法 id 的消息只按主键去重，不被退化键误杀（v0.4.5 F12）
            _row("m3", "100", "T", 1, "bbb"),
            _row("m4", "100", "T", 2, "ccc"),
        ]
        f = ProfileFetcher(
            FakeMySQL(rows), FakeConfig({"profile_relation_context": False})
        )
        out = _run(f.fetch(_GROUP_TARGET, FakeEvent()))
        self.assertEqual(
            [m.message_id for m in out.target_messages], ["m1", "m2", "m3", "m4"]
        )

    def test_non_text_rows_filtered(self):
        rows = [
            _row("x1", "100", "T", 0, ""),  # 空内容
            _row("x2", "100", "T", 1, "img", mtype="image"),  # 非文本类型
            _row("x3", "100", "T", 2, "ok"),
        ]
        f = ProfileFetcher(
            FakeMySQL(rows), FakeConfig({"profile_relation_context": False})
        )
        out = _run(f.fetch(_GROUP_TARGET, FakeEvent()))
        self.assertEqual([m.message_id for m in out.target_messages], ["x3"])


class TestAllFetch(unittest.TestCase):
    """全局拉取：group_id=None 跨群、绝不走 OneBot。"""

    def _rows(self):
        return [
            _row("a1", "100", "T", 0, "g1-a", gid="9001"),
            _row("a2", "100", "T", 1, "g2-a", gid="9002"),
            _row("a3", "100", "T", 2, "g1-b", gid="9001"),
            _row("a4", "100", "T", 3, "g2-b", gid="9002"),
            _row("a5", "100", "T", 4, "g1-c", gid="9001"),
        ]

    def test_all_scope_cross_group_asc_no_onebot(self):
        api = FakeApi()
        f = ProfileFetcher(FakeMySQL(self._rows()), FakeConfig())  # 全默认配置
        target = ProfileTarget(
            sender_id="100", sender_name="T", scope="all", group_id=""
        )
        out = _run(f.fetch(target, None))  # Web 全局触发 event=None
        self.assertEqual(len(out.target_messages), 5)
        self.assertEqual({m.group_id for m in out.target_messages}, {"9001", "9002"})
        tss = [m.timestamp for m in out.target_messages]
        self.assertEqual(tss, sorted(tss))
        self.assertEqual(api.calls, [])  # 全局绝不走 OneBot
        self.assertFalse(out.onebot_attempted)
        self.assertTrue(all(c["group_id"] is None for c in f._mysql_mgr.query_calls))
        self.assertEqual(out.sources, {"mysql": 5, "onebot": 0})

    def test_all_scope_short_still_no_onebot_even_with_event(self):
        api = FakeApi()  # 条数远不足阈值，即使带 event 也不补齐
        f = ProfileFetcher(FakeMySQL(self._rows()[:2]), FakeConfig())
        target = ProfileTarget(
            sender_id="100", sender_name="T", scope="all", group_id=""
        )
        out = _run(f.fetch(target, FakeEvent(api)))
        self.assertEqual(len(out.target_messages), 2)
        self.assertEqual(api.calls, [])
        self.assertFalse(out.onebot_attempted)


class TestOnebotTargetFill(unittest.TestCase):
    """单群不足 → OneBot 补齐（筛目标、保留 at/reply、去重、异常降级）。"""

    def test_shortfall_fills_with_target_only(self):
        mysql_rows = [_row(f"m0{i}", "100", "T", i, f"db-{i}") for i in range(5)]
        api = FakeApi()
        api.queue.append(
            {
                "messages": [
                    _raw("om1", "100", "T", 5, "from onebot", at=["9"], reply="m00"),
                    _raw("m04", "100", "T", 4, "dup of db"),  # 与 MySQL message_id 重复
                    _raw("om2", "777", "Other", 6, "not target"),  # 他人消息
                    _raw("om3", "100", "T", 7, text=None),  # 纯图片 → 解析 None
                    _raw("om4", "100", "T", 8, "second"),
                ]
            }
        )
        cfg = FakeConfig({"profile_max_count": 100, "profile_relation_context": False})
        f = ProfileFetcher(FakeMySQL(mysql_rows), cfg)
        out = _run(f.fetch(_GROUP_TARGET, FakeEvent(api)))

        self.assertTrue(out.onebot_attempted)
        self.assertIsNone(out.onebot_error)
        self.assertEqual(len(out.target_messages), 7)  # 5 MySQL + 2 新 OneBot
        self.assertNotIn("777", {m.sender_id for m in out.target_messages})
        om1 = next(m for m in out.target_messages if m.message_id == "om1")
        self.assertEqual(om1.at_list, ["9"])  # OneBot 消息携带 @ 标记
        self.assertEqual(om1.reply_id, "m00")  # 与回复标记
        self.assertEqual(om1.source, "onebot")
        tss = [m.timestamp for m in out.target_messages]
        self.assertEqual(tss, sorted(tss))
        self.assertEqual(out.sources, {"mysql": 5, "onebot": 2})

    def test_no_fill_when_mysql_enough(self):
        mysql_rows = [_row(f"m{i:02d}", "100", "T", i, f"db-{i}") for i in range(9)]
        api = FakeApi()
        cfg = FakeConfig({"profile_max_count": 10, "profile_relation_context": False})
        f = ProfileFetcher(FakeMySQL(mysql_rows), cfg)
        out = _run(f.fetch(_GROUP_TARGET, FakeEvent(api)))
        self.assertEqual(len(out.target_messages), 9)  # 9 ≥ 10 × 0.8 → 不补齐
        self.assertFalse(out.onebot_attempted)
        self.assertEqual(api.calls, [])

    def test_onebot_error_degrades_without_raise(self):
        mysql_rows = [_row(f"m0{i}", "100", "T", i, f"db-{i}") for i in range(5)]
        api = FakeApi()
        api.exc = RuntimeError("boom")
        cfg = FakeConfig({"profile_max_count": 100, "profile_relation_context": False})
        f = ProfileFetcher(FakeMySQL(mysql_rows), cfg)
        out = _run(f.fetch(_GROUP_TARGET, FakeEvent(api)))
        self.assertTrue(out.onebot_attempted)
        self.assertIn("boom", out.onebot_error or "")
        self.assertEqual(len(out.target_messages), 5)  # 仅 MySQL 数据
        self.assertEqual(out.sources, {"mysql": 5, "onebot": 0})


class TestRelationContextOn(unittest.TestCase):
    """关系开关开：partners 三路识别 + Top N + context 拉取。"""

    def test_partners_identification_and_context(self):
        api = FakeApi()
        api.queue.append(_relation_onebot_response())
        f, mysql, event = _relation_fetcher(api)
        out = _run(f.fetch(_GROUP_TARGET, event))

        # partners：频次降序，并列按 sender_id 升序
        self.assertEqual(
            out.partners,
            [
                ("200", "AliceNew", 5),  # at2 + 扫描 s1/s4 + onebot o1（o2 与 s4 去重）
                ("300", "Bob", 2),  # at1 + 回复反查 rB
                ("400", "Carol", 1),  # 回复反查 rC
                ("500", "Dave", 1),  # 扫描 s2（回复目标消息）
            ],
        )
        # 600（无互动）不计入
        self.assertNotIn("600", {p[0] for p in out.partners})

        # reply 反查调用：reply_id 去重排序后传入
        self.assertEqual(mysql.by_ids_calls, [["rB", "rC"]])

        # 扫描池查询：仅 group_id 无 sender_id，限量 _SCAN_POOL_SIZE
        scan_calls = [
            c
            for c in mysql.query_calls
            if c["group_id"] == "9001" and not c["sender_id"]
        ]
        self.assertEqual(len(scan_calls), 1)
        self.assertEqual(scan_calls[0]["page_size"], fetcher_mod._SCAN_POOL_SIZE)

        # context：四个对象的消息都在（含扫描池行），升序，不含目标消息
        ctx_mids = {m.message_id for m in out.context_messages}
        self.assertTrue({"cA1", "cB1", "cC1", "cD1"} <= ctx_mids)
        self.assertFalse({"tm1", "tm2", "tm3", "tm4"} & ctx_mids)
        tss = [m.timestamp for m in out.context_messages]
        self.assertEqual(tss, sorted(tss))

        self.assertTrue(out.relation_context_complete)
        self.assertTrue(out.onebot_attempted)  # 关系阶段 OneBot 补齐
        self.assertIsNone(out.onebot_error)
        # sources：目标 4 + 上下文（200×3 + 300×1 + 400×1 + 500×2 = 7）
        self.assertEqual(out.sources, {"mysql": 11, "onebot": 0})

    def test_max_partners_limits_context(self):
        f, mysql, event = _relation_fetcher(
            settings={"profile_relation_max_partners": 2}
        )
        out = _run(f.fetch(_GROUP_TARGET, event))
        self.assertEqual([p[0] for p in out.partners], ["200", "300"])
        ctx_senders = {m.sender_id for m in out.context_messages}
        self.assertTrue(ctx_senders <= {"200", "300"})  # 只为 Top2 拉上下文
        ctx_fetch_sids = {
            c["sender_id"]
            for c in mysql.query_calls
            if c["sender_id"] and c["sender_id"] != "100"
        }
        self.assertEqual(ctx_fetch_sids, {"200", "300"})


class TestRelationContextOff(unittest.TestCase):
    """关系开关关：partners/context 为空，不反查不扫描不拉 OneBot。"""

    def test_off_yields_empty_relation_outputs(self):
        api = FakeApi()
        f, mysql, event = _relation_fetcher(
            api, settings={"profile_relation_context": False}
        )
        out = _run(f.fetch(_GROUP_TARGET, event))
        self.assertEqual(len(out.target_messages), 4)
        self.assertEqual(out.partners, [])
        self.assertEqual(out.context_messages, [])
        self.assertTrue(out.relation_context_complete)
        self.assertEqual(mysql.by_ids_calls, [])  # 未反查
        # 仅目标查询（带 sender_id），无扫描池（仅 group_id）调用
        self.assertTrue(all(c["sender_id"] == "100" for c in mysql.query_calls))
        self.assertEqual(
            api.calls, []
        )  # max_count=4 不触发目标补齐，关系又关 → 零 OneBot
        self.assertFalse(out.onebot_attempted)


class TestDegradation(unittest.TestCase):
    """各环节异常：降级不抛、relation_context_complete=False。"""

    def test_by_ids_exception_degrades(self):
        api = FakeApi()
        api.queue.append(_relation_onebot_response())  # OneBot 正常，隔离反查故障
        f, mysql, event = _relation_fetcher(api)
        mysql.fail_by_ids = RuntimeError("db boom")
        out = _run(f.fetch(_GROUP_TARGET, event))  # 不得抛
        self.assertFalse(out.relation_context_complete)
        partners = {sid: cnt for sid, _n, cnt in out.partners}
        self.assertEqual(partners["200"], 5)  # at/扫描/onebot 信号不受影响
        self.assertEqual(partners["300"], 1)  # 丢失回复反查的 +1，仅剩 at

    def test_scan_query_exception_degrades(self):
        api = FakeApi()
        api.queue.append(_relation_onebot_response())  # OneBot 正常，隔离扫描故障
        f, mysql, event = _relation_fetcher(api)
        mysql.fail_scan = True
        out = _run(f.fetch(_GROUP_TARGET, event))
        self.assertFalse(out.relation_context_complete)
        partners = {sid: cnt for sid, _n, cnt in out.partners}
        # at2 + onebot o1/o2 各 1（扫描丢失 → o2 无库内 s4 可去重，按独立证据计数）
        self.assertEqual(partners["200"], 4)
        self.assertNotIn("500", partners)  # 仅扫描来源的 500 消失
        self.assertTrue(out.onebot_attempted)  # OneBot 补齐仍执行

    def test_onebot_exception_marks_incomplete(self):
        api = FakeApi()
        api.exc = RuntimeError("conn boom")
        f, _mysql, event = _relation_fetcher(api)
        out = _run(f.fetch(_GROUP_TARGET, event))
        self.assertFalse(out.relation_context_complete)
        self.assertTrue(out.onebot_attempted)
        self.assertIn("boom", out.onebot_error or "")
        partners = {sid: cnt for sid, _n, cnt in out.partners}
        self.assertEqual(partners["200"], 4)  # at2 + 扫描2，无 onebot +1
        self.assertEqual(len(out.target_messages), 4)  # 目标消息不受影响

    def test_target_mysql_exception_no_raise(self):
        api = FakeApi()
        api.queue.append(_relation_onebot_response())
        f, mysql, event = _relation_fetcher(api)
        mysql.fail_query = RuntimeError("mysql down")
        out = _run(f.fetch(_GROUP_TARGET, event))  # 不得抛
        self.assertEqual(out.target_messages, [])
        self.assertFalse(out.relation_context_complete)  # 扫描/上下文查询同样失败
        self.assertEqual(out.sources, {"mysql": 0, "onebot": 0})

    def test_event_none_group_scope_skips_onebot(self):
        # 单群但 event=None（防御场景）：不触发任何 OneBot，不抛
        f, _mysql, _event = _relation_fetcher()
        out = _run(f.fetch(_GROUP_TARGET, None))
        self.assertEqual(len(out.target_messages), 4)
        self.assertFalse(out.onebot_attempted)
        self.assertTrue(out.relation_context_complete)  # MySQL 侧关系链路完整


class _StatsHost(StatsMixin):
    """StatsMixin 挂载宿主（v0.9.0 漂移同步）：get_all_groups_summary 的
    行→dict 归一与排序断言随 SQL 逻辑从 fetcher 下沉到 db_mysql/stats.py，
    此处以既有 FakePool 驱动真实 Mixin（pool.acquire→cursor→execute→fetchall）。"""

    def __init__(self, pool):
        self.pool = pool

    async def _execute(self, cur, sql, params=None, timeout=None):
        return await cur.execute(sql, params)


class TestGroupsSummary(unittest.TestCase):
    """get_all_groups_summary（v0.5.6 群下拉模式感知配套）。

    v0.9.0 漂移同步：fetcher 该方法改为委托存储管理器同名方法
    （core/db_mysql/stats.StatsMixin.get_all_groups_summary，SQL 与结构逐字
    迁移），本组用例分两层——归一/排序语义经 StatsMixin 真实实现断言，
    fetcher 侧断言纯委托。
    """

    def _mgr(self, pool):
        return _StatsHost(pool)

    def test_counts_and_orders_by_count_desc(self):
        rows = [("9001", 5, _ts(10)), ("8000", 9, _ts(20)), ("7000", 3, _ts(30))]
        got = _run(self._mgr(FakePool(rows=rows)).get_all_groups_summary())
        self.assertEqual(
            [g["group_id"] for g in got], ["8000", "9001", "7000"]
        )  # COUNT DESC
        self.assertEqual([g["count"] for g in got], [9, 5, 3])
        # 字段类型：group_id 归一为 str、count 归一为 int、保留 last_active
        self.assertEqual(
            got[0], {"group_id": "8000", "count": 9, "last_active": _ts(20)}
        )

    def test_coerces_int_group_id_and_zero_count(self):
        rows = [(9001, 0, None)]
        got = _run(self._mgr(FakePool(rows=rows)).get_all_groups_summary())
        self.assertEqual(got, [{"group_id": "9001", "count": 0, "last_active": None}])

    def test_empty_rows_returns_empty(self):
        self.assertEqual(
            _run(self._mgr(FakePool(rows=[])).get_all_groups_summary()), []
        )

    def test_pool_failure_raises(self):
        # 契约：查询失败向上抛，由 service 层兜底为空列表
        with self.assertRaises(RuntimeError):
            _run(
                self._mgr(
                    FakePool(rows=[("9001", 1, None)], fail=True)
                ).get_all_groups_summary()
            )

    def test_fetcher_delegates_to_manager(self):
        """v0.9.0：fetcher.get_all_groups_summary 纯委托 mysql_mgr 同名方法。"""

        class _DelegatingMgr:
            def __init__(self):
                self.calls = 0

            async def get_all_groups_summary(self):
                self.calls += 1
                return [{"group_id": "1", "count": 2, "last_active": None}]

        mgr = _DelegatingMgr()
        f = ProfileFetcher(mgr, FakeConfig())
        got = _run(f.get_all_groups_summary())
        self.assertEqual(mgr.calls, 1)
        self.assertEqual(got, [{"group_id": "1", "count": 2, "last_active": None}])


if __name__ == "__main__":
    unittest.main(verbosity=2)
