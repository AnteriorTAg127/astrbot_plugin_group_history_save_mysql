"""astrbot_plugin_group_history_save_mysql v0.3 离线集成测试。

覆盖 v0.3「群聊历史自动总结」可离线验证的全部模块，重点是**跨模块集成**：
用真实上游模块（fetcher/summarizer/formatter/storage/scheduler/service）+
假基础设施（mysql_mgr / onebot 协议端 / context.llm_generate / star 渲染）
组装全链路。

模块覆盖：
- summary/models.py        归一化纯函数（时间戳三级回退 / OneBot 段解析）
- summary/formatter.py     strip_markdown 纯函数 + forward/image 渲染降级
- summary/fetcher.py       MySQL 优先 + OneBot 补齐 + 去重过滤合并
- summary/summarizer.py    统计 / 占位符渲染 / 截断 / provider 回退 / 板块解析
- summary/storage.py       save/read/list/cleanup + 路径穿越防护
- summary/scheduler.py     定时清理 start/stop 生命周期
- summary/service.py       端到端编排（校验链 + 错误兜底 + 落盘 + 发送）
- web_api.py               10 个 summary 端点
- db_config.py             summary 扩展方法（真 aiosqlite 临时文件）

运行方式（插件根目录）：
    python -m pytest "tests/v0.3/test_v03.py" -v

范式沿用 tests/v0.2/test_v02.py：异步用例用内置 async_test 装饰器经
asyncio.run 驱动（不依赖 pytest-asyncio / conftest.py）；astrbot.* 与
aiomysql 全部 sys.modules stub，且 stub 必须在 import 被测包之前完成。
"""

import asyncio
import functools
import json
import os
import re
import sys
import tempfile
import time
import types
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest


def async_test(fn):
    """装饰器:用 asyncio.run 运行异步测试函数(环境无 pytest-asyncio 依赖)。"""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        asyncio.run(fn(*args, **kwargs))

    return wrapper


# ============================================================
# 一、stub 注入（必须在任何 from astrbot_plugin_group_history_save_mysql... import 之前）
# ============================================================


def _new_module(name: str) -> types.ModuleType:
    """创建一个空模块对象。"""
    return types.ModuleType(name)


# ---- astrbot 主包及其子包 ----
_astrbot = _new_module("astrbot")
_astrbot_api = _new_module("astrbot.api")
_astrbot_mc = _new_module("astrbot.api.message_components")
_astrbot_event = _new_module("astrbot.api.event")
_astrbot_star = _new_module("astrbot.api.star")
_astrbot_web = _new_module("astrbot.api.web")
_astrbot_core = _new_module("astrbot.core")
_astrbot_core_utils = _new_module("astrbot.core.utils")
_astrbot_core_utils_path = _new_module("astrbot.core.utils.astrbot_path")


# ---- astrbot.api: logger（带记录能力，供 warning 断言）+ AstrBotConfig ----
class _StubLogger:
    """记录各级日志内容，便于断言（如脏时间戳 warning）。"""

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


class _StubAstrBotConfig:
    pass


_astrbot_api.logger = _STUB_LOGGER
_astrbot_api.AstrBotConfig = _StubAstrBotConfig


# ---- astrbot.api.message_components: Plain / Image / Node / Nodes ----
class _StubPlain:
    def __init__(self, text=""):
        self.text = text


class _StubImage:
    def __init__(self, url=None, file=None):
        self.url = url
        self.file = file

    @classmethod
    def fromURL(cls, url):
        return cls(url=url)

    @classmethod
    def fromFileSystem(cls, path):
        return cls(file=path)


class _StubNode:
    def __init__(self, uin="", name="", content=None):
        self.uin = uin
        self.name = name
        self.content = content or []


class _StubNodes:
    def __init__(self, nodes=None):
        self.nodes = nodes or []


_astrbot_mc.Plain = _StubPlain
_astrbot_mc.Image = _StubImage
_astrbot_mc.Node = _StubNode
_astrbot_mc.Nodes = _StubNodes


# ---- astrbot.api.event: AstrMessageEvent / MessageChain / filter ----
class _StubAstrMessageEvent:
    pass


class _StubMessageChain:
    """MessageChain 替身：仅承载 chain 列表（MessageEventResult 亦继承它）。"""

    def __init__(self, chain=None):
        self.chain = chain or []


def _identity_decorator_factory(*args, **kwargs):
    def _decorator(func):
        return func

    return _decorator


class _StubEventType:
    GROUP_MESSAGE = "GROUP_MESSAGE"


class _StubPermissionType:
    ADMIN = "ADMIN"


class _StubPlatformAdapterType:
    AIOCQHTTP = "AIOCQHTTP"


class _StubFilter:
    EventMessageType = _StubEventType
    PermissionType = _StubPermissionType
    PlatformAdapterType = _StubPlatformAdapterType

    event_message_type = staticmethod(_identity_decorator_factory)
    platform_adapter_type = staticmethod(_identity_decorator_factory)
    permission_type = staticmethod(_identity_decorator_factory)
    command = staticmethod(_identity_decorator_factory)


_astrbot_event.AstrMessageEvent = _StubAstrMessageEvent
_astrbot_event.MessageChain = _StubMessageChain
_astrbot_event.filter = _StubFilter()


# ---- astrbot.api.star: Context / Star / register / StarTools ----
class _StubContext:
    pass


class _StubStar:
    def __init__(self, context):
        pass


def _stub_register(*args, **kwargs):
    def _decorator(cls):
        return cls

    return _decorator


_TEMP_DATA_DIR = tempfile.mkdtemp(prefix="astrbot_hist_test_v03_")


class _StubStarTools:
    @classmethod
    def get_data_dir(cls, plugin_name=None):
        return Path(_TEMP_DATA_DIR) / (plugin_name or "test")


_astrbot_star.Context = _StubContext
_astrbot_star.Star = _StubStar
_astrbot_star.register = _stub_register
_astrbot_star.StarTools = _StubStarTools


# ---- astrbot.api.web: request / json_response / error_response ----
class _StubRequest:
    async def json(self, default=None):
        return default

    query = None


def _stub_json_response(data, status_code: int = 200):
    return {"__json__": True, "data": data, "status": status_code}


def _stub_error_response(message, status_code: int = 400):
    return {"__error__": True, "message": message, "status": status_code}


def _stub_file_response(path, filename=None):
    return {"__file__": True, "path": path, "filename": filename}


_astrbot_web.request = _StubRequest()
_astrbot_web.json_response = _stub_json_response
_astrbot_web.error_response = _stub_error_response
_astrbot_web.file_response = _stub_file_response

# ---- astrbot.core.utils.astrbot_path ----
_astrbot_core_utils_path.get_astrbot_plugin_data_path = lambda: _TEMP_DATA_DIR

# ---- astrbot.core.utils.io: save_temp_img（v0.4.2 起 web_api.py 顶层导入）----
_astrbot_core_utils_io = _new_module("astrbot.core.utils.io")
_astrbot_core_utils_io.save_temp_img = lambda data: ""

# ---- aiomysql（db_mysql.py 顶层导入；本套件不触达真实连接池）----
_aiomysql = _new_module("aiomysql")
_aiomysql.Connection = object
_aiomysql.DictCursor = object

# ---- 多测试文件合跑兼容：剔除可能被其他测试文件先行导入的插件包模块，
#      使本文件的 import 重新执行并绑定到本文件的 stub（各版本测试文件 fake 互异） ----
for _name in list(sys.modules):
    if _name == "astrbot_plugin_group_history_save_mysql" or _name.startswith(
        "astrbot_plugin_group_history_save_mysql."
    ):
        del sys.modules[_name]

# ---- 注入 sys.modules ----
sys.modules["astrbot"] = _astrbot
sys.modules["astrbot.api"] = _astrbot_api
sys.modules["astrbot.api.message_components"] = _astrbot_mc
sys.modules["astrbot.api.event"] = _astrbot_event
sys.modules["astrbot.api.star"] = _astrbot_star
sys.modules["astrbot.api.web"] = _astrbot_web
sys.modules["astrbot.core"] = _astrbot_core
sys.modules["astrbot.core.utils"] = _astrbot_core_utils
sys.modules["astrbot.core.utils.astrbot_path"] = _astrbot_core_utils_path
sys.modules["astrbot.core.utils.io"] = _astrbot_core_utils_io
sys.modules["aiomysql"] = _aiomysql

# 让被测包可被导入：<plugins 目录> 加入 sys.path
_PLUGINS_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..")
)
if _PLUGINS_DIR not in sys.path:
    sys.path.insert(0, _PLUGINS_DIR)

# aiosqlite 可用性探测（db_config 真实 SQLite 测试的跳过开关）
try:
    import aiosqlite  # noqa: F401

    HAS_AIOSQLITE = True
except ImportError:
    HAS_AIOSQLITE = False

needs_aiosqlite = pytest.mark.skipif(
    not HAS_AIOSQLITE,
    reason="aiosqlite 未安装，跳过真实 SQLite 配置库测试",
)

# ============================================================
# 二、导入被测代码（stub 已就位）
# ============================================================

from astrbot_plugin_group_history_save_mysql.core.db_config import (  # noqa: E402
    ConfigManager,
)
from astrbot_plugin_group_history_save_mysql.core.summary import (  # noqa: E402
    fetcher as fetcher_mod,
)
from astrbot_plugin_group_history_save_mysql.core.summary import (  # noqa: E402
    onebot as onebot_mod,
)
from astrbot_plugin_group_history_save_mysql.core.summary import (  # noqa: E402
    scheduler as scheduler_mod,
)
from astrbot_plugin_group_history_save_mysql.core.summary import (  # noqa: E402
    service as service_mod,
)
from astrbot_plugin_group_history_save_mysql.core.summary.fetcher import (  # noqa: E402
    HistoryFetcher,
)
from astrbot_plugin_group_history_save_mysql.core.summary.formatter import (  # noqa: E402
    SummaryFormatter,
    strip_markdown,
)
from astrbot_plugin_group_history_save_mysql.core.summary.models import (  # noqa: E402
    ChatMessage,
    StatsResult,
    SummaryResult,
    mysql_row_to_message,
    parse_onebot_message,
)
from astrbot_plugin_group_history_save_mysql.core.summary.onebot import (  # noqa: E402
    OneBotHistoryError,
)
from astrbot_plugin_group_history_save_mysql.core.summary.scheduler import (  # noqa: E402
    CleanupScheduler,
)
from astrbot_plugin_group_history_save_mysql.core.summary.service import (  # noqa: E402
    SummaryService,
)
from astrbot_plugin_group_history_save_mysql.core.summary.storage import (  # noqa: E402
    SummaryStorage,
)
from astrbot_plugin_group_history_save_mysql.core.summary.summarizer import (  # noqa: E402
    Summarizer,
    SummaryProviderError,
)
from astrbot_plugin_group_history_save_mysql.core.webapi import WebAPI  # noqa: E402
from astrbot_plugin_group_history_save_mysql.core.webapi.base import (  # noqa: E402
    PLUGIN_NAME,
    SummaryFacade,
)

Image = _StubImage
Plain = _StubPlain
Node = _StubNode
Nodes = _StubNodes
MessageChain = _StubMessageChain

# 默认 4 板块 LLM 输出文本（预制，供 summarizer/service 测试复用）
FOUR_SECTION_LLM_TEXT = """📢 重要通知与结论
- 通知 A：周五发布

💬 讨论要点 / 争议
- 讨论 B：方案选型

🎉 有趣片段
- 片段 C：表情包大战

✅ TODO / 待跟进
- 任务 D：补充文档"""


# ============================================================
# 三、测试辅助（假基础设施）
# ============================================================


class MockRequest:
    """模拟 astrbot 的 request 对象（payload + query 参数）。"""

    def __init__(self, payload=None, query_params=None):
        self._payload = payload if payload is not None else {}
        self._query_params = query_params or {}

    async def json(self, default=None):
        return self._payload

    @property
    def query(self):
        params = self._query_params

        class _Query:
            def get(self, key, default=None, type=None):
                value = params.get(key, default)
                if type is not None and value is not None:
                    try:
                        return type(value)
                    except (ValueError, TypeError):
                        return default
                return value

        return _Query()


def _patch_summary_request(monkeypatch, stub):
    """v0.6.0 拆分后 request 按 webapi mixin 模块绑定：patch core.webapi.summary。

    旧版 web_api.py 单文件内 ``request`` 为模块级全局，patch 包属性即生效；
    拆分后 summary 模块以 ``from astrbot.api.web import request`` 在 import 时
    绑定同名全局，必须 patch 该 mixin 模块的模块级 ``request`` 才能命中运行路径。
    """
    import astrbot_plugin_group_history_save_mysql.core.webapi.summary as _summary_mod

    monkeypatch.setattr(_summary_mod, "request", stub)


class FakeConfigMgr:
    """ConfigManager 内存替身。

    以原始字符串 dict 为底（默认取真实 SUMMARY_DEFAULTS），typed 读取复用
    生产侧 ConfigManager._convert_summary_value 转换逻辑，保证假件行为与
    真实配置层一致。
    """

    def __init__(self, overrides=None, ignore=None):
        self.store = dict(ConfigManager.SUMMARY_DEFAULTS)
        if overrides:
            self.store.update(overrides)
        self.ignore = {str(k): list(v) for k, v in (ignore or {}).items()}
        self.set_calls = []
        self.reset_calls = []

    async def get_summary_setting(self, key, default=None):
        fallback = ConfigManager.SUMMARY_DEFAULTS.get(key, "")
        if key in self.store:
            return self.store[key]
        return default if default is not None else fallback

    async def set_summary_setting(self, key, value):
        self.set_calls.append((key, value))
        self.store[key] = value
        return True

    async def get_all_summary_settings(self):
        return {
            key: self.store.get(key, value)
            for key, value in ConfigManager.SUMMARY_DEFAULTS.items()
        }

    async def reset_summary_settings(self, keys=None):
        self.reset_calls.append(keys)
        targets = list(ConfigManager.SUMMARY_DEFAULTS) if keys is None else keys
        for key in targets:
            if key in ConfigManager.SUMMARY_DEFAULTS:
                self.store[key] = ConfigManager.SUMMARY_DEFAULTS[key]
        return await self.get_all_summary_settings()

    async def get_summary_setting_typed(self, key):
        raw = await self.get_summary_setting(key)
        target = ConfigManager.SUMMARY_TYPES.get(key, str)
        try:
            return ConfigManager._convert_summary_value(raw, target)
        except Exception:
            default_raw = ConfigManager.SUMMARY_DEFAULTS.get(key, "")
            return ConfigManager._convert_summary_value(default_raw, target)

    # ---- 忽略名单 ----
    async def get_ignore_senders(self, group_id):
        return [
            {"sender_id": sid, "created_at": "2026-01-01T00:00:00Z"}
            for sid in self.ignore.get(str(group_id), [])
        ]

    async def add_ignore_sender(self, group_id, sender_id):
        bucket = self.ignore.setdefault(str(group_id), [])
        if str(sender_id) in bucket:
            return False
        bucket.append(str(sender_id))
        return True

    async def remove_ignore_sender(self, group_id, sender_id):
        bucket = self.ignore.get(str(group_id), [])
        if str(sender_id) not in bucket:
            return False
        bucket.remove(str(sender_id))
        return True

    async def list_ignore_groups(self):
        return sorted(g for g, v in self.ignore.items() if v)


class FakeMySQLMgr:
    """MySQLManager 替身：query_messages 返回预制行或抛异常。"""

    def __init__(self, rows=None, total=None, exc=None):
        self.rows = rows or []
        self.total = len(self.rows) if total is None else total
        self.exc = exc
        self.calls = []

    async def query_messages(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc is not None:
            raise self.exc
        return {"records": self.rows, "total": self.total}


class FakeOneBotFetch:
    """summary.onebot.fetch_group_history 替身（记录调用、返回预制消息）。"""

    def __init__(self, msgs=None, exc=None):
        self.msgs = msgs or []
        self.exc = exc
        self.calls = []

    async def __call__(self, event, group_id, count):
        self.calls.append({"event": event, "group_id": group_id, "count": count})
        if self.exc is not None:
            raise self.exc
        return list(self.msgs)


class FakeEvent:
    """AstrMessageEvent 替身：群/用户/bot 身份 + send 记录。"""

    def __init__(
        self,
        group_id="12345",
        sender_id="111",
        self_id="999",
        session_id="sess-1",
        umo="umo-1",
    ):
        self._group_id = group_id
        self._sender_id = sender_id
        self._self_id = self_id
        self._session_id = session_id
        self.unified_msg_origin = umo
        self.sent = []

    def get_group_id(self):
        return self._group_id

    def get_sender_id(self):
        return self._sender_id

    def get_self_id(self):
        return self._self_id

    def get_session_id(self):
        return self._session_id

    def plain_result(self, text):
        # 真实框架返回 MessageEventResult（MessageChain 子类）；此处用可辨识元组
        return ("plain", text)

    async def send(self, chain):
        self.sent.append(chain)

    # ---- 断言辅助 ----
    def plain_texts(self):
        return [c[1] for c in self.sent if isinstance(c, tuple) and c[0] == "plain"]

    def chains(self):
        return [c for c in self.sent if isinstance(c, MessageChain)]


class FakeLLMContext:
    """Context 替身：llm_generate 返回预制文本 / 抛异常；会话 provider 可配。"""

    def __init__(
        self,
        llm_text=FOUR_SECTION_LLM_TEXT,
        chat_provider="chat-prov",
        chat_provider_exc=None,
        llm_exc=None,
    ):
        self._llm_text = llm_text
        self._chat_provider = chat_provider
        self._chat_provider_exc = chat_provider_exc
        self._llm_exc = llm_exc
        self.llm_calls = []
        self.provider_queries = []

    async def get_current_chat_provider_id(self, umo):
        self.provider_queries.append(umo)
        if self._chat_provider_exc is not None:
            raise self._chat_provider_exc
        return self._chat_provider

    async def llm_generate(self, chat_provider_id=None, prompt=None):
        self.llm_calls.append({"provider": chat_provider_id, "prompt": prompt})
        if self._llm_exc is not None:
            raise self._llm_exc
        return SimpleNamespace(completion_text=self._llm_text)


class FakeStar:
    """Star 替身：text_to_image / html_render 可配成功 URL 或异常。"""

    def __init__(
        self,
        t2i_url="https://render.example/sum.png",
        t2i_exc=None,
        html_url=None,
        html_exc=None,
        context=None,
    ):
        self._t2i_url = t2i_url
        self._t2i_exc = t2i_exc
        self._html_url = html_url
        self._html_exc = html_exc
        self.context = context
        self.t2i_calls = []
        self.html_calls = []

    async def text_to_image(self, text, return_url=True):
        self.t2i_calls.append(text)
        if self._t2i_exc is not None:
            raise self._t2i_exc
        return self._t2i_url

    async def html_render(self, tmpl, data, return_url=True, options=None):
        self.html_calls.append((tmpl, data))
        if self._html_exc is not None:
            raise self._html_exc
        return self._html_url


def make_row(
    ts, sender="111", name="Alice", content="hello", gid="12345", mid="m1", mtype="text"
):
    """构造 MySQL query_messages 风格的单行记录（timestamp 为字符串）。"""
    return {
        "id": 1,
        "timestamp": ts.strftime("%Y-%m-%d %H:%M:%S"),
        "group_id": gid,
        "sender_id": sender,
        "sender_name": name,
        "message_type": mtype,
        "content": content,
        "message_id": mid,
    }


def make_rows_desc(n, base=None, sender="111", name="Alice", prefix="msg"):
    """构造 n 行按 timestamp DESC（新→旧）排列的记录，模拟真实查询排序。"""
    base = base or datetime(2026, 7, 30, 12, 0, 0)
    return [
        make_row(
            base - timedelta(minutes=i),
            sender=sender,
            name=name,
            content=f"{prefix}{i}",
            mid=f"m{i}",
        )
        for i in range(n)
    ]


def cm(
    ts,
    sender="333",
    name="Carol",
    content="onebot msg",
    gid="12345",
    mid="",
    source="onebot",
):
    """构造 ChatMessage（默认 onebot 源）。"""
    return ChatMessage(
        timestamp=ts,
        group_id=gid,
        sender_id=sender,
        sender_name=name,
        content=content,
        message_id=mid,
        source=source,
    )


def make_summary_result(**overrides):
    """构造一个完整的 SummaryResult（storage/formatter 测试复用）。"""
    stats = StatsResult(
        total=2,
        participant_count=2,
        time_start=datetime(2026, 7, 1, 10, 0, 0),
        time_end=datetime(2026, 7, 1, 11, 0, 0),
        top_senders=[("111", "Alice", 1), ("222", "Bob", 1)],
    )
    kwargs = {
        "stats": stats,
        "sections": [
            ("📢 重要通知与结论", "通知内容"),
            ("✅ TODO / 待跟进", "任务内容"),
        ],
        "raw_llm_text": "raw llm text",
        "provider_id": "prov-x",
        "messages_used": 2,
        "sources": {"mysql": 2, "onebot": 0},
        "scope_desc": "最近 2 条消息",
    }
    kwargs.update(overrides)
    return SummaryResult(**kwargs)


# ============================================================
# 四、models：归一化纯函数
# ============================================================


class TestModelsMysqlRow:
    def test_standard_timestamp(self):
        """标准 'YYYY-MM-DD HH:MM:SS' 格式解析 + 字段映射 + source=mysql。"""
        row = make_row(
            datetime(2026, 7, 1, 12, 30, 45),
            sender="123",
            name="张三",
            content="你好",
            gid="9876",
            mid="mid-1",
        )
        msg = mysql_row_to_message(row)
        assert msg.timestamp == datetime(2026, 7, 1, 12, 30, 45)
        assert msg.group_id == "9876"
        assert msg.sender_id == "123"
        assert msg.sender_name == "张三"
        assert msg.content == "你好"
        assert msg.message_id == "mid-1"
        assert msg.source == "mysql"

    def test_iso_timestamp_fallback(self):
        """标准格式失败 → fromisoformat 二级回退成功。"""
        row = make_row(datetime(2026, 7, 1, 12, 30, 45))
        row["timestamp"] = "2026-07-01T12:30:45"
        msg = mysql_row_to_message(row)
        assert msg.timestamp == datetime(2026, 7, 1, 12, 30, 45)

    def test_bad_timestamp_epoch_with_warning(self):
        """坏时间戳 → epoch(1970) 兜底且记 warning，不抛异常。"""
        before = len(_STUB_LOGGER.records["warning"])
        row = make_row(datetime(2026, 7, 1))
        row["timestamp"] = "not-a-time"
        msg = mysql_row_to_message(row)
        assert msg.timestamp.year == 1970
        new_warnings = _STUB_LOGGER.records["warning"][before:]
        assert any("无法解析 MySQL 消息时间戳" in w for w in new_warnings)

    def test_none_fields_to_empty_string(self):
        """所有可空字段为 None → 归一化为空串。"""
        row = {
            "timestamp": "2026-07-01 12:00:00",
            "group_id": None,
            "sender_id": None,
            "sender_name": None,
            "content": None,
            "message_id": None,
        }
        msg = mysql_row_to_message(row)
        assert msg.group_id == ""
        assert msg.sender_id == ""
        assert msg.sender_name == ""
        assert msg.content == ""
        assert msg.message_id == ""

    def test_int_ids_stringified(self):
        """数字型 ID（非字符串）→ 强制字符串化。"""
        row = make_row(datetime(2026, 7, 1))
        row["group_id"] = 12345
        row["sender_id"] = 678
        msg = mysql_row_to_message(row)
        assert msg.group_id == "12345"
        assert msg.sender_id == "678"


class TestModelsParseOneBot:
    def _raw(self, segments, ts=1751342400, mid=777, user_id=123, nickname="nn"):
        return {
            "time": ts,
            "message_id": mid,
            "sender": {"user_id": user_id, "nickname": nickname},
            "message": segments,
        }

    def test_text_segments_joined_non_text_ignored(self):
        """仅拼接 text 段，图片段忽略；字段映射正确。"""
        raw = self._raw(
            [
                {"type": "text", "data": {"text": "a"}},
                {"type": "image", "data": {"file": "x.image", "url": "https://x"}},
                {"type": "text", "data": {"text": "b"}},
            ]
        )
        msg = parse_onebot_message(raw, "12345")
        assert msg is not None
        assert msg.content == "ab"
        assert msg.sender_id == "123"
        assert msg.sender_name == "nn"
        assert msg.message_id == "777"
        assert msg.group_id == "12345"
        assert msg.source == "onebot"
        assert msg.timestamp == datetime.fromtimestamp(1751342400)

    def test_image_only_returns_none(self):
        """纯图片消息（无文本段）→ None。"""
        raw = self._raw([{"type": "image", "data": {"file": "x"}}])
        assert parse_onebot_message(raw, "12345") is None

    def test_blank_text_returns_none(self):
        """文本段去空白后为空 → None。"""
        raw = self._raw([{"type": "text", "data": {"text": "   "}}])
        assert parse_onebot_message(raw, "12345") is None

    def test_missing_fields_return_none(self):
        """缺 message / sender / time 等关键字段 → None（不抛异常）。"""
        assert parse_onebot_message({}, "12345") is None
        assert parse_onebot_message({"message": []}, "12345") is None
        no_sender = self._raw([{"type": "text", "data": {"text": "x"}}])
        del no_sender["sender"]
        assert parse_onebot_message(no_sender, "12345") is None
        no_time = self._raw([{"type": "text", "data": {"text": "x"}}])
        del no_time["time"]
        assert parse_onebot_message(no_time, "12345") is None

    def test_non_dict_raw_returns_none(self):
        """raw 非 dict（如 None / list）→ None。"""
        assert parse_onebot_message(None, "12345") is None
        assert parse_onebot_message(["not", "dict"], "12345") is None

    def test_nickname_none_to_empty(self):
        """nickname 缺失 → 空串。"""
        raw = self._raw([{"type": "text", "data": {"text": "x"}}], nickname=None)
        msg = parse_onebot_message(raw, "12345")
        assert msg is not None
        assert msg.sender_name == ""


# ============================================================
# 四-bis、onebot.fetch_group_history 多轮翻页
# ============================================================


class _FakeOneBotAPI:
    """client.api 替身：按 message_seq 分桶返回预制页，记录每轮请求参数。"""

    def __init__(self, pages, exc_on_round=None):
        # pages: {message_seq: [raw_msg, ...]}；未登记的 seq 返回空页
        self.pages = pages
        self.exc_on_round = exc_on_round  # 第 N 轮（1 起）抛 RuntimeError
        self.calls = []

    async def call_action(self, action, **kwargs):
        self.calls.append({"action": action, **kwargs})
        if self.exc_on_round is not None and len(self.calls) >= self.exc_on_round:
            raise RuntimeError("protocol boom")
        return {"messages": list(self.pages.get(kwargs.get("message_seq"), []))}


class _EndlessNonTextAPI(_FakeOneBotAPI):
    """每轮返回一整页纯图片消息（seq 全局递减）：模拟翻不到底的协议端。"""

    def __init__(self, page_size):
        super().__init__({})
        self.page_size = page_size
        self._next_seq = 100000

    async def call_action(self, action, **kwargs):
        self.calls.append({"action": action, **kwargs})
        msgs = []
        for _ in range(self.page_size):
            self._next_seq -= 1
            msgs.append(
                {
                    "time": self._next_seq,
                    "message_id": f"img-{self._next_seq}",
                    "message_seq": self._next_seq,
                    "sender": {"user_id": 1, "nickname": "u"},
                    "message": [{"type": "image", "data": {"file": "x"}}],
                }
            )
        return {"messages": msgs}


def _raw_msg(seq, ts, mid=None, text="m"):
    """构造带 message_seq 的文本原始消息（mid 默认 seq*10 便于断言）。"""
    return {
        "time": ts,
        "message_id": mid if mid is not None else seq * 10,
        "message_seq": seq,
        "sender": {"user_id": 100 + seq, "nickname": f"u{seq}"},
        "message": [{"type": "text", "data": {"text": text}}],
    }


def _raw_img(seq, ts):
    """构造带 message_seq 的纯图片原始消息（parse 后为 None）。"""
    return {
        "time": ts,
        "message_id": f"img-{seq}",
        "message_seq": seq,
        "sender": {"user_id": 1, "nickname": "u"},
        "message": [{"type": "image", "data": {"file": "x"}}],
    }


class TestOneBotPagination:
    """onebot.fetch_group_history 多轮翻页行为（真实函数 + 假协议端）。"""

    def _event_with_bot(self, api):
        event = FakeEvent()
        event.bot = SimpleNamespace(api=api)
        return event

    async def _fetch(self, monkeypatch, api, count, group_id="12345"):
        monkeypatch.setattr(onebot_mod, "ROUND_DELAY_SECONDS", 0)
        return await onebot_mod.fetch_group_history(
            self._event_with_bot(api), group_id, count
        )

    @async_test
    async def test_short_page_stops_after_one_round(self, monkeypatch):
        """协议端一轮返回不足请求量（缓存到头）→ 仅一次调用，升序输出。"""
        api = _FakeOneBotAPI({0: [_raw_msg(2, 200), _raw_msg(1, 100)]})
        msgs = await self._fetch(monkeypatch, api, count=50)
        assert len(api.calls) == 1
        assert api.calls[0]["message_seq"] == 0
        assert [m.message_id for m in msgs] == ["10", "20"]  # 时间升序

    @async_test
    async def test_multi_round_pagination_uses_min_seq_of_whole_page(self, monkeypatch):
        """整页含非文本时，翻页 seq 取整轮最早（含图片消息）；凑满后截到 target。"""
        # count=5 → 首轮请求 ceil(5*1.3)=7：3 文本 + 4 图片（最早 seq=3 是图片）
        page0 = [
            _raw_msg(9, 900),
            _raw_img(8, 800),
            _raw_msg(7, 700),
            _raw_img(6, 600),
            _raw_msg(5, 500),
            _raw_img(4, 400),
            _raw_img(3, 300),
        ]
        # 第二轮 message_seq=3 → 请求 ceil(2*1.3)=3：短页 2 条 → 到头
        page3 = [_raw_msg(2, 200), _raw_msg(1, 100)]
        api = _FakeOneBotAPI({0: page0, 3: page3})
        msgs = await self._fetch(monkeypatch, api, count=5)
        assert len(api.calls) == 2
        assert api.calls[1]["message_seq"] == 3  # 图片消息的 seq 也被计入翻页
        # 文本共 5 条（seq 1,2,5,7,9）恰好等于 target，升序全保留
        assert [m.message_id for m in msgs] == ["10", "20", "50", "70", "90"]

    @async_test
    async def test_overfetch_trimmed_to_target(self, monkeypatch):
        """超量请求多拉到的消息丢弃最旧的，结果不超过 count。"""
        # count=3 → 请求 ceil(3*1.3)=4：满页 4 条文本，result=4 > target
        page0 = [_raw_msg(s, s * 100) for s in (6, 5, 4, 3)]
        api = _FakeOneBotAPI({0: page0})
        msgs = await self._fetch(monkeypatch, api, count=3)
        assert len(api.calls) == 1  # result>=target，不再翻页
        assert [m.message_id for m in msgs] == ["40", "50", "60"]  # 保留最近 3 条

    @async_test
    async def test_first_round_failure_raises(self, monkeypatch):
        """首轮即失败（无已拉数据）→ 抛 OneBotHistoryError（契约不变）。"""
        api = _FakeOneBotAPI({}, exc_on_round=1)
        with pytest.raises(OneBotHistoryError):
            await self._fetch(monkeypatch, api, count=10)

    @async_test
    async def test_later_round_failure_returns_partial(self, monkeypatch):
        """第 2 轮失败 → 不抛异常，降级返回第 1 轮已拉到的消息。"""
        # count=10 → 首轮请求 13：6 文本 + 7 图片 → result=6 < 10，满页 → 翻页
        page0 = [_raw_msg(s, s * 100) for s in range(13, 7, -1)] + [
            _raw_img(s, s * 100) for s in range(7, 0, -1)
        ]
        api = _FakeOneBotAPI({0: page0}, exc_on_round=2)
        msgs = await self._fetch(monkeypatch, api, count=10)
        assert len(api.calls) == 2
        assert len(msgs) == 6  # 仅第 1 轮的文本
        assert [m.message_id for m in msgs] == [
            "80",
            "90",
            "100",
            "110",
            "120",
            "130",
        ]

    @async_test
    async def test_no_message_seq_field_single_round(self, monkeypatch):
        """协议端不回 message_seq → 无法翻页，满页也止步第一轮。"""
        page0 = []
        for s in range(13, 0, -1):  # count=10 → 请求 13 满页
            raw = _raw_msg(s, s * 100) if s > 8 else _raw_img(s, s * 100)
            del raw["message_seq"]
            page0.append(raw)
        api = _FakeOneBotAPI({0: page0})
        msgs = await self._fetch(monkeypatch, api, count=10)
        assert len(api.calls) == 1
        assert len(msgs) == 5  # 仅 seq 9..13 的文本

    @async_test
    async def test_max_rounds_cap_on_bottomless_cache(self, monkeypatch):
        """协议端永远返回满页纯图片 → 达到 MAX_ROUNDS 上限停止，返回空。"""
        api = _EndlessNonTextAPI(page_size=100)
        msgs = await self._fetch(monkeypatch, api, count=10)
        assert len(api.calls) == onebot_mod.MAX_ROUNDS
        assert msgs == []

    @async_test
    async def test_cross_round_message_id_dedup(self, monkeypatch):
        """跨轮重复 message_id 只保留首次出现。"""
        # count=5 → 首轮请求 7：3 文本 + 4 图片 → result=3 < 5 → 翻页
        page0 = [
            _raw_msg(9, 900, mid="A"),
            _raw_img(8, 800),
            _raw_msg(7, 700, mid="B"),
            _raw_img(6, 600),
            _raw_msg(5, 500, mid="C"),
            _raw_img(4, 400),
            _raw_img(3, 300),
        ]
        # 第二轮 3 条：2 条与首轮重复（A/B），1 条新增
        page3 = [
            _raw_msg(2, 200, mid="A"),
            _raw_msg(1, 100, mid="B"),
            _raw_msg(0, 50, mid="D"),
        ]
        api = _FakeOneBotAPI({0: page0, 3: page3})
        msgs = await self._fetch(monkeypatch, api, count=5)
        assert [m.message_id for m in msgs] == ["D", "C", "B", "A"]  # 升序、无重复

    @async_test
    async def test_non_positive_count_no_call(self, monkeypatch):
        """count <= 0 → 直接返回空，不触达协议端。"""
        api = _FakeOneBotAPI({})
        assert await self._fetch(monkeypatch, api, count=0) == []
        assert await self._fetch(monkeypatch, api, count=-3) == []
        assert api.calls == []


# ============================================================
# 五、formatter.strip_markdown 纯函数
# ============================================================


class TestStripMarkdown:
    def test_headings(self):
        assert strip_markdown("# 标题一\n## 标题二\n###### 标题六") == (
            "标题一\n标题二\n标题六"
        )

    def test_bold_italic_underline(self):
        assert strip_markdown("**粗体** *斜体* __下划线__ _斜体2_") == (
            "粗体 斜体 下划线 斜体2"
        )

    def test_bold_italic_triple(self):
        assert strip_markdown("***粗斜体***") == "粗斜体"

    def test_strikethrough(self):
        assert strip_markdown("这是~~删除线~~文本") == "这是删除线文本"

    def test_inline_code_content_preserved(self):
        """行内代码：标记剥离、内容保留（含内部的 ** 不被误伤）。"""
        assert strip_markdown("执行 `pip install` 命令") == "执行 pip install 命令"
        assert strip_markdown("`**不是粗体**`") == "**不是粗体**"

    def test_fenced_code_block_content_preserved(self):
        """围栏代码块：``` 标记行移除，代码内容原样保留（# 不当标题）。"""
        text = "前言\n```python\n# 这是注释\nx = 1\n```\n后记"
        out = strip_markdown(text)
        assert "```" not in out
        assert "# 这是注释" in out
        assert "x = 1" in out
        assert "前言" in out and "后记" in out

    def test_blockquote(self):
        assert strip_markdown("> 引用一\n>> 多层引用") == "引用一\n多层引用"

    def test_link_and_image_take_text(self):
        assert strip_markdown(
            "见 [文档](https://example.com) 与 ![图注](https://x.png)"
        ) == ("见 文档 与 图注")

    def test_list_markers(self):
        assert strip_markdown("- 无序一\n* 无序二\n+ 无序三\n1. 有序一\n2) 有序二") == (
            "无序一\n无序二\n无序三\n有序一\n有序二"
        )

    def test_backslash_escape_literal(self):
        """反斜杠转义 → 字面字符保留，不参与强调解析。"""
        assert strip_markdown(r"a\*b") == "a*b"
        assert strip_markdown(r"\#不是标题") == "#不是标题"

    def test_none_and_non_str(self):
        """None / 空串 → 空串；非 str 先 str 化，不崩溃。"""
        assert strip_markdown(None) == ""
        assert strip_markdown("") == ""
        assert strip_markdown(123) == "123"

    def test_plain_text_unchanged(self):
        assert strip_markdown("普通中文文本，无标记。") == "普通中文文本，无标记。"


# ============================================================
# 六、formatter：forward / image 渲染与降级
# ============================================================


class TestFormatterRender:
    @async_test
    async def test_forward_nodes_count_and_strip(self):
        """forward：1 统计节点 + N 板块节点，单 Nodes 包裹，文本剥 Markdown。"""
        result = make_summary_result(
            sections=[
                ("📢 重要通知与结论", "**加粗通知**"),
                ("💬 讨论要点 / 争议", "# 不是标题"),
                ("🎉 有趣片段", "哈哈"),
                ("✅ TODO / 待跟进", "待办"),
            ]
        )
        formatter = SummaryFormatter(FakeStar())
        chain = await formatter.render(result, "forward")
        assert isinstance(chain, MessageChain)
        assert len(chain.chain) == 1
        nodes_comp = chain.chain[0]
        assert isinstance(nodes_comp, Nodes)
        assert len(nodes_comp.nodes) == 5  # 1 统计 + 4 板块

        stats_text = nodes_comp.nodes[0].content[0].text
        assert "消息总数：2" in stats_text
        assert "参与者：2 人" in stats_text
        assert "数据源构成：MySQL 2 + OneBot 0" in stats_text

        sec1_text = nodes_comp.nodes[1].content[0].text
        assert "加粗通知" in sec1_text and "**" not in sec1_text
        sec2_text = nodes_comp.nodes[2].content[0].text
        assert "不是标题" in sec2_text and "# " not in sec2_text
        # 节点署名兜底值
        assert nodes_comp.nodes[0].uin == "10000"
        assert nodes_comp.nodes[0].name == "总结助手"
        # 零宽空格补齐（防平台端 strip 首尾空白）
        assert stats_text.startswith("​") and stats_text.endswith("​")

    @async_test
    async def test_image_mode_t2i_success(self):
        """image：text_to_image 成功 → Image.fromURL 链，不走 html_render。"""
        star = FakeStar(t2i_url="https://render.example/out.png")
        formatter = SummaryFormatter(star)
        chain = await formatter.render(make_summary_result(), "image")
        img = chain.chain[0]
        assert isinstance(img, Image)
        assert img.url == "https://render.example/out.png"
        assert star.html_calls == []
        # Markdown 保留进入渲染文本
        assert "# 群聊总结" in star.t2i_calls[0]

    @async_test
    async def test_image_mode_t2i_fail_html_fallback_local_path(self):
        """image：自研模板两轮全失败 → 降级 text_to_image；本地路径 → fromFileSystem。

        v0.3.2 链路变更：html_render 不再是单轮兜底级，已移入 T2IRenderer
        内部两轮渲染（R1 png / R2 jpeg，超时 T 与 2T）；两轮皆败 render
        返回 None 后才降级 text_to_image。需构造时注入 config_mgr 才启用
        自研模板级（不注入时签名兼容 v0.3.1 链路，行为同 test_v031 覆盖）。
        「text_to_image 也失败 → 纯文本兜底」由本类既有
        test_image_mode_all_fail_plain_fallback 与 v0.3.2 专项
        TestFormatterImageChain 覆盖。
        """
        star = FakeStar(html_exc=RuntimeError("render down"), t2i_url="/local/out.png")
        formatter = SummaryFormatter(star, FakeConfigMgr())
        chain = await formatter.render(make_summary_result(), "image")
        img = chain.chain[0]
        assert isinstance(img, Image)
        assert img.file == "/local/out.png"
        assert len(star.html_calls) == 2  # 自研模板两轮渲染均尝试
        assert len(star.t2i_calls) == 1  # 最终降级到 text_to_image

    @async_test
    async def test_image_mode_all_fail_plain_fallback(self):
        """image：两级渲染全失败 → 剥 Markdown 纯文本链（render 绝不抛）。"""
        star = FakeStar(t2i_exc=RuntimeError("t2i"), html_exc=RuntimeError("html"))
        formatter = SummaryFormatter(star)
        chain = await formatter.render(make_summary_result(), "image")
        plain = chain.chain[0]
        assert isinstance(plain, Plain)
        assert "群聊总结" in plain.text
        assert "**" not in plain.text  # Markdown 已剥

    @async_test
    async def test_unknown_mode_as_forward(self):
        """未知 mode → 按 forward 处理。"""
        formatter = SummaryFormatter(FakeStar())
        chain = await formatter.render(make_summary_result(), "something-else")
        assert isinstance(chain.chain[0], Nodes)


# ============================================================
# 七、fetcher：混合数据获取
# ============================================================


class TestFetcher:
    def _make(
        self,
        monkeypatch,
        rows=None,
        total=None,
        mysql_exc=None,
        onebot_msgs=None,
        onebot_exc=None,
        config_overrides=None,
        ignore=None,
    ):
        config = FakeConfigMgr(overrides=config_overrides, ignore=ignore)
        mysql = FakeMySQLMgr(rows=rows, total=total, exc=mysql_exc)
        onebot = FakeOneBotFetch(msgs=onebot_msgs, exc=onebot_exc)
        monkeypatch.setattr(fetcher_mod, "fetch_group_history", onebot)
        fetcher = HistoryFetcher(mysql, config)
        event = FakeEvent()
        return fetcher, event, onebot, mysql, config

    @async_test
    async def test_count_insufficient_backfill_amount(self, monkeypatch):
        """数量模式不足（m < count*ratio）→ 补齐条数 = min(N-m, max_fetch)。"""
        rows = make_rows_desc(5)  # m=5 < 10*0.8=8
        fetcher, event, onebot, _, _ = self._make(monkeypatch, rows=rows)
        outcome = await fetcher.fetch_by_count(group_id="12345", event=event, count=10)
        assert outcome.onebot_attempted is True
        assert onebot.calls[-1]["count"] == min(10 - 5, 200)
        assert outcome.sources["mysql"] == 5

    @async_test
    async def test_count_sufficient_no_onebot(self, monkeypatch):
        """数量模式充足（m >= count*ratio）→ 不尝试 OneBot。"""
        rows = make_rows_desc(10)
        fetcher, event, onebot, _, _ = self._make(monkeypatch, rows=rows)
        outcome = await fetcher.fetch_by_count(group_id="12345", event=event, count=10)
        assert outcome.onebot_attempted is False
        assert onebot.calls == []
        assert outcome.sources == {"mysql": 10, "onebot": 0}
        # 时间升序
        stamps = [m.timestamp for m in outcome.messages]
        assert stamps == sorted(stamps)

    @async_test
    async def test_count_backfill_capped_by_max_fetch(self, monkeypatch):
        """补齐条数受 summary_onebot_max_fetch 上限约束。"""
        fetcher, event, onebot, _, _ = self._make(
            monkeypatch,
            rows=[],
            config_overrides={"summary_onebot_max_fetch": "3"},
        )
        await fetcher.fetch_by_count(group_id="12345", event=event, count=10)
        assert onebot.calls[-1]["count"] == 3  # min(10-0, 3)

    @async_test
    async def test_window_empty_triggers_backfill_and_window_filter(self, monkeypatch):
        """时间模式 m==0 → 补齐 max_fetch；OneBot 窗口外消息被丢弃。"""
        window_end = datetime(2026, 7, 30, 12, 0, 0)
        window_start = window_end - timedelta(hours=24)
        in_win = cm(window_start + timedelta(hours=2), content="in-window")
        out_win = cm(window_start - timedelta(hours=2), content="out-window")
        fetcher, event, onebot, _, _ = self._make(
            monkeypatch,
            rows=[],
            total=0,
            onebot_msgs=[in_win, out_win],
            config_overrides={"summary_onebot_max_fetch": "77"},
        )
        outcome = await fetcher.fetch_by_window(
            group_id="12345",
            event=event,
            window_start=window_start,
            window_end=window_end,
        )
        assert outcome.onebot_attempted is True
        assert onebot.calls[-1]["count"] == 77
        contents = [m.content for m in outcome.messages]
        assert contents == ["in-window"]
        assert outcome.sources == {"mysql": 0, "onebot": 1}

    @async_test
    async def test_window_within_tolerance_no_backfill(self, monkeypatch):
        """最早消息在容差内（<= window_start + gap）且数据取全 → 不补齐。"""
        window_end = datetime(2026, 7, 30, 12, 0, 0)
        window_start = window_end - timedelta(hours=2)
        base = window_start + timedelta(minutes=20)  # 早于容差下限(+30min)
        rows = make_rows_desc(3, base=base)
        fetcher, event, onebot, _, _ = self._make(monkeypatch, rows=rows, total=3)
        outcome = await fetcher.fetch_by_window(
            group_id="12345",
            event=event,
            window_start=window_start,
            window_end=window_end,
        )
        assert outcome.onebot_attempted is False
        assert onebot.calls == []
        assert len(outcome.messages) == 3

    @async_test
    async def test_window_gap_beyond_tolerance_backfills(self, monkeypatch):
        """最早消息晚于容差下限（窗口头部有缺口）→ 触发补齐。"""
        window_end = datetime(2026, 7, 30, 12, 0, 0)
        window_start = window_end - timedelta(hours=2)
        base = window_start + timedelta(minutes=40)  # 晚于 +30min 容差
        rows = make_rows_desc(3, base=base)
        fetcher, event, onebot, _, _ = self._make(monkeypatch, rows=rows, total=3)
        outcome = await fetcher.fetch_by_window(
            group_id="12345",
            event=event,
            window_start=window_start,
            window_end=window_end,
        )
        assert outcome.onebot_attempted is True

    @async_test
    async def test_window_not_fully_fetched_no_backfill(self, monkeypatch):
        """total > m（窗口数据未取全）→ 即使最早消息晚于容差也不补齐。"""
        window_end = datetime(2026, 7, 30, 12, 0, 0)
        window_start = window_end - timedelta(hours=2)
        base = window_start + timedelta(minutes=90)
        rows = make_rows_desc(2, base=base)
        fetcher, event, onebot, _, _ = self._make(monkeypatch, rows=rows, total=99)
        outcome = await fetcher.fetch_by_window(
            group_id="12345",
            event=event,
            window_start=window_start,
            window_end=window_end,
        )
        assert outcome.onebot_attempted is False

    @async_test
    async def test_dedup_by_message_id_mysql_wins(self, monkeypatch):
        """两源同 message_id → 保留一条（MySQL 优先）。"""
        ts = datetime(2026, 7, 30, 12, 0, 0)
        rows = [make_row(ts, mid="shared-id", content="dup")]
        onebot_msgs = [cm(ts, mid="shared-id", content="dup")]
        fetcher, event, _, _, _ = self._make(
            monkeypatch, rows=rows, onebot_msgs=onebot_msgs
        )
        outcome = await fetcher.fetch_by_count(group_id="12345", event=event, count=10)
        assert len(outcome.messages) == 1
        assert outcome.messages[0].source == "mysql"
        assert outcome.sources == {"mysql": 1, "onebot": 0}

    @async_test
    async def test_dedup_by_fallback_key_when_id_empty(self, monkeypatch):
        """message_id 为空 → 退化键（秒级时间戳+sender+content[:32]）去重。

        v0.4.5 F12：退化键仅对 message_id 为空的消息生效，故两源都置空 id；
        原「一侧空一侧有 id」场景新语义下为可接受的双份保留，不再去重。
        """
        ts = datetime(2026, 7, 30, 12, 0, 0)
        rows = [make_row(ts, sender="111", mid="", content="same content body")]
        onebot_msgs = [cm(ts, sender="111", mid="", content="same content body")]
        fetcher, event, _, _, _ = self._make(
            monkeypatch, rows=rows, onebot_msgs=onebot_msgs
        )
        outcome = await fetcher.fetch_by_count(group_id="12345", event=event, count=10)
        assert len(outcome.messages) == 1
        assert outcome.messages[0].source == "mysql"

    @async_test
    async def test_three_filters(self, monkeypatch):
        """三类过滤：非文本行 / bot 自身 / 忽略名单。"""
        base = datetime(2026, 7, 30, 12, 0, 0)
        rows = [
            make_row(base, sender="111", mid="ok", content="normal msg"),
            make_row(
                base - timedelta(minutes=1),
                sender="999",
                mid="bot",
                content="bot says hi",
            ),  # bot 自身（self_id=999）
            make_row(
                base - timedelta(minutes=2),
                sender="555",
                mid="ig",
                content="ignored user",
            ),  # 忽略名单
            make_row(
                base - timedelta(minutes=3),
                sender="111",
                mid="img",
                content="x",
                mtype="image",
            ),  # 非文本类型
            make_row(
                base - timedelta(minutes=4), sender="111", mid="empty", content="   "
            ),  # 空内容
        ]
        fetcher, event, _, _, _ = self._make(
            monkeypatch, rows=rows, ignore={"12345": ["555"]}
        )
        outcome = await fetcher.fetch_by_count(group_id="12345", event=event, count=10)
        assert [m.message_id for m in outcome.messages] == ["ok"]
        assert outcome.sources == {"mysql": 1, "onebot": 0}

    @async_test
    async def test_mysql_exception_degrades_and_onebot_still_fills(self, monkeypatch):
        """MySQL 查询异常 → 降级为空；数量模式仍触发 OneBot 补齐。"""
        ob_msgs = [cm(datetime(2026, 7, 30, 12, 0, 0), content="from onebot")]
        fetcher, event, onebot, _, _ = self._make(
            monkeypatch,
            mysql_exc=RuntimeError("connection lost"),
            onebot_msgs=ob_msgs,
        )
        outcome = await fetcher.fetch_by_count(group_id="12345", event=event, count=10)
        assert outcome.onebot_attempted is True
        assert outcome.onebot_error is None
        assert [m.content for m in outcome.messages] == ["from onebot"]
        assert outcome.sources == {"mysql": 0, "onebot": 1}

    @async_test
    async def test_onebot_error_recorded_not_raised(self, monkeypatch):
        """OneBot 失败 → 仅写 onebot_error，不抛；MySQL 数据保留。"""
        rows = make_rows_desc(2)
        fetcher, event, _, _, _ = self._make(
            monkeypatch,
            rows=[],
            onebot_exc=OneBotHistoryError("协议端不支持该 action"),
        )
        # 用 rows 重新构造（上面 rows 参数被误置空，显式重建）
        fetcher, event, _, _, _ = self._make(
            monkeypatch,
            rows=rows,
            onebot_exc=OneBotHistoryError("协议端不支持该 action"),
        )
        outcome = await fetcher.fetch_by_count(group_id="12345", event=event, count=10)
        assert outcome.onebot_attempted is True
        assert outcome.onebot_error == "协议端不支持该 action"
        assert outcome.sources["mysql"] == 2

    @async_test
    async def test_sources_count_after_dedup_filter_and_asc_order(self, monkeypatch):
        """sources = 去重+过滤后保留数；合并结果时间升序。"""
        ts0 = datetime(2026, 7, 30, 12, 0, 0)
        rows = [
            make_row(ts0, sender="111", mid="a1", content="mysql-1"),
            make_row(
                ts0 - timedelta(minutes=5), sender="111", mid="a2", content="mysql-2"
            ),
            make_row(
                ts0 - timedelta(minutes=6),
                sender="555",
                mid="a3",
                content="will-be-ignored",
            ),
        ]
        onebot_msgs = [
            cm(ts0 - timedelta(minutes=2), sender="333", mid="b1", content="onebot-1"),
            cm(ts0 - timedelta(minutes=1), sender="333", mid="b2", content="onebot-2"),
            cm(ts0, sender="111", mid="a1", content="mysql-1"),  # 与 MySQL 重复
        ]
        fetcher, event, _, _, _ = self._make(
            monkeypatch,
            rows=rows,
            onebot_msgs=onebot_msgs,
            ignore={"12345": ["555"]},
        )
        outcome = await fetcher.fetch_by_count(group_id="12345", event=event, count=10)
        assert outcome.sources == {"mysql": 2, "onebot": 2}
        assert len(outcome.messages) == 4
        stamps = [m.timestamp for m in outcome.messages]
        assert stamps == sorted(stamps)


# ============================================================
# 八、summarizer：统计 / 渲染 / 截断 / provider / 板块
# ============================================================


class TestSummarizer:
    def _messages(self):
        """6 条消息：alice×3 / carol×2 / bob×1，时间升序。"""
        base = datetime(2026, 7, 30, 10, 0, 0)
        return [
            cm(
                base + timedelta(minutes=0),
                sender="111",
                name="Alice",
                content="第一条",
                source="mysql",
            ),
            cm(
                base + timedelta(minutes=1),
                sender="222",
                name="Bob",
                content="第二条",
                source="mysql",
            ),
            cm(
                base + timedelta(minutes=2),
                sender="333",
                name="Carol",
                content="第三条",
                source="mysql",
            ),
            cm(
                base + timedelta(minutes=3),
                sender="111",
                name="Alice",
                content="第四条",
                source="onebot",
            ),
            cm(
                base + timedelta(minutes=4),
                sender="333",
                name="Carol",
                content="第五条",
                source="onebot",
            ),
            cm(
                base + timedelta(minutes=5),
                sender="111",
                name="Alice",
                content="第六条",
                source="mysql",
            ),
        ]

    def _make(
        self,
        monkeypatch=None,
        llm_text=FOUR_SECTION_LLM_TEXT,
        config_overrides=None,
        chat_provider="chat-prov",
        chat_provider_exc=None,
        llm_exc=None,
    ):
        config = FakeConfigMgr(overrides=config_overrides)
        context = FakeLLMContext(
            llm_text=llm_text,
            chat_provider=chat_provider,
            chat_provider_exc=chat_provider_exc,
            llm_exc=llm_exc,
        )
        return Summarizer(context, config), context, config

    @async_test
    async def test_stats_total_participants_top_desc(self):
        """统计：total / 参与者 / 时间跨度 / Top N 降序（同数按 ID 升序）。"""
        summarizer, _, _ = self._make()
        result = await summarizer.summarize(
            FakeEvent(), self._messages(), "最近 6 条消息", "forward"
        )
        stats = result.stats
        assert stats.total == 6
        assert stats.participant_count == 3
        assert stats.time_start == datetime(2026, 7, 30, 10, 0, 0)
        assert stats.time_end == datetime(2026, 7, 30, 10, 5, 0)
        assert stats.top_senders == [
            ("111", "Alice", 3),
            ("333", "Carol", 2),
            ("222", "Bob", 1),
        ]
        assert stats.truncated is False
        assert result.messages_used == 6
        assert result.scope_desc == "最近 6 条消息"
        assert result.sources == {}  # 由 service 层注入

    @async_test
    async def test_top_n_config_limit(self):
        """summary_rank_top_n=2 → 排行仅 2 条。"""
        summarizer, _, _ = self._make(config_overrides={"summary_rank_top_n": "2"})
        result = await summarizer.summarize(
            FakeEvent(), self._messages(), "scope", "forward"
        )
        assert len(result.stats.top_senders) == 2
        assert result.stats.top_senders[0] == ("111", "Alice", 3)

    @async_test
    async def test_format_constraint_forward(self):
        """forward 模式 → 注入「不要使用任何 Markdown 格式」约束。"""
        summarizer, context, _ = self._make()
        await summarizer.summarize(FakeEvent(), self._messages(), "s", "forward")
        prompt = context.llm_calls[0]["prompt"]
        assert "不要使用任何 Markdown 格式" in prompt

    @async_test
    async def test_format_constraint_image(self):
        """image 模式 → 注入「可以使用 Markdown 格式」约束。"""
        summarizer, context, _ = self._make()
        await summarizer.summarize(FakeEvent(), self._messages(), "s", "image")
        prompt = context.llm_calls[0]["prompt"]
        assert "可以使用 Markdown 格式" in prompt

    @async_test
    async def test_default_placeholders_all_rendered(self):
        """默认模板 5 个占位符全部渲染，无残留 {}。"""
        summarizer, context, _ = self._make()
        await summarizer.summarize(
            FakeEvent(), self._messages(), "最近 6 条消息", "forward"
        )
        prompt = context.llm_calls[0]["prompt"]
        for ph in (
            "{stats}",
            "{messages}",
            "{time_range}",
            "{group_id}",
            "{format_constraint}",
        ):
            assert ph not in prompt
        assert "最近 6 条消息" in prompt  # time_range 值
        assert "12345" in prompt  # group_id 取自消息
        assert "消息总数: 6" in prompt  # stats 文本块
        assert "第一条" in prompt and "第六条" in prompt  # messages 素材
        assert not re.search(r"\{[A-Za-z_][A-Za-z0-9_]*\}", prompt)

    @async_test
    async def test_custom_template_unknown_placeholder_cleared(self):
        """用户自定义模板的未知占位符 → 渲染自检清空。"""
        tmpl = (
            "自定义 {group_id} {unknown_key} 统计{stats} 素材{messages} "
            "范围{time_range} 约束{format_constraint}"
        )
        summarizer, context, _ = self._make(config_overrides={"summary_prompt": tmpl})
        await summarizer.summarize(FakeEvent(), self._messages(), "s", "forward")
        prompt = context.llm_calls[0]["prompt"]
        assert "{unknown_key}" not in prompt
        assert "自定义 12345" in prompt  # 已知占位符正常替换
        assert not re.search(r"\{[A-Za-z_][A-Za-z0-9_]*\}", prompt)

    @async_test
    async def test_empty_template_falls_back_to_builtin(self):
        """模板被清空 → 回退内置默认模板（占位符体系完整）。"""
        summarizer, context, _ = self._make(config_overrides={"summary_prompt": "  "})
        await summarizer.summarize(FakeEvent(), self._messages(), "s", "forward")
        prompt = context.llm_calls[0]["prompt"]
        assert "QQ 群聊记录总结助手" in prompt

    @async_test
    async def test_truncation_keeps_recent_stats_full(self):
        """超素材长度预算（Web 配置）→ 保留最近消息、truncated=True、统计仍全量。"""
        base = datetime(2026, 7, 30, 10, 0, 0)
        msgs = [
            cm(
                base + timedelta(minutes=i),
                sender="111",
                name="Alice",
                content=f"line{i} " + "x" * 90,
                source="mysql",
            )
            for i in range(30)
        ]
        summarizer, context, _ = self._make(
            config_overrides={"summary_max_prompt_chars": "800"}
        )
        result = await summarizer.summarize(FakeEvent(), msgs, "s", "forward")
        prompt = context.llm_calls[0]["prompt"]
        assert result.stats.truncated is True
        assert result.stats.total == 30  # 统计不受截断影响
        assert result.messages_used < 30
        assert "line29" in prompt  # 保留最近的消息
        assert "line0 " not in prompt  # 最旧的消息被丢弃

    @async_test
    async def test_invalid_budget_config_falls_back_to_constant(self):
        """summary_max_prompt_chars 非法 → 回退常量 MAX_PROMPT_CHARS，不误截断。"""
        base = datetime(2026, 7, 30, 10, 0, 0)
        msgs = [
            cm(
                base + timedelta(minutes=i),
                sender="111",
                name="Alice",
                content=f"line{i}",
                source="mysql",
            )
            for i in range(30)
        ]
        summarizer, _, _ = self._make(
            config_overrides={"summary_max_prompt_chars": "not-a-number"}
        )
        result = await summarizer.summarize(FakeEvent(), msgs, "s", "forward")
        assert result.stats.truncated is False
        assert result.messages_used == 30

    @async_test
    async def test_sections_parsed_four_standard(self):
        """4 板块标题切分成功 → 标准标题 + 标准顺序。"""
        summarizer, _, _ = self._make()
        result = await summarizer.summarize(
            FakeEvent(), self._messages(), "s", "forward"
        )
        titles = [t for t, _ in result.sections]
        assert titles == [
            "📢 重要通知与结论",
            "💬 讨论要点 / 争议",
            "🎉 有趣片段",
            "✅ TODO / 待跟进",
        ]
        bodies = dict(result.sections)
        assert "通知 A" in bodies["📢 重要通知与结论"]
        assert "任务 D" in bodies["✅ TODO / 待跟进"]

    @async_test
    async def test_sections_fallback_single(self):
        """匹配板块 < 2 → 兜底单段 [("全部", raw)]。"""
        raw = "随便一段总结文字，没有任何板块标题关键词。"
        summarizer, _, _ = self._make(llm_text=raw)
        result = await summarizer.summarize(
            FakeEvent(), self._messages(), "s", "forward"
        )
        assert result.sections == [("全部", raw)]

    @async_test
    async def test_provider_from_config(self):
        """配置 summary_provider_id 非空 → 优先使用。"""
        summarizer, context, _ = self._make(
            config_overrides={"summary_provider_id": "cfg-prov"}
        )
        result = await summarizer.summarize(
            FakeEvent(), self._messages(), "s", "forward"
        )
        assert result.provider_id == "cfg-prov"
        assert context.llm_calls[0]["provider"] == "cfg-prov"
        assert context.provider_queries == []  # 无需回退查询

    @async_test
    async def test_provider_fallback_to_chat_session(self):
        """配置为空 → 回退会话 provider。"""
        summarizer, context, _ = self._make(chat_provider="chat-prov")
        result = await summarizer.summarize(
            FakeEvent(), self._messages(), "s", "forward"
        )
        assert result.provider_id == "chat-prov"
        assert context.llm_calls[0]["provider"] == "chat-prov"

    @async_test
    async def test_provider_none_raises(self):
        """配置为空且会话 provider 获取失败 → SummaryProviderError。"""
        summarizer, _, _ = self._make(
            chat_provider_exc=Exception("ProviderNotFoundError")
        )
        with pytest.raises(SummaryProviderError):
            await summarizer.summarize(FakeEvent(), self._messages(), "s", "forward")

    @async_test
    async def test_empty_llm_text_runtime_error(self):
        """LLM 返回空白文本 → RuntimeError。"""
        summarizer, _, _ = self._make(llm_text="   ")
        with pytest.raises(RuntimeError):
            await summarizer.summarize(FakeEvent(), self._messages(), "s", "forward")

    @async_test
    async def test_llm_exception_propagates(self):
        """LLM 调用异常 → 原样向上抛（由 service 层兜底）。"""
        summarizer, _, _ = self._make(llm_exc=ValueError("boom"))
        with pytest.raises(ValueError):
            await summarizer.summarize(FakeEvent(), self._messages(), "s", "forward")


# ============================================================
# 九、storage + scheduler
# ============================================================


class TestStorage:
    @async_test
    async def test_save_read_roundtrip(self, tmp_path):
        """save → read 往返：各字段一致（datetime 转字符串、tuple 转 list）。"""
        storage = SummaryStorage(tmp_path)
        result = make_summary_result()
        path = await storage.save("12345", result)
        assert path.exists()
        assert path.parent.name == "12345"
        assert re.match(r"^\d+_[0-9a-f]{6}\.json$", path.name)

        data = await storage.read("12345", path.name)
        assert data is not None
        assert data["group_id"] == "12345"
        assert data["scope_desc"] == "最近 2 条消息"
        assert data["sources"] == {"mysql": 2, "onebot": 0}
        assert data["provider_id"] == "prov-x"
        assert data["messages_used"] == 2
        assert data["raw_llm_text"] == "raw llm text"
        assert data["stats"]["total"] == 2
        assert data["stats"]["time_start"] == "2026-07-01 10:00:00"
        assert data["stats"]["top_senders"] == [["111", "Alice", 1], ["222", "Bob", 1]]
        assert data["sections"] == [
            ["📢 重要通知与结论", "通知内容"],
            ["✅ TODO / 待跟进", "任务内容"],
        ]
        assert data["generated_at"]

    @async_test
    async def test_save_invalid_group_id_raises(self, tmp_path):
        """非纯数字群号 → ValueError（防路径穿越）。"""
        storage = SummaryStorage(tmp_path)
        with pytest.raises(ValueError):
            await storage.save("../evil", make_summary_result())

    @async_test
    async def test_list_by_group_desc_paging(self, tmp_path):
        """列表按 generated_at 降序分页；非规范命名文件被忽略。"""
        storage = SummaryStorage(tmp_path)

        def plant(gid, fname, generated_at):
            d = tmp_path / gid
            d.mkdir(parents=True, exist_ok=True)
            (d / fname).write_text(
                json.dumps(
                    {
                        "generated_at": generated_at,
                        "scope_desc": "s",
                        "provider_id": "p",
                        "messages_used": 3,
                    }
                ),
                encoding="utf-8",
            )

        plant("12345", "100_aaa111.json", "2026-07-01 10:00:00")
        plant("12345", "200_bbb222.json", "2026-07-03 10:00:00")
        plant("12345", "300_ccc333.json", "2026-07-02 10:00:00")
        plant("12345", "badname.json", "2026-07-09 10:00:00")  # 非规范命名

        page1 = await storage.list_by_group("12345", page=1, page_size=2)
        assert page1["total"] == 3
        assert [it["filename"] for it in page1["items"]] == [
            "200_bbb222.json",
            "300_ccc333.json",
        ]
        page2 = await storage.list_by_group("12345", page=2, page_size=2)
        assert [it["filename"] for it in page2["items"]] == ["100_aaa111.json"]

        # group_id=None → 跨群汇总
        plant("67890", "400_ddd444.json", "2026-07-05 10:00:00")
        all_list = await storage.list_by_group(None, page=1, page_size=20)
        assert all_list["total"] == 4
        assert all_list["items"][0]["filename"] == "400_ddd444.json"

    @async_test
    async def test_list_invalid_group_id_raises(self, tmp_path):
        storage = SummaryStorage(tmp_path)
        with pytest.raises(ValueError):
            await storage.list_by_group("12ab")

    @async_test
    async def test_read_path_traversal_rejected(self, tmp_path):
        """路径穿越（../ / 反斜杠 / 非数字群号）→ 一律 None。"""
        storage = SummaryStorage(tmp_path)
        # 先正常保存一份，取得合法文件名
        path = await storage.save("12345", make_summary_result())
        fname = path.name
        assert await storage.read("12345", fname) is not None  # 合法可读出

        assert await storage.read("12345", "../../config.db") is None
        assert await storage.read("12345", "..\\12345\\x.json") is None
        assert await storage.read("..", fname) is None
        assert await storage.read("12ab", fname) is None
        assert await storage.read("12345", fname + ".bak") is None
        # 穿越尝试不得在 base_dir 之外产生任何文件（read 只读不写，天然满足）

    @async_test
    async def test_cleanup_expired_by_mtime_and_empty_dir_removed(self, tmp_path):
        """按 mtime 清理过期文件、返回计数；空群目录一并移除。"""
        storage = SummaryStorage(tmp_path)
        old_ts = time.time() - 10 * 86400

        g1 = tmp_path / "12345"
        g1.mkdir(parents=True)
        old_file = g1 / "100_aaa111.json"
        old_file.write_text("{}", encoding="utf-8")
        os.utime(old_file, (old_ts, old_ts))
        new_file = g1 / "200_bbb222.json"
        new_file.write_text("{}", encoding="utf-8")

        g2 = tmp_path / "67890"
        g2.mkdir(parents=True)
        only_old = g2 / "300_ccc333.json"
        only_old.write_text("{}", encoding="utf-8")
        os.utime(only_old, (old_ts, old_ts))

        deleted = await storage.cleanup_expired(7)
        assert deleted == 2
        assert not old_file.exists()
        assert new_file.exists()
        assert g1.is_dir()  # 仍有新文件，目录保留
        assert not only_old.exists()
        assert not g2.exists()  # 清空后目录移除

    @async_test
    async def test_cleanup_missing_dir_returns_zero(self, tmp_path):
        storage = SummaryStorage(tmp_path / "not-exist")
        assert await storage.cleanup_expired(30) == 0


class TestScheduler:
    @async_test
    async def test_start_runs_once_then_stop_cancels(self, tmp_path, monkeypatch):
        """start → 启动先跑一轮 → stop 取消任务后不再执行；stop 幂等。"""
        monkeypatch.setattr(scheduler_mod, "_CLEANUP_INTERVAL", 0.02)

        calls = []

        class _RecordingStorage:
            async def cleanup_expired(self, retention_days):
                calls.append(retention_days)
                return 0

        config = FakeConfigMgr()  # retention 默认 30
        sched = CleanupScheduler(_RecordingStorage(), config)

        await sched.start()
        first_task = sched._task
        await sched.start()  # 重复 start 防护
        assert sched._task is first_task

        await asyncio.sleep(0.1)
        assert len(calls) >= 1
        assert calls[0] == 30

        await sched.stop()
        assert sched._task is None
        count_after_stop = len(calls)
        await asyncio.sleep(0.1)
        assert len(calls) == count_after_stop  # 停止后不再执行

        await sched.stop()  # 幂等 no-op

    @async_test
    async def test_invalid_retention_falls_back_default(self, tmp_path):
        """retention 配置非法（<=0）→ 回退默认 30 天，仍执行清理。"""
        calls = []

        class _RecordingStorage:
            async def cleanup_expired(self, retention_days):
                calls.append(retention_days)
                return 0

        config = FakeConfigMgr(overrides={"summary_retention_days": "0"})
        sched = CleanupScheduler(_RecordingStorage(), config)
        await sched._cleanup_once()
        assert calls == [scheduler_mod.DEFAULT_RETENTION_DAYS]


# ============================================================
# 十、service 端到端（真实 fetcher+summarizer+formatter+storage，仅基础设施假）
# ============================================================


class TestServiceE2E:
    def _make(
        self,
        tmp_path,
        monkeypatch,
        *,
        rows=None,
        config_overrides=None,
        ignore=None,
        llm_text=FOUR_SECTION_LLM_TEXT,
        llm_exc=None,
        chat_provider_exc=None,
        onebot_msgs=None,
    ):
        # 数据目录重定向到 tmp_path（仅本测试期间生效）
        monkeypatch.setattr(
            service_mod.StarTools,
            "get_data_dir",
            staticmethod(lambda plugin_name=None: tmp_path),
        )
        onebot = FakeOneBotFetch(msgs=onebot_msgs)
        monkeypatch.setattr(fetcher_mod, "fetch_group_history", onebot)

        config = FakeConfigMgr(
            overrides={
                # 白名单放行测试群
                "summary_group_whitelist": '["12345"]',
                # E2E 用例聚焦总结主流程：关闭触发反馈（v0.3.1 新增，默认 reaction
                # 会往消息流插入确认消息），反馈行为由 v0.3.1 测试专项覆盖
                "summary_feedback_mode": "none",
                **(config_overrides or {}),
            },
            ignore=ignore,
        )
        mysql = FakeMySQLMgr(rows=rows or [])
        context = FakeLLMContext(
            llm_text=llm_text,
            llm_exc=llm_exc,
            chat_provider_exc=chat_provider_exc,
        )
        star = FakeStar()
        svc = SummaryService(context, config, mysql, star)
        return svc, config, mysql, context, onebot

    def _saved_payloads(self, tmp_path, group_id="12345"):
        gdir = tmp_path / "summaries" / group_id
        if not gdir.is_dir():
            return []
        return [
            json.loads(p.read_text(encoding="utf-8"))
            for p in sorted(gdir.glob("*.json"))
        ]

    @async_test
    async def test_happy_path_count_forward(self, tmp_path, monkeypatch):
        """happy path：消息链发送（5 节点）+ JSON 落盘 + sources 注入。"""
        rows = [
            make_row(
                datetime(2026, 7, 30, 12, 0, 0) - timedelta(minutes=i),
                sender=["111", "222", "111", "333", "111"][i],
                name=["Alice", "Bob", "Alice", "Carol", "Alice"][i],
                content=f"msg{i}",
                mid=f"m{i}",
            )
            for i in range(5)
        ]
        svc, _, mysql, context, onebot = self._make(tmp_path, monkeypatch, rows=rows)
        event = FakeEvent()
        await svc.handle_count_command(event, "5")

        # m=5 >= 5*0.8 → 未尝试 OneBot
        assert onebot.calls == []
        # 恰好发出一条渲染消息链（非提示文本）
        assert event.plain_texts() == []
        chains = event.chains()
        assert len(chains) == 1
        nodes = chains[0].chain[0]
        assert isinstance(nodes, Nodes)
        assert len(nodes.nodes) == 5  # 1 统计 + 4 板块
        stats_text = nodes.nodes[0].content[0].text
        assert "消息总数：5" in stats_text
        assert "参与者：3 人" in stats_text
        assert "数据源构成：MySQL 5 + OneBot 0" in stats_text

        # JSON 落盘且 sources 已由 service 注入
        payloads = self._saved_payloads(tmp_path)
        assert len(payloads) == 1
        data = payloads[0]
        assert data["group_id"] == "12345"
        assert data["sources"] == {"mysql": 5, "onebot": 0}
        assert data["scope_desc"] == "最近 5 条消息"
        assert data["provider_id"] == "chat-prov"
        assert data["messages_used"] == 5
        assert len(data["sections"]) == 4
        assert len(context.llm_calls) == 1

    @async_test
    async def test_happy_path_window_24h(self, tmp_path, monkeypatch):
        """时间模式 happy path：/消息总结时间 24h → 窗口总结落盘。"""
        now = datetime.now()
        rows = [
            make_row(now - timedelta(hours=i + 1), content=f"w{i}", mid=f"w{i}")
            for i in range(3)
        ]
        svc, _, _, _, _ = self._make(tmp_path, monkeypatch, rows=rows)
        event = FakeEvent()
        await svc.handle_window_command(event, "24h")
        assert event.plain_texts() == []
        assert len(event.chains()) == 1
        data = self._saved_payloads(tmp_path)[0]
        assert data["scope_desc"] == "最近 24 小时"

    @async_test
    async def test_window_1d_scope_desc(self, tmp_path, monkeypatch):
        """时间模式 1d → scope「最近 1 天」。"""
        now = datetime.now()
        rows = [make_row(now - timedelta(minutes=30), content="x", mid="x1")]
        svc, _, _, _, _ = self._make(tmp_path, monkeypatch, rows=rows)
        event = FakeEvent()
        await svc.handle_window_command(event, "1d")
        assert len(event.chains()) == 1
        assert self._saved_payloads(tmp_path)[0]["scope_desc"] == "最近 1 天"

    @async_test
    async def test_disabled_switch(self, tmp_path, monkeypatch):
        """总开关关闭 → 「总结功能未启用」提示，不走后续流程。"""
        svc, _, _, context, _ = self._make(
            tmp_path,
            monkeypatch,
            config_overrides={"summary_enabled": "false"},
        )
        event = FakeEvent()
        await svc.handle_count_command(event, "50")
        assert event.plain_texts() == ["总结功能未启用，请在 Web 管理后台开启"]
        assert context.llm_calls == []

    @async_test
    async def test_private_chat_rejected(self, tmp_path, monkeypatch):
        """私聊（get_group_id 为空）→ 「请在群内使用」。"""
        svc, _, _, _, _ = self._make(tmp_path, monkeypatch)
        event = FakeEvent(group_id="")
        await svc.handle_count_command(event, "50")
        assert event.plain_texts() == ["请在群内使用"]

    @async_test
    async def test_whitelist_rejected(self, tmp_path, monkeypatch):
        """白名单模式下群不在列表 → 「本群未开启总结功能」。"""
        svc, _, _, _, _ = self._make(tmp_path, monkeypatch)
        event = FakeEvent(group_id="99999")
        await svc.handle_count_command(event, "50")
        assert event.plain_texts() == ["本群未开启总结功能"]

    @async_test
    async def test_whitelist_mode_all_skips_check(self, tmp_path, monkeypatch):
        """mode=all → 跳过白名单校验，任意群可用。"""
        rows = make_rows_desc(2)
        svc, _, _, _, _ = self._make(
            tmp_path,
            monkeypatch,
            rows=rows,
            config_overrides={"summary_whitelist_mode": "all"},
        )
        event = FakeEvent(group_id="99999")
        await svc.handle_count_command(event, "5")
        assert len(event.chains()) == 1

    @async_test
    async def test_missing_or_bad_count_usage(self, tmp_path, monkeypatch):
        """缺参 / 非数字 / 0 → 用法提示。"""
        svc, _, _, context, _ = self._make(tmp_path, monkeypatch)
        usage = "用法：/消息总结 <数量>，如 /消息总结 512"
        for arg in ("", "abc", "0", "-5"):
            event = FakeEvent()
            await svc.handle_count_command(event, arg)
            assert event.plain_texts() == [usage], f"arg={arg!r}"
        assert context.llm_calls == []

    @async_test
    async def test_count_over_limit_rejected(self, tmp_path, monkeypatch):
        """超过 summary_max_count（默认 1000）→ 拒绝并提示上限。"""
        svc, _, _, context, _ = self._make(tmp_path, monkeypatch)
        event = FakeEvent()
        await svc.handle_count_command(event, "1001")
        texts = event.plain_texts()
        assert len(texts) == 1
        assert "最大支持 1000 条" in texts[0]
        assert context.llm_calls == []

    @async_test
    async def test_window_bad_arg_and_over_limit(self, tmp_path, monkeypatch):
        """时间参数非法 → 用法提示；换算超上限 → 提示上限。"""
        svc, _, _, _, _ = self._make(tmp_path, monkeypatch)
        event = FakeEvent()
        await svc.handle_window_command(event, "24x")
        assert event.plain_texts() == [
            "用法：/消息总结时间 <时长>，如 /消息总结时间 24h 或 1d"
        ]

        event2 = FakeEvent()
        await svc.handle_window_command(event2, "8d")  # 192h > 168h
        texts = event2.plain_texts()
        assert len(texts) == 1 and "168" in texts[0]

    @async_test
    async def test_user_cooldown_second_call_rejected(self, tmp_path, monkeypatch):
        """同一用户连续触发：第二次被拒，文案含剩余秒数（<=60）。"""
        rows = make_rows_desc(3)
        svc, _, _, context, _ = self._make(tmp_path, monkeypatch, rows=rows)
        event = FakeEvent(sender_id="111")
        await svc.handle_count_command(event, "5")
        assert len(event.chains()) == 1  # 第一次成功

        await svc.handle_count_command(event, "5")
        texts = event.plain_texts()
        assert len(texts) == 1
        m = re.match(r"^操作太频繁，请 (\d+) 秒后再试$", texts[0])
        assert m is not None
        assert 0 < int(m.group(1)) <= 60
        assert len(context.llm_calls) == 1  # 第二次未进入总结流程

    @async_test
    async def test_group_cooldown_other_user_rejected(self, tmp_path, monkeypatch):
        """群维度冷却：用户 A 触发后，用户 B 在同群立即触发被拒（<=120s）。"""
        rows = make_rows_desc(3)
        svc, _, _, _, _ = self._make(tmp_path, monkeypatch, rows=rows)
        ev_a = FakeEvent(sender_id="111")
        await svc.handle_count_command(ev_a, "5")
        assert len(ev_a.chains()) == 1

        ev_b = FakeEvent(sender_id="222")  # 不同用户、同群
        await svc.handle_count_command(ev_b, "5")
        texts = ev_b.plain_texts()
        assert len(texts) == 1
        m = re.match(r"^操作太频繁，请 (\d+) 秒后再试$", texts[0])
        assert m is not None
        assert 60 < int(m.group(1)) <= 120  # 命中群冷却（120）而非用户冷却（60）

    @async_test
    async def test_invalid_arg_does_not_stamp_cooldown(self, tmp_path, monkeypatch):
        """非法参数不盖冷却章：先失败一次，合法调用仍可执行。"""
        rows = make_rows_desc(3)
        svc, _, _, _, _ = self._make(tmp_path, monkeypatch, rows=rows)
        event = FakeEvent(sender_id="111")
        await svc.handle_count_command(event, "abc")  # 用法提示，不盖章
        await svc.handle_count_command(event, "5")  # 不应被冷却拒绝
        assert len(event.chains()) == 1

    @async_test
    async def test_empty_material_message(self, tmp_path, monkeypatch):
        """素材为空（MySQL 空 + OneBot 空）→ 「该范围内没有可总结的消息」。"""
        svc, _, _, context, onebot = self._make(tmp_path, monkeypatch, rows=[])
        event = FakeEvent()
        await svc.handle_count_command(event, "50")
        assert event.plain_texts() == ["该范围内没有可总结的消息"]
        assert onebot.calls  # 数量模式 m=0 → 尝试过补齐
        assert context.llm_calls == []

    @async_test
    async def test_provider_error_message(self, tmp_path, monkeypatch):
        """SummaryProviderError → 配置总结模型提示文案。"""
        rows = make_rows_desc(3)
        svc, _, _, _, _ = self._make(
            tmp_path,
            monkeypatch,
            rows=rows,
            chat_provider_exc=Exception("ProviderNotFoundError"),
        )
        event = FakeEvent()
        await svc.handle_count_command(event, "5")
        assert event.plain_texts() == [
            "未配置总结模型且无法获取会话模型，请在 Web 管理后台配置"
        ]

    @async_test
    async def test_llm_exception_fallback_message(self, tmp_path, monkeypatch):
        """LLM 调用异常 → 「总结生成失败，请稍后重试」兜底，不冒泡。"""
        rows = make_rows_desc(3)
        svc, _, _, _, _ = self._make(
            tmp_path, monkeypatch, rows=rows, llm_exc=ValueError("boom")
        )
        event = FakeEvent()
        await svc.handle_count_command(event, "5")  # 不得抛异常
        assert event.plain_texts() == ["总结生成失败，请稍后重试"]

    @async_test
    async def test_image_output_mode(self, tmp_path, monkeypatch):
        """output_mode=image → 发送图片消息链。"""
        rows = make_rows_desc(3)
        svc, _, _, context, _ = self._make(
            tmp_path,
            monkeypatch,
            rows=rows,
            config_overrides={"summary_output_mode": "image"},
        )
        event = FakeEvent()
        await svc.handle_count_command(event, "5")
        chains = event.chains()
        assert len(chains) == 1
        assert isinstance(chains[0].chain[0], Image)
        # image 模式注入 Markdown 许可约束
        assert "可以使用 Markdown 格式" in context.llm_calls[0]["prompt"]


# ============================================================
# 十一、web_api：10 个 summary 端点
# ============================================================


class FakeWebContext:
    """Web 端 Context 替身：记录路由注册 + 提供 provider 列表。"""

    def __init__(self, providers=None, providers_exc=None):
        self.registered = []
        self._providers = providers if providers is not None else []
        self._providers_exc = providers_exc

    def register_web_api(self, route, handler, methods, desc):
        self.registered.append((route, handler, methods, desc))

    def get_all_providers(self):
        if self._providers_exc is not None:
            raise self._providers_exc
        return self._providers


class FakeSummaryStorage:
    """SummaryStorage 替身（history/detail 端点测试）。"""

    def __init__(self, list_result=None, read_result=None, list_exc=None):
        self._list_result = list_result or {
            "total": 0,
            "items": [],
            "page": 1,
            "page_size": 20,
        }
        self._read_result = read_result
        self._list_exc = list_exc
        self.list_calls = []
        self.read_calls = []

    async def list_by_group(self, group_id=None, page=1, page_size=20):
        self.list_calls.append((group_id, page, page_size))
        if self._list_exc is not None:
            raise self._list_exc
        return self._list_result

    async def read(self, group_id, filename):
        self.read_calls.append((group_id, filename))
        return self._read_result


class TestWebApiSummary:
    def _make(
        self, config=None, storage="__default__", providers=None, providers_exc=None
    ):
        context = FakeWebContext(providers=providers, providers_exc=providers_exc)
        config = config or FakeConfigMgr()
        if storage == "__default__":
            storage = FakeSummaryStorage()
        # v0.8.2 R4 构造形态同步：域依赖按 Facade 打包注入（行为断言不变）
        instance = WebAPI(
            context, None, config, None, summary=SummaryFacade(storage=storage)
        )
        return instance, context, config, storage

    @async_test
    async def test_routes_registered_22_with_10_summary(self):
        """路由总数 43（12 旧 + 11 summary + 10 profile + 6 stats；v0.5.1 新增
        群列表端点；v0.7.0 +3 query_log；v0.9.0 +1 storage/info——计数随版本演进）。"""
        _, context, _, _ = self._make()
        routes = [r[0] for r in context.registered]
        assert len(routes) == 43
        summary_routes = [r for r in routes if "/summary" in r]
        assert len(summary_routes) == 11
        profile_routes = [r for r in routes if "/profile" in r]
        assert len(profile_routes) == 10
        prefix = f"/{PLUGIN_NAME}"
        for suffix in (
            "/summary/settings",
            "/summary/settings/save",
            "/summary/settings/reset",
            "/summary/providers",
            "/summary/ignore/groups",
            "/summary/ignore",
            "/summary/ignore/add",
            "/summary/ignore/remove",
            "/summary/history",
            "/summary/history/detail",
        ):
            assert prefix + suffix in routes
        # v0.5.0 数据分析 6 端点守卫（v0.5.1 +群列表）
        for suffix in (
            "/stats/data",
            "/stats/groups",
            "/stats/settings",
            "/stats/settings/save",
            "/stats/settings/reset",
            "/stats/push/toggle",
        ):
            assert prefix + suffix in routes

    @async_test
    async def test_settings_get_three_keys(self, monkeypatch):
        """GET settings → settings/defaults/types 三键齐全（各 24 项：v0.3.1 为 19，v0.3.2 +5 T2I 渲染项）。"""
        instance, _, _, _ = self._make()
        _patch_summary_request(monkeypatch, MockRequest())
        resp = await instance.api_summary_settings()
        assert resp["__json__"] is True
        data = resp["data"]
        assert set(data.keys()) == {"settings", "defaults", "types"}
        assert len(data["settings"]) == 24
        assert len(data["defaults"]) == 24
        assert len(data["types"]) == 24
        assert data["types"]["summary_enabled"] == "bool"
        assert data["types"]["summary_group_whitelist"] == "list"
        assert data["defaults"] == ConfigManager.SUMMARY_DEFAULTS

    @async_test
    async def test_save_ok_normalizes_values(self, monkeypatch):
        """POST save 合法 → bool/list 归一化为存储字符串后写入。"""
        config = FakeConfigMgr()
        instance, _, _, _ = self._make(config=config)
        payload = {
            "settings": {
                "summary_enabled": True,
                "summary_group_whitelist": ["123", "456"],
                "summary_user_cooldown": 90,
            }
        }
        _patch_summary_request(monkeypatch, MockRequest(payload=payload))
        resp = await instance.api_summary_settings_save()
        assert resp["__json__"] is True
        assert resp["data"]["saved"] is True
        assert config.store["summary_enabled"] == "true"
        assert config.store["summary_group_whitelist"] == '["123", "456"]'
        assert config.store["summary_user_cooldown"] == "90"
        assert len(config.set_calls) == 3

    @async_test
    async def test_save_invalid_value_rejects_whole_batch(self, monkeypatch):
        """任一值非法 → 整批拒写（400），合法项也不写入。"""
        config = FakeConfigMgr()
        instance, _, _, _ = self._make(config=config)
        payload = {
            "settings": {
                "summary_user_cooldown": "90",  # 合法
                "summary_enabled": "maybe",  # 非法 bool
            }
        }
        _patch_summary_request(monkeypatch, MockRequest(payload=payload))
        resp = await instance.api_summary_settings_save()
        assert resp["__error__"] is True
        assert resp["status"] == 400
        assert config.set_calls == []  # 整批未写入
        assert config.store["summary_user_cooldown"] == "60"  # 默认值未动

    @async_test
    async def test_save_unknown_key_400(self, monkeypatch):
        """未知配置键 → 400。"""
        config = FakeConfigMgr()
        instance, _, _, _ = self._make(config=config)
        _patch_summary_request(
            monkeypatch, MockRequest(payload={"settings": {"nope": "1"}})
        )
        resp = await instance.api_summary_settings_save()
        assert resp["__error__"] is True and resp["status"] == 400
        assert "未知的配置键" in resp["message"]
        assert config.set_calls == []

    @async_test
    async def test_save_bad_payload_shape_400(self, monkeypatch):
        """settings 非对象 → 400。"""
        instance, _, _, _ = self._make()
        _patch_summary_request(monkeypatch, MockRequest(payload={}))
        resp = await instance.api_summary_settings_save()
        assert resp["__error__"] is True and resp["status"] == 400

    @async_test
    async def test_reset_missing_keys_resets_all(self, monkeypatch):
        """reset 缺省 keys → 传 None（全部重置）。"""
        config = FakeConfigMgr(overrides={"summary_user_cooldown": "99"})
        instance, _, _, _ = self._make(config=config)
        _patch_summary_request(monkeypatch, MockRequest(payload={}))
        resp = await instance.api_summary_settings_reset()
        assert resp["__json__"] is True
        assert resp["data"]["reset"] is True
        assert config.reset_calls == [None]
        assert config.store["summary_user_cooldown"] == "60"  # 已重置

    @async_test
    async def test_reset_empty_list_resets_nothing(self, monkeypatch):
        """reset keys=[] → 原样透传（不重置任何项）。"""
        config = FakeConfigMgr(overrides={"summary_user_cooldown": "99"})
        instance, _, _, _ = self._make(config=config)
        _patch_summary_request(monkeypatch, MockRequest(payload={"keys": []}))
        resp = await instance.api_summary_settings_reset()
        assert resp["__json__"] is True
        assert config.reset_calls == [[]]
        assert config.store["summary_user_cooldown"] == "99"  # 未被重置

    @async_test
    async def test_reset_keys_bad_type_400(self, monkeypatch):
        config = FakeConfigMgr()
        instance, _, _, _ = self._make(config=config)
        _patch_summary_request(monkeypatch, MockRequest(payload={"keys": "not-a-list"}))
        resp = await instance.api_summary_settings_reset()
        assert resp["__error__"] is True and resp["status"] == 400

    @async_test
    async def test_providers_list(self, monkeypatch):
        """GET providers → {providers: [{id, name}]}；异常 → 空列表不 500。"""
        provs = [
            SimpleNamespace(provider_config={"id": "p1", "name": "P One"}),
            SimpleNamespace(provider_config={"id": "p2", "name": ""}),  # name 空回退 id
            SimpleNamespace(provider_config={"id": "  ", "name": "x"}),  # id 空被跳过
        ]
        instance, _, _, _ = self._make(providers=provs)
        _patch_summary_request(monkeypatch, MockRequest())
        resp = await instance.api_summary_providers()
        assert resp["data"]["providers"] == [
            {"id": "p1", "name": "P One"},
            {"id": "p2", "name": "p2"},
        ]

        instance2, _, _, _ = self._make(providers_exc=RuntimeError("no ctx"))
        resp2 = await instance2.api_summary_providers()
        assert resp2["__json__"] is True
        assert resp2["data"]["providers"] == []

    @async_test
    async def test_ignore_groups_list(self, monkeypatch):
        config = FakeConfigMgr(ignore={"200": ["1"], "100": ["2"]})
        instance, _, _, _ = self._make(config=config)
        _patch_summary_request(monkeypatch, MockRequest())
        resp = await instance.api_summary_ignore_groups()
        assert resp["data"]["groups"] == ["100", "200"]

    @async_test
    async def test_ignore_list_param_validation(self, monkeypatch):
        """GET ignore：缺 group_id / 非数字 → 400；合法 → 名单透传。"""
        config = FakeConfigMgr(ignore={"12345": ["777"]})
        instance, _, _, _ = self._make(config=config)

        _patch_summary_request(monkeypatch, MockRequest(query_params={}))
        resp = await instance.api_summary_ignore_list()
        assert resp["__error__"] is True and resp["status"] == 400

        _patch_summary_request(
            monkeypatch, MockRequest(query_params={"group_id": "12ab"})
        )
        resp = await instance.api_summary_ignore_list()
        assert resp["__error__"] is True and resp["status"] == 400

        _patch_summary_request(
            monkeypatch, MockRequest(query_params={"group_id": "12345"})
        )
        resp = await instance.api_summary_ignore_list()
        assert resp["__json__"] is True
        assert [s["sender_id"] for s in resp["data"]["senders"]] == ["777"]

    @async_test
    async def test_ignore_add_ok_and_duplicate_409(self, monkeypatch):
        """add：成功 added=True；重复 → 409。"""
        config = FakeConfigMgr(ignore={"12345": ["777"]})
        instance, _, _, _ = self._make(config=config)

        payload = {"group_id": "12345", "sender_id": "888"}
        _patch_summary_request(monkeypatch, MockRequest(payload=payload))
        resp = await instance.api_summary_ignore_add()
        assert resp["__json__"] is True and resp["data"]["added"] is True

        payload_dup = {"group_id": "12345", "sender_id": "777"}
        _patch_summary_request(monkeypatch, MockRequest(payload=payload_dup))
        resp = await instance.api_summary_ignore_add()
        assert resp["__error__"] is True and resp["status"] == 409

        # 缺参 / 非数字群号
        _patch_summary_request(monkeypatch, MockRequest(payload={"group_id": "12345"}))
        resp = await instance.api_summary_ignore_add()
        assert resp["status"] == 400
        _patch_summary_request(
            monkeypatch, MockRequest(payload={"group_id": "12ab", "sender_id": "1"})
        )
        resp = await instance.api_summary_ignore_add()
        assert resp["status"] == 400

    @async_test
    async def test_ignore_remove_ok_and_missing_404(self, monkeypatch):
        """remove：成功 removed=True；不存在 → 404。"""
        config = FakeConfigMgr(ignore={"12345": ["777"]})
        instance, _, _, _ = self._make(config=config)

        _patch_summary_request(
            monkeypatch, MockRequest(payload={"group_id": "12345", "sender_id": "777"})
        )
        resp = await instance.api_summary_ignore_remove()
        assert resp["__json__"] is True and resp["data"]["removed"] is True

        resp = await instance.api_summary_ignore_remove()  # 已被移除
        assert resp["__error__"] is True and resp["status"] == 404

    @async_test
    async def test_history_passthrough_paging(self, monkeypatch):
        """GET history：分页参数透传 storage，结果原样返回。"""
        list_result = {
            "total": 3,
            "items": [{"group_id": "12345", "filename": "1_aaaaaa.json"}],
            "page": 2,
            "page_size": 5,
        }
        storage = FakeSummaryStorage(list_result=list_result)
        instance, _, _, _ = self._make(storage=storage)
        _patch_summary_request(
            monkeypatch,
            MockRequest(
                query_params={"group_id": "12345", "page": "2", "page_size": "5"}
            ),
        )
        resp = await instance.api_summary_history()
        assert resp["__json__"] is True
        assert resp["data"] == list_result
        assert storage.list_calls == [("12345", 2, 5)]

        # 空 group_id → None（全部群）；非法 page → 归一默认
        _patch_summary_request(
            monkeypatch, MockRequest(query_params={"group_id": "  ", "page": "x"})
        )
        await instance.api_summary_history()
        assert storage.list_calls[-1] == (None, 1, 20)

    @async_test
    async def test_history_value_error_400(self, monkeypatch):
        """storage 抛 ValueError（如群号非法）→ 400。"""
        storage = FakeSummaryStorage(list_exc=ValueError("非法群号（仅允许纯数字）"))
        instance, _, _, _ = self._make(storage=storage)
        _patch_summary_request(
            monkeypatch, MockRequest(query_params={"group_id": "12ab"})
        )
        resp = await instance.api_summary_history()
        assert resp["__error__"] is True
        assert resp["status"] == 400
        assert "非法群号" in resp["message"]

    @async_test
    async def test_history_detail_ok_and_none_404(self, monkeypatch):
        """detail：读到 → {detail}；None → 404；缺参 → 400。"""
        detail = {"group_id": "12345", "stats": {"total": 2}}
        storage = FakeSummaryStorage(read_result=detail)
        instance, _, _, _ = self._make(storage=storage)
        _patch_summary_request(
            monkeypatch,
            MockRequest(
                query_params={"group_id": "12345", "filename": "100_aaa111.json"}
            ),
        )
        resp = await instance.api_summary_history_detail()
        assert resp["__json__"] is True
        assert resp["data"]["detail"] == detail
        assert storage.read_calls == [("12345", "100_aaa111.json")]

        storage_none = FakeSummaryStorage(read_result=None)
        instance2, _, _, _ = self._make(storage=storage_none)
        resp = await instance2.api_summary_history_detail()
        assert resp["__error__"] is True and resp["status"] == 404

        _patch_summary_request(
            monkeypatch, MockRequest(query_params={"group_id": "12345"})
        )
        resp = await instance2.api_summary_history_detail()
        assert resp["status"] == 400

    @async_test
    async def test_history_endpoints_503_without_storage(self, monkeypatch):
        """summary_storage 未注入 → history / detail 均 503。"""
        instance, _, _, _ = self._make(storage=None)
        _patch_summary_request(monkeypatch, MockRequest())
        resp = await instance.api_summary_history()
        assert resp["__error__"] is True and resp["status"] == 503
        resp = await instance.api_summary_history_detail()
        assert resp["__error__"] is True and resp["status"] == 503

    @async_test
    async def test_history_with_real_storage_roundtrip(self, tmp_path, monkeypatch):
        """与真实 SummaryStorage 集成：save → history 列表 → detail 读取。"""
        storage = SummaryStorage(tmp_path)
        await storage.save("12345", make_summary_result())
        instance, _, _, _ = self._make(storage=storage)

        _patch_summary_request(
            monkeypatch, MockRequest(query_params={"group_id": "12345"})
        )
        resp = await instance.api_summary_history()
        assert resp["data"]["total"] == 1
        filename = resp["data"]["items"][0]["filename"]

        _patch_summary_request(
            monkeypatch,
            MockRequest(query_params={"group_id": "12345", "filename": filename}),
        )
        resp = await instance.api_summary_history_detail()
        assert resp["data"]["detail"]["stats"]["total"] == 2

        # 非法 filename（路径穿越）→ storage 拒绝 → 404
        _patch_summary_request(
            monkeypatch,
            MockRequest(
                query_params={"group_id": "12345", "filename": "../../config.db"}
            ),
        )
        resp = await instance.api_summary_history_detail()
        assert resp["status"] == 404


# ============================================================
# 十二、db_config：summary 扩展方法（真 aiosqlite 临时文件）
# ============================================================


@needs_aiosqlite
class TestDbConfigSummary:
    def _mgr(self, tmp_path):
        mgr = ConfigManager()
        mgr.db_path = str(tmp_path / "config.db")  # 重定向到临时文件
        return mgr

    @async_test
    async def test_defaults_seeded_15(self, tmp_path):
        """initialize 后默认值全部播种且与 SUMMARY_DEFAULTS 一致。

        断言随版本演进：v0.3.1 为 19 项；v0.3.2 新增 5 项 T2I 渲染配置后
        为 24 项（方法名保留历史命名，语义为「全量默认值播种」）。
        """
        mgr = self._mgr(tmp_path)
        assert await mgr.initialize() is True
        try:
            settings = await mgr.get_all_summary_settings()
            assert len(settings) == 24
            assert settings == ConfigManager.SUMMARY_DEFAULTS
        finally:
            await mgr.close()

    @async_test
    async def test_set_get_roundtrip(self, tmp_path):
        mgr = self._mgr(tmp_path)
        await mgr.initialize()
        try:
            assert await mgr.set_summary_setting("summary_user_cooldown", "90") is True
            assert await mgr.get_summary_setting("summary_user_cooldown") == "90"
            # 自定义 default 回退（键不存在时）
            assert (
                await mgr.get_summary_setting("no_such_key", "fallback") == "fallback"
            )
        finally:
            await mgr.close()

    @async_test
    async def test_typed_conversions(self, tmp_path):
        """typed：bool 'false'→False / int / float / list 均按声明类型转换。"""
        mgr = self._mgr(tmp_path)
        await mgr.initialize()
        try:
            await mgr.set_summary_setting("summary_enabled", "false")
            assert await mgr.get_summary_setting_typed("summary_enabled") is False
            await mgr.set_summary_setting("summary_enabled", "TRUE")
            assert await mgr.get_summary_setting_typed("summary_enabled") is True

            await mgr.set_summary_setting("summary_user_cooldown", "90")
            assert await mgr.get_summary_setting_typed("summary_user_cooldown") == 90

            await mgr.set_summary_setting("summary_min_mysql_ratio", "0.5")
            assert await mgr.get_summary_setting_typed("summary_min_mysql_ratio") == 0.5

            await mgr.set_summary_setting("summary_group_whitelist", '["111", "222"]')
            assert await mgr.get_summary_setting_typed("summary_group_whitelist") == [
                "111",
                "222",
            ]

            assert isinstance(
                await mgr.get_summary_setting_typed("summary_output_mode"), str
            )
        finally:
            await mgr.close()

    @async_test
    async def test_typed_bad_value_falls_back_default(self, tmp_path):
        """typed 转换失败 → warning + 回退默认值的同类型结果。"""
        mgr = self._mgr(tmp_path)
        await mgr.initialize()
        try:
            await mgr.set_summary_setting("summary_user_cooldown", "abc")
            value = await mgr.get_summary_setting_typed("summary_user_cooldown")
            assert value == 60  # 默认 "60" → int
            assert isinstance(value, int)

            await mgr.set_summary_setting("summary_group_whitelist", "{not-json")
            assert (
                await mgr.get_summary_setting_typed("summary_group_whitelist") == []
            )  # 默认 "[]"
        finally:
            await mgr.close()

    @async_test
    async def test_get_missing_key_autoseeds(self, tmp_path):
        """缺失键 get → 返回默认值并自动播种回表。"""
        mgr = self._mgr(tmp_path)
        await mgr.initialize()
        try:
            await mgr.db.execute(
                "DELETE FROM summary_settings WHERE key = 'summary_max_hours'"
            )
            await mgr.db.commit()
            value = await mgr.get_summary_setting("summary_max_hours")
            assert value == "168"
            async with mgr.db.execute(
                "SELECT value FROM summary_settings WHERE key = 'summary_max_hours'"
            ) as cursor:
                row = await cursor.fetchone()
            assert row is not None and row[0] == "168"  # 已重新播种
        finally:
            await mgr.close()

    @async_test
    async def test_reset_all(self, tmp_path):
        """reset(None) → 全量 24 项恢复默认（v0.3.1 为 19 项，v0.3.2 +5 T2I 渲染项）。"""
        mgr = self._mgr(tmp_path)
        await mgr.initialize()
        try:
            await mgr.set_summary_setting("summary_user_cooldown", "99")
            await mgr.set_summary_setting("summary_output_mode", "image")
            result = await mgr.reset_summary_settings(None)
            assert len(result) == 24
            assert result["summary_user_cooldown"] == "60"
            assert result["summary_output_mode"] == "forward"
        finally:
            await mgr.close()

    @async_test
    async def test_reset_specific_keys(self, tmp_path):
        """reset([key]) → 仅重置指定键，其余保持用户值；未知键跳过。"""
        mgr = self._mgr(tmp_path)
        await mgr.initialize()
        try:
            await mgr.set_summary_setting("summary_user_cooldown", "99")
            await mgr.set_summary_setting("summary_output_mode", "image")
            result = await mgr.reset_summary_settings(
                ["summary_user_cooldown", "no_such_key"]
            )
            assert result["summary_user_cooldown"] == "60"
            assert result["summary_output_mode"] == "image"  # 未被重置
        finally:
            await mgr.close()

    @async_test
    async def test_ignore_add_idempotent_and_list(self, tmp_path):
        """忽略名单：add 幂等（重复 False）/ get / remove / list_ignore_groups。"""
        mgr = self._mgr(tmp_path)
        await mgr.initialize()
        try:
            assert await mgr.add_ignore_sender("12345", "777") is True
            assert await mgr.add_ignore_sender("12345", "777") is False  # 重复
            assert await mgr.add_ignore_sender(12345, 777) is False  # 数字入参等效

            senders = await mgr.get_ignore_senders("12345")
            assert len(senders) == 1
            assert senders[0]["sender_id"] == "777"
            assert senders[0]["created_at"]

            assert await mgr.remove_ignore_sender("12345", "777") is True
            assert await mgr.remove_ignore_sender("12345", "777") is False
            assert await mgr.get_ignore_senders("12345") == []

            await mgr.add_ignore_sender("200", "1")
            await mgr.add_ignore_sender("100", "2")
            assert await mgr.list_ignore_groups() == ["100", "200"]
        finally:
            await mgr.close()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
