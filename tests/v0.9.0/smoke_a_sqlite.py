"""模块 A 冒烟测试 — SQLite 存储后端（离线测试工具）。

真实 aiosqlite 临时文件库全链路冒烟：
建表 → 插入 → 查询（过滤/转义/闭区间/分页/排序/NULL 语义）→ 分块去重 →
count_messages → get_all_groups_summary → get_stats/get_daily_stats →
ping → meta 读写 → clean_old_images → purge_all → 自愈重连 →
busy_timeout 参数解析 → 唯一索引幂等。

astrbot.api / astrbot.api.star 以桩模块注入（logger 用标准 logging，
StarTools.get_data_dir 指向临时目录），core/db_sqlite.py 以独立模块加载，
不触发真实框架与其余插件模块。
"""

import asyncio
import importlib.util
import logging
import sqlite3
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path

import aiosqlite

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
DB_SQLITE_PATH = PLUGIN_ROOT / "core" / "db_sqlite.py"

# ---------------------------------------------------------------------------
# astrbot 桩模块注入
# ---------------------------------------------------------------------------

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

_astrbot_mod = types.ModuleType("astrbot")
_api_mod = types.ModuleType("astrbot.api")
_api_mod.logger = logging.getLogger("smoke_a_sqlite")
_star_mod = types.ModuleType("astrbot.api.star")


class _StarTools:
    """StarTools 桩：get_data_dir 指向临时目录（自动建目录，语义同真实实现）。"""

    _data_dir: Path | None = None

    @staticmethod
    def get_data_dir(plugin_name: str) -> Path:
        assert _StarTools._data_dir is not None
        target = _StarTools._data_dir / plugin_name
        target.mkdir(parents=True, exist_ok=True)
        return target


_star_mod.StarTools = _StarTools
_astrbot_mod.api = _api_mod
sys.modules["astrbot"] = _astrbot_mod
sys.modules["astrbot.api"] = _api_mod
sys.modules["astrbot.api.star"] = _star_mod

_spec = importlib.util.spec_from_file_location("db_sqlite_under_test", DB_SQLITE_PATH)
assert _spec is not None and _spec.loader is not None
db_sqlite = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(db_sqlite)

# 固定「当前时间」：db_sqlite 模块内的 datetime.now() 全部返回 T_NOW，
# 使冒烟与真实系统时钟解耦（get_stats/get_daily_stats 的"今日"判定、
# clean_old_images 的清理窗口、timestamp=None 缺省时间均可确定性断言）
_T_NOW = datetime(2026, 8, 20, 12, 0, 0)


class _FixedNowDatetime(datetime):
    """datetime 子类：now() 返回 T_NOW，其余行为与真实 datetime 一致。"""

    @classmethod
    def now(cls, tz=None):  # noqa: ARG003
        return _T_NOW


db_sqlite.datetime = _FixedNowDatetime
SQLiteManager = db_sqlite.SQLiteManager

# ---------------------------------------------------------------------------
# 断言计数器
# ---------------------------------------------------------------------------

passed = 0
failed = 0


def check(cond: bool, name: str) -> None:
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"  [FAIL] {name}")


def section(title: str) -> None:
    print(f"== {title}")


# ---------------------------------------------------------------------------
# 用例数据
# ---------------------------------------------------------------------------

T0 = datetime(2026, 8, 19, 10, 0, 0)  # 相对固定"今日"（2026-08-20 12:00）为昨日


def ts(offset_seconds: int) -> datetime:
    return T0 + timedelta(seconds=offset_seconds)


def fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


MESSAGES = [
    # (group_id, sender_id, sender_name, message_type, content, message_id,
    #  at_list, reply_id, timestamp)
    ("111", "a1", "张三", "text", "今天天气不错", "m1", "", "", ts(0)),
    ("111", "a2", "李四", "text", "是啊 100% 适合 _ 出行", "m2", "11111", "m1", ts(60)),
    ("111", "a1", "张三", "mixed", "看图 [图片]", "m3", "", "", ts(120)),
    ("222", "a1", "张三", "text", "222群的独立消息 %特殊", "m4", "", "", ts(180)),
    ("111", "a3", "王五", "text", "无ID消息一", "", "", "", ts(240)),
    ("111", "a3", "王五", "text", "无ID消息二", "", "", "", ts(300)),
    ("111", "a1", "张三", "text", "x" * 60001, "m7", "", "", ts(360)),
    ("222", "a2", "李四", "text", "Hello World ABC", "m8", "", "", ts(420)),
]

IMAGES = [
    # (group_id, sender_id, image_url, sender_name, timestamp)
    ("111", "a1", "http://img/1.jpg", "张三", ts(10)),
    ("111", "a2", "http://img/2.jpg", "李四", ts(70)),
    ("111", "a1", "http://img/3.jpg", "张三", ts(130)),
    ("222", "a1", "http://img/4.jpg", "张三", ts(190)),
]

G111_ALL = {"m1", "m2", "m3", "m7"}  # g111 非空 message_id（m5/m6 为空 ID）


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


async def main() -> None:
    mgr = SQLiteManager(wal_mode=True, busy_timeout_ms=5000)
    db_path = mgr.db_path

    # ------------------------------------------------------------------
    section("T1 建表与初始化")
    # ------------------------------------------------------------------
    check(db_path.endswith("history.db"), "db 文件名为 history.db")
    check(
        str(Path(db_path).parent.name) == "astrbot_plugin_group_history_save_mysql",
        "db 位于 StarTools 数据目录下（与 config.db 分文件）",
    )
    check(await mgr.initialize() is True, "initialize() 返回 True")
    check(await mgr.initialize() is True, "initialize() 幂等（重复调用仍 True）")

    async with aiosqlite.connect(db_path) as conn:
        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ) as cursor:
            tables = {row[0] for row in await cursor.fetchall()}
        check(
            {"chat_history", "image_records", "meta"}.issubset(tables),
            "三张表全部建成（chat_history/image_records/meta）",
        )
        async with conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='index'"
            " AND name LIKE 'idx_%'"
        ) as cursor:
            index_rows = {row[0]: row[1] for row in await cursor.fetchall()}
        expected_indexes = {
            "idx_chat_history_message_id",
            "idx_chat_history_group_time",
            "idx_chat_history_sender",
            "idx_chat_history_timestamp",
            "idx_image_records_group_time",
        }
        check(
            expected_indexes.issubset(index_rows),
            "5 个索引全部建成（CREATE INDEX IF NOT EXISTS 幂等）",
        )
        check(
            index_rows.get("idx_chat_history_message_id", "")
            .lstrip()
            .upper()
            .startswith("CREATE UNIQUE INDEX"),
            "idx_chat_history_message_id 为唯一索引",
        )
        async with conn.execute("PRAGMA journal_mode") as cursor:
            mode_row = await cursor.fetchone()
        check(
            mode_row is not None and str(mode_row[0]).lower() == "wal", "WAL 模式已生效"
        )
        async with conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='chat_history'"
        ) as cursor:
            ddl = (await cursor.fetchone())[0]
        for col in (
            "id",
            "message_id",
            "group_id",
            "sender_id",
            "sender_name",
            "content",
            "timestamp",
            "type",
            "raw_data",
            "image_urls",
            "at_list",
            "reply_id",
        ):
            check(col in ddl, f"chat_history 含列 {col}")
        check(mgr.wal_active is True, "wal_active 标记为 True")

    # ------------------------------------------------------------------
    section("T2 插入（insert_chat_message / insert_image_record）")
    # ------------------------------------------------------------------
    for args in MESSAGES:
        ok = await mgr.insert_chat_message(*args)
        check(ok is True, f"insert_chat_message {args[5] or '空ID'} 成功")

    # 重复 message_id：唯一索引去重，幂等返回 True 且不产生重复行
    dup_args = MESSAGES[0]
    check(
        await mgr.insert_chat_message(*dup_args) is True,
        "重复 message_id 幂等返回 True（唯一索引去重）",
    )
    async with aiosqlite.connect(db_path) as conn:
        async with conn.execute(
            "SELECT COUNT(*) FROM chat_history WHERE message_id = 'm1'"
        ) as cursor:
            check((await cursor.fetchone())[0] == 1, "m1 仅一条（无重复行）")

    # 空 message_id 可共存多条（NULL 不参与 UNIQUE 去重，与 MySQL 行为一致）
    async with aiosqlite.connect(db_path) as conn:
        async with conn.execute(
            "SELECT COUNT(*) FROM chat_history WHERE message_id IS NULL"
        ) as cursor:
            check((await cursor.fetchone())[0] == 2, "两条空 message_id 消息共存")

    # 超长内容截断（对齐 MySQL 侧 60000 字节截断行为）
    async with aiosqlite.connect(db_path) as conn:
        async with conn.execute(
            "SELECT content FROM chat_history WHERE message_id = 'm7'"
        ) as cursor:
            content = (await cursor.fetchone())[0]
    check(
        content.endswith("\n…[内容过长已截断]")
        and len(content.encode("utf-8")) <= 60000 + len("\n…[内容过长已截断]".encode()),
        "超长内容已截断并留标记",
    )

    for args in IMAGES:
        check(
            await mgr.insert_image_record(*args) is True,
            f"insert_image_record {args[2]} 成功",
        )
    # 同 URL 重复写入（image_records 无唯一约束，与 MySQL 口径一致）
    check(
        await mgr.insert_image_record(*IMAGES[0]) is True,
        "重复图片 URL 正常写入（无唯一约束）",
    )
    # 超长 URL 跳过入库
    check(
        await mgr.insert_image_record("111", "a1", "h" * 2000) is False,
        "超长图片 URL 跳过入库返回 False",
    )
    async with aiosqlite.connect(db_path) as conn:
        async with conn.execute("SELECT COUNT(*) FROM image_records") as cursor:
            check((await cursor.fetchone())[0] == 5, "image_records 共 5 行")

    # ------------------------------------------------------------------
    section("T3 查询（query_messages 多条件/转义/闭区间/分页/排序）")
    # ------------------------------------------------------------------
    result = await mgr.query_messages(group_id="111")
    check(result["total"] == 6, "g111 total=6")
    check(
        [r["message_id"] for r in result["records"]]
        == ["m7", None, None, "m3", "m2", "m1"],
        "排序 timestamp DESC（空 ID 为 None）",
    )
    expected_keys = [
        "id",
        "timestamp",
        "group_id",
        "sender_id",
        "sender_name",
        "message_type",
        "content",
        "message_id",
        "at_list",
        "reply_id",
    ]
    check(
        list(result["records"][0].keys()) == expected_keys,
        "记录键与 MySQL 侧 DictCursor 完全同构",
    )
    m2 = next(r for r in result["records"] if r["message_id"] == "m2")
    check(
        m2["at_list"] == "11111" and m2["reply_id"] == "m1", "at_list/reply_id 原样返回"
    )
    check(m2["timestamp"] == fmt(ts(60)), "timestamp 输出 'YYYY-MM-DD HH:MM:SS' 字符串")
    m5 = result["records"][2]
    check(
        m5["message_id"] is None and m5["sender_name"] == "王五",
        "NULL 语义保持原样（message_id=None）",
    )
    check(m2["message_type"] == "text", "type 列映射为 message_type 键")

    # LIKE 通配符转义：'%' '_' 仅按字面匹配
    result = await mgr.query_messages(group_id="111", keyword="100%")
    check(
        result["total"] == 1 and result["records"][0]["message_id"] == "m2",
        "keyword '100%' 仅命中字面量",
    )
    result = await mgr.query_messages(group_id="111", keyword="_")
    check(
        result["total"] == 1 and result["records"][0]["message_id"] == "m2",
        "keyword '_' 仅命中字面量",
    )
    result = await mgr.query_messages(group_id="111", keyword="无ID消息")
    check(result["total"] == 2, "keyword 常规命中 2 条")
    # 大小写不敏感（对齐 MySQL utf8mb4_unicode_ci）
    result = await mgr.query_messages(group_id="222", keyword="hello world")
    check(
        result["total"] == 1 and result["records"][0]["message_id"] == "m8",
        "keyword 大小写不敏感",
    )

    # 时间闭区间（含两端）
    result = await mgr.query_messages(
        group_id="111", time_start=fmt(ts(60)), time_end=fmt(ts(240))
    )
    check(
        [r["message_id"] for r in result["records"]] == [None, "m3", "m2"]
        and result["total"] == 3,
        "时间闭区间 [60s, 240s] 命中 m2/m3/空ID",
    )
    # 纯日期边界（按零点起算）
    result = await mgr.query_messages(group_id="111", time_start="2026-08-19")
    check(result["total"] == 6, "纯日期 time_start 零点起算")
    # 非法时间参数 → 空结果（与 MySQL 隐式转换失败行为等效）
    result = await mgr.query_messages(group_id="111", time_start="not-a-time")
    check(result["total"] == 0 and result["records"] == [], "非法时间参数返回空结果")

    # 分页 + 排序稳定
    page1 = await mgr.query_messages(group_id="111", page=1, page_size=2)
    page2 = await mgr.query_messages(group_id="111", page=2, page_size=2)
    page4 = await mgr.query_messages(group_id="111", page=4, page_size=2)
    check([r["message_id"] for r in page1["records"]] == ["m7", None], "分页第 1 页")
    check([r["message_id"] for r in page2["records"]] == [None, "m3"], "分页第 2 页")
    check(page4["records"] == [] and page4["total"] == 6, "超出页码空记录但 total 不变")

    # sender 过滤
    result = await mgr.query_messages(group_id="111", sender_id="a1")
    check(
        result["total"] == 3
        and {r["message_id"] for r in result["records"]} == {"m1", "m3", "m7"},
        "sender_id 过滤",
    )

    # raise_on_error=True 路径（正常参数不抛）
    result = await mgr.query_messages(group_id="111", raise_on_error=True)
    check(result["total"] == 6, "raise_on_error=True 正常路径不受影响")

    # ------------------------------------------------------------------
    section("T4 get_messages_by_ids（分块 ≤500）与批量去重")
    # ------------------------------------------------------------------
    rows = await mgr.get_messages_by_ids(["m1", "m3", "m4"])
    check(len(rows) == 3, "get_messages_by_ids 命中 3 条")
    check(
        list(rows[0].keys()) == expected_keys[1:],
        "get_messages_by_ids 键与 MySQL 侧一致（不含 id）",
    )
    check(
        rows[0]["timestamp"] == fmt(ts(0)) and rows[0]["message_id"] == "m1",
        "按 ID 查询行内容正确",
    )
    check(await mgr.get_messages_by_ids([]) == [], "空入参返回 []")
    check(await mgr.get_messages_by_ids(["", ""]) == [], "全空串入参返回 []")

    # 分块：600 个 id（8 个真实 + 592 个不存在），跨 2 块
    big_ids = ["m1", "m2", "m3", "m4", "m7", "m8"] + [f"fake{i}" for i in range(594)]
    rows = await mgr.get_messages_by_ids(big_ids)
    check(
        {r["message_id"] for r in rows} == {"m1", "m2", "m3", "m4", "m7", "m8"},
        "600 id 分块查询跨块聚合正确",
    )

    existing = await mgr.get_existing_message_ids("111", big_ids)
    check(
        existing == G111_ALL,
        "get_existing_message_ids 分块去重集合正确（空 ID 不返回）",
    )
    check(await mgr.get_existing_message_ids("111", []) == set(), "空入参返回空集")
    existing = await mgr.get_existing_image_urls(
        "111", ["http://img/1.jpg", "http://img/2.jpg"] + [f"u{i}" for i in range(600)]
    )
    check(
        existing == {"http://img/1.jpg", "http://img/2.jpg"},
        "get_existing_image_urls 分块去重集合正确",
    )

    group_ids = await mgr.get_all_group_ids()
    check(set(group_ids) == {"111", "222"}, "get_all_group_ids 去重清单")

    recent = await mgr.get_recent_messages("111", 2)
    check(
        [r["message_id"] for r in recent] == ["m7", ""]
        and recent[1]["content"] == "无ID消息二",
        "get_recent_messages 按 timestamp DESC（空 ID 兜底空串）",
    )
    check(await mgr.get_recent_messages("111", 0) == [], "limit<=0 返回空列表")
    check(await mgr.get_recent_messages("111", -5) == [], "limit 负数返回空列表")

    last_time = await mgr.get_last_message_time("111")
    check(
        isinstance(last_time, datetime) and last_time == ts(360),
        "get_last_message_time 返回 datetime 且为最大 timestamp",
    )
    check(await mgr.get_last_message_time("999") is None, "无记录群返回 None")

    # ------------------------------------------------------------------
    section("T5 count_messages（GROUP BY 聚合 / 参数校验 / _error 键）")
    # ------------------------------------------------------------------
    out = await mgr.count_messages(group_id="111")
    check(
        out.get("group_total") == 6 and "senders" not in out and "_error" not in out,
        "仅 group_id：group_total=6 且无 senders/_error 键",
    )
    out = await mgr.count_messages(group_id="111", sender_ids=["a1", "a2", "a3"])
    check(out["group_total"] == 6, "群+人：group_total=6")
    check(
        out["senders"]["a1"] == {"in_group": 3, "total": 4}
        and out["senders"]["a2"] == {"in_group": 1, "total": 2}
        and out["senders"]["a3"] == {"in_group": 2, "total": 2},
        "批量人统计（in_group/total）正确",
    )
    out = await mgr.count_messages(sender_ids=["a1"])
    check(
        out["senders"]["a1"] == {"in_group": 0, "total": 4},
        "仅 sender_ids：in_group=0（对齐 MySQL NULL 比较行为）",
    )
    out = await mgr.count_messages(group_id="111", time_start=fmt(ts(60)))
    check(out["group_total"] == 5, "时间条件（闭区间下界）计数")
    out = await mgr.count_messages(group_id="111", time_start="bad-time")
    check(out.get("group_total") == 0 and "_error" not in out, "非法时间参数计数为 0")
    out = await mgr.count_messages()
    check("_error" in out, "参数全缺 → _error 键")
    out = await mgr.count_messages(sender_ids=[f"s{i}" for i in range(501)])
    check("_error" in out and "500" in out["_error"], "sender_ids 超 500 → _error 键")

    # ------------------------------------------------------------------
    section("T6 get_all_groups_summary / get_stats / get_daily_stats")
    # ------------------------------------------------------------------
    summary = await mgr.get_all_groups_summary()
    check(
        [s["group_id"] for s in summary] == ["111", "222"]
        and [s["count"] for s in summary] == [6, 2],
        "群汇总按 count DESC 排序",
    )
    check(
        all(isinstance(s["last_active"], datetime) for s in summary)
        and summary[0]["last_active"] == ts(360),
        "last_active 为 datetime（MAX timestamp）",
    )

    # 今日消息（timestamp=None → datetime.now()，已 patch 为 T_NOW）与今日图片
    check(
        await mgr.insert_chat_message("111", "a1", "张三", "text", "刚刚的消息", "m9"),
        "插入今日消息（timestamp=None 缺省取当前时间）",
    )
    # 图片时间回退 5 秒：确保落在 clean_old_images(0) 的严格下界之内
    check(
        await mgr.insert_image_record(
            "111", "a1", "http://img/now.jpg", timestamp=_T_NOW - timedelta(seconds=5)
        ),
        "插入今日图片（时间回退 5 秒）",
    )
    stats = await mgr.get_stats()
    check(
        stats["total_messages"] == 9 and stats["total_images"] == 6,
        "get_stats 总量正确",
    )
    check(
        stats["today_messages"] == 1 and stats["today_images"] == 1,
        "get_stats 今日计数正确",
    )
    daily = await mgr.get_daily_stats(7)
    daily_map = {d["date"]: d for d in daily}
    check(len(daily) == 2, "get_daily_stats 仅昨日/今日两行")
    check(
        daily_map.get("2026-08-19", {}).get("messages") == 8
        and daily_map.get("2026-08-19", {}).get("images") == 5,
        "get_daily_stats 昨日行正确（DATE(unixepoch, localtime)）",
    )
    check(
        daily_map.get("2026-08-20", {}).get("messages") == 1
        and daily_map.get("2026-08-20", {}).get("images") == 1,
        "get_daily_stats 今日行正确",
    )

    # ------------------------------------------------------------------
    section("T7 ping 结构")
    # ------------------------------------------------------------------
    ping = await mgr.ping()
    check(ping["connected"] is True, "ping connected=True")
    check(
        isinstance(ping["latency_ms"], (int, float)) and ping["latency_ms"] >= 0,
        "ping latency 合理",
    )
    check(
        ping["pool"] == {"used": 1, "current_size": 1, "min_size": 1, "max_size": 1},
        "pool 占位结构（commands.py pool_info 取值路径不炸）",
    )
    check(ping["db"] == db_path, "ping db 键为 history.db 完整路径")

    # ------------------------------------------------------------------
    section("T8 meta 读写")
    # ------------------------------------------------------------------
    check(await mgr.get_meta("migrated_from_mysql") is None, "meta 键不存在返回 None")
    check(
        await mgr.set_meta("migrated_from_mysql", "2026-08-20T00:00:00") is True,
        "set_meta 返回 True",
    )
    check(
        await mgr.get_meta("migrated_from_mysql") == "2026-08-20T00:00:00",
        "get_meta 读回迁移标记",
    )
    check(await mgr.set_meta("migrated_from_mysql", "done") is True, "set_meta 覆盖写")
    check(await mgr.get_meta("migrated_from_mysql") == "done", "覆盖后读取新值")

    # ------------------------------------------------------------------
    section("T9 clean_old_images")
    # ------------------------------------------------------------------
    deleted = await mgr.clean_old_images(0)
    check(deleted == 6, "clean_old_images(0) 清掉全部 6 条图片记录")
    async with aiosqlite.connect(db_path) as conn:
        async with conn.execute("SELECT COUNT(*) FROM image_records") as cursor:
            check((await cursor.fetchone())[0] == 0, "image_records 已清空")
    check(await mgr.clean_old_images(99999) == 0, "超范围保留天数钳制后正常执行")
    check(await mgr.clean_old_images(7) == 0, "空表清理返回 0")

    # ------------------------------------------------------------------
    section("T10 purge_all 与自增复位")
    # ------------------------------------------------------------------
    result = await mgr.purge_all()
    check(
        result["success"] is True
        and result["deleted_messages"] == 9
        and result["deleted_images"] == 0
        and result["truncated"] is False,
        "purge_all 返回结构与计数正确",
    )
    async with aiosqlite.connect(db_path) as conn:
        async with conn.execute("SELECT COUNT(*) FROM chat_history") as cursor:
            check((await cursor.fetchone())[0] == 0, "chat_history 已清空")
        async with conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name='chat_history'"
        ) as cursor:
            check(await cursor.fetchone() is None, "sqlite_sequence 已复位")
    check(
        await mgr.get_meta("migrated_from_mysql") == "done",
        "purge 后 meta 表不受影响",
    )
    # 清空后自增 ID 从 1 重新开始（对齐 MySQL TRUNCATE 语义）
    check(
        await mgr.insert_chat_message(
            "111", "a1", "张三", "text", "purge 后第一条", "m10"
        )
        is True,
        "purge 后重新插入成功",
    )
    async with aiosqlite.connect(db_path) as conn:
        async with conn.execute(
            "SELECT id FROM chat_history WHERE message_id='m10'"
        ) as cursor:
            check((await cursor.fetchone())[0] == 1, "自增 ID 复位后从 1 开始")

    # ------------------------------------------------------------------
    section("T11 自愈重连 / close 幂等 / 参数解析 / DB 级唯一约束")
    # ------------------------------------------------------------------
    await mgr.close()
    await mgr.close()  # 幂等
    check(True, "close() 幂等无异常")
    # 关闭后再次读写：_ensure_db 自愈重连
    check(
        await mgr.insert_chat_message("111", "a2", "李四", "text", "重连后消息", "m11")
        is True,
        "close 后写入自愈重连",
    )
    result = await mgr.query_messages(group_id="111")
    check(result["total"] == 2, "close 后查询自愈重连（m10/m11）")
    ping = await mgr.ping()
    check(ping["connected"] is True, "close 后 ping 自愈重连")
    await mgr.close()

    # busy_timeout 参数解析：非法回退 / 超范围夹取
    mgr2 = SQLiteManager(wal_mode=True, busy_timeout_ms="abc")
    check(mgr2.busy_timeout_ms == 5000, "busy_timeout 非法值回退 5000")
    mgr3 = SQLiteManager(wal_mode=True, busy_timeout_ms="99999")
    check(mgr3.busy_timeout_ms == 60000, "busy_timeout 超 60000 夹取上界")
    mgr4 = SQLiteManager(wal_mode=True, busy_timeout_ms=-1)
    check(mgr4.busy_timeout_ms == 0, "busy_timeout 负值夹取下界")
    mgr5 = SQLiteManager(wal_mode=False)
    check(
        mgr5.busy_timeout_ms == 5000 and mgr5.wal_mode is False,
        "默认参数（WAL 关/timeout 5000）",
    )

    # DB 级唯一索引：绕过 manager 直接插入重复 message_id 必须被拒绝
    async with aiosqlite.connect(db_path) as conn:
        raised = False
        try:
            await conn.execute(
                "INSERT INTO chat_history (message_id, group_id, timestamp, type)"
                " VALUES ('m10', '111', 0, 'text')"
            )
            await conn.commit()
        except sqlite3.IntegrityError:
            raised = True
        check(raised, "DB 级唯一索引拒绝重复 message_id（INSERT OR IGNORE 幂等可用）")

    await mgr2.initialize()
    await mgr2.close()
    await mgr3.initialize()
    await mgr3.close()
    await mgr4.initialize()
    await mgr4.close()
    await mgr5.initialize()
    check(mgr5.wal_active is False, "wal_mode=False 不执行 WAL PRAGMA")
    await mgr5.close()

    print()
    print(f"冒烟结果：{passed}/{passed + failed} 通过")
    if failed:
        sys.exit(1)


def _run() -> None:
    # 工作目录建在本目录下（tests/ 整体被忽略；DSH 沙箱不允许 tempfile
    # 安全区与 mkdtemp 目录内创建文件，普通目录可写）
    import shutil
    import time as _time

    _tmp_root = Path(__file__).resolve().parent / ".smoke_tmp"
    if _tmp_root.exists():
        shutil.rmtree(_tmp_root, ignore_errors=True)
    _StarTools._data_dir = _tmp_root / f"run_{int(_time.time())}"
    _StarTools._data_dir.mkdir(parents=True)
    print(f"临时数据目录: {_StarTools._data_dir}")
    try:
        asyncio.run(main())
    finally:
        shutil.rmtree(_tmp_root, ignore_errors=True)


if __name__ == "__main__":
    # DSH 沙箱下控制台默认 GBK：强制 UTF-8 输出中文日志不乱码
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    _run()
