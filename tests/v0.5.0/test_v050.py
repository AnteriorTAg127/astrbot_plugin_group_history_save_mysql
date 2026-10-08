# ruff: noqa: I001
"""v0.5.0 数据分析 集成测试（Test-Agent · dev-flow 阶段 6-7）。

定位：各模块单测冒烟（smoke_a/b/c/d/e/f/g/h/m）已按「单模块 + fake 注入」
覆盖各自契约；本文件专补**冒烟未覆盖的缺口与跨模块组合路径**：

一、PRD 行为缺口
  1. /群统计 指令别名注册（群数据/统计）——smoke_m 的恒等装饰器 stub 丢弃
     装饰器参数，别名从未被断言（TC-17）；
  2. group_stats 参数注解 GreedyStr（剩余全文，防日期区间被空格截断）（TC-18）；
  3. @register 元数据（插件名/版本 0.5.0）（TC-18b）；
  4. stats_cooldown=0 不限流 / 冷却期内拒绝不盖章 / 群隔离——经**真实**
     typed 配置读取链路（smoke_g 用 FakeConfig）（TC-09/10）；
  5. stats/__init__ PEP 562 惰性导出 identity / 未知属性 AttributeError /
     轻量子模块导入不拉起 repository 重链（TC-19/19b）。

二、跨模块组合路径（既有冒烟均为单模块 fake 注入，组合链未跑通）
  6. StatsService.build_stats × **真实** ImageSnapshotManager × **真实**
     aiosqlite ConfigManager × fake MySQL 聚合仓储：
     refresh_current_hour 聚合 → snapshot_upsert 落真实 SQLite →
     fill_counts 经 snapshot_query 回注 total_images / 排行 / 群排行 /
     个人 image_count（smoke_g 用 FakeSnapshotManager、smoke_d 手搓
     StatsData，两端从未串联）（TC-01~04）；
  7. push_report × **真实** get_push_groups 白名单 LEFT JOIN 门控
     （非白名单 enabled 行忽略 / disabled 群跳过 / 无 umo 跳过 /
     渲染失败降级纯文本内容 / daily 与 weekly 时间窗口口径）（TC-05~08）；
  8. web_api 五个 stats 端点 × **真实** ConfigManager 落库往返
     （save 归一化与全量校验拒绝部分写入 / reset / toggle 持久化 /
     stats_data 端点串真实 StatsService 全链路）（TC-11~16）。

三、独立冒烟脚本并入同一 pytest 进程
  smoke_a_config.py / smoke_d_snapshot.py 为独立脚本（if __name__ ==
  "__main__"），pytest 直接收集时贡献 0 用例；本文件以包装用例在
  **同一 pytest 进程内**调用其 main()（内置 assert 断言），使「9 份冒烟
  + 本文件同进程合跑」真正覆盖两份脚本的全部断言（TC-20/21）。

范式：沿用 tests/v0.3/test_v03.py 的 @async_test 装饰器（asyncio.run，
无 pytest-asyncio / conftest.py）；astrbot.* / aiomysql 全部 sys.modules
stub 且注入先于一切被测导入；被测包缓存剔除兼容多测试文件合跑（含无
__file__ 的 fake 注入模块与顶层名导入两种形态）。

导入分两阶段（顺序不可颠倒）：
  阶段 B：记录式 filter.command/register stub + 重依赖兄弟模块 fake 注入
          → 导入真实 main.py（别名注册断言的数据来源）；随后全量剔除；
  阶段 A：真实链路导入（db_config / stats.* / web_api）——若先于阶段 B，
          main.py 导入会经 stats 包 PEP 562 __getattr__ 把 fake 类缓存进
          包 __dict__，污染 TC-19 的惰性导出 identity 断言。

运行方式（插件根目录）：
    python -m pytest "tests/v0.5.0/test_v050.py" -v
    # 与全部 9 份冒烟脚本合跑（同一 pytest 进程）：
    python -m pytest "tests/v0.5.0/smoke_a_config.py" \
        "tests/v0.5.0/smoke_b_repository.py" \
        "tests/v0.5.0/smoke_c_parser.py" \
        "tests/v0.5.0/smoke_d_snapshot.py" \
        "tests/v0.5.0/smoke_e_render.py" \
        "tests/v0.5.0/smoke_f_scheduler.py" \
        "tests/v0.5.0/smoke_g_service.py" \
        "tests/v0.5.0/smoke_h_webapi.py" \
        "tests/v0.5.0/smoke_m_main.py" \
        "tests/v0.5.0/test_v050.py" -v
"""

import asyncio
import functools
import importlib
import inspect
import sys
import tempfile
import time
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
_PLUGIN_ROOT = _TEST_DIR.parents[1]  # tests/v0.5.0 → 插件根
_PLUGINS_DIR = str(_PLUGIN_ROOT.parent)  # data/plugins（包导入根）


# ============================================================
# 一、通用工具：模块剔除（多测试文件合跑兼容）
# ============================================================


def _purge_plugin_modules() -> None:
    """剔除被测插件的全部缓存模块（两种导入形态都覆盖）。

    1) 包内名（astrbot_plugin_group_history_save_mysql 及其一切子模块）：
       **无条件**删除——smoke_m 等文件以 fake 模块注入 sys.modules 且不带
       __file__，仅按路径判定会漏删，导致后续 import 取到残留 fake；
    2) 顶层名导入（smoke_d 以顶层 db_config / stats 导入）：凡 __file__
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
    """记录各级日志（断言无 umo warning / 推送计数等）。"""

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
_DATA_DIR_HOLDER = {"path": tempfile.mkdtemp(prefix="v050_it_")}


def _fresh_data_dir() -> Path:
    _DATA_DIR_HOLDER["path"] = tempfile.mkdtemp(prefix="v050_it_")
    return Path(_DATA_DIR_HOLDER["path"])


_astrbot = _new_module("astrbot")
_astrbot_api = _new_module("astrbot.api")
_astrbot_api.logger = _STUB_LOGGER
_astrbot_api.AstrBotConfig = dict


class _StubStarTools:
    @staticmethod
    def get_data_dir(plugin_name=None):
        return Path(_DATA_DIR_HOLDER["path"])


# ---- astrbot.api.event: MessageChain / AstrMessageEvent / filter（记录式）----
_astrbot_event = _new_module("astrbot.api.event")


class FakeMessageChain:
    """消息链 stub：承载 chain 列表（与真实 MessageChain 构造签名一致）。"""

    def __init__(self, chain=None):
        self.chain = list(chain or [])


class _StubAstrMessageEvent:
    pass


class _EventType:
    GROUP_MESSAGE = "group_message"


class _AdapterType:
    AIOCQHTTP = "aiocqhttp"


class _PermissionType:
    ADMIN = "admin"


# 记录式 filter.command：捕获指令名与 alias 等装饰器参数（别名注册断言依赖）
_COMMAND_RECORDS: list[dict] = []


def _identity_decorator_factory():
    def _factory(*args, **kwargs):
        def _decorator(fn):
            return fn

        return _decorator

    return _factory


class _RecordingFilter:
    EventMessageType = _EventType
    PlatformAdapterType = _AdapterType
    PermissionType = _PermissionType
    event_message_type = staticmethod(_identity_decorator_factory())
    platform_adapter_type = staticmethod(_identity_decorator_factory())
    permission_type = staticmethod(_identity_decorator_factory())

    @staticmethod
    def command(name, **kwargs):
        def _decorator(fn):
            _COMMAND_RECORDS.append(
                {"name": name, "kwargs": kwargs, "func": fn.__name__}
            )
            return fn

        return _decorator


_astrbot_event.MessageChain = FakeMessageChain
_astrbot_event.AstrMessageEvent = _StubAstrMessageEvent
_astrbot_event.filter = _RecordingFilter
_astrbot_event.EventMessageType = _EventType
_astrbot_event.PlatformAdapterType = _AdapterType
_astrbot_event.PermissionType = _PermissionType

# ---- astrbot.api.message_components: Plain / Image ----
_astrbot_comp = _new_module("astrbot.api.message_components")


class FakePlain:
    def __init__(self, text=""):
        self.text = text


class FakeImage:
    def __init__(self, kind, source):
        self.kind = kind  # "url" / "file"
        self.source = source
        self.url = source if kind == "url" else None
        self.file = source if kind == "file" else None

    @staticmethod
    def fromURL(url):
        return FakeImage("url", url)

    @staticmethod
    def fromFileSystem(path):
        return FakeImage("file", path)


_astrbot_comp.Plain = FakePlain
_astrbot_comp.Image = FakeImage

# ---- astrbot.api.star: Context / Star / register / StarTools ----
_astrbot_star = _new_module("astrbot.api.star")

_REGISTER_RECORDS: list[dict] = []


class _StubContext:
    def __init__(self, *args, **kwargs):
        pass


class _StubStar:
    def __init__(self, context, *args, **kwargs):
        self.context = context


def _stub_register(*args, **kwargs):
    _REGISTER_RECORDS.append({"args": args, "kwargs": kwargs})

    def _decorator(cls):
        return cls

    return _decorator


_astrbot_star.Context = _StubContext
_astrbot_star.Star = _StubStar
_astrbot_star.register = _stub_register
_astrbot_star.StarTools = _StubStarTools

# ---- astrbot.api.web: request / json_response / error_response / file_response ----
_astrbot_web = _new_module("astrbot.api.web")


class JsonResp:
    def __init__(self, data=None, status_code=200):
        self.data = {} if data is None else data
        self.status_code = status_code


class ErrResp:
    def __init__(self, message, status_code=400):
        self.message = message
        self.status_code = status_code


def _json_response(data=None, *, status_code=200, headers=None):
    return JsonResp(data, status_code)


def _error_response(message, *, status_code=400, data=None, headers=None):
    return ErrResp(message, status_code)


def _file_response(path, filename=None, content_type=None):
    return {"__file__": True, "path": path, "filename": filename}


class _StubQuery:
    """模拟 PluginMultiDict：get(key[, default]) 行为对齐。"""

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

    async def json(self, default=None):
        return self._json if self._json is not None else default


_STUB_REQUEST = _StubRequest()
_astrbot_web.request = _STUB_REQUEST
_astrbot_web.json_response = _json_response
_astrbot_web.error_response = _error_response
_astrbot_web.file_response = _file_response

# ---- astrbot.core.star.filter.command: GreedyStr ----
_astrbot_core = _new_module("astrbot.core")
_astrbot_core_star = _new_module("astrbot.core.star")
_astrbot_core_star_filter = _new_module("astrbot.core.star.filter")
_astrbot_core_star_filter_command = _new_module("astrbot.core.star.filter.command")


class _StubGreedyStr(str):
    """指令剩余文本参数标记（真实实现为 str 子类，stub 同形）。"""


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

if _PLUGINS_DIR not in sys.path:
    sys.path.insert(0, _PLUGINS_DIR)

# ============================================================
# 三、阶段 B：导入 main.py（别名注册断言用记录式装饰器）
#     重依赖兄弟模块以 fake 注入（范式同 smoke_m），stats.models / parser /
#     profile.capture 保留真实。导入完成后全量剔除，防止污染阶段 A。
# ============================================================

_purge_plugin_modules()


def _inject_fake(module_path: str, attrs: dict):
    mod = _new_module(module_path)
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[module_path] = mod
    parent_path, _, child = module_path.rpartition(".")
    parent = sys.modules.get(parent_path)
    if parent is not None:
        setattr(parent, child, mod)
    return mod


class _FakeMySQLManager:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _FakeConfigManager:
    def __init__(self):
        pass


class _FakeImageCleaner:
    def __init__(self, mysql_mgr, config_mgr):
        pass


class _FakeSummaryService:
    def __init__(self, context, config_mgr, mysql_mgr, plugin):
        self.storage = object()
        self.renderer = object()


class _FakeProfileService:
    def __init__(self, context, config_mgr, mysql_mgr, plugin):
        self.storage = object()
        self.renderer = object()


class _FakeWebAPI:
    def __init__(self, *args, **kwargs):
        pass


class _FakeMainStatsService:
    # v0.6.0 main.py 构造：StatsService(context, mysql_mgr, config_mgr, self)
    def __init__(self, context=None, mysql_mgr=None, config_mgr=None, plugin=None):
        pass


class _FakeMainStatsBuildError(Exception):
    pass


_inject_fake(f"{_PKG}.core.db_mysql", {"MySQLManager": _FakeMySQLManager})
_inject_fake(f"{_PKG}.core.db_config", {"ConfigManager": _FakeConfigManager})
_inject_fake(f"{_PKG}.core.cleaner", {"ImageCleaner": _FakeImageCleaner})
_inject_fake(f"{_PKG}.core.summary", {"SummaryService": _FakeSummaryService})
_inject_fake(f"{_PKG}.core.webapi", {"WebAPI": _FakeWebAPI})
_inject_fake(f"{_PKG}.core.profile.service", {"ProfileService": _FakeProfileService})
_inject_fake(
    f"{_PKG}.core.stats.service",
    {
        "StatsService": _FakeMainStatsService,
        "StatsBuildError": _FakeMainStatsBuildError,
    },
)

import astrbot_plugin_group_history_save_mysql.main as MAIN  # noqa: E402

# 阶段 B 收尾：全量剔除（fake 注入项 + main + 其拉起的真实子模块），
# 保证阶段 A 的 import 全部重新执行并绑定本文件 stub
_purge_plugin_modules()

# ============================================================
# 四、阶段 A：导入真实被测模块（集成测试用真实链路）
# ============================================================

from astrbot_plugin_group_history_save_mysql.core.db_config import (  # noqa: E402
    ConfigManager,
)

# 轻量子模块导入不拉起重链断言的采样点：stats.models 导入后、
# stats.service（顶层 import repository）导入前
from astrbot_plugin_group_history_save_mysql.core.stats.models import (  # noqa: E402
    StatsQuery,
    StatsTimeRange,
)

_MODELS_IMPORT_KEEP_LIGHT = (
    f"{_PKG}.core.stats.repository" not in sys.modules
    and f"{_PKG}.core.stats.service" not in sys.modules
)

from astrbot_plugin_group_history_save_mysql.core.stats.snapshot import (  # noqa: E402
    ImageSnapshotManager,
)
from astrbot_plugin_group_history_save_mysql.core.stats.service import (  # noqa: E402
    StatsService,
)

import astrbot_plugin_group_history_save_mysql.core.webapi as WEB  # noqa: E402

# ============================================================
# 五、集成测试用 fake 替身
# ============================================================


class FakeStatsRepo:
    """MySQL 聚合仓储替身：八个查询入口全部可编排并记录调用参数。

    与 StatsRepository 公开方法签名一致；StatsService 构造后以属性覆写
    （service.repo 公开即为测试注入设计）。get_image_window_counts 同时
    服务于真实 ImageSnapshotManager（组合链路的关键拼接点）。
    """

    def __init__(self):
        self.calls: list[tuple] = []  # (method, args)
        self.overview = {"total": 0, "active_senders": 0, "first": None, "last": None}
        self.hourly = [0] * 24
        self.weekday = [0] * 7
        self.trend: list = []  # [("YYYY-MM-DD", count)]
        self.sender_rows: list[dict] = []
        self.group_rows: list[dict] = []
        self.member_overview = {"count": 0, "name": "", "active_days": 0}
        self.image_counts = {"groups": {}, "senders": {}}
        self.raise_in: str | None = None  # 指定方法名 → 该方法抛异常

    def _record(self, method, *args):
        self.calls.append((method, args))

    def _maybe_raise(self, method):
        if self.raise_in == method:
            raise RuntimeError(f"fake repo boom in {method}")

    def calls_of(self, method) -> list[tuple]:
        return [args for name, args in self.calls if name == method]

    async def get_overview(self, group_id, start, end):
        self._record("get_overview", group_id, start, end)
        self._maybe_raise("get_overview")
        return self.overview

    async def get_overview_meta(self, group_id, start, end):
        # v0.5.5 快照路径专用：总消息数由快照供数，实时只补活跃数/首末条时间
        # （与真实 StatsRepository.get_overview_meta 契约一致，不含 total）
        self._record("get_overview_meta", group_id, start, end)
        self._maybe_raise("get_overview_meta")
        return {
            "active_senders": self.overview.get("active_senders", 0),
            "first": self.overview.get("first"),
            "last": self.overview.get("last"),
        }

    async def get_group_active_senders(self, start, end):
        # v0.5.5 群排行快照路径专用：窗口内每群活跃成员数
        # （由群排行行数据推导，与 repo.get_group_ranking 的 active_senders 同源）
        self._record("get_group_active_senders", start, end)
        self._maybe_raise("get_group_active_senders")
        return {r["group_id"]: r.get("active_senders", 0) for r in self.group_rows}

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
        return list(self.sender_rows)

    async def get_group_ranking(self, start, end, limit):
        self._record("get_group_ranking", start, end, limit)
        self._maybe_raise("get_group_ranking")
        return list(self.group_rows)

    async def get_member_overview(self, group_id, sender_id, start, end):
        self._record("get_member_overview", group_id, sender_id, start, end)
        self._maybe_raise("get_member_overview")
        return dict(self.member_overview)

    async def get_image_window_counts(self, start, end, top_k):
        self._record("get_image_window_counts", start, end, top_k)
        self._maybe_raise("get_image_window_counts")
        return self.image_counts


class FakeRenderer:
    """T2I 渲染器替身：可编排返回路径 / None（降级路径）。"""

    def __init__(self, result="/tmp/fake_card.png"):
        self.result = result
        self.render_calls: list[tuple] = []  # (data, title)

    async def render_card(self, data, title):
        self.render_calls.append((data, title))
        return self.result


class FakeContext:
    """AstrBot Context 替身：记录 send_message / register_web_api 调用。"""

    def __init__(self):
        self.sent: list[tuple] = []  # (umo, chain)
        self.routes: list[tuple] = []

    async def send_message(self, umo, chain):
        self.sent.append((umo, chain))

    def register_web_api(self, route, handler, methods, desc):
        self.routes.append((route, handler, methods, desc))


async def _fresh_config() -> ConfigManager:
    """每用例全新数据目录 + 真实 ConfigManager 初始化（含 stats 四表播种）。"""
    _fresh_data_dir()
    cfg = ConfigManager()
    assert await cfg.initialize() is True, "真实 ConfigManager 初始化失败"
    return cfg


def _build_service(cfg: ConfigManager):
    """真实 StatsService 骨架 + fake repo/renderer/context；快照保留真实。

    组合路径核心：service.snapshot = 真实 ImageSnapshotManager(cfg, fake_repo)
    —— build_stats 的强制刷新与 fill_counts 全程走真实 SQLite。
    """
    ctx = FakeContext()
    # v0.5.2 起 StatsService 构造含第 4 参 star（T2I 渲染器注入用）；本测试
    # 随后整体替换 renderer/repo/snapshot，star 传 None 即可
    svc = StatsService(ctx, None, cfg, None)
    repo = FakeStatsRepo()
    svc.repo = repo
    svc.snapshot = ImageSnapshotManager(cfg, repo)
    renderer = FakeRenderer()
    svc.renderer = renderer
    return svc, ctx, repo, renderer


def _today_range() -> StatsTimeRange:
    now = datetime.now()
    start = datetime(now.year, now.month, now.day)
    return StatsTimeRange(start=start, end=start + timedelta(days=1), label="今日")


def _expect_refresh_contribution() -> bool:
    """当前时刻是否恰在整点边界（refresh_current_hour 窗口为空会跳过）。"""
    now = datetime.now()
    return not (now.minute == 0 and now.second == 0 and now.microsecond == 0)


# ============================================================
# 六、组合路径一：build_stats × 真实快照 × 真实 SQLite 配置库
# ============================================================


class TestBuildStatsWithRealSnapshot(unittest.TestCase):
    """TC-01~04：service.build_stats × 真实 ImageSnapshotManager × 真实
    ConfigManager（aiosqlite）× fake MySQL 仓储——冒烟从未串联的组合链。
    """

    @async_test
    async def test_tc01_group_view_full_snapshot_chain(self):
        """TC-01 单群视图：强制刷新当前小时 → UPSERT 真实 SQLite →
        fill_counts 回注 total_images 与排行 image_count。"""
        cfg = await _fresh_config()
        try:
            svc, _ctx, repo, _renderer = _build_service(cfg)
            now = datetime.now()
            gid = "111"
            repo.overview = {
                "total": 120,
                "active_senders": 3,
                "first": now - timedelta(hours=2),
                "last": now,
            }
            repo.hourly = [0] * 21 + [30, 60, 30]  # 峰值 22 时
            repo.sender_rows = [
                {"sender_id": "u1", "sender_name": "甲", "count": 50},
                {"sender_id": "u2", "sender_name": "乙", "count": 40},
                {"sender_id": "u3", "sender_name": "丙", "count": 30},
            ]
            repo.trend = [(now.strftime("%Y-%m-%d"), 120)]
            # 快照源：任意窗口返回固定聚合（refresh 与定时任务共用入口）
            repo.image_counts = {
                "groups": {gid: 9},
                "senders": {gid: [("u1", "甲", 5), ("u2", "乙", 4)]},
            }
            # v0.5.5 快照供数：total/趋势由消息三层归并取数，先播种今日
            # 消息小时快照（空快照归并返回 0 而非 None，不会回退 repo 实时行）
            await cfg.snapshot_upsert_msg_hour(
                [(now.strftime("%Y-%m-%d"), 0, gid, 120)], []
            )

            data = await svc.build_stats(
                StatsQuery(group_id=gid, member_id=None, time_range=_today_range())
            )

            # ---- 消息侧组装（真实 service 逻辑）----
            self.assertEqual(data.total_messages, 120)
            self.assertEqual(data.active_senders, 3)
            self.assertEqual(data.peak_hour, 22)
            self.assertEqual(len(data.daily_trend), 1)
            self.assertEqual(data.daily_trend[0]["count"], 120)

            # ---- 快照侧组合链：refresh 查询参数为 [本小时整点, now) ----
            hour_floor = now.replace(minute=0, second=0, microsecond=0)
            img_calls = repo.calls_of("get_image_window_counts")
            if _expect_refresh_contribution():
                self.assertEqual(len(img_calls), 1)
                start_arg, end_arg, top_k = img_calls[0]
                self.assertEqual(start_arg, hour_floor)
                self.assertEqual(
                    end_arg.replace(microsecond=0), now.replace(microsecond=0)
                )
                self.assertEqual(top_k, 20)  # stats_image_top_k 默认值透传

                # 真实 SQLite 落库：image_stats_hourly / _top 各 1 群 2 人
                async with cfg.db.execute(
                    "SELECT group_id, image_count FROM image_stats_hourly "
                    "WHERE date = ? AND hour = ?",
                    (now.strftime("%Y-%m-%d"), now.hour),
                ) as cur:
                    hour_rows = await cur.fetchall()
                self.assertEqual(hour_rows, [(gid, 9)])
                async with cfg.db.execute(
                    "SELECT sender_id, image_count FROM image_stats_hourly_top "
                    "WHERE date = ? AND hour = ? ORDER BY sender_id",
                    (now.strftime("%Y-%m-%d"), now.hour),
                ) as cur:
                    top_rows = await cur.fetchall()
                self.assertEqual(top_rows, [("u1", 5), ("u2", 4)])

                # fill_counts 经真实 snapshot_query 回注
                self.assertEqual(data.total_images, 9)
                self.assertEqual(data.sender_ranking[0].image_count, 5)
                self.assertEqual(data.sender_ranking[1].image_count, 4)
                self.assertEqual(data.sender_ranking[2].image_count, 0)  # 缺失保持 0
            else:  # pragma: no cover - 整点边界防御（概率≈0）
                self.assertEqual(data.total_images, 0)
        finally:
            await cfg.close()

    @async_test
    async def test_tc02_all_groups_view_group_ranking_images(self):
        """TC-02 全部群视图：群排行 image_count 注入 + 发言人排行恒空。"""
        cfg = await _fresh_config()
        try:
            svc, _ctx, repo, _renderer = _build_service(cfg)
            now = datetime.now()
            repo.overview = {"total": 300, "active_senders": 8}
            repo.group_rows = [
                {"group_id": "g1", "count": 200, "active_senders": 5},
                {"group_id": "g2", "count": 100, "active_senders": 3},
            ]
            repo.image_counts = {
                "groups": {"g1": 7, "g2": 3},
                "senders": {"g1": [("u1", "甲", 4)]},
            }
            # v0.5.5 快照供数：群排行 count 由消息三层归并取数，先播种今日
            # 消息小时快照（空快照归并返回 0 而非 None，不会回退 repo 群排行行）
            await cfg.snapshot_upsert_msg_hour(
                [
                    (now.strftime("%Y-%m-%d"), 0, "g1", 200),
                    (now.strftime("%Y-%m-%d"), 0, "g2", 100),
                ],
                [],
            )

            data = await svc.build_stats(
                StatsQuery(group_id=None, member_id=None, time_range=_today_range())
            )

            self.assertEqual(data.sender_ranking, [])  # 全部群视图无发言人排行
            self.assertEqual([g.group_id for g in data.group_ranking], ["g1", "g2"])
            self.assertEqual(data.group_ranking[0].count, 200)
            self.assertEqual(data.group_ranking[0].active_senders, 5)
            if _expect_refresh_contribution():
                self.assertEqual(data.total_images, 10)
                self.assertEqual(data.group_ranking[0].image_count, 7)
                self.assertEqual(data.group_ranking[1].image_count, 3)
            else:  # pragma: no cover
                self.assertEqual(data.total_images, 0)
        finally:
            await cfg.close()

    @async_test
    async def test_tc03_all_groups_member_view_cross_group_sum(self):
        """TC-03 全部群个人视图：member.image_count 跨群按 sender 求和、
        rank 恒 None、不查发言人排行。"""
        cfg = await _fresh_config()
        try:
            svc, _ctx, repo, _renderer = _build_service(cfg)
            repo.overview = {"total": 300, "active_senders": 8}
            repo.member_overview = {"count": 60, "name": "甲", "active_days": 4}
            repo.image_counts = {
                "groups": {"g1": 7, "g2": 3},
                "senders": {
                    "g1": [("u1", "甲", 4), ("u2", "乙", 2)],
                    "g2": [("u1", "甲", 6)],
                },
            }

            data = await svc.build_stats(
                StatsQuery(group_id=None, member_id="u1", time_range=_today_range())
            )

            self.assertIsNotNone(data.member)
            self.assertEqual(data.member.sender_id, "u1")
            self.assertEqual(data.member.count, 60)
            self.assertIsNone(data.member.rank)  # 全部群个人视图名次恒 None
            self.assertEqual(data.sender_ranking, [])  # group_id=None 分支
            self.assertEqual(repo.calls_of("get_sender_ranking"), [])
            if _expect_refresh_contribution():
                self.assertEqual(data.member.image_count, 10)  # 4 + 6 跨群求和
                self.assertEqual(data.total_images, 10)
            else:  # pragma: no cover
                self.assertEqual(data.member.image_count, 0)
        finally:
            await cfg.close()

    @async_test
    async def test_tc04_group_member_view_rank_lookup_and_limit50(self):
        """TC-04 单群个人视图：完整排行以 limit=50 查询（名次扫描深度，
        单次查询两用）、rank 按 sender_id 匹配、member.image_count 按
        (gid, sid) 注入、展示排行切 top_n。"""
        cfg = await _fresh_config()
        try:
            svc, _ctx, repo, _renderer = _build_service(cfg)
            gid = "222"
            repo.overview = {"total": 200, "active_senders": 4}
            repo.member_overview = {"count": 50, "name": "乙", "active_days": 2}
            # 完整排行 60 行：目标成员 u1 位于第 2 名（超出展示 top_n=10）
            repo.sender_rows = [
                {"sender_id": "u0", "sender_name": "头名", "count": 80},
                {"sender_id": "u1", "sender_name": "乙", "count": 50},
            ] + [
                {"sender_id": f"x{i}", "sender_name": f"路人{i}", "count": 10 - i}
                for i in range(58)
            ]
            repo.image_counts = {
                "groups": {gid: 12},
                "senders": {gid: [("u1", "乙", 11)]},
            }
            # v0.5.5 快照供数：total 由消息三层归并取数（空快照归并返回 0
            # 而非 None，不回退 repo 实时行），播种今日消息小时快照
            await cfg.snapshot_upsert_msg_hour(
                [(datetime.now().strftime("%Y-%m-%d"), 0, gid, 200)], []
            )

            data = await svc.build_stats(
                StatsQuery(
                    group_id=gid, member_id="u1", time_range=_today_range(), top_n=10
                )
            )

            # 名次查找 + 展示共用完整排行 limit=50（单次调用）
            rank_calls = repo.calls_of("get_sender_ranking")
            self.assertEqual(len(rank_calls), 1)
            self.assertEqual(rank_calls[0][3], 50)
            self.assertEqual(data.member.rank, 2)
            self.assertEqual(len(data.sender_ranking), 10)
            self.assertAlmostEqual(data.member.ratio, 50 / 200)
            if _expect_refresh_contribution():
                self.assertEqual(data.member.image_count, 11)
                self.assertEqual(data.total_images, 12)
            else:  # pragma: no cover
                self.assertEqual(data.member.image_count, 0)
        finally:
            await cfg.close()


# ============================================================
# 七、组合路径二：push_report × 真实群推送开关（白名单门控）
# ============================================================


class TestPushReportWithRealPushGroups(unittest.TestCase):
    """TC-05~08：push_report × 真实 get_push_groups（LEFT JOIN 白名单门控）
    × 真实配置读取——smoke_g 以 FakeConfig 覆盖，白名单 JOIN 语义从未与
    推送编排串联。"""

    def setUp(self):
        _STUB_LOGGER.clear()

    async def _seed_groups(self, cfg: ConfigManager):
        """白名单：111 / 222；推送开关：111 开、222 关、333（非白名单）开。"""
        self.assertTrue(await cfg.add_group(111))
        self.assertTrue(await cfg.add_group(222))
        self.assertTrue(await cfg.set_push_group("111", True))
        self.assertTrue(await cfg.set_push_group("222", False))
        self.assertTrue(await cfg.set_push_group("333", True))

    @async_test
    async def test_tc05_whitelist_gating_only_enabled_whitelisted_pushed(self):
        """TC-05 仅「白名单 ∩ enabled」群被推送；非白名单 enabled 行忽略。"""
        cfg = await _fresh_config()
        try:
            svc, ctx, repo, renderer = _build_service(cfg)
            await self._seed_groups(cfg)
            svc.record_group_umo("111", "umo-111")
            svc.record_group_umo("222", "umo-222")
            svc.record_group_umo("333", "umo-333")
            repo.overview = {"total": 42, "active_senders": 2}
            renderer.result = "/tmp/daily.png"

            await svc.push_report("daily", now=datetime(2026, 8, 4, 21, 0))

            # 只有 111 命中（222 disabled、333 非白名单被 LEFT JOIN 排除）
            self.assertEqual(len(ctx.sent), 1)
            umo, chain = ctx.sent[0]
            self.assertEqual(umo, "umo-111")
            self.assertEqual(len(chain.chain), 1)
            img = chain.chain[0]
            self.assertIsInstance(img, FakeImage)
            self.assertEqual(img.kind, "file")
            self.assertEqual(img.source, "/tmp/daily.png")
            # 推送标题走日报口径
            self.assertEqual(renderer.render_calls[0][1], "群聊数据日报")
            # 完成日志计数 1/1
            self.assertTrue(
                any("1/1" in m for m in _STUB_LOGGER.records["info"]),
                f"未见 1/1 完成日志: {_STUB_LOGGER.records['info']}",
            )
        finally:
            await cfg.close()

    @async_test
    async def test_tc06_weekly_range_is_last_full_week(self):
        """TC-06 weekly 时间窗口 = 上一整周 [上周一 00:00, 本周一 00:00)，
        label「上周」，与触发星期无关。"""
        cfg = await _fresh_config()
        try:
            svc, ctx, repo, renderer = _build_service(cfg)
            await self._seed_groups(cfg)
            svc.record_group_umo("111", "umo-111")

            # 2026-08-05 为周三：上一整周 = [2026-07-27 周一, 2026-08-03 周一)
            await svc.push_report("weekly", now=datetime(2026, 8, 5, 9, 0))

            # v0.5.5 起：可服务范围内总消息数由快照三层归并供数，实时 SQL
            # 只补 overview_meta（活跃数/首末条时间），get_overview 不再被调
            overview_calls = repo.calls_of("get_overview_meta")
            self.assertEqual(len(overview_calls), 1)
            _gid, start, end = overview_calls[0]
            self.assertEqual(start, datetime(2026, 7, 27))
            self.assertEqual(end, datetime(2026, 8, 3))
            self.assertEqual(renderer.render_calls[0][1], "群聊数据周报")
            self.assertEqual(len(ctx.sent), 1)
            # top_n 读真实配置默认 10 并透传排行查询
            rank_calls = repo.calls_of("get_sender_ranking")
            self.assertEqual(rank_calls[0][3], 10)
        finally:
            await cfg.close()

    @async_test
    async def test_tc07_render_fail_fallback_text_content(self):
        """TC-07 渲染失败降级纯文本：零宽空格包裹 + 【群聊日报·今日】格式 +
        Top3 发言人内容（昵称缺失回退 QQ 号）。"""
        cfg = await _fresh_config()
        try:
            svc, ctx, repo, renderer = _build_service(cfg)
            await self._seed_groups(cfg)
            svc.record_group_umo("111", "umo-111")
            repo.overview = {"total": 123, "active_senders": 3}
            repo.sender_rows = [
                {"sender_id": "u1", "sender_name": "甲", "count": 50},
                {"sender_id": "u2", "sender_name": "乙", "count": 40},
                {"sender_id": "u3", "sender_name": "", "count": 30},
            ]
            renderer.result = None  # 渲染失败
            # v0.5.5 快照供数：总消息数由消息三层归并取数（窗口为注入的
            # 2026-08-04 当日），播种该日消息小时快照
            await cfg.snapshot_upsert_msg_hour([("2026-08-04", 0, "111", 123)], [])

            await svc.push_report("daily", now=datetime(2026, 8, 4, 21, 0))

            self.assertEqual(len(ctx.sent), 1)
            umo, chain = ctx.sent[0]
            self.assertEqual(umo, "umo-111")
            seg = chain.chain[0]
            self.assertIsInstance(seg, FakePlain)
            text = seg.text
            # 零宽空格（U+200B）包裹防 aiocqhttp plain 段 strip
            self.assertTrue(text.startswith("\u200b") and text.endswith("\u200b"))
            self.assertIn("【群聊日报·今日】", text)
            self.assertIn("总消息数：123", text)
            self.assertIn("甲 50条", text)
            self.assertIn("乙 40条", text)
            self.assertIn("u3 30条", text)  # 昵称缺失回退 QQ 号
            self.assertIn("本条为纯文本摘要", text)
            self.assertTrue(
                any("降级纯文本摘要" in m for m in _STUB_LOGGER.records["warning"])
            )
        finally:
            await cfg.close()

    @async_test
    async def test_tc08_no_umo_warn_skip_not_block(self):
        """TC-08 enabled 群无 umo 缓存：warning 跳过、不发送、不抛异常。"""
        cfg = await _fresh_config()
        try:
            svc, ctx, _repo, _renderer = _build_service(cfg)
            self.assertTrue(await cfg.add_group(222))
            self.assertTrue(await cfg.set_push_group("222", True))
            # 不登记 umo

            await svc.push_report("daily", now=datetime(2026, 8, 4, 21, 0))

            self.assertEqual(ctx.sent, [])
            self.assertTrue(
                any("无 umo 缓存" in m for m in _STUB_LOGGER.records["warning"]),
                f"未见无 umo warning: {_STUB_LOGGER.records['warning']}",
            )
            self.assertTrue(any("0/1" in m for m in _STUB_LOGGER.records["info"]))
        finally:
            await cfg.close()


# ============================================================
# 八、缺口：冷却配置 0=不限流（真实 typed 配置链路）
# ============================================================


class TestCooldownWithRealConfig(unittest.TestCase):
    """TC-09/10：check_cooldown 经真实 ConfigManager typed 读取。"""

    @async_test
    async def test_tc09_zero_cooldown_unlimited(self):
        """TC-09 stats_cooldown=0：连续多次全部放行且不盖章。"""
        cfg = await _fresh_config()
        try:
            svc, _ctx, _repo, _renderer = _build_service(cfg)
            self.assertTrue(await cfg.set_stats_setting("stats_cooldown", 0))
            self.assertEqual(await cfg.get_stats_setting_typed("stats_cooldown"), 0)

            for _ in range(3):
                self.assertTrue(await svc.check_cooldown("g1"))
            # 0=不限流不盖章：冷却表保持空
            self.assertEqual(svc._cooldown, {})
        finally:
            await cfg.close()

    @async_test
    async def test_tc10_cooldown_reject_isolate_expire(self):
        """TC-10 冷却 600s：期内拒绝（不盖章刷新）、群隔离、到期放行。"""
        cfg = await _fresh_config()
        try:
            svc, _ctx, _repo, _renderer = _build_service(cfg)
            self.assertTrue(await cfg.set_stats_setting("stats_cooldown", 600))

            self.assertTrue(await svc.check_cooldown("g1"))  # 首次放行盖章
            self.assertFalse(await svc.check_cooldown("g1"))  # 期内拒绝
            self.assertTrue(await svc.check_cooldown("g2"))  # 群隔离
            self.assertFalse(await svc.check_cooldown("g2"))

            # 模拟时间推进越过冷却期（直接回拨盖章时间戳）
            svc._cooldown["g1"] = time.monotonic() - 601
            self.assertTrue(await svc.check_cooldown("g1"))
        finally:
            await cfg.close()


# ============================================================
# 九、组合路径三：web_api stats 端点 × 真实 ConfigManager
# ============================================================

# v0.5.0 新增的 6 条 stats 路由（与 web_api._register_routes 一致；v0.5.1 +群列表）
_STATS_ROUTE_SUFFIXES = (
    "/stats/data",
    "/stats/groups",
    "/stats/settings",
    "/stats/settings/save",
    "/stats/settings/reset",
    "/stats/push/toggle",
)


class TestWebApiStatsWithRealConfig(unittest.TestCase):
    """TC-11~16：五个 stats 端点 × 真实 ConfigManager 落库往返。
    smoke_h 以 FakeConfigMgr 覆盖端点逻辑，真实校验/归一化/持久化从未串联。
    """

    def _build_api(self, cfg, stats_service=None):
        ctx = FakeContext()
        api = WEB.WebAPI(
            ctx,
            object(),  # mysql_mgr（stats 端点不使用）
            cfg,
            object(),  # cleaner（stats 端点不使用）
            stats_service=stats_service,
        )
        # 五个 stats 路由全部注册（顺带回归路由挂载）
        registered = {r[0] for r in ctx.routes}
        for suffix in _STATS_ROUTE_SUFFIXES:
            self.assertTrue(
                any(path.endswith(suffix) for path in registered),
                f"缺少 stats 路由: {suffix}",
            )
        return api

    def _set_request(self, query=None, body=None):
        _STUB_REQUEST.query = _StubQuery(query or {})
        _STUB_REQUEST._json = body if body is not None else {}

    @async_test
    async def test_tc11_settings_get_real_db(self):
        """TC-11 stats/settings：8 项 typed 配置 + push_groups 真实读取。"""
        cfg = await _fresh_config()
        try:
            self.assertTrue(await cfg.add_group(111))
            self.assertTrue(await cfg.set_push_group("111", True))
            api = self._build_api(cfg)
            self._set_request()

            resp = await api.api_stats_settings()

            self.assertIsInstance(resp, JsonResp)
            settings = resp.data["settings"]
            self.assertEqual(len(settings), 8)
            self.assertEqual(settings["stats_top_n"], 10)
            self.assertIs(settings["push_daily_enabled"], True)
            self.assertIs(settings["push_weekly_enabled"], False)
            self.assertEqual(settings["push_daily_time"], "21:00")
            self.assertEqual(settings["push_weekly_weekday"], 1)
            self.assertEqual(
                resp.data["push_groups"], [{"group_id": "111", "enabled": True}]
            )
        finally:
            await cfg.close()

    @async_test
    async def test_tc12_settings_save_normalize_and_persist(self):
        """TC-12 save：扁平 body 归一化写入真实 DB（"9:00"→"09:00"、
        int 透传），返回归一后全量 settings。"""
        cfg = await _fresh_config()
        try:
            api = self._build_api(cfg)
            self._set_request(
                body={
                    "push_daily_time": "9:00",
                    "stats_top_n": 25,
                    "push_daily_enabled": False,
                }
            )

            resp = await api.api_stats_settings_save()

            self.assertIsInstance(resp, JsonResp)
            self.assertTrue(resp.data["saved"])
            self.assertEqual(resp.data["settings"]["push_daily_time"], "09:00")
            self.assertEqual(resp.data["settings"]["stats_top_n"], 25)
            self.assertIs(resp.data["settings"]["push_daily_enabled"], False)
            # 真实 DB 落库核验
            self.assertEqual(await cfg.get_stats_setting("push_daily_time"), "09:00")
            self.assertEqual(await cfg.get_stats_setting_typed("stats_top_n"), 25)
        finally:
            await cfg.close()

    @async_test
    async def test_tc13_settings_save_invalid_rejects_all_no_partial_write(self):
        """TC-13 save 全量校验：任一非法整体 400，合法项也不写入。"""
        cfg = await _fresh_config()
        try:
            api = self._build_api(cfg)
            # stats_cooldown=999 超范围；stats_top_n=5 合法——不得部分写入
            self._set_request(body={"stats_cooldown": 999, "stats_top_n": 5})

            resp = await api.api_stats_settings_save()

            self.assertIsInstance(resp, ErrResp)
            self.assertEqual(resp.status_code, 400)
            self.assertIn("stats_cooldown", resp.message)
            self.assertEqual(await cfg.get_stats_setting_typed("stats_top_n"), 10)
            self.assertEqual(await cfg.get_stats_setting_typed("stats_cooldown"), 30)
        finally:
            await cfg.close()

    @async_test
    async def test_tc14_settings_reset_restores_defaults(self):
        """TC-14 reset：真实 DB 中已改值全部恢复默认。"""
        cfg = await _fresh_config()
        try:
            self.assertTrue(await cfg.set_stats_setting("stats_top_n", 49))
            self.assertTrue(await cfg.set_stats_setting("push_daily_time", "08:30"))
            api = self._build_api(cfg)
            self._set_request()

            resp = await api.api_stats_settings_reset()

            self.assertIsInstance(resp, JsonResp)
            self.assertTrue(resp.data["reset"])
            self.assertEqual(resp.data["settings"]["stats_top_n"], 10)
            self.assertEqual(resp.data["settings"]["push_daily_time"], "21:00")
            self.assertEqual(await cfg.get_stats_setting("stats_top_n"), "10")
        finally:
            await cfg.close()

    @async_test
    async def test_tc15_push_toggle_persists_and_validates(self):
        """TC-15 toggle：真实 push_group 表持久化 + 参数校验（非数字群号 /
        非法 enabled → 400）。"""
        cfg = await _fresh_config()
        try:
            self.assertTrue(await cfg.add_group(123))
            api = self._build_api(cfg)

            self._set_request(body={"group_id": 123, "enabled": True})
            resp = await api.api_stats_push_toggle()
            self.assertIsInstance(resp, JsonResp)
            self.assertEqual(resp.data, {"group_id": "123", "enabled": True})
            self.assertEqual(
                await cfg.get_push_groups(), [{"group_id": "123", "enabled": True}]
            )

            self._set_request(body={"group_id": "abc", "enabled": True})
            resp = await api.api_stats_push_toggle()
            self.assertIsInstance(resp, ErrResp)
            self.assertEqual(resp.status_code, 400)

            self._set_request(body={"group_id": "123", "enabled": "maybe"})
            resp = await api.api_stats_push_toggle()
            self.assertIsInstance(resp, ErrResp)
            self.assertEqual(resp.status_code, 400)
        finally:
            await cfg.close()

    @async_test
    async def test_tc16_stats_data_endpoint_with_real_service(self):
        """TC-16 stats/data 端点 × 真实 StatsService 全链路：顶层 stats 键、
        datetime 序列化格式、快照注入、显式单日与缺省窗口口径。"""
        cfg = await _fresh_config()
        try:
            svc, _ctx, repo, _renderer = _build_service(cfg)
            api = self._build_api(cfg, stats_service=svc)
            now = datetime.now()
            today_s = now.strftime("%Y-%m-%d")
            repo.overview = {
                "total": 77,
                "active_senders": 2,
                "first": now - timedelta(hours=1),
                "last": now,
            }
            repo.sender_rows = [{"sender_id": "u1", "sender_name": "甲", "count": 77}]
            repo.image_counts = {"groups": {"111": 6}, "senders": {}}
            # v0.5.5 快照供数：total_messages 由消息三层归并取数（空快照归并
            # 返回 0 而非 None，不回退 repo 实时行），播种今日消息小时快照
            await cfg.snapshot_upsert_msg_hour([(today_s, 0, "111", 77)], [])

            self._set_request(
                query={"group_id": "111", "start": today_s, "end": today_s}
            )
            resp = await api.api_stats_data()

            self.assertIsInstance(resp, JsonResp)
            stats = resp.data["stats"]
            self.assertEqual(stats["query"]["group_id"], "111")
            # 显式单日窗口 label = 该日期字符串
            self.assertEqual(stats["query"]["time_range"]["label"], today_s)
            self.assertEqual(stats["total_messages"], 77)
            # datetime 序列化格式 YYYY-MM-DD HH:MM:SS
            self.assertRegex(
                stats["generated_at"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$"
            )
            self.assertRegex(
                stats["first_msg_time"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$"
            )
            # 快照注入经真实 SQLite（整点边界防御）
            if _expect_refresh_contribution():
                self.assertEqual(stats["total_images"], 6)
            # 缺省窗口口径：不传 start/end → 近7天
            self._set_request(query={})
            resp2 = await api.api_stats_data()
            self.assertEqual(
                resp2.data["stats"]["query"]["time_range"]["label"], "近7天"
            )
        finally:
            await cfg.close()


# ============================================================
# 十、缺口：main.py 指令接线（别名注册 + GreedyStr 注解）
# ============================================================


class TestCommandWiring(unittest.TestCase):
    """TC-17/18：/群统计 装饰器参数断言（smoke_m 恒等装饰器丢弃参数，
    别名注册从未被覆盖）。"""

    def test_tc17_group_stats_registered_with_aliases(self):
        """TC-17 主指令名 群统计 + 别名 {群数据, 统计}（PRD F2）。"""
        records = [r for r in _COMMAND_RECORDS if r["func"] == "group_stats"]
        self.assertEqual(len(records), 1)
        rec = records[0]
        self.assertEqual(rec["name"], "群统计")
        self.assertEqual(set(rec["kwargs"].get("alias", set())), {"群数据", "统计"})

    def test_tc18_group_stats_arg_annotation_greedystr(self):
        """TC-18 arg 参数注解为 GreedyStr（收指令名后全部剩余文本，
        防空格分隔日期区间被截断）。"""
        handler = MAIN.GroupHistoryPlugin.group_stats
        params = inspect.signature(handler).parameters
        self.assertIn("arg", params)
        self.assertIs(params["arg"].annotation, _StubGreedyStr)

    def test_tc18b_register_metadata_version_050(self):
        """TC-18b @register 插件名与版本（同源装饰器记录）。

        v0.9.0 漂移同步：版本文本随 @register 演进（0.6.0 → 0.9.0），
        测的是「装饰器记录与 main.py @register 同源」这一接线事实。
        """
        self.assertTrue(_REGISTER_RECORDS)
        args = _REGISTER_RECORDS[-1]["args"]
        self.assertEqual(args[0], "astrbot_plugin_group_history_save_mysql")
        self.assertEqual(args[3], "0.9.0")


# ============================================================
# 十一、缺口：stats/__init__ PEP 562 惰性导出
# ============================================================


class TestStatsPackageLazyExports(unittest.TestCase):
    """TC-19：惰性导出 identity / 未知属性 AttributeError / 轻量导入。"""

    def test_tc19_lazy_exports_identity_and_errors(self):
        # 跨文件合跑时各测试文件顶层会剔除插件包 sys.modules 并重导入，
        # 单进程内可并存多代模块对象——本套件顶层绑定的是收集期旧代，
        # 而 STATS_PKG.__getattr__ 运行期会从 sys.modules 解析到新生代，
        # 跨代 identity 断言必假（与 PEP 562 机制本身无关）。故断言前统一
        # 取当前代模块对象：测的是「惰性导出返回真实子类」这一机制。
        pkg = importlib.import_module(
            "astrbot_plugin_group_history_save_mysql.core.stats"
        )
        svc_mod = importlib.import_module(
            "astrbot_plugin_group_history_save_mysql.core.stats.service"
        )
        repo_mod = importlib.import_module(
            "astrbot_plugin_group_history_save_mysql.core.stats.repository"
        )
        # 导出名与子模块真实类同一对象
        self.assertIs(pkg.StatsService, svc_mod.StatsService)
        self.assertIs(pkg.StatsBuildError, svc_mod.StatsBuildError)
        self.assertIs(pkg.StatsRepository, repo_mod.StatsRepository)
        # 缓存生效：二次访问走 __dict__，不再经 __getattr__
        self.assertIn("StatsService", pkg.__dict__)
        # 未知属性抛 AttributeError
        with self.assertRaises(AttributeError):
            _ = pkg.NoSuchExport
        # v0.5.5 起惰性导出登记新增 SnapshotManager / ImageSnapshotManager（快照）
        self.assertEqual(
            set(pkg.__all__),
            {
                "StatsRepository",
                "StatsService",
                "StatsBuildError",
                "SnapshotManager",
                "ImageSnapshotManager",
            },
        )

    def test_tc19b_models_import_keeps_light(self):
        # 阶段 A 采样点：stats.models 导入时未拉起 repository / service 重链
        self.assertTrue(
            _MODELS_IMPORT_KEEP_LIGHT,
            "stats.models 导入拉起了 repository/service 重依赖链",
        )


# ============================================================
# 十二、独立冒烟脚本并入同一 pytest 进程
# ============================================================


class TestStandaloneSmokesInProcess(unittest.TestCase):
    """TC-20/21：smoke_a / smoke_d 为 if __name__ == "__main__" 独立脚本，
    pytest 直接收集贡献 0 用例；此处在其各自模块内调用 main()（ok() 内置
    assert，失败即抛出），使全部断言在同一 pytest 进程内执行。两份脚本自
    带独立 stub 与数据目录，与本套件互不干扰。"""

    def test_tc20_smoke_a_config_main_green(self):
        mod = importlib.import_module("smoke_a_config")
        asyncio.run(mod.main())

    def test_tc21_smoke_d_snapshot_main_green(self):
        mod = importlib.import_module("smoke_d_snapshot")
        asyncio.run(mod.main())


if __name__ == "__main__":
    unittest.main()
