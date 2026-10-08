# ruff: noqa: I001
"""v0.5.0 模块 M 主入口集成（main.py）冒烟脚本。

被测对象：``main.py`` —— v0.5.0 数据分析主入口集成层，覆盖：

- ``/群统计``（别名 群数据/统计）指令 handler：
  私聊回用法 + stop、冷却静默（不回复但 stop）、解析错误回 usage、
  正常链路 image_result 参数、render None 降级文本含 Top3、
  StatsBuildError 文案透传、未知异常兜底、@ 目标剔除 bot 自身；
- ``on_group_message`` 白名单分支：umo 推送目标登记（服务在）/ 跳过（服务 None）；
- 生命周期：构造期 StatsService 创建 + WebAPI ``stats_service=`` 注入、
  ``initialize`` → MySQL 成功后 ``stats_service.start()``、
  ``terminate`` LIFO 停用（stats → summary → profile → cleaner → mysql → config）。

范式沿用 tests/v0.4.0/test_profile_main.py（Agent-C stub 隔离技巧）：
astrbot.* 全部 sys.modules stub；db_mysql / db_config / cleaner / summary /
web_api / profile.service / stats.service 以 fake 模块注入 sys.modules 满足
main.py 顶层 import（避免拉整条重依赖链）。**保留真实** stats/__init__（验证
PEP 562 惰性导出）、stats.parser / stats.models（真实解析与数据模型）、
profile/__init__（惰性）与 profile.capture（真实 @ 提取），确保指令参数链路
走真实代码而非替身。

运行方式（插件根目录，二者皆可）：
    python -m pytest "tests/v0.5.0/smoke_m_main.py" -v
    python "tests/v0.5.0/smoke_m_main.py"
"""

import asyncio
import os
import sys
import types
import unittest


# ============================================================
# 一、astrbot.* stub 注入（必须在任何 from astrbot_plugin_group_history_save_mysql... import 之前）
# ============================================================


def _new_module(name: str) -> types.ModuleType:
    return types.ModuleType(name)


_astrbot = _new_module("astrbot")
_astrbot_api = _new_module("astrbot.api")
_astrbot_event = _new_module("astrbot.api.event")
_astrbot_star = _new_module("astrbot.api.star")
_astrbot_mc = _new_module("astrbot.api.message_components")
# v0.5.0 起 main.py 顶层 import GreedyStr，需要补齐 astrbot.core 链 stub
_astrbot_core = _new_module("astrbot.core")
_astrbot_core_star = _new_module("astrbot.core.star")
_astrbot_core_star_filter = _new_module("astrbot.core.star.filter")
_astrbot_core_star_filter_command = _new_module("astrbot.core.star.filter.command")


class _StubGreedyStr(str):
    """指令剩余文本参数标记（真实实现为 str 子类，stub 同形）。"""


_astrbot_core_star_filter_command.GreedyStr = _StubGreedyStr


# ---- astrbot.api: logger（带记录能力，验证未知异常记日志）----
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


_STUB_LOGGER = _StubLogger()
_astrbot_api.logger = _STUB_LOGGER
_astrbot_api.AstrBotConfig = dict  # main.py: from astrbot.api import AstrBotConfig


# ---- astrbot.api.event: filter / AstrMessageEvent / MessageChain ----
class _IdentityDecoratorFactory:
    def __call__(self, *args, **kwargs):
        def _decorator(fn):
            return fn

        return _decorator


class _StubFilter:
    event_message_type = staticmethod(_IdentityDecoratorFactory())
    platform_adapter_type = staticmethod(_IdentityDecoratorFactory())
    permission_type = staticmethod(_IdentityDecoratorFactory())
    command = staticmethod(_IdentityDecoratorFactory())


class _StubEventMessageType:
    GROUP_MESSAGE = "group_message"


class _StubPlatformAdapterType:
    AIOCQHTTP = "aiocqhttp"


class _StubPermissionType:
    ADMIN = "admin"


class _StubAstrMessageEvent:
    pass


class _StubMessageChain:
    """消息链类型占位（stats/service.py 顶层导入，仅类型标注用）。"""


_FILTER_STUB = _StubFilter()
_FILTER_STUB.EventMessageType = _StubEventMessageType
_FILTER_STUB.PlatformAdapterType = _StubPlatformAdapterType
_FILTER_STUB.PermissionType = _StubPermissionType
_astrbot_event.filter = _FILTER_STUB
_astrbot_event.EventMessageType = _StubEventMessageType
_astrbot_event.PlatformAdapterType = _StubPlatformAdapterType
_astrbot_event.PermissionType = _StubPermissionType
_astrbot_event.AstrMessageEvent = _StubAstrMessageEvent
_astrbot_event.MessageChain = _StubMessageChain


# ---- astrbot.api.star: Context / Star / register ----
class _StubContext:
    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs


class _StubStar:
    def __init__(self, context=None, *args, **kwargs):
        self.context = context


def _stub_register(name, author, desc, version):
    def _decorator(cls):
        cls._registered = (name, author, desc, version)
        return cls

    return _decorator


_astrbot_star.Context = _StubContext
_astrbot_star.Star = _StubStar
_astrbot_star.register = _stub_register
# v0.9.0 漂移同步：core/db_sqlite.py（经 bootstrap 导入链）顶层 import StarTools，
# 补最小 stub（get_data_dir 指向临时目录，语义同真实实现）
from pathlib import Path as _Path  # noqa: E402


class _StubStarTools:
    @classmethod
    def get_data_dir(cls, plugin_name=None):
        return _Path(__file__).resolve().parent / "tmp_data"


_astrbot_star.StarTools = _StubStarTools


# ---- astrbot.api.message_components: Plain / Image / At / AtAll / Reply ----
class _StubPlain:
    def __init__(self, text=""):
        self.text = text


class _StubImage:
    def __init__(self, url=None, file=None):
        self.url = url
        self.file = file

    @staticmethod
    def fromURL(url):
        return _StubImage(url=url)

    @staticmethod
    def fromFileSystem(path):
        return _StubImage(file=path)


class _StubAt:
    def __init__(self, qq="", name=""):
        self.qq = qq
        self.name = name


class _StubAtAll(_StubAt):
    def __init__(self):
        super().__init__(qq="all")


class _StubReply:
    def __init__(self, id=""):
        self.id = id


_astrbot_mc.Plain = _StubPlain
_astrbot_mc.Image = _StubImage
_astrbot_mc.At = _StubAt
_astrbot_mc.AtAll = _StubAtAll
_astrbot_mc.Reply = _StubReply


# ---- 注入 sys.modules ----
sys.modules["astrbot"] = _astrbot
sys.modules["astrbot.api"] = _astrbot_api
sys.modules["astrbot.api.event"] = _astrbot_event
sys.modules["astrbot.api.star"] = _astrbot_star
sys.modules["astrbot.api.message_components"] = _astrbot_mc
sys.modules["astrbot.core"] = _astrbot_core
sys.modules["astrbot.core.star"] = _astrbot_core_star
sys.modules["astrbot.core.star.filter"] = _astrbot_core_star_filter
sys.modules["astrbot.core.star.filter.command"] = _astrbot_core_star_filter_command

# ---- 多测试文件合跑兼容：剔除被测包缓存（Agent-C stub 隔离技巧）----
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
# 二、fake 内部模块注入（重依赖链以 fake 模块注入 sys.modules，均在 import main 之前）
# ============================================================

import importlib  # noqa: E402

_pkg_mod = importlib.import_module(_PKG)


def _inject_fake(module_path: str, attrs: dict):
    """将 attrs 写入新模块并挂到 sys.modules 与父包对象上（合跑隔离）。"""
    mod = _new_module(module_path)
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[module_path] = mod
    parent_path, _, child = module_path.rpartition(".")
    parent = sys.modules.get(parent_path)
    if parent is not None:
        setattr(parent, child, mod)
    return mod


# terminate LIFO 停用顺序记录（各 fake 的 stop/close 追加自身名）
_STOP_ORDER: list[str] = []


class FakeMySQLManager:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.init_result = True
        self.closed = False

    async def initialize(self):
        return self.init_result

    async def ping(self):
        return {"connected": True, "latency_ms": 1, "pool": {}}

    async def get_stats(self):
        return {}

    async def insert_chat_message(self, **kwargs):
        return True

    async def insert_image_record(self, **kwargs):
        return True

    async def close(self):
        self.closed = True
        _STOP_ORDER.append("mysql")


class FakeConfigManager:
    def __init__(self):
        self.settings = {"stats_top_n": 10, "stats_cooldown": 30}
        self.group_enabled = True
        self.group_enabled_calls: list = []
        self.initialized = False
        self.closed = False

    async def initialize(self):
        self.initialized = True
        return True

    async def is_group_enabled(self, group_id):
        self.group_enabled_calls.append(group_id)
        return self.group_enabled

    async def get_stats_setting_typed(self, key):
        return self.settings.get(key)

    async def get_setting(self, key, default=None):
        return self.settings.get(key, default)

    async def get_all_settings(self):
        return dict(self.settings)

    async def add_group(self, group_id):
        return True

    async def remove_group(self, group_id):
        return True

    async def get_groups(self):
        return []

    async def close(self):
        self.closed = True
        _STOP_ORDER.append("config")


class FakeImageCleaner:
    def __init__(self, mysql_mgr, config_mgr):
        self.mysql_mgr = mysql_mgr
        self.config_mgr = config_mgr
        self.started = 0
        self.stopped = 0

    async def start(self):
        self.started += 1

    async def stop(self):
        self.stopped += 1
        _STOP_ORDER.append("cleaner")

    async def manual_clean(self, days=None):
        return 0


class FakeSummaryService:
    def __init__(self, context, config_mgr, mysql_mgr, plugin):
        self.context = context
        self.storage = object()
        self.renderer = object()
        self.started = 0
        self.stopped = 0

    async def start(self):
        self.started += 1

    async def stop(self):
        self.stopped += 1
        _STOP_ORDER.append("summary")

    async def handle_count_command(self, event, arg):
        pass

    async def handle_window_command(self, event, arg):
        pass


class FakeProfileService:
    def __init__(self, context, config_mgr, mysql_mgr, plugin):
        self.context = context
        self.storage = object()
        self.renderer = object()
        self.started = 0
        self.stopped = 0

    async def start(self):
        self.started += 1

    async def stop(self):
        self.stopped += 1
        _STOP_ORDER.append("profile")

    async def handle_command(self, event, arg):
        pass


class FakeWebAPI:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs


# ---- 模块 G 契约替身：StatsService + StatsBuildError ----
class StatsBuildError(Exception):
    """模块 G ``StatsBuildError`` 契约替身（面向用户的友好文案异常）。"""


class FakeStatsService:
    """模块 G 契约替身：可编排 cooldown/build/render 返回，记录 umo/build/render 调用。

    v0.5.2 起构造签名为 (context, mysql_mgr, config_mgr, star)；
    v0.5.5 起补 startup_backfill（启动快照回填发起入口，记录调用次数）。
    """

    def __init__(self, context=None, mysql_mgr=None, config_mgr=None, star=None):
        self.context = context
        self.mysql_mgr = mysql_mgr
        self.config_mgr = config_mgr
        self.star = star
        self.cooldown_ok = True
        self.build_result = None
        self.build_error = None
        self.render_result = "/tmp/stats_card.png"
        self.umo_calls: list = []
        self.cooldown_calls: list = []
        self.build_calls: list = []
        self.render_calls: list = []
        self.started = 0
        self.stopped = 0
        self.backfill_calls = 0

    async def start(self):
        self.started += 1

    async def stop(self):
        self.stopped += 1
        _STOP_ORDER.append("stats")

    async def startup_backfill(self):
        self.backfill_calls += 1

    def record_group_umo(self, group_id, umo):
        self.umo_calls.append((group_id, umo))

    async def check_cooldown(self, group_id):
        self.cooldown_calls.append(group_id)
        return self.cooldown_ok

    async def build_stats(self, query):
        self.build_calls.append(query)
        if self.build_error is not None:
            raise self.build_error
        return self.build_result

    async def render(self, data, title):
        self.render_calls.append((data, title))
        return self.render_result


class FakeSaver:
    """v0.6.0 core.saver.MessageSaver 契约替身：is_initialized/set_initialized 记状态。"""

    def __init__(self, mysql_mgr, config_mgr, stats_service=None):
        self.mysql_mgr = mysql_mgr
        self.config_mgr = config_mgr
        self.stats_service = stats_service
        self._initialized = False

    @property
    def is_initialized(self):
        return self._initialized

    def set_initialized(self):
        self._initialized = True

    def mark_gave_up(self):
        return 0

    async def flush_pending(self):
        pass

    async def handle_group_message(self, event):
        pass


class FakeBackfill:
    """v0.6.0 core.backfill.ReloadBackfill 契约替身：start/stop 记调用（stop 不入 LIFO 序）。

    v0.9.0 漂移同步：bootstrap 构造已注入 saver=/stats_service= 关键字参数
    （v0.6.1 起真实接线），替身吸收任意扩展 kwargs，并补 maybe_trigger
    （main.on_group_message 委托入口）。
    """

    def __init__(self, context=None, mysql_mgr=None, config_mgr=None, **kwargs):
        self.started = 0
        self.stopped = 0
        self.kwargs = kwargs
        self.maybe_trigger_calls: list = []

    async def start(self):
        self.started += 1

    async def stop(self):
        self.stopped += 1

    async def maybe_trigger(self, event):
        self.maybe_trigger_calls.append(event)


_inject_fake(f"{_PKG}.core.db_mysql", {"MySQLManager": FakeMySQLManager})
_inject_fake(f"{_PKG}.core.db_config", {"ConfigManager": FakeConfigManager})
_inject_fake(f"{_PKG}.core.cleaner", {"ImageCleaner": FakeImageCleaner})
_inject_fake(f"{_PKG}.core.summary", {"SummaryService": FakeSummaryService})
_inject_fake(f"{_PKG}.core.webapi", {"WebAPI": FakeWebAPI})
# profile.service 注入 fake；profile/__init__（惰性）与 profile.capture 保留真实
_inject_fake(f"{_PKG}.core.profile.service", {"ProfileService": FakeProfileService})
# stats.service 注入 fake；stats/__init__（惰性导出）/ models / parser 保留真实
_inject_fake(
    f"{_PKG}.core.stats.service",
    {"StatsService": FakeStatsService, "StatsBuildError": StatsBuildError},
)
# v0.6.0 saver / backfill 注入 fake（main.py 构造与生命周期接线）
# core.saver 使用真实 MessageSaver（轻量依赖，可在 stub 环境导入）：umo 登记 /
# 白名单检查 / 解析·缓冲·落库全链路走真实实现，on_group_message 委托测试可见
_inject_fake(f"{_PKG}.core.backfill", {"ReloadBackfill": FakeBackfill})


# ============================================================
# 三、导入被测代码与真实 parser / models
# ============================================================

import astrbot_plugin_group_history_save_mysql.main as MAIN  # noqa: E402
from astrbot_plugin_group_history_save_mysql.core.stats.models import StatsQuery  # noqa: E402
from astrbot_plugin_group_history_save_mysql.core.stats.parser import (  # noqa: E402
    USAGE_TEXT,
)


def _run(coro):
    return asyncio.run(coro)


def _drive_gen(gen):
    """驱动异步生成器 handler 至结束，收集全部 yield 结果。"""
    results = []

    async def _collect():
        async for item in gen:
            results.append(item)

    asyncio.run(_collect())
    return results


def _make_plugin(config=None):
    """构造完整 GroupHistoryPlugin（fake 依赖经 sys.modules 注入）。"""
    return MAIN.GroupHistoryPlugin(_StubContext(), config)


# ============================================================
# 四、事件替身
# ============================================================


class CmdEvent:
    """/群统计 指令事件替身：记录 plain/image 产出与 stop_event 调用。"""

    def __init__(self, group_id="123", self_id="999", messages=None):
        self._group_id = group_id
        self._self_id = self_id
        self._messages = messages if messages is not None else []
        self.stopped = False

    def get_group_id(self):
        return self._group_id

    def get_self_id(self):
        return self._self_id

    def get_messages(self):
        return self._messages

    def plain_result(self, text):
        return ("plain", text)

    def image_result(self, path):
        return ("image", path)

    def stop_event(self):
        self.stopped = True


class GroupEvent:
    """群消息事件替身（on_group_message umo 登记路径用）。"""

    def __init__(
        self,
        group_id="123",
        sender_id="111",
        sender_name="Alice",
        umo="umo-123",
        messages=None,
    ):
        self._group_id = group_id
        self._sender_id = sender_id
        self._sender_name = sender_name
        self.unified_msg_origin = umo
        self._messages = messages if messages is not None else []
        self.message_obj = types.SimpleNamespace(message_id="m1", raw_message=None)

    def get_group_id(self):
        return self._group_id

    def get_sender_id(self):
        return self._sender_id

    def get_sender_name(self):
        return self._sender_name

    def get_messages(self):
        return self._messages


def _rank(name, count, sender_id=""):
    return types.SimpleNamespace(
        sender_name=name, sender_id=sender_id or name, count=count
    )


def _make_data(total=100, active=5, ranking=None):
    """构造 StatsData 鸭子同构对象（降级文本读取的字段）。"""
    if ranking is None:
        ranking = [_rank("Alice", 50), _rank("Bob", 30), _rank("Carol", 10)]
    return types.SimpleNamespace(
        total_messages=total,
        active_senders=active,
        sender_ranking=ranking,
    )


# ============================================================
# 五、/群统计 指令 handler
# ============================================================


class TestGroupStatsCommand(unittest.TestCase):
    def _handle(self, plugin, event, arg=""):
        return _drive_gen(plugin.group_stats(event, arg))

    def test_private_chat_replies_usage_and_stops(self):
        plugin = _make_plugin()
        event = CmdEvent(group_id="")  # 私聊：群号为空
        results = self._handle(plugin, event)
        self.assertEqual(len(results), 1)
        kind, text = results[0]
        self.assertEqual(kind, "plain")
        self.assertIn("请在群内使用", text)
        self.assertIn("用法", text)  # 附带用法文案
        self.assertTrue(event.stopped)

    def test_cooldown_silent_no_reply_but_stops(self):
        plugin = _make_plugin()
        plugin.stats_service.cooldown_ok = False  # 冷却期内
        event = CmdEvent()
        results = self._handle(plugin, event, "7天")
        # 静默：无任何回复产出
        self.assertEqual(results, [])
        # 但 stop_event 仍被调用（防止指令文本流入 LLM）
        self.assertTrue(event.stopped)
        # 冷却拒绝后不应进入 build/render
        self.assertEqual(plugin.stats_service.build_calls, [])
        self.assertEqual(plugin.stats_service.render_calls, [])

    def test_parse_error_replies_usage(self):
        plugin = _make_plugin()
        event = CmdEvent()
        # 真实 parser 对无法识别 token 抛 StatsParseError（附带 usage）
        results = self._handle(plugin, event, "瞎写")
        self.assertEqual(len(results), 1)
        kind, text = results[0]
        self.assertEqual(kind, "plain")
        self.assertEqual(text, USAGE_TEXT)
        self.assertTrue(event.stopped)
        self.assertEqual(plugin.stats_service.build_calls, [])

    def test_normal_path_yields_image_result(self):
        plugin = _make_plugin()
        svc = plugin.stats_service
        svc.build_result = _make_data()
        svc.render_result = "/tmp/stats_card.png"
        event = CmdEvent()
        results = self._handle(plugin, event, "7天")
        self.assertEqual(len(results), 1)
        kind, path = results[0]
        self.assertEqual(kind, "image")
        self.assertEqual(path, "/tmp/stats_card.png")
        self.assertTrue(event.stopped)
        # build 收到 StatsQuery：group_id=当前群，top_n 读 stats_top_n 配置
        self.assertEqual(len(svc.build_calls), 1)
        query = svc.build_calls[0]
        self.assertIsInstance(query, StatsQuery)
        self.assertEqual(query.group_id, "123")
        self.assertIsNone(query.member_id)
        self.assertEqual(query.time_range.label, "近7天")
        self.assertEqual(query.top_n, 10)
        # render 收到 data 与「{群号} 群聊统计」标题
        self.assertEqual(len(svc.render_calls), 1)
        data, title = svc.render_calls[0]
        self.assertIs(data, svc.build_result)
        self.assertEqual(title, "123 群聊统计")

    def test_render_none_fallback_text_contains_top3(self):
        plugin = _make_plugin()
        svc = plugin.stats_service
        svc.build_result = _make_data()
        svc.render_result = None  # 渲染失败
        event = CmdEvent()
        results = self._handle(plugin, event, "7天")
        self.assertEqual(len(results), 1)
        kind, text = results[0]
        self.assertEqual(kind, "plain")
        # 降级文本：时间范围 label + 总数 + 活跃成员 + Top3 发言人
        self.assertIn("近7天", text)
        self.assertIn("总消息数：100", text)
        self.assertIn("活跃成员：5", text)
        self.assertIn("Top3 发言人", text)
        self.assertIn("Alice: 50条", text)
        self.assertIn("Bob: 30条", text)
        self.assertIn("Carol: 10条", text)
        self.assertTrue(event.stopped)

    def test_build_error_text_passthrough(self):
        plugin = _make_plugin()
        svc = plugin.stats_service
        svc.build_error = StatsBuildError("统计数据查询失败，请稍后重试")
        event = CmdEvent()
        results = self._handle(plugin, event, "7天")
        self.assertEqual(len(results), 1)
        kind, text = results[0]
        self.assertEqual(kind, "plain")
        self.assertEqual(text, "统计数据查询失败，请稍后重试")
        self.assertTrue(event.stopped)
        self.assertEqual(svc.render_calls, [])  # build 失败不进渲染

    def test_unknown_exception_fallback(self):
        plugin = _make_plugin()
        svc = plugin.stats_service

        async def _boom(query):
            raise RuntimeError("unexpected boom")

        svc.build_stats = _boom  # 非 StatsBuildError 的未知异常
        event = CmdEvent()
        results = self._handle(plugin, event, "7天")
        self.assertEqual(len(results), 1)
        kind, text = results[0]
        self.assertEqual(kind, "plain")
        self.assertEqual(text, "统计失败，请稍后重试")
        self.assertTrue(event.stopped)
        # 异常记日志（exc_info 不冒泡出 handler）
        self.assertTrue(any("群统计" in m for m in _STUB_LOGGER.records["error"]))

    def test_self_id_excluded_from_at_targets(self):
        plugin = _make_plugin()
        svc = plugin.stats_service
        svc.build_result = _make_data()
        # 消息链同时 @ bot 自身（999）与目标成员（555）：自身应被剔除
        event = CmdEvent(
            self_id="999",
            messages=[_StubAt(qq="999"), _StubAt(qq="555")],
        )
        results = self._handle(plugin, event, "7天")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][0], "image")
        # 个人维度目标为剔除 bot 自身后的第一个 @ 目标
        self.assertEqual(len(svc.build_calls), 1)
        self.assertEqual(svc.build_calls[0].member_id, "555")


# ============================================================
# 六、on_group_message umo 登记
# ============================================================


class TestUmoRecording(unittest.TestCase):
    def test_umo_recorded_on_group_message(self):
        plugin = _make_plugin()
        svc = plugin.stats_service
        event = GroupEvent(group_id="123", umo="umo-xyz")
        _run(plugin.on_group_message(event))
        # 白名单群消息经过即登记 群→umo 推送目标缓存
        self.assertIn(("123", "umo-xyz"), svc.umo_calls)

    def test_umo_skipped_when_service_none(self):
        plugin = _make_plugin()
        plugin.stats_service = None  # 服务未创建
        event = GroupEvent(group_id="123", umo="umo-xyz")
        # 不应因 service None 抛异常；白名单检查照常进行
        _run(plugin.on_group_message(event))
        self.assertEqual(plugin.config_mgr.group_enabled_calls, [123])


# ============================================================
# 七、生命周期（构造注入 / initialize / terminate LIFO）
# ============================================================


class TestLifecycle(unittest.TestCase):
    def test_register_metadata_v050(self):
        name, author, desc, version = MAIN.GroupHistoryPlugin._registered
        self.assertEqual(name, "astrbot_plugin_group_history_save_mysql")
        # v0.9.0 漂移同步：版本文本随 @register 演进（0.6.0 → 0.9.0）
        self.assertEqual(version, "0.9.0")
        self.assertIn("数据分析", desc)
        self.assertIn("分段快照统计", desc)  # v0.5.5 desc 文案

    def test_constructor_creates_stats_service_and_injects_webapi(self):
        plugin = _make_plugin()
        # 构造期创建 StatsService（context/mysql/config 透传）
        self.assertIsInstance(plugin.stats_service, FakeStatsService)
        self.assertIs(plugin.stats_service.mysql_mgr, plugin.mysql_mgr)
        self.assertIs(plugin.stats_service.config_mgr, plugin.config_mgr)
        # WebAPI stats_service= 注入（同一实例）
        self.assertIs(plugin.web_api.kwargs["stats_service"], plugin.stats_service)

    def test_initialize_starts_stats_service(self):
        plugin = _make_plugin()
        plugin.mysql_mgr.init_result = True

        async def _go():
            await plugin.initialize()
            # 与后台初始化任务同一事件循环内等待其完成
            if plugin._init_task is not None:
                await plugin._init_task

        _run(_go())
        self.assertTrue(plugin.saver.is_initialized)
        # MySQL 成功后依次 start：cleaner / summary / profile / stats
        self.assertEqual(plugin.stats_service.started, 1)
        self.assertEqual(plugin.summary_service.started, 1)
        self.assertEqual(plugin.profile_service.started, 1)
        self.assertEqual(plugin.cleaner.started, 1)
        # v0.5.5：stats 服务启动后发起一次快照启动回填
        self.assertEqual(plugin.stats_service.backfill_calls, 1)

    def test_initialize_backfill_failure_does_not_block(self):
        plugin = _make_plugin()
        plugin.mysql_mgr.init_result = True

        async def _backfill_boom():
            raise RuntimeError("backfill boom")

        # v0.5.5：startup_backfill 抛异常仅记日志，不阻断插件初始化
        plugin.stats_service.startup_backfill = _backfill_boom

        async def _go():
            await plugin.initialize()
            if plugin._init_task is not None:
                await plugin._init_task

        _run(_go())
        self.assertTrue(plugin.saver.is_initialized)
        self.assertTrue(any("回填" in m for m in _STUB_LOGGER.records["error"]))

    def test_initialize_stats_start_failure_does_not_block(self):
        plugin = _make_plugin()
        plugin.mysql_mgr.init_result = True

        async def _start_boom():
            raise RuntimeError("stats start boom")

        # stats.start 抛异常仅记日志，不阻断插件初始化
        plugin.stats_service.start = _start_boom

        async def _go():
            await plugin.initialize()
            if plugin._init_task is not None:
                await plugin._init_task

        _run(_go())
        self.assertTrue(plugin.saver.is_initialized)  # 其余服务照常完成

    def test_terminate_lifo_stop_order(self):
        _STOP_ORDER.clear()
        plugin = _make_plugin()
        _run(plugin.terminate())
        # LIFO：最后启动的 stats 最先停，随后 summary / profile / cleaner，
        # 最后关闭 mysql 与 config
        self.assertEqual(
            _STOP_ORDER, ["stats", "summary", "profile", "cleaner", "mysql", "config"]
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
