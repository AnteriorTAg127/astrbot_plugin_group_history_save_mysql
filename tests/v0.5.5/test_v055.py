# ruff: noqa: I001
"""v0.5.5 分段快照体系 集成测试（Test-Agent · dev-flow 阶段 6-7）。

定位：各模块冒烟（smoke_a/b/d/f/g/h/m）按「单模块 + fake 注入」覆盖各自
契约；本文件专补**冒烟各自为政测不到的跨模块组合链**：

- TC-01 真实 ConfigManager（aiosqlite）+ 假 repo → SnapshotManager 三层归并
  端到端：跨「旧月+上月+近7天+今天」快照数据，断言 msg_total /
  msg_per_group / msg_daily_trend 无重叠无空洞、hourly 存在性优先、
  不可服务范围返 None（F2 判定规则 1/2 与趋势特例）；
- TC-02 backfill_on_startup 端到端：真实 SQLite 落库行级核验（小时/日/月
  层行数）+ 月游标推进 + 二次调用月层幂等跳过（F4.1）；
- TC-03 evict_expired 真实 DELETE 行级核验：四阈值 + monthly 不动（F4.3）；
- TC-04 真实 SnapshotManager × 真实 StatsService.build_stats 快照路径
  （total/trend/群排行三处迁快照 + overview_meta/group_active 实时配套）；
- TC-05 build_stats 回退路径：快照读取侧全 None 时 repo 调用清单断言
  （回退矩阵逐处核验）；
- TC-06 overview_daily_stats 真实链路：种子数据 → items 结构/补零/升序；
  days=32 / days=0 → None（F3）;
- TC-07 web_api.api_daily_stats 快照优先与回落（list / None 两态 +
  stats_service 未注入 + days 夹取 90）；
- TC-08 scheduler 三类任务同一轮触发序列（真实 SnapshotManager 写入真实
  SQLite）+ 日失败不 evict 不盖章且月被阻（F5）；
- TC-09 前端/元数据版本文本断言（?v=0.5.5 × 3 / label 文案 / metadata）；
- TC-10 stats 包 SnapshotManager 惰性导出与兼容别名 identity；
- TC-11 StatsService.startup_backfill 幂等与 stop 取消安全；
- TC-12/13 独立冒烟脚本（smoke_a / smoke_d）并入同一 pytest 进程执行
  （其全部内置断言纳入合跑，范式同 test_v050 TC-20/21）。

范式：沿用 tests/v0.5.0/test_v050.py —— @async_test 装饰器
（asyncio.run，无 pytest-asyncio / conftest.py）；astrbot.* / aiomysql
全部 sys.modules stub 且注入先于一切被测导入；被测包缓存剔除兼容多测试
文件合跑（含无 __file__ 的 fake 注入模块与顶层名导入两种形态）。

运行方式（插件根目录）：
    python -m pytest "tests/v0.5.5/test_v055.py" -v
"""

import asyncio
import functools
import sys
import tempfile
import types
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path


def async_test(fn):
    """装饰器：用 asyncio.run 运行异步测试函数（环境无 pytest-asyncio 依赖）。"""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        asyncio.run(fn(*args, **kwargs))

    return wrapper


_PKG = "astrbot_plugin_group_history_save_mysql"
_TEST_DIR = Path(__file__).resolve().parent
_PLUGIN_ROOT = _TEST_DIR.parents[1]  # tests/v0.5.5 → 插件根
_PLUGINS_DIR = str(_PLUGIN_ROOT.parent)  # data/plugins（包导入根）


# ============================================================
# 一、通用工具：模块剔除（多测试文件合跑兼容，范式同 test_v050）
# ============================================================


def _purge_plugin_modules() -> None:
    """剔除被测插件的全部缓存模块（两种导入形态都覆盖）。

    1) 包内名（astrbot_plugin_group_history_save_mysql 及其一切子模块）：
       **无条件**删除——smoke_m/h 等文件以 fake 模块注入 sys.modules 且不带
       __file__，仅按路径判定会漏删，导致后续 import 取到残留 fake；
    2) 顶层名导入（smoke_a/d 以顶层 db_config / stats 导入）：凡 __file__
       落在插件根内的顶层模块一律剔除。
    """
    for name in list(sys.modules):
        if name == _PKG or name.startswith(_PKG + "."):
            del sys.modules[name]
    for name, mod in list(sys.modules.items()):
        file = getattr(mod, "__file__", None)
        if not file:
            continue
        try:
            inside = Path(file).resolve().is_relative_to(_PLUGIN_ROOT)
        except OSError:
            continue
        if inside and (
            name in ("db_config", "stats", "main", "web_api", "cleaner", "db_mysql")
            or name.startswith("stats.")
        ):
            del sys.modules[name]


# ============================================================
# 二、astrbot.* / aiomysql stub 注入（必须先于任何被测导入）
# ============================================================


def _new_module(name: str) -> types.ModuleType:
    return types.ModuleType(name)


class _StubLogger:
    """记录各级日志（断言「异常仅记日志」与回填/淘汰日志）。"""

    def __init__(self):
        self.records = {"info": [], "warning": [], "error": [], "debug": []}

    def clear(self):
        for lines in self.records.values():
            lines.clear()

    def _log(self, level, msg, args):
        try:
            text = msg % args if args else str(msg)
        except Exception:
            text = str(msg)
        self.records[level].append(text)

    def info(self, msg, *args, **kwargs):
        self._log("info", msg, args)

    def warning(self, msg, *args, **kwargs):
        self._log("warning", msg, args)

    def error(self, msg, *args, **kwargs):
        self._log("error", msg, args)

    def debug(self, msg, *args, **kwargs):
        self._log("debug", msg, args)


_STUB_LOGGER = _StubLogger()

# ---- 数据目录：每个用例重置为全新临时目录（ConfigManager 构造时固化路径）----
_DATA_DIR_HOLDER = {"path": tempfile.mkdtemp(prefix="v055_it_")}


def _fresh_data_dir() -> Path:
    _DATA_DIR_HOLDER["path"] = tempfile.mkdtemp(prefix="v055_it_")
    return Path(_DATA_DIR_HOLDER["path"])


_astrbot = _new_module("astrbot")
_astrbot_api = _new_module("astrbot.api")
_astrbot_api.logger = _STUB_LOGGER
_astrbot_api.AstrBotConfig = dict


class _StubStarTools:
    @staticmethod
    def get_data_dir(plugin_name=None):
        return Path(_DATA_DIR_HOLDER["path"])


# ---- astrbot.api.event: MessageChain / AstrMessageEvent（service 顶层导入）----
_astrbot_event = _new_module("astrbot.api.event")


class FakeMessageChain:
    def __init__(self, chain=None):
        self.chain = list(chain or [])


class _StubAstrMessageEvent:
    pass


_astrbot_event.MessageChain = FakeMessageChain
_astrbot_event.AstrMessageEvent = _StubAstrMessageEvent

# ---- astrbot.api.message_components: Plain / Image ----
_astrbot_comp = _new_module("astrbot.api.message_components")


class FakePlain:
    def __init__(self, text=""):
        self.text = text


class FakeImage:
    def __init__(self, kind, source):
        self.kind = kind
        self.source = source

    @staticmethod
    def fromURL(url):
        return FakeImage("url", url)

    @staticmethod
    def fromFileSystem(path):
        return FakeImage("file", path)


_astrbot_comp.Plain = FakePlain
_astrbot_comp.Image = FakeImage

# ---- astrbot.api.star: Context / Star / StarTools ----
_astrbot_star = _new_module("astrbot.api.star")


class _StubContext:
    def __init__(self, *args, **kwargs):
        self.sent = []

    async def send_message(self, umo, chain):
        self.sent.append((umo, chain))

    def register_web_api(self, route, handler, methods, desc):
        pass


class _StubStar:
    def __init__(self, context=None, *args, **kwargs):
        self.context = context


_astrbot_star.Context = _StubContext
_astrbot_star.Star = _StubStar
_astrbot_star.StarTools = _StubStarTools

# ---- astrbot.api.web: request / json_response / error_response / file_response ----
_astrbot_web = _new_module("astrbot.api.web")


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
    def __init__(self):
        self.query = _StubQuery()
        self._json = {}
        self.method = "GET"

    async def json(self, default=None):
        return self._json if self._json is not None else default


_STUB_REQUEST = _StubRequest()
_astrbot_web.request = _STUB_REQUEST
_astrbot_web.json_response = _json_response
_astrbot_web.error_response = _error_response
_astrbot_web.file_response = _file_response

# ---- astrbot.core.star.filter.command: GreedyStr（main.py 链兼容）----
_astrbot_core = _new_module("astrbot.core")
_astrbot_core_star = _new_module("astrbot.core.star")
_astrbot_core_star_filter = _new_module("astrbot.core.star.filter")
_astrbot_core_star_filter_command = _new_module("astrbot.core.star.filter.command")


class _StubGreedyStr(str):
    pass


_astrbot_core_star_filter_command.GreedyStr = _StubGreedyStr

# ---- astrbot.core.utils.io: save_temp_img（web_api.py 顶层导入）----
_astrbot_core_utils = _new_module("astrbot.core.utils")
_astrbot_core_utils_io = _new_module("astrbot.core.utils.io")
_astrbot_core_utils_io.save_temp_img = lambda data: ""

# ---- aiomysql（db_mysql.py 顶层导入；本套件不触达真实连接池）----
_aiomysql = _new_module("aiomysql")
_aiomysql.connect = None
_aiomysql.DictCursor = "DictCursor"
_aiomysql.Connection = object

sys.modules["astrbot"] = _astrbot
sys.modules["astrbot.api"] = _astrbot_api
sys.modules["astrbot.api.event"] = _astrbot_event
sys.modules["astrbot.api.message_components"] = _astrbot_comp
sys.modules["astrbot.api.star"] = _astrbot_star
sys.modules["astrbot.api.web"] = _astrbot_web
sys.modules["astrbot.core"] = _astrbot_core
sys.modules["astrbot.core.star"] = _astrbot_core_star
sys.modules["astrbot.core.star.filter"] = _astrbot_core_star_filter
sys.modules["astrbot.core.star.filter.command"] = _astrbot_core_star_filter_command
sys.modules["astrbot.core.utils"] = _astrbot_core_utils
sys.modules["astrbot.core.utils.io"] = _astrbot_core_utils_io
sys.modules["aiomysql"] = _aiomysql

_purge_plugin_modules()

if _PLUGINS_DIR not in sys.path:
    sys.path.insert(0, _PLUGINS_DIR)

# ============================================================
# 三、导入真实被测模块（集成测试用真实链路）
# ============================================================

import astrbot_plugin_group_history_save_mysql.core.stats as STATS_PKG  # noqa: E402
from astrbot_plugin_group_history_save_mysql.core.db_config import (  # noqa: E402
    ConfigManager,
)
from astrbot_plugin_group_history_save_mysql.core.stats.models import (  # noqa: E402
    StatsQuery,
    StatsTimeRange,
)
from astrbot_plugin_group_history_save_mysql.core.stats.scheduler import (  # noqa: E402
    StatsScheduler,
)
from astrbot_plugin_group_history_save_mysql.core.stats.service import (  # noqa: E402
    StatsService,
)
from astrbot_plugin_group_history_save_mysql.core.stats.snapshot import (  # noqa: E402
    SnapshotManager,
)

import astrbot_plugin_group_history_save_mysql.core.webapi as WEB  # noqa: E402

# v0.6.0 拆分后包级 __init__ 不导出 request：以共享 stub 实例补齐包属性，
# 使既有 WEB.request.query = ... 注入对 mixin 模块（同一对象）生效。
WEB.request = _STUB_REQUEST


# ============================================================
# 四、集成测试用 fake 替身与工具
# ============================================================


class FakeRepo:
    """MySQL 聚合仓储替身：快照写入侧批量聚合 + 服务侧实时查询全部可编排。

    与 StatsRepository v0.5.5 公开方法签名一致；SnapshotManager /
    StatsService 构造后以属性覆写注入。
    """

    def __init__(self):
        self.calls: list[tuple] = []  # (method, args)
        self.overview = {
            "total": 100,
            "active_senders": 5,
            "first": None,
            "last": None,
        }
        self.overview_meta = {"active_senders": 0, "first": None, "last": None}
        self.hourly = [0] * 24
        self.weekday = [0] * 7
        self.trend: list = []
        self.sender_rows: list = []
        self.group_rows: list = []
        self.group_active: dict = {}
        self.member_overview = {"count": 0, "active_days": 0, "name": ""}
        # 快照写入侧（默认空结果：UPSERT 空列表无副作用）
        self.window_counts = {"groups": {}, "senders": {}}
        self.hourly_batch_results = {"msg": ([], []), "image": ([], [])}
        self.daily_batch_results = {"msg": [], "image": []}
        self.monthly_batch_results = {"msg": [], "image": []}
        self.raise_in: set = set()  # 方法名 → 抛 RuntimeError

    def _record(self, method, *args):
        self.calls.append((method, args))

    def _maybe_raise(self, method):
        if method in self.raise_in:
            raise RuntimeError(f"fake repo boom in {method}")

    def calls_of(self, method) -> list:
        return [args for name, args in self.calls if name == method]

    # ---- 服务侧实时查询 ----

    async def get_overview(self, group_id, start, end):
        self._record("get_overview", group_id, start, end)
        self._maybe_raise("get_overview")
        return dict(self.overview)

    async def get_overview_meta(self, group_id, start, end):
        self._record("get_overview_meta", group_id, start, end)
        self._maybe_raise("get_overview_meta")
        return dict(self.overview_meta)

    async def get_hourly_dist(self, group_id, sender_id, start, end):
        self._record("get_hourly_dist", group_id, sender_id, start, end)
        self._maybe_raise("get_hourly_dist")
        return list(self.hourly)

    async def get_weekday_dist(self, group_id, sender_id, start, end):
        self._record("get_weekday_dist", group_id, sender_id, start, end)
        self._maybe_raise("get_weekday_dist")
        return list(self.weekday)

    async def get_daily_trend(self, group_id, start, end):
        self._record("get_daily_trend", group_id, start, end)
        self._maybe_raise("get_daily_trend")
        return list(self.trend)

    async def get_sender_ranking(self, group_id, start, end, limit):
        self._record("get_sender_ranking", group_id, start, end, limit)
        self._maybe_raise("get_sender_ranking")
        return [dict(r) for r in self.sender_rows]

    async def get_group_ranking(self, start, end, limit):
        self._record("get_group_ranking", start, end, limit)
        self._maybe_raise("get_group_ranking")
        return [dict(r) for r in self.group_rows]

    async def get_group_active_senders(self, start, end):
        self._record("get_group_active_senders", start, end)
        self._maybe_raise("get_group_active_senders")
        return dict(self.group_active)

    async def get_member_overview(self, group_id, sender_id, start, end):
        self._record("get_member_overview", group_id, sender_id, start, end)
        self._maybe_raise("get_member_overview")
        return dict(self.member_overview)

    # ---- 快照写入侧（双源窗口聚合 + 批量聚合） ----

    async def get_image_window_counts(self, start, end, top_k):
        self._record("get_image_window_counts", start, end, top_k)
        self._maybe_raise("get_image_window_counts")
        return self.window_counts

    async def get_msg_window_counts(self, start, end, top_k):
        self._record("get_msg_window_counts", start, end, top_k)
        self._maybe_raise("get_msg_window_counts")
        return self.window_counts

    async def get_hourly_batch(self, source, start, end, top_k):
        self._record("get_hourly_batch", source, start, end, top_k)
        self._maybe_raise("get_hourly_batch")
        return self.hourly_batch_results[source]

    async def get_daily_batch(self, source, start, end):
        self._record("get_daily_batch", source, start, end)
        self._maybe_raise("get_daily_batch")
        return list(self.daily_batch_results[source])

    async def get_monthly_batch(self, source, start, end):
        self._record("get_monthly_batch", source, start, end)
        self._maybe_raise("get_monthly_batch")
        return list(self.monthly_batch_results[source])


class FakeContext:
    def __init__(self):
        self.sent: list = []

    async def send_message(self, umo, chain):
        self.sent.append((umo, chain))

    def register_web_api(self, route, handler, methods, desc):
        pass


async def _fresh_config() -> ConfigManager:
    """每用例全新数据目录 + 真实 ConfigManager 初始化（含 v0.5.5 十张快照表）。"""
    _fresh_data_dir()
    cfg = ConfigManager()
    assert await cfg.initialize() is True, "真实 ConfigManager 初始化失败"
    return cfg


def _horizon(now: datetime) -> datetime:
    """日层地平线：上月 1 日 00:00（与 SnapshotManager._daily_horizon 同口径）。"""
    return (
        now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        - timedelta(days=1)
    ).replace(day=1)


def _month_first(moment: datetime) -> datetime:
    return moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def _select_all(cfg: ConfigManager, sql: str, params=()) -> list:
    async with cfg.db.execute(sql, params) as cur:
        return await cur.fetchall()


async def _count(cfg: ConfigManager, table: str) -> int:
    rows = await _select_all(cfg, f"SELECT COUNT(*) FROM {table}")
    return rows[0][0]


# ============================================================
# 五、TC-01 三层归并端到端（真实 aiosqlite + 假 repo）
# ============================================================


class TestThreeLayerMerge(unittest.TestCase):
    """TC-01：跨「旧月+上月+近7天+今天」快照数据，三层归并无重叠无空洞、
    hourly 存在性优先、不可服务范围返 None。"""

    @async_test
    async def test_tc01_three_layer_merge_end_to_end(self):
        cfg = await _fresh_config()
        try:
            snap = SnapshotManager(cfg, FakeRepo())
            now = datetime.now()
            today0 = datetime(now.year, now.month, now.day)
            horizon = _horizon(now)
            yesterday = today0 - timedelta(days=1)
            hm1 = (horizon - timedelta(days=1)).replace(day=1)  # horizon 前一月
            hm2 = (hm1 - timedelta(days=1)).replace(day=1)  # 再前一旧月
            hm1s, hm2s = hm1.strftime("%Y-%m"), hm2.strftime("%Y-%m")

            # ---- 月层：两个 horizon 之前的旧月 ----
            await cfg.snapshot_upsert_monthly(
                [(hm2s, "g1", 200), (hm1s, "g1", 100), (hm1s, "g2", 50)],
                [(hm1s, "g1", 10)],
            )
            # ---- 日层：horizon 起至昨日逐日（含 d1，hourly 将与其共存验优先级）----
            daily_days: list[date] = []
            day = horizon.date()
            while day <= yesterday.date():
                daily_days.append(day)
                day += timedelta(days=1)
            await cfg.snapshot_upsert_daily(
                [(d, "g1", 7) for d in daily_days] + [(d, "g2", 3) for d in daily_days],
                [],
            )
            # ---- 时层：昨日（与日层共存）+ 今天（仅时层）----
            d1s = yesterday.strftime("%Y-%m-%d")
            today_s = today0.strftime("%Y-%m-%d")
            await cfg.snapshot_upsert_msg_hour(
                [
                    (d1s, 10, "g1", 5),
                    (d1s, 11, "g1", 6),
                    (d1s, 10, "g2", 4),
                    (today_s, 0, "g1", 2),
                ],
                [],
            )

            # ---- 期望值（独立推导）----
            n_daily = len(daily_days)
            monthly_sum = 200 + 100 + 50
            # 昨日 d1 走 hourly（15），其余完整日走 daily（每日 10）；今天仅 hourly（2）
            expected_total = monthly_sum + (n_daily - 1) * 10 + 15 + 2
            expected_g1 = 300 + (n_daily - 1) * 7 + 11 + 2
            expected_g2 = 50 + (n_daily - 1) * 3 + 4

            full_start, full_end = datetime(2000, 1, 1), today0 + timedelta(days=1)

            total = await snap.msg_total(full_start, full_end)
            self.assertEqual(
                total, expected_total, "三层归并总数：月+日+时无重叠无空洞"
            )
            per_group = await snap.msg_per_group(full_start, full_end)
            self.assertEqual(
                per_group, {"g1": expected_g1, "g2": expected_g2}, "按群明细一致"
            )
            self.assertEqual(
                await snap.msg_total(full_start, full_end, group_id="g1"),
                expected_g1,
                "群过滤口径一致",
            )

            # ---- 日趋势：horizon 起逐日可解析；昨日 hourly 存在性优先 ----
            trend = await snap.msg_daily_trend(horizon, full_end)
            self.assertIsNotNone(trend)
            self.assertEqual(
                set(trend.keys()),
                {
                    (horizon.date() + timedelta(days=i)).strftime("%Y-%m-%d")
                    for i in range((today0.date() - horizon.date()).days + 1)
                },
                "趋势覆盖 horizon~今天全部日期（无空洞）",
            )
            self.assertEqual(
                trend[d1s], 15, "昨日取 hourly 值（存在性优先，非 daily 的 10）"
            )
            self.assertEqual(trend[today_s], 2, "今天 = 小时快照求和")
            other_day = daily_days[0].strftime("%Y-%m-%d")
            if other_day != d1s:
                self.assertEqual(trend[other_day], 10, "非重叠日取 daily 行值")

            # ---- 图片侧同算法（月层单行）----
            self.assertEqual(await snap.image_total(full_start, full_end), 10)
            self.assertEqual(
                await snap.image_per_group(full_start, full_end), {"g1": 10}
            )

            # ---- 不可服务范围 → None（F2 规则 1/2 与趋势特例）----
            self.assertIsNone(
                await snap.msg_total(
                    hm1 + timedelta(days=10), hm1 + timedelta(days=20)
                ),
                "规则1：起点切断旧月中间 → 不可服务",
            )
            self.assertIsNone(
                await snap.msg_total(datetime(2000, 1, 1), hm2 + timedelta(days=15)),
                "规则2：终点切断旧月中间 → 不可服务",
            )
            self.assertIsNone(
                await snap.msg_daily_trend(hm1 + timedelta(days=20), full_end),
                "趋势特例：start < horizon → None",
            )
        finally:
            await cfg.close()


# ============================================================
# 六、TC-02 启动回填端到端（行级核验 + 游标推进 + 幂等）
# ============================================================


class TestBackfillOnStartup(unittest.TestCase):
    """TC-02：backfill_on_startup 真实 SQLite 落库行级核验与月游标语义。"""

    @async_test
    async def test_tc02_backfill_rows_cursor_and_idempotent(self):
        cfg = await _fresh_config()
        try:
            repo = FakeRepo()
            snap = SnapshotManager(cfg, repo)
            now = datetime.now()
            today0 = datetime(now.year, now.month, now.day)
            horizon = _horizon(now)
            hm1 = (horizon - timedelta(days=1)).replace(day=1)
            hm1s, hm2s = (
                hm1.strftime("%Y-%m"),
                ((hm1 - timedelta(days=1)).replace(day=1)).strftime("%Y-%m"),
            )
            ys = (today0 - timedelta(days=1)).strftime("%Y-%m-%d")

            repo.hourly_batch_results = {
                "msg": ([(ys, 9, "g1", 12)], [(ys, 9, "g1", "u1", "甲", 12)]),
                "image": ([(ys, 10, "g1", 4)], [(ys, 10, "g1", "u1", "甲", 4)]),
            }
            repo.daily_batch_results = {
                "msg": [(ys, "g1", 40)],
                "image": [(ys, "g1", 6)],
            }
            repo.monthly_batch_results = {
                "msg": [(hm2s, "g1", 500), (hm1s, "g1", 600)],
                "image": [(hm1s, "g1", 7)],
            }

            await snap.backfill_on_startup(now=now)

            # ---- 窗口参数核验（批量聚合而非逐小时循环）----
            hourly_calls = repo.calls_of("get_hourly_batch")
            self.assertEqual(len(hourly_calls), 2)  # msg + image 各一条批量 SQL
            for source, win_start, win_end, top_k in hourly_calls:
                self.assertEqual(win_start, today0 - timedelta(days=7))
                self.assertEqual(
                    win_end, now.replace(minute=0, second=0, microsecond=0)
                )
                self.assertEqual(top_k, 20)  # stats_image_top_k 默认值
            daily_calls = repo.calls_of("get_daily_batch")
            for _source, win_start, win_end in daily_calls:
                self.assertEqual(win_start, horizon)
                self.assertEqual(win_end, today0)
            monthly_calls = repo.calls_of("get_monthly_batch")
            # 首次安装游标缺省 → 全历史窗口起点 2000-01-01，终点本月 1 日
            for _source, win_start, win_end in monthly_calls:
                self.assertEqual(win_start, datetime(2000, 1, 1))
                self.assertEqual(win_end, _month_first(now))

            # ---- 行级落库核验 ----
            self.assertEqual(
                await _select_all(
                    cfg, "SELECT date, hour, group_id, msg_count FROM msg_stats_hourly"
                ),
                [(ys, 9, "g1", 12)],
            )
            self.assertEqual(
                await _select_all(
                    cfg,
                    "SELECT sender_id, sender_name, msg_count FROM msg_stats_hourly_top",
                ),
                [("u1", "甲", 12)],
            )
            self.assertEqual(
                await _select_all(
                    cfg,
                    "SELECT date, hour, group_id, image_count FROM image_stats_hourly",
                ),
                [(ys, 10, "g1", 4)],
            )
            self.assertEqual(
                await _select_all(
                    cfg, "SELECT date, group_id, msg_count FROM msg_stats_daily"
                ),
                [(ys, "g1", 40)],
            )
            self.assertEqual(
                await _select_all(
                    cfg, "SELECT date, group_id, image_count FROM image_stats_daily"
                ),
                [(ys, "g1", 6)],
            )
            self.assertEqual(
                await _select_all(
                    cfg,
                    "SELECT month, group_id, msg_count FROM msg_stats_monthly ORDER BY month",
                ),
                [(hm2s, "g1", 500), (hm1s, "g1", 600)],
            )
            self.assertEqual(
                await _select_all(
                    cfg, "SELECT month, group_id, image_count FROM image_stats_monthly"
                ),
                [(hm1s, "g1", 7)],
            )

            # ---- 月游标推进至上月 "YYYY-MM"（上月完整、为本月之前最后一月）----
            prev_s = (_month_first(now) - timedelta(days=1)).strftime("%Y-%m")
            self.assertEqual(
                await cfg.get_setting("snapshot_monthly_msg_covered", ""), prev_s
            )
            self.assertEqual(
                await cfg.get_setting("snapshot_monthly_image_covered", ""), prev_s
            )

            # ---- 二次调用：窗口层幂等重跑，月层游标防重不再聚合 ----
            monthly_before = len(repo.calls_of("get_monthly_batch"))
            await snap.backfill_on_startup(now=now)
            self.assertEqual(
                len(repo.calls_of("get_monthly_batch")),
                monthly_before,
                "游标已至上月：二次回填不再发月层聚合",
            )
            self.assertEqual(
                len(repo.calls_of("get_hourly_batch")), 4, "小时层幂等重跑"
            )
            self.assertEqual(await _count(cfg, "msg_stats_monthly"), 2, "月层行数不变")
        finally:
            await cfg.close()


# ============================================================
# 七、TC-03 淘汰真实 DELETE 行级核验
# ============================================================


class TestEvictExpired(unittest.TestCase):
    """TC-03：四阈值淘汰 + monthly 永不淘汰（行级核验）。"""

    @async_test
    async def test_tc03_evict_row_level_thresholds(self):
        cfg = await _fresh_config()
        try:
            snap = SnapshotManager(cfg, FakeRepo())
            now = datetime.now()
            today0 = datetime(now.year, now.month, now.day)
            horizon = _horizon(now)

            def _ds(offset_days: int) -> str:
                return (today0 - timedelta(days=offset_days)).strftime("%Y-%m-%d")

            hs = horizon.strftime("%Y-%m-%d")
            h_before = (horizon - timedelta(days=1)).strftime("%Y-%m-%d")
            hm1s = ((horizon - timedelta(days=1)).replace(day=1)).strftime("%Y-%m")

            # 小时层群级双表 + 消息 Top K：7 天阈值（<today-7d 删）
            hour_seed = [
                (_ds(8), 0, "g1", 1),
                (_ds(7), 0, "g1", 2),
                (_ds(1), 0, "g1", 3),
            ]
            await cfg.snapshot_upsert_msg_hour(
                hour_seed,
                [
                    (_ds(8), 0, "g1", "u1", "旧", 1),
                    (_ds(7), 0, "g1", "u1", "边界", 2),
                    (_ds(1), 0, "g1", "u1", "新", 3),
                ],
            )
            await cfg.snapshot_upsert(hour_seed, [])
            # 图片 Top K：31 天阈值（不受 7 天影响）
            await cfg.snapshot_upsert(
                [],
                [
                    (_ds(32), 0, "g1", "u1", "很旧", 1),
                    (_ds(31), 0, "g1", "u1", "边界", 2),
                    (_ds(8), 0, "g1", "u1", "近", 3),
                ],
            )
            # 日层：horizon 阈值
            await cfg.snapshot_upsert_daily(
                [(h_before, "g1", 1), (hs, "g1", 2), (_ds(1), "g1", 3)],
                [(h_before, "g1", 1), (hs, "g1", 2), (_ds(1), "g1", 3)],
            )
            # 月层：永不淘汰
            await cfg.snapshot_upsert_monthly([(hm1s, "g1", 9)], [(hm1s, "g1", 1)])

            await snap.evict_expired(now=now)

            self.assertEqual(
                await _count(cfg, "msg_stats_hourly"),
                2,
                "hourly/msg_top < today-7d 删（边界日保留）",
            )
            self.assertEqual(await _count(cfg, "image_stats_hourly"), 2)
            self.assertEqual(await _count(cfg, "msg_stats_hourly_top"), 2)
            self.assertEqual(
                await _count(cfg, "image_stats_hourly_top"),
                2,
                "图片 top 31 天阈值：32 天前删、31 天边界与 8 天前均保留",
            )
            self.assertEqual(
                await _count(cfg, "msg_stats_daily"),
                2,
                "daily < horizon 删（horizon 当日保留）",
            )
            self.assertEqual(await _count(cfg, "image_stats_daily"), 2)
            self.assertEqual(
                await _count(cfg, "msg_stats_monthly"), 1, "monthly 永不淘汰"
            )
            self.assertEqual(await _count(cfg, "image_stats_monthly"), 1)
        finally:
            await cfg.close()


# ============================================================
# 八、TC-04 / TC-05 build_stats 快照路径与回退路径
# ============================================================


def _build_real_service(cfg: ConfigManager):
    """真实 StatsService + 真实 SnapshotManager（共享 FakeRepo）。"""
    svc = StatsService(FakeContext(), None, cfg, object())
    repo = FakeRepo()
    svc.repo = repo
    svc.snapshot = SnapshotManager(cfg, repo)
    return svc, repo


class TestBuildStatsSnapshotPath(unittest.TestCase):
    """TC-04：真实 SnapshotManager × 真实 StatsService.build_stats 快照路径。"""

    @async_test
    async def test_tc04_group_view_snapshot_path(self):
        cfg = await _fresh_config()
        try:
            svc, repo = _build_real_service(cfg)
            now = datetime.now()
            today0 = datetime(now.year, now.month, now.day)
            start = today0 - timedelta(days=6)
            end = today0 + timedelta(days=1)
            today_s = today0.strftime("%Y-%m-%d")
            # 种子：前 6 天 daily（g1=10/g2=5）+ 今天 hourly（g1=4 / 图片=1）
            daily_rows, img_daily = [], []
            for i in range(6, 0, -1):
                d = (today0 - timedelta(days=i)).strftime("%Y-%m-%d")
                daily_rows.append((d, "g1", 10))
                daily_rows.append((d, "g2", 5))
                img_daily.append((d, "g1", 3))
            await cfg.snapshot_upsert_daily(daily_rows, img_daily)
            await cfg.snapshot_upsert_msg_hour([(today_s, 0, "g1", 4)], [])
            await cfg.snapshot_upsert([(today_s, 0, "g1", 1)], [])

            repo.overview_meta = {
                "active_senders": 4,
                "first": now - timedelta(hours=2),
                "last": now,
            }
            repo.hourly = [0] * 23 + [64]
            repo.sender_rows = [
                {"sender_id": "u1", "sender_name": "甲", "count": 40},
                {"sender_id": "u2", "sender_name": "乙", "count": 24},
            ]

            data = await svc.build_stats(
                StatsQuery(
                    group_id="g1",
                    member_id=None,
                    time_range=StatsTimeRange(start=start, end=end, label="近7天"),
                )
            )

            # total 由快照三层归并供数（6*10 + 今日 hourly 4）
            self.assertEqual(data.total_messages, 64)
            self.assertEqual(
                repo.calls_of("get_overview"), [], "快照路径不发全量 overview"
            )
            self.assertEqual(len(repo.calls_of("get_overview_meta")), 1, "仅补 meta")
            self.assertEqual(data.active_senders, 4)
            # 趋势由快照供数（前 6 天 10、今天 4），不发 get_daily_trend
            self.assertEqual(repo.calls_of("get_daily_trend"), [])
            self.assertEqual(len(data.daily_trend), 7)
            self.assertEqual([t["count"] for t in data.daily_trend], [10] * 6 + [4])
            # 图片注入口径：total_images 走图片三层归并（6*3 + 1）
            self.assertEqual(data.total_images, 19)
            self.assertEqual(data.peak_hour, 23)
        finally:
            await cfg.close()

    @async_test
    async def test_tc04b_all_groups_ranking_snapshot_merge(self):
        cfg = await _fresh_config()
        try:
            svc, repo = _build_real_service(cfg)
            now = datetime.now()
            today0 = datetime(now.year, now.month, now.day)
            start = today0 - timedelta(days=6)
            end = today0 + timedelta(days=1)
            daily_rows = []
            for i in range(6, 0, -1):
                d = (today0 - timedelta(days=i)).strftime("%Y-%m-%d")
                daily_rows.append((d, "g1", 10))
                daily_rows.append((d, "g2", 5))
            await cfg.snapshot_upsert_daily(daily_rows, [])
            repo.group_active = {"g1": 3, "g2": 1}

            data = await svc.build_stats(
                StatsQuery(
                    group_id=None,
                    member_id=None,
                    time_range=StatsTimeRange(start=start, end=end, label="近7天"),
                )
            )

            # 群排行 count 由快照供数 + 活跃数实时补齐，Python 侧合并排序
            self.assertEqual(repo.calls_of("get_group_ranking"), [], "不发实时群排行")
            self.assertEqual(len(repo.calls_of("get_group_active_senders")), 1)
            self.assertEqual([g.group_id for g in data.group_ranking], ["g1", "g2"])
            self.assertEqual(data.group_ranking[0].count, 60)
            self.assertEqual(data.group_ranking[0].active_senders, 3)
            self.assertEqual(data.group_ranking[1].count, 30)
            self.assertEqual(data.group_ranking[1].active_senders, 1)
        finally:
            await cfg.close()


class _NullSnapshot:
    """读取侧恒 None 的快照替身：验证 service 回退矩阵的 repo 调用清单。"""

    def __init__(self):
        self.refresh_calls = 0

    async def refresh_current_hour(self, now=None):
        self.refresh_calls += 1

    async def msg_total(self, start, end, group_id=None):
        return None

    async def msg_daily_trend(self, start, end, group_id=None):
        return None

    async def msg_per_group(self, start, end):
        return None

    async def fill_counts(self, data):
        return data


class TestBuildStatsFallbackPath(unittest.TestCase):
    """TC-05：快照读取侧全 None → 逐处精确回退实时 SQL（调用清单断言）。"""

    @async_test
    async def test_tc05_fallback_repo_call_list(self):
        cfg = await _fresh_config()
        try:
            svc = StatsService(FakeContext(), None, cfg, object())
            repo = FakeRepo()
            svc.repo = repo
            svc.snapshot = _NullSnapshot()
            repo.overview = {
                "total": 777,
                "active_senders": 6,
                "first": None,
                "last": None,
            }
            repo.trend = [("2026-01-01", 3)]
            repo.group_rows = [{"group_id": "g9", "count": 50, "active_senders": 2}]
            start, end = datetime(2026, 1, 1), datetime(2026, 1, 2)
            tr = StatsTimeRange(start=start, end=end, label="t")

            # 单群视图：回退整体 get_overview + get_daily_trend（无 meta）
            data = await svc.build_stats(
                StatsQuery(group_id="g1", member_id=None, time_range=tr)
            )
            self.assertEqual(data.total_messages, 777, "回退 overview.total")
            self.assertEqual(len(repo.calls_of("get_overview")), 1)
            self.assertEqual(
                repo.calls_of("get_overview_meta"), [], "回退路径不发 meta"
            )
            self.assertEqual(len(repo.calls_of("get_daily_trend")), 1)

            # 全部群视图：回退 get_group_ranking（含 limit），不发活跃数补齐
            repo.calls.clear()
            data_all = await svc.build_stats(
                StatsQuery(group_id=None, member_id=None, time_range=tr, top_n=10)
            )
            gr = repo.calls_of("get_group_ranking")
            self.assertEqual(len(gr), 1)
            self.assertEqual(gr[0][2], 10, "limit=top_n 透传")
            self.assertEqual(repo.calls_of("get_group_active_senders"), [])
            self.assertEqual(data_all.group_ranking[0].group_id, "g9")
        finally:
            await cfg.close()


# ============================================================
# 九、TC-06 overview_daily_stats 真实链路
# ============================================================


class TestOverviewDailyStats(unittest.TestCase):
    """TC-06：真实 ConfigManager + 真实 SnapshotManager 供数（结构/补零/升序/越界）。"""

    @async_test
    async def test_tc06_items_structure_zero_fill_and_bounds(self):
        cfg = await _fresh_config()
        try:
            svc, repo = _build_real_service(cfg)
            now = datetime.now()
            today0 = datetime(now.year, now.month, now.day)
            today_s = today0.strftime("%Y-%m-%d")
            start = today0 - timedelta(days=7)  # overview 窗口起点
            miss = (today0 - timedelta(days=3)).strftime("%Y-%m-%d")
            # 逐日种子（缺一天验补零）；今天由 hourly 承载
            msg_rows, img_rows = [], []
            for i in range(7, 0, -1):
                d = (today0 - timedelta(days=i)).strftime("%Y-%m-%d")
                if d == miss:
                    continue
                msg_rows.append((d, "g1", 11))
                img_rows.append((d, "g1", 2))
            await cfg.snapshot_upsert_daily(msg_rows, img_rows)
            await cfg.snapshot_upsert_msg_hour([(today_s, 0, "g1", 5)], [])
            await cfg.snapshot_upsert([(today_s, 0, "g1", 1)], [])

            items = await svc.overview_daily_stats(7, now=now)
            self.assertIsNotNone(items)
            self.assertEqual(len(items), 8, "窗口 [今天-7天, 今天] 共 days+1 条")
            dates = [it["date"] for it in items]
            self.assertEqual(dates, sorted(dates), "按日期升序")
            self.assertEqual(dates[0], start.strftime("%Y-%m-%d"), "首日 = 窗口起点")
            self.assertEqual(dates[-1], today_s, "末日 = 今天")
            self.assertEqual(set(items[0].keys()), {"date", "messages", "images"})
            by_date = {it["date"]: it for it in items}
            self.assertEqual(
                (by_date[miss]["messages"], by_date[miss]["images"]),
                (0, 0),
                "窗口内缺失日补 0",
            )
            self.assertEqual(
                (by_date[today_s]["messages"], by_date[today_s]["images"]),
                (5, 1),
                "今天 = 小时快照求和",
            )
            seeded = (today0 - timedelta(days=6)).strftime("%Y-%m-%d")
            self.assertEqual(
                (by_date[seeded]["messages"], by_date[seeded]["images"]), (11, 2)
            )

            # 越界 → None（契约：调用方回落 MySQL 实时路径）
            self.assertIsNone(await svc.overview_daily_stats(32, now=now))
            self.assertIsNone(await svc.overview_daily_stats(0, now=now))
        finally:
            await cfg.close()


# ============================================================
# 十、TC-07 web_api.api_daily_stats 快照优先与回落
# ============================================================


class _FakeStatsServiceForWeb:
    def __init__(self, result):
        self.result = result
        self.calls: list = []

    async def overview_daily_stats(self, days, now=None):
        self.calls.append(days)
        return self.result


class _FakeMySQLForWeb:
    def __init__(self, rows):
        self.rows = rows
        self.calls: list = []

    async def get_daily_stats(self, days):
        self.calls.append(days)
        return list(self.rows)


class TestApiDailyStats(unittest.TestCase):
    """TC-07：快照优先（list）/ 回落（None / 未注入 / 越界夹取）。"""

    def _api(self, service, mysql):
        return WEB.WebAPI(_StubContext(), mysql, None, None, stats_service=service)

    def test_tc07_snapshot_preferred(self):
        items = [{"date": "2026-08-01", "messages": 3, "images": 1}]
        service = _FakeStatsServiceForWeb(items)
        mysql = _FakeMySQLForWeb([])
        api = self._api(service, mysql)
        WEB.request.query = _StubQuery({"days": "7"})
        resp = asyncio.run(api.api_daily_stats())
        self.assertIsInstance(resp, JsonResp)
        self.assertEqual(resp.data, {"days": 7, "items": items})
        self.assertEqual(service.calls, [7])
        self.assertEqual(mysql.calls, [], "快照可供数时不打 MySQL")

    def test_tc07_snapshot_none_falls_back_mysql(self):
        mysql_rows = [{"date": "2026-08-01", "messages": 9, "images": 0}]
        service = _FakeStatsServiceForWeb(None)
        mysql = _FakeMySQLForWeb(mysql_rows)
        api = self._api(service, mysql)
        WEB.request.query = _StubQuery({"days": "7"})
        resp = asyncio.run(api.api_daily_stats())
        self.assertEqual(resp.data, {"days": 7, "items": mysql_rows})
        self.assertEqual(service.calls, [7])
        self.assertEqual(mysql.calls, [7], "None → 回落 MySQL 实时路径")

    def test_tc07_service_not_injected_mysql_path(self):
        mysql = _FakeMySQLForWeb([])
        api = self._api(None, mysql)
        WEB.request.query = _StubQuery({})
        resp = asyncio.run(api.api_daily_stats())
        self.assertEqual(resp.data["days"], 7)
        self.assertEqual(mysql.calls, [7], "未注入 stats_service 直接 MySQL")

    def test_tc07_days_clamped_90_then_fallback(self):
        service = _FakeStatsServiceForWeb(None)
        mysql = _FakeMySQLForWeb([])
        api = self._api(service, mysql)
        WEB.request.query = _StubQuery({"days": "120"})
        resp = asyncio.run(api.api_daily_stats())
        self.assertEqual(resp.data["days"], 90, ">90 夹取 90")
        self.assertEqual(service.calls, [90], "days>31 仍先问快照（由服务层返 None）")
        self.assertEqual(mysql.calls, [90], "回落 MySQL 同一 days")


# ============================================================
# 十一、TC-08 调度循环三类任务（真实 SnapshotManager 写真实 SQLite）
# ============================================================


class _FakeSchedConfig:
    def __init__(self):
        self.values = {
            "push_daily_enabled": False,
            "push_daily_time": "21:00",
            "push_weekly_enabled": False,
            "push_weekly_weekday": 1,
            "push_weekly_time": "09:00",
        }

    async def get_stats_setting_typed(self, key):
        return self.values.get(key)


class _FakeClock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class _FakePushService:
    def __init__(self):
        self.calls = []

    async def push_report(self, kind, now=None):
        self.calls.append(kind)


class TestSchedulerSnapshotLoopRealSnapshot(unittest.IsolatedAsyncioTestCase):
    """TC-08：同一轮顺序触发 小时→日(+淘汰)→月，真实快照管理器写入真实
    SQLite；日失败不淘汰不盖章且月被阻。"""

    async def _wait_until(self, cond, timeout=5.0):
        import time as _t

        deadline = _t.monotonic() + timeout
        while _t.monotonic() < deadline:
            if cond():
                return True
            await asyncio.sleep(0.005)
        return cond()

    def _seed_repo(self, repo, now):
        today0 = datetime(now.year, now.month, now.day)
        ys = (today0 - timedelta(days=1)).strftime("%Y-%m-%d")
        # 月快照目标 = 上一自然月（run_monthly_snapshot 防御性过滤只留目标月行）
        prev_s = (_month_first(now) - timedelta(days=1)).strftime("%Y-%m")
        # 小时任务走窗口聚合（上一完整小时），行键取窗口起点
        prev_hour = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
        repo.window_counts = {"groups": {"g1": 12}, "senders": {}}
        repo.daily_batch_results = {"msg": [(ys, "g1", 40)], "image": []}
        repo.monthly_batch_results = {
            "msg": [(prev_s, "g1", 600)],
            "image": [],
        }
        return ys, prev_s, prev_hour

    async def test_tc08_same_round_three_tasks_real_writes(self):
        cfg = await _fresh_config()
        try:
            repo = FakeRepo()
            now = datetime.now()
            ys, hm1s, prev_hour = self._seed_repo(repo, now)
            snap = SnapshotManager(cfg, repo)
            sched = StatsScheduler(_FakePushService(), snap, _FakeSchedConfig())
            sched.PUSH_CHECK_INTERVAL = 0.01
            sched.SNAPSHOT_CHECK_INTERVAL = 0.01
            sched._BACKOFF_SEQUENCE = [0.01, 0.02, 0.04]
            sched._now_fn = _FakeClock(now.replace(minute=26, second=0))
            await sched.start()
            try:
                # 分钟 26：三类任务同一轮全部触发，真实写入 SQLite
                self.assertTrue(
                    await self._wait_until(
                        lambda: (
                            sched._monthly_last_done == hm1s
                            and sched._daily_last_done is not None
                            and sched._snapshot_last_done is not None
                        )
                    )
                )
                self.assertEqual(sched._daily_last_done, now.date() - timedelta(days=1))
                # 行级核验：小时/日/月三层数据均已落库
                self.assertEqual(
                    await _select_all(
                        cfg, "SELECT date, hour, msg_count FROM msg_stats_hourly"
                    ),
                    [(prev_hour.strftime("%Y-%m-%d"), prev_hour.hour, 12)],
                    "小时任务聚合上一完整小时并行键落窗口起点",
                )
                self.assertEqual(
                    await _select_all(
                        cfg, "SELECT date, msg_count FROM msg_stats_daily"
                    ),
                    [(ys, 40)],
                )
                self.assertEqual(
                    await _select_all(
                        cfg, "SELECT month, msg_count FROM msg_stats_monthly"
                    ),
                    [(hm1s, 600)],
                )
                self.assertEqual(
                    await cfg.get_setting("snapshot_monthly_msg_covered", ""), hm1s
                )
            finally:
                await sched.stop()
        finally:
            await cfg.close()

    async def test_tc08b_daily_failure_no_evict_no_stamp_monthly_blocked(self):
        cfg = await _fresh_config()
        try:
            repo = FakeRepo()
            now = datetime.now()
            ys, hm1s, _prev_hour = self._seed_repo(repo, now)
            snap = SnapshotManager(cfg, repo)
            # 故障注入在调度器契约边界：run_daily_snapshot 持续抛出直至放开
            # （真实实现内部吞异常仅日志；此处验证调度器「任一步抛异常 →
            # 不盖章不淘汰、退避补跑」的循环语义）
            real_daily = snap.run_daily_snapshot
            state = {"attempts": 0, "fail": True}

            async def _flaky_daily(now=None):
                state["attempts"] += 1
                if state["fail"]:
                    raise RuntimeError("模拟日快照失败")
                return await real_daily(now)

            snap.run_daily_snapshot = _flaky_daily
            sched = StatsScheduler(_FakePushService(), snap, _FakeSchedConfig())
            sched.PUSH_CHECK_INTERVAL = 0.01
            sched.SNAPSHOT_CHECK_INTERVAL = 0.01
            sched._BACKOFF_SEQUENCE = [0.01, 0.02, 0.04]
            sched._now_fn = _FakeClock(now.replace(minute=26, second=0))
            await sched.start()
            try:
                # 小时任务照常成功；日持续失败（至少两轮退避重试）期间：
                # 不盖章、不淘汰、月被阻
                self.assertTrue(
                    await self._wait_until(
                        lambda: (
                            sched._snapshot_last_done is not None
                            and state["attempts"] >= 2
                        )
                    )
                )
                self.assertIsNone(sched._daily_last_done, "日失败不盖章")
                self.assertIsNone(sched._monthly_last_done, "日被阻月不执行")
                self.assertEqual(await _count(cfg, "msg_stats_daily"), 0, "无日落库")
                self.assertEqual(await _count(cfg, "msg_stats_monthly"), 0, "无月落库")
                # 恢复：下一轮日成功后同轮补齐淘汰与月任务
                state["fail"] = False
                self.assertTrue(
                    await self._wait_until(
                        lambda: (
                            sched._daily_last_done is not None
                            and sched._monthly_last_done == hm1s
                        )
                    )
                )
                self.assertEqual(
                    await _select_all(
                        cfg, "SELECT date, msg_count FROM msg_stats_daily"
                    ),
                    [(ys, 40)],
                )
            finally:
                await sched.stop()
        finally:
            await cfg.close()


# ============================================================
# 十二、TC-09 前端与元数据版本断言（文本级）
# ============================================================


class TestFrontendAndMetadata(unittest.TestCase):
    """TC-09：?v=0.5.6 缓存破除 ×3 / label 文案 / metadata 版本。"""

    def test_tc09_index_html_cache_busting(self):
        # v0.9.0 漂移同步：?v= 缓存参数随前端升版（0.5.6 → 0.9.0）
        html = (_PLUGIN_ROOT / "pages" / "dashboard" / "index.html").read_text(
            encoding="utf-8"
        )
        self.assertEqual(
            html.count("?v=0.9.0"), 3, "style.css/app.js/data-analysis.js 三处"
        )
        self.assertIn("./style.css?v=0.9.0", html)
        self.assertIn("./app.js?v=0.9.0", html)
        self.assertIn("./data-analysis.js?v=0.9.0", html)
        self.assertNotIn("?v=0.5.6", html, "旧版本号无残留")

    def test_tc09_data_analysis_label(self):
        js = (_PLUGIN_ROOT / "pages" / "dashboard" / "data-analysis.js").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            'label: "快照 Top K（图片/消息）"', js, "stats_image_top_k label 文案升级"
        )

    def test_tc09_metadata_version(self):
        # v0.9.0 漂移同步：metadata 版本随发布演进（v0.6.0 → v0.9.0）；
        # desc「分段快照统计」文案保留，断言不变
        meta = (_PLUGIN_ROOT / "metadata.yaml").read_text(encoding="utf-8")
        self.assertIn("version: v0.9.0", meta)
        self.assertIn("分段快照统计", meta)


# ============================================================
# 十三、TC-10 stats 包惰性导出与兼容别名
# ============================================================


class TestLazyExports(unittest.TestCase):
    """TC-10：SnapshotManager / ImageSnapshotManager 惰性导出 identity。"""

    def test_tc10_lazy_export_identity_and_alias(self):
        self.assertIn("SnapshotManager", STATS_PKG.__all__)
        self.assertIn("ImageSnapshotManager", STATS_PKG.__all__)
        import astrbot_plugin_group_history_save_mysql.core.stats.snapshot as snap_mod

        self.assertIs(STATS_PKG.SnapshotManager, snap_mod.SnapshotManager)
        self.assertIs(STATS_PKG.ImageSnapshotManager, snap_mod.ImageSnapshotManager)
        # v0.5.0 兼容别名：类更名后同一类对象
        self.assertIs(snap_mod.ImageSnapshotManager, snap_mod.SnapshotManager)
        # 重复访问缓存一致
        self.assertIs(STATS_PKG.SnapshotManager, STATS_PKG.SnapshotManager)


# ============================================================
# 十四、TC-11 startup_backfill 幂等与 stop 取消安全
# ============================================================


class _GateSnapshot:
    """backfill_on_startup 等待事件门，便于验证任务句柄生命周期。"""

    def __init__(self):
        self.gate = asyncio.Event()
        self.calls = 0

    async def backfill_on_startup(self, now=None):
        self.calls += 1
        await self.gate.wait()


class _InstantSnapshot:
    def __init__(self):
        self.calls = 0

    async def backfill_on_startup(self, now=None):
        self.calls += 1


class TestStartupBackfillLifecycle(unittest.TestCase):
    """TC-11：startup_backfill 幂等（句柄未结束不重建）+ stop 取消安全。"""

    @async_test
    async def test_tc11_idempotent_and_stop_cancels(self):
        cfg = await _fresh_config()
        try:
            svc = StatsService(FakeContext(), None, cfg, object())
            gate_snap = _GateSnapshot()
            svc.snapshot = gate_snap

            await svc.startup_backfill()
            await asyncio.sleep(0)  # 让出事件循环，使后台任务进入 backfill 协程
            first_task = svc._backfill_task
            self.assertIsNotNone(first_task)
            await svc.startup_backfill()  # 句柄未结束 → 幂等不重建
            self.assertIs(svc._backfill_task, first_task)
            self.assertEqual(gate_snap.calls, 1)

            await svc.stop()  # 取消安全清理
            self.assertTrue(first_task.done())
            self.assertIsNone(svc._backfill_task)
            await svc.stop()  # 幂等

            # 句柄自然结束后允许再次发起（新任务执行）
            svc.snapshot = _InstantSnapshot()
            await svc.startup_backfill()
            await asyncio.sleep(0.01)
            self.assertTrue(svc._backfill_task is None or svc._backfill_task.done())
            await svc.startup_backfill()
            await asyncio.sleep(0.01)
            self.assertEqual(svc.snapshot.calls, 2, "自然结束后再次发起且新任务执行")
            await svc.stop()
        finally:
            await cfg.close()


# ============================================================
# 十五、TC-12/13 独立冒烟脚本并入同一 pytest 进程
# ============================================================

# smoke_a_config.py / smoke_d_snapshot.py 为独立脚本（if __name__ ==
# "__main__"），pytest 直接收集时贡献 0 用例；此处以包装用例在同一
# pytest 进程内调用其 main()（内置 assert 断言），范式同 test_v050 TC-20/21。
_SMOKE_DIR = _PLUGIN_ROOT / "tests" / "v0.5.0"
if str(_SMOKE_DIR) not in sys.path:
    sys.path.insert(0, str(_SMOKE_DIR))


class TestLegacySmokeScriptsInProcess(unittest.TestCase):
    """TC-12/13：smoke_a / smoke_d 全部内置断言同进程执行。"""

    @async_test
    async def test_tc12_smoke_a_config_main_green(self):
        import importlib

        mod = importlib.import_module("smoke_a_config")
        await mod.main()

    @async_test
    async def test_tc13_smoke_d_snapshot_main_green(self):
        import importlib

        mod = importlib.import_module("smoke_d_snapshot")
        await mod.main()


if __name__ == "__main__":
    unittest.main(verbosity=2)
