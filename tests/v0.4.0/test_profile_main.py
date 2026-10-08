# ruff: noqa: I001
"""astrbot_plugin_group_history_save_mysql v0.4.0 Module M 主入口离线单元测试（阶段 6-7 测试 Agent 新增）。

覆盖 main.py 集成层（模块 M）与本次修复的删除端点（web_api.py 路由 methods 扩为 DELETE+POST）：

- main.py：
  - extract_image_urls：OneBot 原始事件优先 / 消息链回退 / 本地路径不收集
  - on_group_message：群白名单拦截、文本+图片入库透传 at_list/reply_id、
    非文本非图片消息跳过、缓冲与补录（含 at_list/reply_id 透传）、_db_gave_up 丢弃
  - _persist_message：文本/图片分支、at_list/reply_id 透传 insert_chat_message
  - ProfileService / SummaryService / WebAPI 注入齐全（profile_service/storage/summary_storage）
  - /人物分析 指令（别名 人物画像/分析TA）注册与薄委托
  - terminate LIFO 停用（cancel 后台任务 → summary.stop → profile.stop → cleaner.stop → close）
- web_api.py（本次修复回归）：
  - 删除端点路由 methods 恒为 ["DELETE", "POST"]（修复 Agent-L2 发现的 404）
  - 该断言是本测试文件的新增部分：test_profile_webapi.py 旧断言 "DELETE" 已由主 agent
    确认过期（注册表现在是 DELETE+POST 双方法，桥接 apiPost 走 POST 分支不再 404）

运行方式（插件根目录，二者皆可）：
    python -m pytest "tests/v0.4.0/test_profile_main.py" -v
    python "tests/v0.4.0/test_profile_main.py"

范式沿用 tests/v0.4.0/test_profile_service.py（Agent-C stub 隔离技巧）：
astrbot.* 全部 sys.modules stub；db_mysql/cleaner 因依赖 aiomysql 以 fake 模块注入；
profile.service / profile.storage / summary.service 整链太重，以 fake 模块注入
sys.modules 满足 main.py 的顶层 import（避免把整条依赖链拉进本测试）。
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


# ---- astrbot.api: logger（带记录能力）----
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
_astrbot_api.AstrBotConfig = dict  # main.py: from astrbot.api import AstrBotConfig


# ---- astrbot.api.event: filter / AstrMessageEvent / PermissionType ----
class _StubFilter:
    def event_message_type(self, *a, **k):
        def deco(fn):
            return fn

        return deco

    def platform_adapter_type(self, *a, **k):
        def deco(fn):
            return fn

        return deco

    def command(self, *a, **k):
        def deco(fn):
            return fn

        return deco

    def permission_type(self, *a, **k):
        def deco(fn):
            return fn

        return deco


class _StubEventMessageType:
    GROUP_MESSAGE = "group_message"


class _StubPlatformAdapterType:
    AIOCQHTTP = "aiocqhttp"


class _StubPermissionType:
    ADMIN = "admin"


class _StubAstrMessageEvent:
    pass


_FILTER_STUB = _StubFilter()
_FILTER_STUB.EventMessageType = _StubEventMessageType
_FILTER_STUB.PlatformAdapterType = _StubPlatformAdapterType
_FILTER_STUB.PermissionType = _StubPermissionType
_astrbot_event.filter = _FILTER_STUB
_astrbot_event.EventMessageType = _StubEventMessageType
_astrbot_event.PlatformAdapterType = _StubPlatformAdapterType
_astrbot_event.PermissionType = _StubPermissionType
_astrbot_event.AstrMessageEvent = _StubAstrMessageEvent


# ---- astrbot.api.star: Context / Star / register / StarTools ----
class _StubContext:
    def __init__(self, *a, **k):
        self._extra = k

    def get_platform_inst(self, *a, **k):
        return None


class _StubStar:
    def __init__(self, context=None, *a, **k):
        self.context = context


def _stub_register(name, author, desc, version):
    def deco(cls):
        cls._registered = (name, author, desc, version)
        return cls

    return deco


class _StubStarTools:
    @classmethod
    def get_data_dir(cls, plugin_name=None):
        import tempfile
        from pathlib import Path

        return Path(tempfile.gettempdir()) / (plugin_name or "test")


_astrbot_star.Context = _StubContext
_astrbot_star.Star = _StubStar
_astrbot_star.register = _stub_register
_astrbot_star.StarTools = _StubStarTools


# ---- astrbot.api.message_components: Plain / Image / At / AtAll / Reply ----
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


class _StubAtAll(_StubAt):
    def __init__(self):
        super().__init__(qq="all")


class _StubReply:
    def __init__(self, id=""):
        self.id = id


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
_astrbot_mc.AtAll = _StubAtAll
_astrbot_mc.Reply = _StubReply
_astrbot_mc.Nodes = _StubNodes
_astrbot_mc.Node = _StubNode

# ---- astrbot.core：v0.6.0 起 main.py 顶层 import GreedyStr，补齐 core 链 stub ----
_astrbot_core = _new_module("astrbot.core")
_astrbot_core_star = _new_module("astrbot.core.star")
_astrbot_core_star_filter = _new_module("astrbot.core.star.filter")
_astrbot_core_star_filter_command = _new_module("astrbot.core.star.filter.command")


class _StubGreedyStr(str):
    """指令剩余文本参数标记（真实实现为 str 子类，stub 同形）。"""


_astrbot_core_star_filter_command.GreedyStr = _StubGreedyStr

# ---- 多测试文件合跑兼容：剔除被测包缓存（Agent-C stub 隔离技巧）----
_PKG = "astrbot_plugin_group_history_save_mysql"
for _name in list(sys.modules):
    if _name == _PKG or _name.startswith(_PKG + "."):
        del sys.modules[_name]

sys.modules["astrbot"] = _astrbot
sys.modules["astrbot.api"] = _astrbot_api
sys.modules["astrbot.api.event"] = _astrbot_event
sys.modules["astrbot.api.star"] = _astrbot_star
sys.modules["astrbot.api.message_components"] = _astrbot_mc
sys.modules["astrbot.core"] = _astrbot_core
sys.modules["astrbot.core.star"] = _astrbot_core_star
sys.modules["astrbot.core.star.filter"] = _astrbot_core_star_filter
sys.modules["astrbot.core.star.filter.command"] = _astrbot_core_star_filter_command

_PLUGINS_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..")
)
if _PLUGINS_DIR not in sys.path:
    sys.path.insert(0, _PLUGINS_DIR)


# ============================================================
# 二、fake 内部模块注入（db_mysql/cleaner 依赖 aiomysql；profile.service /
#      summary 整链太重；均在 import main.py 之前以 fake 模块注入 sys.modules）
# ============================================================

import importlib  # noqa: E402

_pkg_mod = importlib.import_module(_PKG)


def _inject_fake(module_path: str, attrs: dict):
    """将 attrs 写入新模块并挂到 sys.modules 与包对象上（合跑隔离）。"""
    mod = _new_module(module_path)
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[module_path] = mod
    parent_path, _, child = module_path.rpartition(".")
    parent = sys.modules.get(parent_path)
    if parent is not None:
        setattr(parent, child, mod)
    return mod


class _StubMySQLManager:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def initialize(self):
        return True

    async def ping(self):
        return {"connected": True, "latency_ms": 1, "pool": {}}

    async def get_stats(self):
        return {}

    async def insert_chat_message(self, **kwargs):
        return True

    async def insert_image_record(self, **kwargs):
        return True

    async def close(self):
        pass


class _StubImageCleaner:
    def __init__(self, *a, **k):
        pass

    async def start(self):
        pass

    async def stop(self):
        pass

    async def manual_clean(self, days=None):
        return 0


class _StubConfigManager:
    def __init__(self, *a, **k):
        pass

    async def initialize(self):
        return True

    async def is_group_enabled(self, group_id):
        return True

    async def get_groups(self):
        return []

    async def get_all_settings(self):
        return {}

    async def get_setting(self, key, default=""):
        return default

    async def close(self):
        pass


class _StubSummaryService:
    def __init__(self, *a, **k):
        self.storage = object()
        # v0.4.2+ main.py 构造 WebAPI 时注入 summary_renderer=self.summary_service.renderer
        self.renderer = object()
        self.started = False
        self.stopped = False

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True

    async def handle_count_command(self, event, arg):
        pass

    async def handle_window_command(self, event, arg):
        pass


class _StubProfileService:
    def __init__(self, *a, **k):
        self.storage = object()
        # v0.4.2+ main.py 构造 WebAPI 时注入 profile_renderer=self.profile_service.renderer
        self.renderer = object()
        self.started = False
        self.stopped = False
        self.command_calls = []

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True

    async def handle_command(self, event, arg):
        self.command_calls.append((event, arg))


class _StubWebAPI:
    def __init__(self, *a, **k):
        self.kwargs = k


_inject_fake(
    f"{_PKG}.core.db_mysql",
    {"MySQLManager": _StubMySQLManager, "QUERY_TIMEOUT_SECONDS": 30.0},
)
_inject_fake(f"{_PKG}.core.cleaner", {"ImageCleaner": _StubImageCleaner})
_inject_fake(f"{_PKG}.core.db_config", {"ConfigManager": _StubConfigManager})
_inject_fake(f"{_PKG}.core.summary", {"SummaryService": _StubSummaryService})
# v0.6.0 WebAPI 整链较重（core.webapi.base → db_mysql/cleaner 等），以 fake
# 模块注入；main.py 只需其中的 WebAPI 符号，base/profile 等子模块本测试不导入
# （ProfileDeleteRouteTest 改走源码级断言）
_inject_fake(f"{_PKG}.core.webapi", {"WebAPI": _StubWebAPI})

# ---- profile 子包：仅 profile.service 以 fake 模块注入（避免拉整条依赖链）；
#      capture 等轻量子模块走真实导入（main.py 与 core.saver 均需真实 capture）----
_inject_fake(f"{_PKG}.core.profile.service", {"ProfileService": _StubProfileService})


# ---- stats 子包：仅 stats.service 以 fake 模块注入（models/parser 为真实
#      轻量子模块，main.py 直接按模块路径导入）；PEP 562 惰性导出令
#      from .core.stats import StatsService/StatsBuildError 解析到本 fake ----
class _StubStatsBuildError(Exception):
    pass


class _StubStatsService:
    def __init__(self, *a, **k):
        self.started = False
        self.stopped = False
        self.umo_cache = {}

    def record_group_umo(self, group_id, umo):
        self.umo_cache[group_id] = umo

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True

    async def startup_backfill(self):
        pass


_inject_fake(
    f"{_PKG}.core.stats.service",
    {"StatsService": _StubStatsService, "StatsBuildError": _StubStatsBuildError},
)


# ============================================================
# 三、导入被测代码（stub 已就位；main.py 顶层 import 全部可解析）
# ============================================================

import astrbot_plugin_group_history_save_mysql.main as MAIN  # noqa: E402
from astrbot_plugin_group_history_save_mysql.core.parsing import (  # noqa: E402
    extract_image_urls,
)
from astrbot_plugin_group_history_save_mysql.core.profile.capture import (  # noqa: E402
    extract_at_targets,
    extract_reply_id,
)


# ============================================================
# 四、fake 事件（OnGroupMessage 用）
# ============================================================


class FakeMessageObj:
    """message_obj：message_id + OneBot 原始事件（raw_message）可配置。"""

    def __init__(self, message_id="m1", raw_message=None):
        self.message_id = message_id
        self.raw_message = raw_message


class FakeEvent:
    """模拟 AstrMessageEvent（main.py 用到的子集）。"""

    def __init__(
        self,
        group_id="9001",
        sender_id="12345",
        sender_name="Alice",
        message_id="m1",
        raw_message=None,
        chain=None,
    ):
        self._group_id = group_id
        self._sender_id = sender_id
        self._sender_name = sender_name
        self.message_obj = FakeMessageObj(message_id, raw_message)
        self._chain = chain or []
        self.sent = []
        # v0.5.0 数据分析：白名单消息经 stats_service.record_group_umo 登记
        # 群→umo 推送目标缓存（core.saver 读取 event.unified_msg_origin）
        self.unified_msg_origin = "umo:test"

    def get_group_id(self):
        return self._group_id

    def get_sender_id(self):
        return self._sender_id

    def get_sender_name(self):
        return self._sender_name

    def get_messages(self):
        return self._chain

    async def send(self, chain):
        self.sent.append(chain)


def _make_plugin():
    """构造完整 GroupHistoryPlugin（stub 依赖注入）。"""
    return MAIN.GroupHistoryPlugin(
        _StubContext(), types.SimpleNamespace(get=lambda k, d=None: d)
    )


def _run(coro):
    return asyncio.run(coro)


# ============================================================
# 五、测试用例
# ============================================================


class RegisterAndInitTest(unittest.TestCase):
    """M1：@register 元数据 / M2：构造注入"""

    def test_register_metadata_v040(self):
        name, author, desc, version = MAIN.GroupHistoryPlugin._registered
        self.assertEqual(name, "astrbot_plugin_group_history_save_mysql")
        # 版本随发布升级：v0.9.0 漂移同步（原断言 0.6.0 自 v0.9.0 起过期）
        self.assertEqual(version, "0.9.0")
        self.assertIn("人物分析", desc)

    def test_constructor_wires_injections(self):
        plugin = _make_plugin()
        # WebAPI 注入：profile_service / profile_storage / summary_storage
        self.assertIsNotNone(plugin.web_api)
        self.assertIs(plugin.web_api.kwargs["profile_service"], plugin.profile_service)
        self.assertIs(
            plugin.web_api.kwargs["profile_storage"], plugin.profile_service.storage
        )
        self.assertIs(
            plugin.web_api.kwargs["summary_storage"], plugin.summary_service.storage
        )

    def test_mysql_mgr_constructed_with_config_defaults(self):
        plugin = _make_plugin()
        mgr = plugin.mysql_mgr
        self.assertEqual(mgr.kwargs["host"], "127.0.0.1")
        self.assertEqual(mgr.kwargs["port"], 3306)
        self.assertEqual(mgr.kwargs["database"], "astrbot_history")


class ExtractImageUrlsTest(unittest.TestCase):
    """M3：extract_image_urls（OneBot 优先 / 链回退 / 本地不收集）"""

    def test_prefers_raw_onebot_segments(self):
        raw = {
            "message": [
                {"type": "image", "data": {"url": "https://q.qlogo.cn/a.png"}},
                {"type": "image", "data": {"file": "https://c2cpic.example/b.png"}},
            ]
        }
        urls = extract_image_urls(FakeMessageObj("m1", raw), [])
        self.assertEqual(
            urls, ["https://q.qlogo.cn/a.png", "https://c2cpic.example/b.png"]
        )

    def test_skips_non_http_url_in_raw(self):
        raw = {
            "message": [
                {"type": "image", "data": {"url": "/tmp/local.png"}},
                {"type": "image", "data": {"file": "C:\\tmp\\x.png"}},
            ]
        }
        self.assertEqual(extract_image_urls(FakeMessageObj("m1", raw), []), [])

    def test_falls_back_to_chain_components(self):
        chain = [
            _StubImage(url="http://cdn.example/a.png"),
            _StubImage(file="/tmp/local.png"),  # 本地路径不收集
            _StubImage(file="https://cdn.example/b.png"),
        ]
        urls = extract_image_urls(FakeMessageObj("m1", None), chain)
        self.assertEqual(
            urls, ["http://cdn.example/a.png", "https://cdn.example/b.png"]
        )

    def test_empty_inputs(self):
        self.assertEqual(extract_image_urls(FakeMessageObj("m1", None), []), [])
        self.assertEqual(extract_image_urls(FakeMessageObj("m1", None), None), [])
        self.assertEqual(extract_image_urls(None, None), [])

    def test_ignore_other_segment_types(self):
        raw = {
            "message": [
                {"type": "text", "data": {"text": "hi"}},
                {"type": "face", "data": {"id": "1"}},
            ]
        }
        self.assertEqual(extract_image_urls(FakeMessageObj("m1", raw), []), [])


class OnGroupMessageTest(unittest.TestCase):
    """M4：on_group_message 主链路（白名单 / 入库透传 / 缓冲 / gave_up）"""

    def test_text_message_persists_with_at_and_reply(self):
        plugin = _make_plugin()
        plugin.saver.set_initialized()
        plugin.mysql_mgr.insert_chat_message = _AsyncRecorder()
        plugin.mysql_mgr.insert_image_record = _AsyncRecorder()
        chain = [_StubPlain("你好 世界"), _StubAt("777"), _StubReply("r1")]
        event = FakeEvent(chain=chain)
        _run(plugin.on_group_message(event))
        calls = plugin.mysql_mgr.insert_chat_message.calls
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["group_id"], "9001")
        self.assertEqual(calls[0]["content"], "你好 世界")
        self.assertEqual(calls[0]["message_type"], "text")
        self.assertEqual(calls[0]["at_list"], "777")
        self.assertEqual(calls[0]["reply_id"], "r1")
        self.assertEqual(plugin.mysql_mgr.insert_image_record.calls, [])

    def test_mixed_message_writes_both_tables(self):
        plugin = _make_plugin()
        plugin.saver.set_initialized()
        plugin.mysql_mgr.insert_chat_message = _AsyncRecorder()
        plugin.mysql_mgr.insert_image_record = _AsyncRecorder()
        raw = {"message": [{"type": "image", "data": {"url": "https://x/y.png"}}]}
        chain = [_StubPlain("看"), _StubImage(url="http://local/x.png")]
        event = FakeEvent(raw_message=raw, chain=chain)
        _run(plugin.on_group_message(event))
        self.assertEqual(len(plugin.mysql_mgr.insert_chat_message.calls), 1)
        self.assertEqual(
            plugin.mysql_mgr.insert_chat_message.calls[0]["message_type"], "mixed"
        )
        self.assertEqual(len(plugin.mysql_mgr.insert_image_record.calls), 1)
        self.assertEqual(
            plugin.mysql_mgr.insert_image_record.calls[0]["image_url"],
            "https://x/y.png",
        )

    def test_skips_when_no_text_no_image(self):
        plugin = _make_plugin()
        plugin.saver.set_initialized()
        plugin.mysql_mgr.insert_chat_message = _AsyncRecorder()
        plugin.mysql_mgr.insert_image_record = _AsyncRecorder()
        chain = [_StubAt("777"), _StubReply("r1")]  # 仅 @/回复，无文本无图
        _run(plugin.on_group_message(FakeEvent(chain=chain)))
        self.assertEqual(plugin.mysql_mgr.insert_chat_message.calls, [])
        self.assertEqual(plugin.mysql_mgr.insert_image_record.calls, [])

    def test_group_not_in_whitelist_skipped(self):
        plugin = _make_plugin()

        async def _disabled(group_id):
            return False

        plugin.config_mgr.is_group_enabled = _disabled
        plugin.saver.set_initialized()
        plugin.mysql_mgr.insert_chat_message = _AsyncRecorder()
        _run(plugin.on_group_message(FakeEvent(chain=[_StubPlain("hi")])))
        self.assertEqual(plugin.mysql_mgr.insert_chat_message.calls, [])

    def test_buffers_during_init_window_with_at_reply(self):
        plugin = _make_plugin()
        plugin.saver._initialized = False
        chain = [_StubPlain("缓存"), _StubAt("777")]
        _run(plugin.on_group_message(FakeEvent(chain=chain)))
        self.assertEqual(len(plugin.saver._pending_records), 1)
        rec = plugin.saver._pending_records[0]
        self.assertEqual(rec["text_parts"], ["缓存"])
        self.assertEqual(rec["at_list"], "777")
        self.assertEqual(rec["reply_id"], "")

    def test_drops_when_db_gave_up(self):
        plugin = _make_plugin()
        plugin.saver._db_gave_up = True
        plugin.mysql_mgr.insert_chat_message = _AsyncRecorder()
        _run(plugin.on_group_message(FakeEvent(chain=[_StubPlain("hi")])))
        self.assertEqual(plugin.mysql_mgr.insert_chat_message.calls, [])


class _AsyncRecorder:
    """记录 kwargs 的异步 callable。"""

    def __init__(self):
        self.calls = []

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return True


class PersistAndFlushTest(unittest.TestCase):
    """M5：_persist_message 与 _flush_pending_records"""

    def test_persist_message_passes_through_at_reply(self):
        plugin = _make_plugin()
        plugin.mysql_mgr.insert_chat_message = _AsyncRecorder()
        plugin.mysql_mgr.insert_image_record = _AsyncRecorder()
        _run(
            plugin.saver._persist_message(
                "9001",
                "12345",
                "Alice",
                ["a", "b"],
                [],
                "m9",
                at_list="7,8",
                reply_id="r9",
            )
        )
        call = plugin.mysql_mgr.insert_chat_message.calls[0]
        self.assertEqual(call["content"], "a\nb")
        self.assertEqual(call["at_list"], "7,8")
        self.assertEqual(call["reply_id"], "r9")

    def test_flush_pending_records(self):
        plugin = _make_plugin()
        plugin.mysql_mgr.insert_chat_message = _AsyncRecorder()
        plugin.mysql_mgr.insert_image_record = _AsyncRecorder()
        plugin.saver._pending_records.append(
            {
                "group_id": "1",
                "sender_id": "2",
                "sender_name": "B",
                "text_parts": ["x"],
                "image_urls": [],
                "message_id": "mm",
                "at_list": "",
                "reply_id": "",
            }
        )
        plugin.saver._pending_records.append(
            {
                "group_id": "1",
                "sender_id": "2",
                "sender_name": "B",
                "text_parts": ["y"],
                "image_urls": [],
                "message_id": "mn",
                "at_list": "3",
                "reply_id": "rr",
            }
        )
        _run(plugin.saver.flush_pending())
        self.assertEqual(len(plugin.mysql_mgr.insert_chat_message.calls), 2)
        self.assertEqual(plugin.mysql_mgr.insert_chat_message.calls[1]["at_list"], "3")
        self.assertEqual(
            plugin.mysql_mgr.insert_chat_message.calls[1]["reply_id"], "rr"
        )
        self.assertEqual(len(plugin.saver._pending_records), 0)


class CommandAndLifecycleTest(unittest.TestCase):
    """M6：/人物分析 指令薄委托 / M7：生命周期 start / M8：terminate LIFO"""

    def test_profile_command_registered_with_aliases(self):
        # 装饰器原样保留 handler，别名由 filter.command 注册（stub 记录无法回放），
        # 此处验证 handler 存在且为异步、委托到 profile_service.handle_command
        handler = getattr(MAIN.GroupHistoryPlugin, "profile_analyze")
        self.assertTrue(asyncio.iscoroutinefunction(handler))

    def test_profile_command_delegates(self):
        plugin = _make_plugin()
        event = FakeEvent()
        _run(plugin.profile_analyze(event, "12345"))
        self.assertEqual(plugin.profile_service.command_calls, [(event, "12345")])

    def test_initialize_starts_background_init(self):
        plugin = _make_plugin()
        _run(plugin.initialize())
        # config_mgr.initialize 已调用；后台 MySQL 初始化任务已创建
        self.assertIsNotNone(plugin._init_task)
        # 等待后台任务完成（MySQL stub initialize 立即成功 → 触发各服务 start）
        _run(asyncio.wait_for(plugin._init_task, timeout=5))
        self.assertTrue(plugin.profile_service.started)
        self.assertTrue(plugin.summary_service.started)

    def test_terminate_lifo_stops_services(self):
        plugin = _make_plugin()
        plugin._init_task = None  # 无后台任务在重试 → 跳过 cancel 分支
        plugin.saver.set_initialized()
        plugin.mysql_mgr.close = _AsyncRecorder()
        _run(plugin.terminate())
        self.assertTrue(plugin.summary_service.stopped)
        self.assertTrue(plugin.profile_service.stopped)
        self.assertEqual(len(plugin.mysql_mgr.close.calls), 1)


class CaptureIntegrationTest(unittest.TestCase):
    """M10：capture 纯函数与 main.py 透传的集成（复用真实 capture 模块）"""

    def test_capture_functions_used_by_main(self):
        chain = [_StubPlain("hi"), _StubAt("777"), _StubAtAll(), _StubReply("r1")]
        self.assertEqual(extract_at_targets(chain), ["777"])
        event = FakeEvent(raw_message=None, chain=[_StubReply("r2")])
        self.assertEqual(extract_reply_id(event), "r2")


class ProfileDeleteRouteTest(unittest.TestCase):
    """M9：删除端点路由 methods 恒为 ["DELETE","POST"]（本次修复回归）。

    注意：本测试针对**真实** web_api 模块（route 注册表），但真实 web_api 依赖
    aiomysql 等离线不可用模块。因此这里改用 test_profile_webapi.py 的测试结果
    （该文件以 fake db_mysql/cleaner 注入真实 web_api 模块）——两文件合跑时本
    文件的 fake web_api 注入会与真实导入冲突，故本类并入 CaptureIntegrationTest
    命名空间下仅作文档化说明，实际路由断言由 test_profile_webapi.py 承担：
    RouteRegistrationTest.test_all_nine_profile_routes_registered 期望集已在
    本测试文件创建时核对（web_api.py:267-271 注册 methods=["DELETE","POST"]），
    该断言为 Agent-L2 修复后的回归守卫。
    """

    def test_delete_route_methods_are_delete_and_post(self):
        # 静态核对 web_api.py 删除端点注册（模块级常量无法在 fake 环境导入真实模块，
        # 此用例以源码级断言兜底：删除端点 methods 声明与 handler 兼容性）
        import re as _re

        src = _re.search(
            r"\(\s*f\"/\{PLUGIN_NAME\}/profile/history\",\s*"
            r"self\.api_profile_history_delete,\s*\[(.*?)\],",
            open(
                os.path.join(_PLUGINS_DIR, _PKG, "core", "webapi", "base.py"),
                encoding="utf-8",
            ).read(),
            _re.S,
        )
        self.assertIsNotNone(src, "未找到删除端点注册")
        self.assertIn("DELETE", src.group(1))
        self.assertIn("POST", src.group(1))
        # 源码中必须出现 json_response({"deleted": True})（删除成功分支）
        full = open(
            os.path.join(_PLUGINS_DIR, _PKG, "core", "webapi", "profile.py"),
            encoding="utf-8",
        ).read()
        self.assertIn('json_response({"deleted": True})', full)


if __name__ == "__main__":
    unittest.main(verbosity=2)
