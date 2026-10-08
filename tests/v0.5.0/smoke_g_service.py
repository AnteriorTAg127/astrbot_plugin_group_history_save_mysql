# ruff: noqa: I001
"""v0.5.0 模块 G 编排服务（stats/service.py）冒烟脚本。

被测对象：``StatsService`` —— build_stats 三端同源数据组装（补零连续性 /
ratio / rank / peak_hour / avg_per_day / 全部群视图 group_ranking / 异常包装
StatsBuildError）、check_cooldown（首次放行/冷却内拒绝/0 不限流）、umo 缓存
（记录/读取/空值不记录）、push_report（无 umo 跳过 / 渲染失败降级纯文本 /
发送调用参数 / 群间顺序与 1s 间隔 / 单群异常不阻断整批）、render 透传、
start/stop 生命周期（可重入、异常不抛、stop 清缓存）。

范式沿用 tests/v0.5.0/smoke_b_repository.py 与 smoke_e_render.py：
astrbot.* / aiomysql 全部 sys.modules stub（注入在任何被测包 import 之前）。

模块 D（snapshot）/ 模块 F（scheduler）已按契约交付，本脚本不再向其
sys.modules 注入同名 fake 子模块（多测试文件合跑时各自的顶层清理代码会
互相驱逐注入项），改为：

- snapshot：真实 ``ImageSnapshotManager(config_mgr, repo)`` 构造轻量无副作用，
  由 StatsService 真实构造后以属性覆写为 ``FakeSnapshotManager``；
- scheduler：生命周期用例在**测试执行期**把真实 ``stats.scheduler`` 模块的
  ``StatsScheduler`` 属性替换为 ``FakeScheduler``（用毕还原）——执行期
  sys.modules 已稳定（跨文件驱逐只发生在收集期顶层代码），替换必然命中
  ``service.start()`` 内部 ``from .scheduler import StatsScheduler`` 的解析。

构造后再以 fake repo/snapshot/renderer/context/config 覆写公开属性，
完全不依赖真实 MySQL / SQLite / AstrBot 运行时。

运行方式（插件根目录，二者皆可）：
    python -m pytest "tests/v0.5.0/smoke_g_service.py" -v
    python "tests/v0.5.0/smoke_g_service.py"
"""

import asyncio
import importlib
import os
import sys
import time
import types
import unittest
from datetime import datetime


# ============================================================
# 一、astrbot.* / aiomysql stub 注入（必须在导入被测包之前）
# ============================================================


def _new_module(name: str) -> types.ModuleType:
    return types.ModuleType(name)


_astrbot = _new_module("astrbot")
_astrbot_api = _new_module("astrbot.api")


class _StubLogger:
    """记录各级日志内容，便于断言（无 umo warning / 降级 warning 等）。"""

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


_STUB_LOGGER = _StubLogger()
_astrbot_api.logger = _STUB_LOGGER

# ---- MessageChain / message_components stub（service 推送链构造依赖）----
_astrbot_api_event = _new_module("astrbot.api.event")


class FakeMessageChain:
    """消息链 stub：仅承载 chain 列表（与真实 MessageChain 构造签名一致）。"""

    def __init__(self, chain=None):
        self.chain = list(chain or [])


_astrbot_api_event.MessageChain = FakeMessageChain

_astrbot_api_comp = _new_module("astrbot.api.message_components")


class FakeImage:
    def __init__(self, kind, source):
        self.kind = kind  # "url" / "file"
        self.source = source

    @staticmethod
    def fromURL(url):
        return FakeImage("url", url)

    @staticmethod
    def fromFileSystem(path):
        return FakeImage("file", path)


class FakePlain:
    def __init__(self, text):
        self.text = text


_astrbot_api_comp.Image = FakeImage
_astrbot_api_comp.Plain = FakePlain

_aiomysql = _new_module("aiomysql")  # db_mysql 依赖占位（service→repository→db_mysql）
_aiomysql.connect = None
_aiomysql.DictCursor = "DictCursor"
_aiomysql.Connection = object

sys.modules["aiomysql"] = _aiomysql
sys.modules["astrbot"] = _astrbot
sys.modules["astrbot.api"] = _astrbot_api
sys.modules["astrbot.api.event"] = _astrbot_api_event
sys.modules["astrbot.api.message_components"] = _astrbot_api_comp

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
# 二、模块 D（snapshot）/ 模块 F（scheduler）契约 fake 替身
# ============================================================


class FakeSnapshotManager:
    """模块 D 契约替身：ImageSnapshotManager(config_mgr, repo)。

    真实构造轻量无副作用，故由 StatsService 真实构造后以属性覆写本替身。
    v0.5.5：新读取侧方法（msg_total / msg_daily_trend / msg_per_group /
    image_daily_trend）恒返 None，使既有用例走回退路径保持原断言
    （回退矩阵见 tests/v0.5.5/分工.md）；快照路径新用例在 test_v055.py。
    """

    def __init__(self, config_mgr, repo):
        self.config_mgr = config_mgr
        self.repo = repo
        self.refresh_calls = 0
        self.fill_calls = 0
        self.fill_raise = False
        self.total_images = 42
        self.sender_image = 7
        self.group_image = 3
        self.member_image = 5
        self.snap_call_log: list[tuple] = []  # v0.5.5 读取侧调用记录

    async def refresh_current_hour(self, now=None):
        self.refresh_calls += 1

    async def msg_total(self, start, end, group_id=None):
        self.snap_call_log.append(("msg_total", start, end, group_id))
        return None  # 回退实时 SQL

    async def msg_daily_trend(self, start, end, group_id=None):
        self.snap_call_log.append(("msg_daily_trend", start, end, group_id))
        return None  # 回退实时 SQL

    async def msg_per_group(self, start, end):
        self.snap_call_log.append(("msg_per_group", start, end))
        return None  # 回退实时 SQL

    async def image_daily_trend(self, start, end, group_id=None):
        self.snap_call_log.append(("image_daily_trend", start, end, group_id))
        return None  # 回退实时 SQL

    async def fill_counts(self, data):
        if self.fill_raise:
            raise RuntimeError("fill boom")
        self.fill_calls += 1
        data.total_images = self.total_images
        for item in data.sender_ranking:
            item.image_count = self.sender_image
        for item in data.group_ranking:
            item.image_count = self.group_image
        if data.member is not None:
            data.member.image_count = self.member_image
        return data


class FakeScheduler:
    """模块 F 契约替身：StatsScheduler(service, snapshot, config_mgr)。

    生命周期用例经 :func:`_use_fake_scheduler` 在测试执行期替换真实
    ``stats.scheduler.StatsScheduler`` 属性（执行期 sys.modules 已稳定）。
    """

    instances: list = []
    fail_next_start = False

    def __init__(self, service, snapshot, config_mgr):
        self.service = service
        self.snapshot = snapshot
        self.config_mgr = config_mgr
        self.started = 0
        self.stopped = 0
        FakeScheduler.instances.append(self)

    async def start(self):
        if FakeScheduler.fail_next_start:
            FakeScheduler.fail_next_start = False
            raise RuntimeError("scheduler start boom")
        self.started += 1

    async def stop(self):
        self.stopped += 1


def _use_fake_scheduler():
    """测试执行期把真实 stats.scheduler.StatsScheduler 替换为 FakeScheduler。

    Returns:
        Callable: 还原函数（务必经 addCleanup / finally 调用）。
    """
    mod = importlib.import_module(_PKG + ".core.stats.scheduler")
    orig = mod.StatsScheduler
    mod.StatsScheduler = FakeScheduler

    def _restore():
        mod.StatsScheduler = orig

    return _restore


# ============================================================
# 三、导入被测代码与真实数据模型
# ============================================================

from astrbot_plugin_group_history_save_mysql.core.stats import service as SERVICE  # noqa: E402
from astrbot_plugin_group_history_save_mysql.core.stats.models import (  # noqa: E402
    GroupRankItem,
    MemberStats,
    SenderRankItem,
    StatsQuery,
    StatsTimeRange,
)


def _run(coro):
    return asyncio.run(coro)


# ============================================================
# 四、Fake 仓库 / 配置 / 上下文
# ============================================================

# 统一测试时窗：2026-08-01 00:00 ~ 2026-08-04 00:00（3 天）
START = datetime(2026, 8, 1)
END = datetime(2026, 8, 4)
FIRST_MSG = datetime(2026, 8, 1, 9, 15)
LAST_MSG = datetime(2026, 8, 3, 22, 40)


class FakeRepo:
    """聚合仓储 stub：可编排返回值、记录调用、按方法名/群号注入异常。"""

    def __init__(self):
        self.calls: list[tuple] = []
        self.overview = {
            "total": 100,
            "active_senders": 5,
            "first": FIRST_MSG,
            "last": LAST_MSG,
        }
        self.hourly = [0] * 24
        self.weekday = [0] * 7
        self.trend: list[tuple] = []
        self.sender_ranking: list[dict] = []
        self.group_ranking: list[dict] = []
        self.member_overview = {"count": 0, "active_days": 0, "name": ""}
        self.member_hourly = [0] * 24
        self.member_weekday = [0] * 7
        self.all_groups_summary: list[dict] = []
        self.raise_on: set[str] = set()  # 方法名 → 抛 RuntimeError
        self.raise_groups: set[str] = set()  # group_id 命中 → 抛 RuntimeError

    def _check_raise(self, method, group_id=None):
        if method in self.raise_on:
            raise RuntimeError(f"{method} boom")
        if group_id is not None and str(group_id) in self.raise_groups:
            raise RuntimeError(f"group {group_id} boom")

    async def get_overview(self, group_id, start, end):
        self.calls.append(("overview", group_id, None, start, end, None))
        self._check_raise("get_overview", group_id)
        return dict(self.overview)

    async def get_hourly_dist(self, group_id, sender_id, start, end):
        self.calls.append(("hourly", group_id, sender_id, start, end, None))
        self._check_raise("get_hourly_dist", group_id)
        return list(self.member_hourly if sender_id is not None else self.hourly)

    async def get_weekday_dist(self, group_id, sender_id, start, end):
        self.calls.append(("weekday", group_id, sender_id, start, end, None))
        self._check_raise("get_weekday_dist", group_id)
        return list(self.member_weekday if sender_id is not None else self.weekday)

    async def get_daily_trend(self, group_id, start, end):
        self.calls.append(("trend", group_id, None, start, end, None))
        self._check_raise("get_daily_trend", group_id)
        return list(self.trend)

    async def get_sender_ranking(self, group_id, start, end, limit):
        self.calls.append(("sender_ranking", group_id, None, start, end, limit))
        self._check_raise("get_sender_ranking", group_id)
        return [dict(r) for r in self.sender_ranking]

    async def get_group_ranking(self, start, end, limit):
        self.calls.append(("group_ranking", None, None, start, end, limit))
        self._check_raise("get_group_ranking")
        return [dict(r) for r in self.group_ranking]

    async def get_member_overview(self, group_id, sender_id, start, end):
        self.calls.append(("member_overview", group_id, sender_id, start, end, None))
        self._check_raise("get_member_overview", group_id)
        return dict(self.member_overview)

    async def get_all_groups_summary(self):
        self.calls.append(("all_groups_summary",))
        self._check_raise("get_all_groups_summary")
        return [dict(g) for g in self.all_groups_summary]

    async def get_overview_meta(self, group_id, start, end):
        # v0.5.5 快照路径专用（回退路径不调用）：返回 overview 去掉 total
        self.calls.append(("overview_meta", group_id, None, start, end, None))
        self._check_raise("get_overview_meta", group_id)
        meta = dict(self.overview)
        meta.pop("total", None)
        return meta

    async def get_group_active_senders(self, start, end):
        # v0.5.5 群排行快照路径专用（回退路径不调用）
        self.calls.append(("group_active", None, None, start, end, None))
        self._check_raise("get_group_active_senders")
        return {}

    def calls_of(self, method):
        return [c for c in self.calls if c[0] == method]


class FakeConfig:
    def __init__(self, settings=None, push_groups=None):
        self.settings = dict(settings or {})
        self.push_groups = list(push_groups or [])
        self.all_settings = {"all_mode": "false"}
        self.push_flags: dict[str, bool] = {}

    async def get_stats_setting_typed(self, key):
        return self.settings.get(key)

    async def get_push_groups(self):
        return list(self.push_groups)

    async def get_all_settings(self):
        return dict(self.all_settings)

    async def get_push_flags(self, group_ids):
        return {g: self.push_flags[g] for g in group_ids if g in self.push_flags}


class FakeContext:
    def __init__(self):
        self.sent: list[tuple] = []
        self.raise_for: set[str] = set()  # umo 命中 → send_message 抛异常

    async def send_message(self, umo, chain):
        if umo in self.raise_for:
            raise RuntimeError(f"send to {umo} failed")
        self.sent.append((umo, chain))
        return True


class FakeRenderer:
    def __init__(self):
        self.result = "/tmp/stats_card.png"
        self.calls: list[tuple] = []

    async def render_card(self, data, title):
        self.calls.append((data, title))
        return self.result


def _make_service(settings=None, push_groups=None):
    """构造 StatsService 并覆写 repo/snapshot/renderer 为可控 fake。

    真实构造链路（StatsRepository / ImageSnapshotManager / StatsT2IRenderer
    构造均轻量无副作用）走通后立即以 fake 覆写公开属性，隔离后续行为。
    v0.5.2 起构造签名为 (context, mysql_mgr, config_mgr, star)。
    """
    ctx = FakeContext()
    cfg = FakeConfig(settings, push_groups)
    svc = SERVICE.StatsService(ctx, object(), cfg, object())
    svc.repo = FakeRepo()
    svc.snapshot = FakeSnapshotManager(cfg, svc.repo)
    svc.renderer = FakeRenderer()
    return svc, ctx, cfg


def _query(group_id="123", member_id=None, start=START, end=END, top_n=10):
    return StatsQuery(
        group_id=group_id,
        member_id=member_id,
        time_range=StatsTimeRange(start=start, end=end, label="测试"),
        top_n=top_n,
    )


# ============================================================
# 五、build_stats — 群维度组装正确性
# ============================================================


class TestBuildGroupView(unittest.TestCase):
    def test_group_view_assembly(self):
        svc, ctx, cfg = _make_service()
        repo = svc.repo
        hourly = [0] * 24
        hourly[21] = 9
        hourly[22] = 5
        repo.hourly = hourly
        repo.weekday = [3, 4, 5, 6, 7, 8, 9]
        # 趋势故意缺 08-02（验证补零）且乱序插入一条范围外脏数据不影响主序列
        repo.trend = [("2026-08-01", 10), ("2026-08-03", 4)]
        repo.sender_ranking = [
            {"sender_id": "111", "sender_name": "Alice", "count": 50},
            {"sender_id": "222", "sender_name": "Bob", "count": 30},
        ]

        data = _run(svc.build_stats(_query()))

        # 总览透传
        self.assertEqual(data.total_messages, 100)
        self.assertEqual(data.active_senders, 5)
        self.assertEqual(data.first_msg_time, FIRST_MSG)
        self.assertEqual(data.last_msg_time, LAST_MSG)
        # 补零连续性：start 所在日 ~ end 前一日（08-01/08-02/08-03）
        self.assertEqual(
            data.daily_trend,
            [
                {"date": "2026-08-01", "count": 10},
                {"date": "2026-08-02", "count": 0},
                {"date": "2026-08-03", "count": 4},
            ],
        )
        # 峰值小时 = 最大值索引
        self.assertEqual(data.peak_hour, 21)
        self.assertEqual(data.hourly_dist, hourly)
        self.assertEqual(data.weekday_dist, [3, 4, 5, 6, 7, 8, 9])
        # 发言人排行转 SenderRankItem + 快照图片数注入
        self.assertEqual(len(data.sender_ranking), 2)
        self.assertIsInstance(data.sender_ranking[0], SenderRankItem)
        self.assertEqual(data.sender_ranking[0].sender_name, "Alice")
        self.assertEqual(data.sender_ranking[0].count, 50)
        self.assertEqual(data.sender_ranking[0].image_count, 7)
        # 单群视图无群排行 / 无个人区
        self.assertEqual(data.group_ranking, [])
        self.assertIsNone(data.member)
        # 快照注入与强制刷新
        self.assertEqual(data.total_images, 42)
        self.assertEqual(svc.snapshot.refresh_calls, 1)
        self.assertEqual(svc.snapshot.fill_calls, 1)
        self.assertIsInstance(data.generated_at, datetime)
        self.assertEqual(data.query.group_id, "123")

    def test_peak_hour_tie_takes_earliest(self):
        svc, _, _ = _make_service()
        svc.repo.hourly = [
            0,
            7,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            7,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
        ]
        data = _run(svc.build_stats(_query()))
        self.assertEqual(data.peak_hour, 1)  # 并列取最早小时

    def test_peak_hour_all_zero_is_none(self):
        svc, _, _ = _make_service()
        svc.repo.hourly = [0] * 24
        data = _run(svc.build_stats(_query()))
        self.assertIsNone(data.peak_hour)


# ============================================================
# 六、build_stats — 全部群视图（group_ranking 有值）
# ============================================================


class TestBuildAllGroupsView(unittest.TestCase):
    def test_all_groups_view(self):
        svc, _, _ = _make_service()
        repo = svc.repo
        repo.group_ranking = [
            {"group_id": "1001", "count": 60, "active_senders": 4},
            {"group_id": "1002", "count": 40, "active_senders": 2},
        ]
        data = _run(svc.build_stats(_query(group_id=None, top_n=10)))

        # 全部群视图 group_ranking 有值（转 GroupRankItem + 图片数注入）
        self.assertEqual(len(data.group_ranking), 2)
        self.assertIsInstance(data.group_ranking[0], GroupRankItem)
        self.assertEqual(data.group_ranking[0].group_id, "1001")
        self.assertEqual(data.group_ranking[0].count, 60)
        self.assertEqual(data.group_ranking[0].active_senders, 4)
        self.assertEqual(data.group_ranking[0].image_count, 3)
        # 无发言人排行 / 无个人区
        self.assertEqual(data.sender_ranking, [])
        self.assertIsNone(data.member)
        # 群排行以 limit=top_n 查询
        gr_calls = repo.calls_of("group_ranking")
        self.assertEqual(len(gr_calls), 1)
        self.assertEqual(gr_calls[0][5], 10)
        # 不应调用单群发言人行排行
        self.assertEqual(repo.calls_of("sender_ranking"), [])


# ============================================================
# 七、build_stats — 个人维度（ratio / rank / avg / sender 分布）
# ============================================================


class TestBuildMemberView(unittest.TestCase):
    def _seed(self, svc, total=100, member_count=30):
        repo = svc.repo
        repo.overview = {
            "total": total,
            "active_senders": 5,
            "first": FIRST_MSG,
            "last": LAST_MSG,
        }
        repo.sender_ranking = [
            {"sender_id": "111", "sender_name": "Alice", "count": 50},
            {"sender_id": "222", "sender_name": "Bob", "count": member_count},
            {"sender_id": "333", "sender_name": "Carol", "count": 10},
        ]
        repo.member_overview = {"count": member_count, "active_days": 3, "name": "Bob"}
        repo.member_hourly = [1] * 24
        repo.member_weekday = [2, 2, 2, 2, 2, 2, 2]

    def test_member_assembly(self):
        svc, _, _ = _make_service()
        self._seed(svc)
        data = _run(svc.build_stats(_query(member_id="222", top_n=2)))

        m = data.member
        self.assertIsInstance(m, MemberStats)
        self.assertEqual(m.sender_id, "222")
        self.assertEqual(m.sender_name, "Bob")
        self.assertEqual(m.count, 30)
        # ratio = member.count / 群 total
        self.assertAlmostEqual(m.ratio, 0.3)
        # rank = 完整排行（limit=50）中按 sender_id 的名次（1 起）
        self.assertEqual(m.rank, 2)
        # avg_per_day = count / max(天数, 1)，3 天窗口 → 30/3
        self.assertAlmostEqual(m.avg_per_day, 10.0)
        self.assertEqual(m.active_days, 3)
        # hourly/weekday 用 sender 维度查询
        self.assertEqual(m.hourly_dist, [1] * 24)
        self.assertEqual(m.weekday_dist, [2] * 7)
        sender_hour_calls = [c for c in svc.repo.calls_of("hourly") if c[2] == "222"]
        sender_week_calls = [c for c in svc.repo.calls_of("weekday") if c[2] == "222"]
        self.assertEqual(len(sender_hour_calls), 1)
        self.assertEqual(len(sender_week_calls), 1)
        # 完整排行查 limit=50；展示排行切 top_n
        rank_calls = svc.repo.calls_of("sender_ranking")
        self.assertEqual(len(rank_calls), 1)
        self.assertEqual(rank_calls[0][5], 50)
        self.assertEqual(len(data.sender_ranking), 2)  # top_n=2 切片
        self.assertEqual(data.sender_ranking[0].sender_id, "111")
        # 个人图片数注入
        self.assertEqual(m.image_count, 5)
        # 全部群视图之外的群排行应为空
        self.assertEqual(data.group_ranking, [])

    def test_member_not_in_ranking_rank_none(self):
        svc, _, _ = _make_service()
        self._seed(svc)
        data = _run(svc.build_stats(_query(member_id="999")))
        self.assertIsNone(data.member.rank)
        self.assertEqual(data.member.count, 30)

    def test_member_ratio_zero_when_total_zero(self):
        svc, _, _ = _make_service()
        self._seed(svc, total=0, member_count=0)
        data = _run(svc.build_stats(_query(member_id="222")))
        self.assertEqual(data.member.ratio, 0.0)
        self.assertEqual(data.member.avg_per_day, 0.0)

    def test_member_all_groups_view_rank_none(self):
        svc, _, _ = _make_service()
        self._seed(svc)
        svc.repo.group_ranking = [{"group_id": "1", "count": 5, "active_senders": 1}]
        data = _run(svc.build_stats(_query(group_id=None, member_id="222")))
        # 全部群视图：rank 恒 None，且单群排行不查询
        self.assertIsNone(data.member.rank)
        self.assertEqual(svc.repo.calls_of("sender_ranking"), [])


# ============================================================
# 八、build_stats — 异常包装 StatsBuildError
# ============================================================


class TestBuildErrors(unittest.TestCase):
    def test_repo_error_wrapped(self):
        svc, _, _ = _make_service()
        svc.repo.raise_on = {"get_overview"}
        with self.assertRaises(SERVICE.StatsBuildError) as cm:
            _run(svc.build_stats(_query()))
        self.assertEqual(str(cm.exception), "统计数据查询失败，请稍后重试")

    def test_fill_counts_error_wrapped(self):
        svc, _, _ = _make_service()
        svc.snapshot.fill_raise = True
        with self.assertRaises(SERVICE.StatsBuildError):
            _run(svc.build_stats(_query()))

    def test_error_class_defined_in_service(self):
        self.assertTrue(issubclass(SERVICE.StatsBuildError, Exception))


# ============================================================
# 九、check_cooldown
# ============================================================


class TestCooldown(unittest.TestCase):
    def test_first_pass_second_reject(self):
        svc, _, _ = _make_service(settings={"stats_cooldown": 30})
        self.assertTrue(_run(svc.check_cooldown("123")))  # 首次放行并盖章
        self.assertFalse(_run(svc.check_cooldown("123")))  # 冷却内拒绝
        # 模拟冷却过期 → 再次放行
        svc._cooldown["123"] = time.monotonic() - 31
        self.assertTrue(_run(svc.check_cooldown("123")))

    def test_zero_disables_cooldown(self):
        svc, _, _ = _make_service(settings={"stats_cooldown": 0})
        self.assertTrue(_run(svc.check_cooldown("123")))
        self.assertTrue(_run(svc.check_cooldown("123")))  # 0 = 不限流
        self.assertNotIn("123", svc._cooldown)  # 不限流时不盖章

    def test_groups_isolated(self):
        svc, _, _ = _make_service(settings={"stats_cooldown": 30})
        self.assertTrue(_run(svc.check_cooldown("1")))
        self.assertTrue(_run(svc.check_cooldown("2")))  # 不同群互不影响
        self.assertFalse(_run(svc.check_cooldown("1")))

    def test_missing_setting_falls_back_default(self):
        svc, _, _ = _make_service(settings={})  # 缺失 → 兜底 30s
        self.assertTrue(_run(svc.check_cooldown("123")))
        self.assertFalse(_run(svc.check_cooldown("123")))


# ============================================================
# 十、umo 缓存
# ============================================================


class TestUmoCache(unittest.TestCase):
    def test_record_and_get(self):
        svc, _, _ = _make_service()
        svc.record_group_umo("123", "umo-abc")
        self.assertEqual(svc.get_group_umo("123"), "umo-abc")
        svc.record_group_umo("123", "umo-new")  # 覆盖更新
        self.assertEqual(svc.get_group_umo("123"), "umo-new")

    def test_empty_values_not_recorded(self):
        svc, _, _ = _make_service()
        svc.record_group_umo("123", "")
        svc.record_group_umo("123", None)
        svc.record_group_umo("", "umo-x")
        svc.record_group_umo(None, "umo-x")
        self.assertIsNone(svc.get_group_umo("123"))
        self.assertEqual(svc._umo_cache, {})

    def test_unknown_group_returns_none(self):
        svc, _, _ = _make_service()
        self.assertIsNone(svc.get_group_umo("999"))
        self.assertIsNone(svc.get_group_umo(""))
        self.assertIsNone(svc.get_group_umo(None))


# ============================================================
# 十一、push_report（patch asyncio.sleep 去除群间 1s 真实等待）
# ============================================================


class TestPushReport(unittest.TestCase):
    def setUp(self):
        self.sleep_log: list[float] = []
        self._orig_sleep = asyncio.sleep

        async def _fake_sleep(delay):
            self.sleep_log.append(delay)

        asyncio.sleep = _fake_sleep

    def tearDown(self):
        asyncio.sleep = self._orig_sleep

    def _push_groups(self):
        return [
            {"group_id": "101", "enabled": True},
            {"group_id": "102", "enabled": True},
            {"group_id": "103", "enabled": False},
            {"group_id": "104", "enabled": True},
        ]

    def _seed_repo(self, repo):
        repo.sender_ranking = [
            {"sender_id": "111", "sender_name": "Alice", "count": 50},
            {"sender_id": "222", "sender_name": "Bob", "count": 30},
            {"sender_id": "333", "sender_name": "Carol", "count": 10},
        ]

    def test_push_daily_success_order_and_args(self):
        svc, ctx, cfg = _make_service(
            settings={"stats_top_n": 7}, push_groups=self._push_groups()
        )
        self._seed_repo(svc.repo)
        svc.record_group_umo("101", "umo-101")
        svc.record_group_umo("104", "umo-104")  # 102 无 umo → 跳过

        now = datetime(2026, 8, 4, 21, 0)
        _run(svc.push_report("daily", now=now))

        # 发送调用参数与群间顺序（103 关闭、102 无 umo 均不发送）
        self.assertEqual([umo for umo, _ in ctx.sent], ["umo-101", "umo-104"])
        chain = ctx.sent[0][1]
        self.assertIsInstance(chain, FakeMessageChain)
        self.assertEqual(len(chain.chain), 1)
        img = chain.chain[0]
        self.assertIsInstance(img, FakeImage)
        self.assertEqual(img.kind, "file")
        self.assertEqual(img.source, "/tmp/stats_card.png")
        # 日报时间范围 [今日 00:00, 明日 00:00)；top_n 读 stats_top_n
        overview_calls = svc.repo.calls_of("overview")
        self.assertTrue(overview_calls)
        for call in overview_calls:
            self.assertEqual(call[3], datetime(2026, 8, 4))
            self.assertEqual(call[4], datetime(2026, 8, 5))
        rank_calls = svc.repo.calls_of("sender_ranking")
        self.assertTrue(rank_calls)
        for call in rank_calls:
            self.assertEqual(call[5], 7)
        # 无 umo 群记 warning 跳过
        self.assertTrue(
            any("102" in m and "umo" in m for m in _STUB_LOGGER.records["warning"])
        )
        # 群间 1s 间隔：3 个 enabled 群 → 2 次 sleep
        self.assertEqual(self.sleep_log, [1.0, 1.0])

    def test_push_weekly_range(self):
        svc, ctx, cfg = _make_service(push_groups=self._push_groups())
        svc.record_group_umo("101", "umo-101")
        svc.record_group_umo("102", "umo-102")
        svc.record_group_umo("104", "umo-104")

        # 2026-08-05 为周三 → 上一整周 [2026-07-27 周一, 2026-08-03 周一)
        self.assertEqual(datetime(2026, 8, 3).weekday(), 0)
        _run(svc.push_report("weekly", now=datetime(2026, 8, 5, 9, 0)))

        overview_calls = svc.repo.calls_of("overview")
        self.assertTrue(overview_calls)
        for call in overview_calls:
            self.assertEqual(call[3], datetime(2026, 7, 27))
            self.assertEqual(call[4], datetime(2026, 8, 3))
        self.assertEqual(len(ctx.sent), 3)

    def test_push_render_fail_fallback_text(self):
        svc, ctx, cfg = _make_service(
            push_groups=[{"group_id": "101", "enabled": True}]
        )
        self._seed_repo(svc.repo)
        svc.renderer.result = None  # 渲染失败
        svc.record_group_umo("101", "umo-101")

        _run(svc.push_report("daily", now=datetime(2026, 8, 4, 21, 0)))

        self.assertEqual(len(ctx.sent), 1)
        umo, chain = ctx.sent[0]
        self.assertEqual(umo, "umo-101")
        comp = chain.chain[0]
        self.assertIsInstance(comp, FakePlain)
        # 降级文本：总消息数 + Top3 发言人一行
        self.assertIn("总消息数：100", comp.text)
        self.assertIn("Alice 50条", comp.text)
        self.assertIn("Bob 30条", comp.text)
        self.assertIn("Carol 10条", comp.text)
        # 渲染失败记 warning
        self.assertTrue(any("渲染失败" in m for m in _STUB_LOGGER.records["warning"]))

    def test_push_single_group_send_error_does_not_block(self):
        svc, ctx, cfg = _make_service(push_groups=self._push_groups())
        svc.record_group_umo("101", "umo-101")
        svc.record_group_umo("102", "umo-102")
        svc.record_group_umo("104", "umo-104")
        ctx.raise_for = {"umo-101"}  # 首个群发送异常

        _run(svc.push_report("daily", now=datetime(2026, 8, 4, 21, 0)))

        # 101 失败不阻断：102/104 仍成功
        self.assertEqual([umo for umo, _ in ctx.sent], ["umo-102", "umo-104"])
        self.assertTrue(any("101" in m for m in _STUB_LOGGER.records["error"]))

    def test_push_single_group_build_error_does_not_block(self):
        svc, ctx, cfg = _make_service(push_groups=self._push_groups())
        svc.record_group_umo("101", "umo-101")
        svc.record_group_umo("102", "umo-102")
        svc.record_group_umo("104", "umo-104")
        svc.repo.raise_groups = {"101"}  # 首个群 build 失败

        _run(svc.push_report("daily", now=datetime(2026, 8, 4, 21, 0)))

        self.assertEqual([umo for umo, _ in ctx.sent], ["umo-102", "umo-104"])

    def test_push_unknown_kind_ignored(self):
        svc, ctx, cfg = _make_service(
            push_groups=[{"group_id": "101", "enabled": True}]
        )
        svc.record_group_umo("101", "umo-101")
        _run(svc.push_report("hourly"))
        self.assertEqual(ctx.sent, [])
        self.assertTrue(any("hourly" in m for m in _STUB_LOGGER.records["warning"]))

    def test_push_no_enabled_groups(self):
        svc, ctx, cfg = _make_service(
            push_groups=[{"group_id": "101", "enabled": False}]
        )
        _run(svc.push_report("daily"))
        self.assertEqual(ctx.sent, [])


# ============================================================
# 十一b、群列表模式感知（v0.5.1 all_mode 修复）
# ============================================================


class TestGroupResolve(unittest.TestCase):
    """resolve_push_groups / resolve_dropdown_groups / is_all_mode。"""

    def test_is_all_mode_default_false(self):
        svc, _, _ = _make_service()
        self.assertFalse(_run(svc.is_all_mode()))

    def test_is_all_mode_true(self):
        svc, _, cfg = _make_service()
        cfg.all_settings = {"all_mode": "true"}
        self.assertTrue(_run(svc.is_all_mode()))

    def test_resolve_push_groups_whitelist_mode(self):
        svc, _, cfg = _make_service(push_groups=[{"group_id": "101", "enabled": True}])
        result = _run(svc.resolve_push_groups())
        self.assertEqual(result, [{"group_id": "101", "enabled": True}])
        # 白名单模式不查 MySQL 群清单
        self.assertEqual(svc.repo.calls_of("all_groups_summary"), [])

    def test_resolve_push_groups_all_mode(self):
        svc, _, cfg = _make_service()
        cfg.all_settings = {"all_mode": "true"}
        svc.repo.all_groups_summary = [
            {"group_id": "101", "count": 500, "last_active": None},
            {"group_id": "102", "count": 30, "last_active": None},
        ]
        cfg.push_flags = {"101": True}  # 102 无行默认关
        result = _run(svc.resolve_push_groups())
        self.assertEqual(
            result,
            [
                {"group_id": "101", "enabled": True, "count": 500},
                {"group_id": "102", "enabled": False, "count": 30},
            ],
        )

    def test_resolve_push_groups_all_mode_repo_error(self):
        svc, _, cfg = _make_service()
        cfg.all_settings = {"all_mode": "true"}
        svc.repo.raise_on = {"get_all_groups_summary"}
        self.assertEqual(_run(svc.resolve_push_groups()), [])

    def test_resolve_dropdown_union_and_order(self):
        svc, _, cfg = _make_service()
        cfg.push_groups = []  # 白名单经 get_groups 读取——FakeConfig 无该方法时
        # FakeConfig 未实现 get_groups：service 应兜底为仅有数据的群
        svc.repo.all_groups_summary = [
            {"group_id": "202", "count": 30, "last_active": None},
            {"group_id": "101", "count": 500, "last_active": None},
        ]
        result = _run(svc.resolve_dropdown_groups())
        self.assertEqual(
            [g["group_id"] for g in result],
            ["101", "202"],  # 消息数降序
        )
        self.assertEqual(result[0]["count"], 500)
        self.assertTrue(result[0]["enabled"])

    def test_push_report_all_mode_targets(self):
        svc, ctx, cfg = _make_service()
        cfg.all_settings = {"all_mode": "true"}
        svc.repo.all_groups_summary = [
            {"group_id": "101", "count": 500, "last_active": None},
            {"group_id": "102", "count": 30, "last_active": None},
        ]
        cfg.push_flags = {"101": True}  # 仅 101 开启推送
        svc.record_group_umo("101", "umo-101")
        svc.record_group_umo("102", "umo-102")
        _run(svc.push_report("daily", now=datetime(2026, 8, 4, 21, 0)))
        self.assertEqual([umo for umo, _ in ctx.sent], ["umo-101"])


# ============================================================
# 十二、render 透传与生命周期
# ============================================================


class TestRenderAndLifecycle(unittest.TestCase):
    def test_render_passthrough(self):
        svc, _, _ = _make_service()
        data = _run(svc.build_stats(_query()))
        svc.renderer.result = "https://cdn.example.com/card.png"
        out = _run(svc.render(data, "测试标题"))
        self.assertEqual(out, "https://cdn.example.com/card.png")
        self.assertEqual(svc.renderer.calls[0], (data, "测试标题"))

    def test_image_chain_url_vs_file(self):
        chain = SERVICE.StatsService._image_chain("https://x.com/a.png")
        self.assertEqual(chain.chain[0].kind, "url")
        chain = SERVICE.StatsService._image_chain("/tmp/a.png")
        self.assertEqual(chain.chain[0].kind, "file")

    def test_start_stop_lifecycle(self):
        svc, _, cfg = _make_service()
        self.addCleanup(_use_fake_scheduler())
        FakeScheduler.instances.clear()

        _run(svc.start())
        self.assertEqual(len(FakeScheduler.instances), 1)
        sched = FakeScheduler.instances[0]
        self.assertEqual(sched.started, 1)
        # 契约构造参数：service / snapshot / config_mgr
        self.assertIs(sched.service, svc)
        self.assertIs(sched.snapshot, svc.snapshot)
        self.assertIs(sched.config_mgr, cfg)

        # 可重入：重复 start 不再创建
        _run(svc.start())
        self.assertEqual(len(FakeScheduler.instances), 1)

        # stop 前制造缓存，验证清理
        svc.record_group_umo("123", "umo-x")
        svc._cooldown["123"] = time.monotonic()
        _run(svc.stop())
        self.assertEqual(sched.stopped, 1)
        self.assertEqual(svc._umo_cache, {})
        self.assertEqual(svc._cooldown, {})
        self.assertIsNone(svc._scheduler)

        # 可重入：重复 stop 不抛
        _run(svc.stop())

    def test_start_failure_swallowed_and_retryable(self):
        svc, _, _ = _make_service()
        self.addCleanup(_use_fake_scheduler())
        FakeScheduler.instances.clear()
        FakeScheduler.fail_next_start = True

        _run(svc.start())  # 启动失败不抛
        self.assertIsNone(svc._scheduler)
        self.assertEqual(len(FakeScheduler.instances), 1)
        self.assertEqual(FakeScheduler.instances[0].started, 0)

        _run(svc.start())  # 失败后可重试
        self.assertIsNotNone(svc._scheduler)
        self.assertEqual(len(FakeScheduler.instances), 2)

    def test_constructor_exposes_public_attrs(self):
        svc, ctx, cfg = _make_service()
        # 构造自建三模块并暴露同名公开属性（此处已被 fake 覆写，仅验证属性存在）
        self.assertTrue(hasattr(svc, "repo"))
        self.assertTrue(hasattr(svc, "snapshot"))
        self.assertTrue(hasattr(svc, "renderer"))
        # 真实构造：snapshot 为模块 D 真实 ImageSnapshotManager
        # （执行期解析 sys.modules，与 service.__init__ 内部导入同源，
        # 避免收集期跨文件模块驱逐导致的类对象不一致）
        # v0.5.2 起构造签名为 (context, mysql_mgr, config_mgr, star)
        ctor = SERVICE.StatsService(ctx, object(), cfg, object())
        snap_mod = importlib.import_module(_PKG + ".core.stats.snapshot")
        self.assertIsInstance(ctor.snapshot, snap_mod.ImageSnapshotManager)
        # 契约：ImageSnapshotManager(config_mgr, repo) 透传构造
        self.assertIs(ctor.snapshot._config, cfg)
        self.assertIs(ctor.snapshot._repo, ctor.repo)
        self.assertIsInstance(ctor.renderer, SERVICE.StatsT2IRenderer)


if __name__ == "__main__":
    unittest.main(verbosity=2)
