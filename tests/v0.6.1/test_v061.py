# ruff: noqa: I001
"""astrbot_plugin_group_history_save_mysql v0.6.1 模块 D 测试：补库修复 + 门控缓冲。

本版（v0.6.1）是 v0.6.0 补库功能的缺陷修复版（9 修复 F1~F10 + 4 加固 R1~R4）。
测试沿用 v0.6.0 范式：@async_test 装饰器（asyncio.run，无 pytest-asyncio /
conftest.py）；astrbot.* / aiomysql 全部 sys.modules stub 且注入先于一切被测
导入；被测包缓存剔除兼容多测试文件合跑；fake client / 服务对象按需构造，
核心逻辑全部为**真实模块**断言。

A 组（core/backfill.py 翻页/排序/窗口/去重）：
- A-1 翻页 cap 分层：BACKFILL_MAX_PER_GROUP == PER_ROUND_CAP * MAX_ROUNDS(5000)；
  多轮 message_seq 翻页真实触发（单群 >1000 条能拉满，count/seq 传参正确）
- A-2 短页判定：len(messages) < request_count（而非 < PER_ROUND_CAP）——
  页长恰为 request_count（< 单轮上限）时继续翻页累计；协议端硬限（页长 <
  request_count）时正确终止
- A-3 插入顺序：parsed 排序最旧→最新，insert_chat_message timestamp 单调递增
- A-4 窗口提前终止：翻页循环内本轮最旧 time < window_start 即 break（不再发起
  后续轮次）；窗口边界消息仍完整保留（权威过滤循环不变）
- A-5 空 message_id 跳过：不写库、计入 skipped、群级 warning；非空 id 正常
- A-6 批内 URL 去重收窄：单条消息内重复 URL 只入一次（dict.fromkeys）；
  两条不同消息共用同一 URL 各写一行（不再被跨消息 inserted_urls 抑制）

B 组（core/saver.py 门控缓冲与去重 flush）：
- B-1 门控：begin_backfill() 后 handle_group_message 走缓冲不落库；
  end_backfill() 后恢复实时落库；_run_all 收尾链 finally 执行 end_backfill
  （含取消路径仍 flush，R1）；补库收尾后触发 stats_service.startup_backfill（F6）
- B-2 去重 flush：dedup=True 整条跳过已存在 message_id、过滤已存在图片 URL、
  空 message_id 正常落库；dedup=False 与旧版一致（逐条落库、空缓冲 None/0 不报错）
- B-3 缓冲容量：_pending_records.maxlen == 5000（R3）

C 组（core/parsing.py + backfill.start 窗口起点）：
- C-1 文本拼接：parse_onebot_raw_message 多段文本以 "\\n" 拼接（F5）
- C-2 time 防御：缺 time / 非数值 time → 返回 None 且记 warning（不抛异常）（F8）
- C-3 窗口起点：有已记录消息 → 最后记录时间 − 5 分钟（BACKFILL_OVERLAP_MINUTES）；
  群无记录 / 读取失败 → 回退 now - backfill_hours；force 显式 hours → now - hours
  （v0.8.0 起不再依赖 last_terminate_time，插件卸载时刻不代表该群数据完整时刻）

D 组（接线与校验）：
- D-1 backfill_hours 校验：int/数字字符串通过；float 12.9 / bool true / "abc"
  → 400 且零写入（F9）
- D-2 循环导入消除：from core.db_mysql.pool import DynamicPool 与包级常量
  直连导入通过，base 从 pool 再导出同一对象（R4）
- D-3 版本号：main.py @register 与 metadata.yaml 均为 0.8.0；ReloadBackfill
  注入 saver/stats_service + force_backfill 方法面；MessageSaver 门控/去重 flush 新方法存在

运行方式（插件根目录，二者皆可）：
    python -m pytest "tests/v0.6.1/test_v061.py" -v
    PYTHONIOENCODING=utf-8 python "tests/v0.6.1/test_v061.py"
"""

import asyncio
import functools
import importlib
import inspect
import sys
import tempfile
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
_PLUGIN_ROOT = _TEST_DIR.parents[1]  # tests/v0.6.1 → 插件根
_PLUGINS_DIR = str(_PLUGIN_ROOT.parent)  # data/plugins（包导入根）


# ============================================================
# 一、astrbot.* / aiomysql stub 注入（必须先于任何被测导入）
# ============================================================


def _new_module(name: str) -> types.ModuleType:
    return types.ModuleType(name)


_astrbot = _new_module("astrbot")
_astrbot_api = _new_module("astrbot.api")
_astrbot_event = _new_module("astrbot.api.event")
_astrbot_star = _new_module("astrbot.api.star")
_astrbot_mc = _new_module("astrbot.api.message_components")
_astrbot_web = _new_module("astrbot.api.web")
_astrbot_core = _new_module("astrbot.core")
_astrbot_core_star = _new_module("astrbot.core.star")
_astrbot_core_star_filter = _new_module("astrbot.core.star.filter")
_astrbot_core_star_filter_command = _new_module("astrbot.core.star.filter.command")
_astrbot_core_utils = _new_module("astrbot.core.utils")
_astrbot_core_utils_io = _new_module("astrbot.core.utils.io")


class _StubLogger:
    """记录各级日志（A/B/C 组断言 warning 跳过 / 汇总计数等）。"""

    def __init__(self):
        self.records = {"info": [], "warning": [], "error": [], "debug": []}

    def clear(self):
        for lines in self.records.values():
            lines.clear()

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
_astrbot_api.AstrBotConfig = dict

# ---- astrbot.api.web：WebAPI mixin 顶层 import ----
_astrbot_web.error_response = lambda *a, **k: {"error": a[0] if a else ""}
_astrbot_web.json_response = lambda *a, **k: {"json": a[0] if a else None}
_astrbot_web.file_response = lambda *a, **k: {"file": a[0] if a else None}
_astrbot_web.request = None  # 各 mixin 模块顶层绑定 request（仅导入不调用）

# ---- astrbot.core：main.py 顶层 import GreedyStr ----


class _StubGreedyStr(str):
    """指令剩余文本参数标记（真实实现为 str 子类，stub 同形）。"""


_astrbot_core_star_filter_command.GreedyStr = _StubGreedyStr

# ---- astrbot.core.utils.io：webapi profile/summary mixin ----
_astrbot_core_utils_io.save_temp_img = lambda *a, **k: ""


class _StubFilter:
    EventMessageType = types.SimpleNamespace(GROUP_MESSAGE="group_message")
    PlatformAdapterType = types.SimpleNamespace(AIOCQHTTP="aiocqhttp")
    PermissionType = types.SimpleNamespace(ADMIN="admin")

    @staticmethod
    def _deco(fn):
        return fn

    event_message_type = staticmethod(lambda *a, **k: _StubFilter._deco)
    platform_adapter_type = staticmethod(lambda *a, **k: _StubFilter._deco)
    permission_type = staticmethod(lambda *a, **k: _StubFilter._deco)

    @staticmethod
    def command(*a, **k):
        return _StubFilter._deco


class _StubAstrMessageEvent:
    pass


class _StubMessageChain:
    def __init__(self, chain=None):
        self.chain = list(chain or [])


_astrbot_event.filter = _StubFilter
_astrbot_event.EventMessageType = _StubFilter.EventMessageType
_astrbot_event.PlatformAdapterType = _StubFilter.PlatformAdapterType
_astrbot_event.PermissionType = _StubFilter.PermissionType
_astrbot_event.AstrMessageEvent = _StubAstrMessageEvent
_astrbot_event.MessageChain = _StubMessageChain


class _StubContext:
    def __init__(self, *a, **k):
        pass


class _StubStar:
    def __init__(self, *a, **k):
        pass


def _stub_register(name, author, desc, version):
    def deco(cls):
        cls._registered = (name, author, desc, version)
        return cls

    return deco


class _StubStarTools:
    @classmethod
    def get_data_dir(cls, plugin_name=None):
        return Path(tempfile.gettempdir()) / (plugin_name or "astrbot_test")


_astrbot_star.Context = _StubContext
_astrbot_star.Star = _StubStar
_astrbot_star.register = _stub_register
_astrbot_star.StarTools = _StubStarTools


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
_astrbot_mc.AtAll = _StubAt
_astrbot_mc.Reply = _StubAt
_astrbot_mc.Nodes = _StubNodes
_astrbot_mc.Node = _StubNode

# ---- aiomysql：db_mysql 系列模块顶层 import（不联网）----
_aiomysql = _new_module("aiomysql")
_aiomysql.connect = None
_aiomysql.DictCursor = "DictCursor"
_aiomysql.Connection = object

# ---- 剔除非被测缓存（多测试文件合跑兼容）----
for _name in list(sys.modules):
    if _name == _PKG or _name.startswith(_PKG + "."):
        del sys.modules[_name]

sys.modules["astrbot"] = _astrbot
sys.modules["astrbot.api"] = _astrbot_api
sys.modules["astrbot.api.event"] = _astrbot_event
sys.modules["astrbot.api.star"] = _astrbot_star
sys.modules["astrbot.api.message_components"] = _astrbot_mc
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
# 二、导入真实被测模块（A 组断言对象；B/C/D 组沿用）
# ============================================================

from astrbot_plugin_group_history_save_mysql.core.backfill import (  # noqa: E402
    BACKFILL_OVERLAP_MINUTES,
    BACKFILL_OVERLAP_THRESHOLD,
    DEFAULT_MAX_ROUNDS,
    DEFAULT_ROUND_CAP,
    ReloadBackfill,
)
from astrbot_plugin_group_history_save_mysql.core.db_mysql import (  # noqa: E402
    CREATE_RETRY_BACKOFF_SECONDS,
    RESET_PENDING_WAIT_SECONDS,
    DynamicPool,
    MySQLManager,
)
from astrbot_plugin_group_history_save_mysql.core.parsing import (  # noqa: E402
    parse_onebot_raw_message,
)
from astrbot_plugin_group_history_save_mysql.core.saver import MessageSaver  # noqa: E402
from astrbot_plugin_group_history_save_mysql.core.summary.onebot import (  # noqa: E402
    fetch_group_history,
)
from astrbot_plugin_group_history_save_mysql.core.webapi import WebAPI  # noqa: E402

import astrbot.api.message_components as Comp  # noqa: E402

# ============================================================
# 三、共享替身（fake config / mysql / client / 事件）
# ============================================================


class _FakeConfigMgr:
    """ReloadBackfill 配置替身：可编排 backfill_enabled / backfill_hours /
    all_mode / 白名单 / 任意额外设置（如 last_terminate_time）。"""

    def __init__(
        self, enabled="true", hours="12", groups=None, all_mode="false", settings=None
    ):
        self._enabled = enabled
        self._hours = hours
        self._all_mode = all_mode
        self._groups = groups or []
        self._extra = dict(settings or {})

    async def get_setting(self, key, default=""):
        if key == "backfill_enabled":
            return self._enabled
        if key == "backfill_hours":
            return self._hours
        if key == "all_mode":
            return self._all_mode
        return self._extra.get(key, default)

    async def get_groups(self):
        return [dict(g) for g in self._groups]


class _FakeGatingConfig:
    """MessageSaver 门控缓冲用配置替身：白名单一律放行。"""

    async def is_group_enabled(self, group_id):
        return True


class _FakeMySQLMgr:
    """MySQLManager 替身：记录调用，可编排已存在集合与插入失败。"""

    def __init__(
        self,
        existing_ids=None,
        existing_urls=None,
        recent_messages=None,
        last_message_time=None,
    ):
        self.existing_ids = set(existing_ids or [])
        self.existing_urls = set(existing_urls or [])
        self.recent_messages = list(
            recent_messages or []
        )  # 重叠参照（get_recent_messages）
        self.last_message_time = (
            last_message_time  # 群最后记录时间（get_last_message_time）
        )
        self.chat_calls: list[dict] = []
        self.img_calls: list[dict] = []
        self.id_lookups: list[tuple] = []
        self.url_lookups: list[tuple] = []
        self.fail_chat = False

    async def get_existing_message_ids(self, group_id, ids):
        self.id_lookups.append((group_id, list(ids)))
        return {i for i in ids if i in self.existing_ids}

    async def get_existing_image_urls(self, group_id, urls):
        self.url_lookups.append((group_id, list(urls)))
        return {u for u in urls if u in self.existing_urls}

    async def get_all_group_ids(self):
        return []

    async def get_recent_messages(self, group_id, limit):
        return list(self.recent_messages)

    async def get_last_message_time(self, group_id):
        return self.last_message_time

    async def insert_chat_message(self, **kwargs):
        self.chat_calls.append(kwargs)
        return not self.fail_chat

    async def insert_image_record(self, **kwargs):
        self.img_calls.append(kwargs)
        return True


class _FakeClientAPI:
    """协议端 call_action 替身：无 message_seq 返回 latest 页，有 message_seq 返回
    固定页（A-2/3/4/5/6 用）。"""

    def __init__(self, pages: dict, latest=None):
        self.pages = pages  # {message_seq: [raw, ...]}
        self.latest = latest  # 无 message_seq（round 1 最新视图）时返回的页
        self.calls: list[dict] = []

    async def call_action(self, action, **kwargs):
        self.calls.append(kwargs)
        assert action == "get_group_msg_history"
        seq = kwargs.get("message_seq") or 0
        if not seq and self.latest is not None:
            return {"messages": self.latest}
        return {"messages": self.pages.get(seq, [])}


class _FailFirstClient:
    """协议端替身：首轮抛错（模拟 NapCat 缓存空「消息不存在」），后续轮正常。"""

    def __init__(self, pages: dict):
        self.pages = pages
        self.calls: list[dict] = []
        self.round = 0

    async def call_action(self, action, **kwargs):
        self.calls.append(kwargs)
        self.round += 1
        if self.round == 1:
            raise RuntimeError("消息0不存在")
        seq = kwargs.get("message_seq", 0)
        return {"messages": self.pages.get(seq, [])}


class _BulkClient:
    """批量翻页替身（A-1/A-2 用）：page_size 视为服务端单页交付上限。

    - page_size == PER_ROUND_CAP 时：每轮正好交付 request_count 条 → 多轮翻页；
    - page_size > request_count 时：超量交付（模拟服务端忽略 count 返回满页）；
    - page_size < request_count 时：单页硬限（模拟协议端约 200 条截断）。
    """

    def __init__(self, messages: list, page_size: int = 1000):
        # 消息按 message_seq 从新（大）到旧（小）排序
        self.all = sorted(messages, key=lambda m: m["message_seq"], reverse=True)
        self.page_size = page_size
        self.calls: list[dict] = []

    async def call_action(self, action, **kwargs):
        self.calls.append(kwargs)
        assert action == "get_group_msg_history"
        seq = kwargs.get("message_seq", 0)
        window = (
            self.all if seq == 0 else [m for m in self.all if m["message_seq"] < seq]
        )
        return {"messages": window[: self.page_size]}


class _NapCatClient:
    """协议端替身：模拟 NapCat 方向语义（A-7 与 A-9 用）。

    NapCat 的 ``get_group_msg_history`` 默认（不传 / 传 false reverse_order）
    返回比锚点**更新**的消息（含锚点自身）；仅 ``reverse_order=true`` 才返回
    更旧消息（含锚点自身）。无 message_seq 时按最新视图返回最新 count 条。
    消息短 ID 不随 time 单调（模拟 msgId 哈希短 ID），故「最旧消息」必须按
    time 判定，不能按短 ID 大小判定。
    """

    def __init__(self, messages: list, page_size: int = 1000):
        self.all = messages
        self.by_short = {m["message_seq"]: m for m in messages}
        self.page_size = page_size
        self.calls: list[dict] = []

    async def call_action(self, action, **kwargs):
        self.calls.append(kwargs)
        assert action == "get_group_msg_history"
        seq = kwargs.get("message_seq") or 0
        count = kwargs.get("count") or self.page_size
        reverse = bool(kwargs.get("reverse_order", False))
        if not seq:
            window = sorted(self.all, key=lambda m: m["time"])
            return {"messages": window[-count:]}
        anchor = self.by_short.get(seq)
        if anchor is None:
            return {"messages": []}  # 短 ID 解析失败（缓存未命中）
        if reverse:
            window = [m for m in self.all if m["time"] <= anchor["time"]]
            window.sort(key=lambda m: m["time"], reverse=True)  # 新→旧
        else:
            window = [m for m in self.all if m["time"] >= anchor["time"]]
            window.sort(key=lambda m: m["time"])  # 旧→新
        return {"messages": window[:count]}


class _StandardSummaryClient:
    """协议端替身：模拟 go-cqhttp/Lagrange 风格（A-10 用）。

    无 message_seq → 返回最新 count 条；有 message_seq → 返回比锚点更旧的消息
    （默认即更旧，与 reverse_order 无关）。"""

    def __init__(self, messages: list, page_size: int = 1000):
        self.all = sorted(messages, key=lambda m: m["message_seq"])
        self.page_size = page_size
        self.calls: list[dict] = []

    async def call_action(self, action, **kwargs):
        self.calls.append(kwargs)
        assert action == "get_group_msg_history"
        seq = kwargs.get("message_seq") or 0
        count = kwargs.get("count") or self.page_size
        window = (
            self.all if not seq else [m for m in self.all if m["message_seq"] < seq]
        )
        window = sorted(window, key=lambda m: m["message_seq"], reverse=True)  # 新→旧
        return {"messages": window[:count]}


class _FakeInst:
    def __init__(self, client):
        self._client = client

    def meta(self):
        return types.SimpleNamespace(name="aiocqhttp")

    def get_client(self):
        return self._client


class _FakeContext:
    def __init__(self, insts=None):
        self._insts = insts or []
        self.platform_manager = types.SimpleNamespace(get_insts=lambda: self._insts)


class _StubEvent:
    """MessageSaver.handle_group_message 用 AstrMessageEvent 替身。"""

    def __init__(
        self, chain, message_id="m1", group_id="111", sender_id="u1", sender_name="甲"
    ):
        self._chain = chain
        self._group_id = group_id
        self._sender_id = sender_id
        self._sender_name = sender_name
        self.message_obj = types.SimpleNamespace(
            message_id=message_id, raw_message=None
        )
        self.unified_msg_origin = "test"

    def get_group_id(self):
        return self._group_id

    def get_sender_id(self):
        return self._sender_id

    def get_sender_name(self):
        return self._sender_name

    def get_messages(self):
        return self._chain


def _raw_text(mid, text, when, seq=1, user="u1", nick="甲"):
    return {
        "time": int(when.timestamp()),
        "message_id": mid,
        "message_seq": seq,
        "sender": {"user_id": user, "nickname": nick},
        "message": [{"type": "text", "data": {"text": text}}],
    }


def _raw_image(mid, url, when, seq=1, user="u1", nick="甲"):
    return {
        "time": int(when.timestamp()),
        "message_id": mid,
        "message_seq": seq,
        "sender": {"user_id": user, "nickname": nick},
        "message": [{"type": "image", "data": {"url": url}}],
    }


def _bulk_raw_messages(count, mid_prefix, base_when, seq_start=1, step_seconds=1):
    """批量生成 seq 递增（新→旧对应 seq 大→小）、time 递增的文本消息。"""
    return [
        {
            "time": int((base_when + timedelta(seconds=i * step_seconds)).timestamp()),
            "message_id": f"{mid_prefix}-{seq_start + i}",
            "message_seq": seq_start + i,
            "sender": {"user_id": str(seq_start + i), "nickname": f"甲{seq_start + i}"},
            "message": [
                {"type": "text", "data": {"text": f"文本{mid_prefix}-{seq_start + i}"}}
            ],
        }
        for i in range(count)
    ]


def _napcat_raw_messages(count, base_when, short_ids):
    """构造 NapCat 风格消息：time 递增（1..count），message_seq 取传入短 ID 排列
    （不随 time 单调，模拟 NapCat 的 msgId 哈希短 ID；time 第 i 条 → 短 ID
    short_ids[i-1]）。"""
    return [
        {
            "time": int((base_when + timedelta(seconds=i)).timestamp()),
            "message_id": f"n-{i}",
            "message_seq": short_ids[i - 1],
            "sender": {"user_id": str(i), "nickname": f"甲{i}"},
            "message": [{"type": "text", "data": {"text": f"文本{i}"}}],
        }
        for i in range(1, count + 1)
    ]


# ============================================================
# 四、A 组：翻页 cap 分层 / 短页判定 / 插入顺序 / 窗口提前终止 /
#           空 message_id / 批内 URL 去重收窄
# ============================================================


class TestBackfillCore(unittest.TestCase):
    """A 组：翻页（起点 seq）/ 排序 / 窗口 / 去重 / 降级 / 解析失败区分。"""

    def test_a01_cap_layering_constants(self):
        # 翻页参数默认：单轮 200 × 最多 5 轮（可经插件设置自定义）；重叠阈值 0.5
        self.assertEqual(DEFAULT_ROUND_CAP, 200)
        self.assertEqual(DEFAULT_MAX_ROUNDS, 5)
        self.assertEqual(BACKFILL_OVERLAP_THRESHOLD, 0.5)

    @async_test
    async def test_a01_multi_round_paging(self):
        # round 1 不传 message_seq（最新视图）→ 最新 1000（seq 3000..2001）；后续轮按
        # 本轮最旧消息 seq 向旧翻页：seq 2001 → 1001 → 1，第 4 轮锚点 seq=1 无更旧
        # 消息 → 空页终止，共 4 轮拉满 3000 条
        window_start = datetime(2026, 8, 1, 12, 0)
        msgs = _bulk_raw_messages(3000, "m", datetime(2026, 8, 7, 0, 0), seq_start=1)
        client = _BulkClient(msgs, page_size=1000)
        ctx = _FakeContext([_FakeInst(types.SimpleNamespace(api=client))])
        mysql = _FakeMySQLMgr()
        bf = ReloadBackfill(ctx, mysql, _FakeConfigMgr())

        result = await bf._backfill_group("111", 3000, window_start, 1000, 5)

        self.assertEqual(result["pulled"], 3000)
        self.assertEqual(result["inserted_text"], 3000)
        self.assertEqual([c["count"] for c in client.calls], [1000, 1000, 1000, 1000])
        self.assertEqual(
            [c.get("message_seq") for c in client.calls], [None, 2001, 1001, 1]
        )

    @async_test
    async def test_a02_page_len_equals_request_count_continues_paging(self):
        # 第 2 轮 request_count = min(1000, 5000-4500) = 500；页长恰 == request_count
        # 不得判定短页 → 继续翻页累计到总上限（F1）
        window_start = datetime(2026, 8, 1, 12, 0)
        base = datetime(2026, 8, 7, 0, 0)
        round1 = _bulk_raw_messages(4500, "m", base, seq_start=1500)  # seq 1500..5999
        # round2 时间严格早于 round1 最旧（seq1500 → base+0s），避免方向探测误判为更新
        round2 = _bulk_raw_messages(
            500, "m2", base - timedelta(seconds=500), seq_start=1000
        )  # seq 1000..1499，time base-500..base-1
        round3 = _bulk_raw_messages(999, "m3", base, seq_start=1)  # seq 1..999
        client = _FakeClientAPI({1500: round2, 1000: round3}, latest=round1)
        ctx = _FakeContext([_FakeInst(types.SimpleNamespace(api=client))])
        mysql = _FakeMySQLMgr()
        bf = ReloadBackfill(ctx, mysql, _FakeConfigMgr())

        result = await bf._backfill_group("111", 6000, window_start, 1000, 5)

        self.assertEqual([c["count"] for c in client.calls], [1000, 500])
        self.assertEqual([c.get("message_seq") for c in client.calls], [None, 1500])
        self.assertEqual(result["pulled"], 5000)

    @async_test
    async def test_a02_protocol_hard_limit_below_request_breaks(self):
        # 协议端单页硬限 200（< request_count=1000）→ 短页判定触发，止于第 1 轮
        window_start = datetime(2026, 8, 1, 12, 0)
        msgs = _bulk_raw_messages(1000, "m", datetime(2026, 8, 7, 0, 0), seq_start=1)
        client = _BulkClient(msgs, page_size=200)
        ctx = _FakeContext([_FakeInst(types.SimpleNamespace(api=client))])
        mysql = _FakeMySQLMgr()
        bf = ReloadBackfill(ctx, mysql, _FakeConfigMgr())

        result = await bf._backfill_group("111", 1000, window_start, 1000, 5)

        self.assertEqual(len(client.calls), 1)
        self.assertEqual(result["pulled"], 200)

    @async_test
    async def test_a01_overlap_boundary_stops_paging(self):
        # 重叠边界停止（v0.6.1）：第 1 轮全为新；第 2 轮 150/200 为已记录 id
        # （≥50%）→ 命中已记录边界不再发起第 3 轮；已记录 id 仍进入去重跳过
        window_start = datetime(2026, 8, 1, 12, 0)
        round1 = _bulk_raw_messages(
            200, "new", datetime(2026, 8, 2, 0, 0), seq_start=401
        )
        round2 = [
            _raw_text(f"rec-{i}", f"旧文本{i}", datetime(2026, 8, 1, 12, 30), seq=i)
            for i in range(1, 151)
        ] + [
            _raw_text(
                f"new2-{i}", f"新二{i}", datetime(2026, 8, 1, 12, 40), seq=150 + i
            )
            for i in range(1, 51)
        ]
        client = _FakeClientAPI({401: round2}, latest=round1)
        ctx = _FakeContext([_FakeInst(types.SimpleNamespace(api=client))])
        recent = [
            {"message_id": f"rec-{j}", "content": f"旧文本{j}"} for j in range(1, 201)
        ]
        mysql = _FakeMySQLMgr(
            recent_messages=recent,
            existing_ids={f"rec-{j}" for j in range(1, 151)},
        )
        bf = ReloadBackfill(ctx, mysql, _FakeConfigMgr())

        result = await bf._backfill_group("111", 600, window_start, 200, 5)

        self.assertEqual(len(client.calls), 2, "重叠边界命中后不应再发起后续轮次")
        self.assertEqual(result["pulled"], 400)
        self.assertEqual(result["inserted_text"], 250)  # 200 new + 50 new2
        self.assertEqual(result["skipped"], 150)  # rec-1..150 已存在跳过
        inserted_ids = [c["message_id"] for c in mysql.chat_calls]
        self.assertTrue(all("rec-" not in mid for mid in inserted_ids))
        self.assertEqual(len(inserted_ids), 250)

    @async_test
    async def test_a02_first_round_failure_degrades_group(self):
        # 首轮失败降级（生产修复）：NapCat 群缓存空报「消息不存在」，不再 raise
        # 整群作废，而是跳过该群返回零计数（warning 保留 traceback 可诊断）
        _STUB_LOGGER.clear()
        window_start = datetime(2026, 8, 1, 12, 0)
        ok = _raw_text("m_ok", "正常", datetime(2026, 8, 1, 13, 0), seq=1)
        client = _FailFirstClient({1: [ok]})
        ctx = _FakeContext([_FakeInst(types.SimpleNamespace(api=client))])
        mysql = _FakeMySQLMgr()
        bf = ReloadBackfill(ctx, mysql, _FakeConfigMgr())

        result = await bf._backfill_group(
            "111", 500, window_start, DEFAULT_ROUND_CAP, DEFAULT_MAX_ROUNDS
        )

        self.assertEqual(result["pulled"], 0)  # 首轮失败降级：不 raise，返回零计数
        self.assertEqual(result["inserted_text"], 0)
        self.assertTrue(
            any("拉取失败" in m for m in _STUB_LOGGER.records["warning"]),
            f"应保留拉取失败 warning: {_STUB_LOGGER.records['warning']}",
        )

    @async_test
    async def test_a03_insertion_order_oldest_first(self):
        # 翻页返回新→旧乱序；排序后最旧→最新插入，timestamp 单调递增（F2）
        window_start = datetime(2026, 8, 1, 12, 0)
        newest = _raw_text("m_new", "最新", datetime(2026, 8, 1, 14, 0), seq=3)
        oldest = _raw_text("m_old", "最旧", datetime(2026, 8, 1, 12, 30), seq=2)
        mid = _raw_text("m_mid", "中间", datetime(2026, 8, 1, 13, 0), seq=1)
        client = _FakeClientAPI({}, latest=[newest, oldest, mid])
        ctx = _FakeContext([_FakeInst(types.SimpleNamespace(api=client))])
        mysql = _FakeMySQLMgr()
        bf = ReloadBackfill(ctx, mysql, _FakeConfigMgr())

        result = await bf._backfill_group(
            "111", 3, window_start, DEFAULT_ROUND_CAP, DEFAULT_MAX_ROUNDS
        )

        self.assertEqual(result["pulled"], 3)
        self.assertEqual(result["inserted_text"], 3)
        timestamps = [c["timestamp"] for c in mysql.chat_calls]
        self.assertEqual(
            timestamps,
            [
                datetime(2026, 8, 1, 12, 30),
                datetime(2026, 8, 1, 13, 0),
                datetime(2026, 8, 1, 14, 0),
            ],
        )
        for a, b in zip(timestamps, timestamps[1:]):
            self.assertLess(a, b, "insert_chat_message 时间戳应单调递增")

    @async_test
    async def test_a04_window_early_termination(self):
        # 1200 条消息，time = 窗口起点 + (seq-150)*10s：seq<=149 落在窗口外。
        # round 1（无 message_seq）拉满最新 1000 条全在窗口内；round 2（锚点 seq 201）
        # 最旧消息 seq 1 已出窗口 → 翻页提前 break 不再发起后续轮次；边界消息 seq 150 保留
        window_start = datetime(2026, 8, 1, 12, 0)
        msgs = [
            _raw_text(
                f"m-{seq}",
                f"消息{seq}",
                window_start + timedelta(seconds=(seq - 150) * 10),
                seq=seq,
            )
            for seq in range(1, 1201)
        ]
        client = _BulkClient(msgs, page_size=1000)
        ctx = _FakeContext([_FakeInst(types.SimpleNamespace(api=client))])
        mysql = _FakeMySQLMgr()
        bf = ReloadBackfill(ctx, mysql, _FakeConfigMgr())

        result = await bf._backfill_group("111", 1200, window_start, 1000, 5)

        self.assertEqual(len(client.calls), 2, "窗口提前终止后不应再发起后续轮次")
        # round 1 最新 1000 条 + round 2 窗口内边界 51 条（seqs 150..200）
        self.assertEqual(result["pulled"], 1051)
        self.assertEqual(result["inserted_text"], 1051)
        ids = [c["message_id"] for c in mysql.chat_calls]
        self.assertIn("m-150", ids)  # 恰在窗口边界的消息保留
        self.assertNotIn("m-149", ids)  # 窗口外消息被权威过滤

    @async_test
    async def test_a05_empty_message_id_skipped(self):
        # 空 message_id 消息不写库、计入 skipped、群级 warning；非空 id 正常（F4）
        _STUB_LOGGER.clear()
        window_start = datetime(2026, 8, 1, 12, 0)
        empty_text = _raw_text("", "空id文本", datetime(2026, 8, 1, 12, 30), seq=2)
        ok = _raw_text("m_ok", "正常", datetime(2026, 8, 1, 13, 0), seq=1)
        empty_img = _raw_image(
            "", "https://a.com/e.jpg", datetime(2026, 8, 1, 13, 30), seq=0
        )
        client = _FakeClientAPI({}, latest=[empty_text, ok, empty_img])
        ctx = _FakeContext([_FakeInst(types.SimpleNamespace(api=client))])
        mysql = _FakeMySQLMgr()
        bf = ReloadBackfill(ctx, mysql, _FakeConfigMgr())

        result = await bf._backfill_group(
            "111", 2, window_start, DEFAULT_ROUND_CAP, DEFAULT_MAX_ROUNDS
        )

        self.assertEqual(result["pulled"], 3)
        self.assertEqual(result["skipped"], 2)  # 两条空 id
        self.assertEqual(result["inserted_text"], 1)
        self.assertEqual(result["inserted_images"], 0)
        self.assertEqual([c["message_id"] for c in mysql.chat_calls], ["m_ok"])
        self.assertEqual(mysql.img_calls, [])
        self.assertTrue(
            any("空 message_id" in m for m in _STUB_LOGGER.records["warning"]),
            f"应输出空 message_id 群级 warning: {_STUB_LOGGER.records['warning']}",
        )

    @async_test
    async def test_a06_url_dedup_narrowed_to_within_message(self):
        # 单条消息内重复 URL 只入一次（dict.fromkeys）；两条不同消息共用同一 URL
        # 各写一行（不再被跨消息 inserted_urls 抑制，F10）
        window_start = datetime(2026, 8, 1, 12, 0)
        m_a = {
            "time": int(datetime(2026, 8, 1, 13, 0).timestamp()),
            "message_id": "m_a",
            "message_seq": 1,
            "sender": {"user_id": "u1", "nickname": "甲"},
            "message": [
                {"type": "image", "data": {"url": "https://a.com/dup.jpg"}},
                {"type": "image", "data": {"url": "https://a.com/dup.jpg"}},
            ],
        }
        m_b = _raw_image(
            "m_b", "https://a.com/dup.jpg", datetime(2026, 8, 1, 14, 0), seq=0
        )
        client = _FakeClientAPI({}, latest=[m_a, m_b])
        ctx = _FakeContext([_FakeInst(types.SimpleNamespace(api=client))])
        mysql = _FakeMySQLMgr()  # existing_urls 空：不抑制跨消息
        bf = ReloadBackfill(ctx, mysql, _FakeConfigMgr())

        result = await bf._backfill_group(
            "111", 1, window_start, DEFAULT_ROUND_CAP, DEFAULT_MAX_ROUNDS
        )

        self.assertEqual(result["pulled"], 2)
        self.assertEqual(result["inserted_images"], 2)  # 两条消息各写一行
        self.assertEqual(len(mysql.img_calls), 2)
        self.assertEqual(
            [c["image_url"] for c in mysql.img_calls], ["https://a.com/dup.jpg"] * 2
        )
        self.assertEqual(
            [c["timestamp"] for c in mysql.img_calls],
            [datetime(2026, 8, 1, 13, 0), datetime(2026, 8, 1, 14, 0)],
        )

    @async_test
    async def test_a05_video_message_not_parse_failure(self):
        # 视频/表情等无 text/image 段的消息是正常历史内容：parse 返回 None 但
        # 不计入「解析失败」warning；含文本但 time 缺失才计 1 条真失败
        _STUB_LOGGER.clear()
        window_start = datetime(2026, 8, 1, 12, 0)
        video = {
            "time": int(datetime(2026, 8, 1, 12, 30).timestamp()),
            "message_id": "m_video",
            "message_seq": 3,
            "sender": {"user_id": "u1", "nickname": "甲"},
            "message": [{"type": "video", "data": {"file": "a.mp4"}}],
        }
        ok = _raw_text("m_ok", "正常", datetime(2026, 8, 1, 13, 0), seq=2)
        bad_time = {
            "message_id": "m_bad",
            "message_seq": 1,
            "sender": {"user_id": "u1", "nickname": "甲"},
            "message": [{"type": "text", "data": {"text": "time 缺失"}}],
        }
        client = _FakeClientAPI({}, latest=[video, ok, bad_time])
        ctx = _FakeContext([_FakeInst(types.SimpleNamespace(api=client))])
        mysql = _FakeMySQLMgr()
        bf = ReloadBackfill(ctx, mysql, _FakeConfigMgr())

        result = await bf._backfill_group(
            "111", 3, window_start, DEFAULT_ROUND_CAP, DEFAULT_MAX_ROUNDS
        )

        self.assertEqual(
            result["pulled"], 1
        )  # 仅 m_ok 入流程（video/bad_time 均 None）
        self.assertEqual(result["inserted_text"], 1)
        self.assertIn(
            "有 1 条含文本/图片的消息解析失败",
            " | ".join(_STUB_LOGGER.records["warning"]),
        )

    @async_test
    async def test_a05_local_path_image_not_parse_failure(self):
        # 图片 url 为本地路径（NapCat 历史图片常见）→ 无可提取 http 链接，正常跳过，
        # 不计入「解析失败」warning；文本消息正常入库
        _STUB_LOGGER.clear()
        window_start = datetime(2026, 8, 1, 12, 0)
        local_img = {
            "time": int(datetime(2026, 8, 1, 12, 30).timestamp()),
            "message_id": "m_img",
            "message_seq": 2,
            "sender": {"user_id": "u1", "nickname": "甲"},
            "message": [
                {
                    "type": "image",
                    "data": {"url": "C:/xx/1.image", "file": "C:/xx/1.image"},
                }
            ],
        }
        ok = _raw_text("m_ok", "正常", datetime(2026, 8, 1, 13, 0), seq=1)
        client = _FakeClientAPI({}, latest=[local_img, ok])
        ctx = _FakeContext([_FakeInst(types.SimpleNamespace(api=client))])
        mysql = _FakeMySQLMgr()
        bf = ReloadBackfill(ctx, mysql, _FakeConfigMgr())

        result = await bf._backfill_group(
            "111", 2, window_start, DEFAULT_ROUND_CAP, DEFAULT_MAX_ROUNDS
        )

        self.assertEqual(result["pulled"], 1)  # 仅 m_ok；local_img 无可提取内容跳过
        self.assertEqual(result["inserted_text"], 1)
        self.assertNotIn(
            "解析失败",
            " | ".join(_STUB_LOGGER.records["warning"]),
            "本地路径图片消息不应误报解析失败",
        )

    @async_test
    async def test_a07_napcat_direction_flips_to_reverse_order(self):
        # NapCat 语义：round 1 无 message_seq → 最新 40 条（time 60..99）；round 2 首个
        # message_seq 轮次默认返回比锚点更新的消息（退回同一页）→ 探测到仅含更新即翻转
        # reverse_order=true 重试取更旧；翻页锚点取本轮最旧（最小 time）消息的短 ID
        # （反序短 ID 时非最小短 ID）
        window_start = datetime(2026, 8, 1, 12, 0)
        count = 99
        # time i 的短 ID = 100-i（反序：最旧消息的短 ID 最大，最小短 ID 反而是最新）
        short_ids = [count + 1 - i for i in range(1, count + 1)]
        msgs = _napcat_raw_messages(count, datetime(2026, 8, 7, 0, 0), short_ids)
        client = _NapCatClient(msgs, page_size=40)
        ctx = _FakeContext([_FakeInst(types.SimpleNamespace(api=client))])
        mysql = _FakeMySQLMgr()
        bf = ReloadBackfill(ctx, mysql, _FakeConfigMgr())

        result = await bf._backfill_group("111", 1, window_start, 40, 5)

        # round 1 无 message_seq/无 reverse_order；round 2 探测无 reverse_order →
        # 翻转后重试带 reverse_order=true；翻页锚点短 ID 依次 40（time60）→ 79（time21）
        self.assertEqual(
            [c.get("reverse_order") for c in client.calls], [None, None, True, True]
        )
        self.assertEqual(
            [c.get("message_seq") for c in client.calls], [None, 40, 40, 79]
        )
        self.assertEqual(result["pulled"], count)
        self.assertEqual(result["inserted_text"], count)

    @async_test
    async def test_a08_standard_protocol_keeps_no_reverse_order(self):
        # go-cqhttp/Lagrange 风格：round 1 最新页，round 2 起向旧翻页，探测判定为
        # 标准语义 → 全程不传 reverse_order（无回归）
        window_start = datetime(2026, 8, 1, 12, 0)
        msgs = _bulk_raw_messages(3000, "m", datetime(2026, 8, 7, 0, 0), seq_start=1)
        client = _BulkClient(msgs, page_size=1000)
        ctx = _FakeContext([_FakeInst(types.SimpleNamespace(api=client))])
        mysql = _FakeMySQLMgr()
        bf = ReloadBackfill(ctx, mysql, _FakeConfigMgr())

        result = await bf._backfill_group("111", 3000, window_start, 1000, 5)

        self.assertEqual(
            [c.get("reverse_order") for c in client.calls], [None, None, None, None]
        )
        self.assertEqual(
            [c.get("message_seq") for c in client.calls], [None, 2001, 1001, 1]
        )
        self.assertEqual(result["pulled"], 3000)
        self.assertEqual(result["inserted_text"], 3000)

    @async_test
    async def test_a11_round1_latest_view_covers_window_gap(self):
        # 生产场景复现：窗口起始后、激活消息前有停机缺口消息（"1+1+1+1"/"白泽"）。
        # round 1 不传 message_seq 走最新视图，天然包含激活消息与缺口消息，一并入库，
        # 不再被 NapCat「message_seq 默认返回更新消息」的方向偏差影响（曾只拉激活 1 条）
        window_start = datetime(2026, 8, 10, 2, 59, 27)
        gap1 = _raw_text("g1", "1+1+1+1", datetime(2026, 8, 10, 2, 59, 32), seq=30)
        gap2 = _raw_text("g2", "白泽", datetime(2026, 8, 10, 2, 59, 47), seq=40)
        activation = _raw_text("act", "2", datetime(2026, 8, 10, 2, 59, 59), seq=50)
        client = _NapCatClient([gap1, gap2, activation], page_size=200)
        ctx = _FakeContext([_FakeInst(types.SimpleNamespace(api=client))])
        mysql = _FakeMySQLMgr()
        bf = ReloadBackfill(ctx, mysql, _FakeConfigMgr())

        result = await bf._backfill_group("111", 50, window_start, 200, 5)

        self.assertEqual([c.get("message_seq") for c in client.calls], [None])
        self.assertEqual(result["pulled"], 3)
        self.assertEqual(result["inserted_text"], 3)
        self.assertEqual(
            sorted(c["message_id"] for c in mysql.chat_calls), ["act", "g1", "g2"]
        )


class TestSummaryHistory(unittest.TestCase):
    """core/summary/onebot.py 方向适配（A-9/A-10）：NapCat 端首个 message_seq
    轮次探测到返回更新消息即翻转 reverse_order=true 重试；标准端多轮不传
    reverse_order 无回归。"""

    @async_test
    async def test_a09_summary_napcat_flips_to_reverse_order(self):
        # 2000 条纯文本，短 ID 反序（time i → 短 ID 2001-i）；target=1100 →
        # round 1 最新 1000 条，round 2 首个 message_seq 探测返回更新（同一页）
        # → 翻转 reverse_order=true 重试取到更旧消息
        count = 2000
        short_ids = [count + 1 - i for i in range(1, count + 1)]
        msgs = _napcat_raw_messages(count, datetime(2026, 8, 7, 0, 0), short_ids)
        client = _NapCatClient(msgs, page_size=1000)
        event = types.SimpleNamespace(bot=types.SimpleNamespace(api=client))

        result = await fetch_group_history(event, "111", 1100)

        # round 1 无 message_seq/无 reverse_order；round 2 探测无 reverse_order，
        # 翻转后重试带 reverse_order=true；锚点 = round 1 最旧（time1001）短 ID 1000
        self.assertEqual(
            [c.get("reverse_order") for c in client.calls], [None, None, True]
        )
        self.assertEqual(
            [c.get("message_seq") for c in client.calls], [None, 1000, 1000]
        )
        self.assertEqual(len(result), 1100)
        self.assertEqual(result[0].content, "文本901")
        self.assertEqual(result[-1].content, "文本2000")

    @async_test
    async def test_a10_summary_standard_keeps_no_reverse_order(self):
        # go-cqhttp/Lagrange 风格：round 1 最新页，round 2 起向旧翻页且全程不传
        # reverse_order（无回归）
        msgs = _bulk_raw_messages(3000, "m", datetime(2026, 8, 7, 0, 0), seq_start=1)
        client = _StandardSummaryClient(msgs, page_size=1000)
        event = types.SimpleNamespace(bot=types.SimpleNamespace(api=client))

        result = await fetch_group_history(event, "111", 1100)

        self.assertEqual([c.get("reverse_order") for c in client.calls], [None, None])
        self.assertEqual([c.get("message_seq") for c in client.calls], [None, 2001])
        self.assertEqual(len(result), 1100)


# ============================================================
# 五、B 组：门控缓冲与去重 flush（core/saver.py）
# ============================================================


class _RecordingSaver:
    """MessageSaver 门控替身：记录 begin/end_backfill 调用（按群）。"""

    def __init__(self, initialized: bool = True):
        self.initialized = initialized
        self.begun: list[str] = []
        self.ended: list[str] = []

    @property
    def is_initialized(self) -> bool:
        return self.initialized

    def begin_backfill(self, group_id: str):
        self.begun.append(group_id)

    async def end_backfill(self, group_id: str) -> int:
        self.ended.append(group_id)
        return 0


class _RecordingStats:
    """StatsService 替身：记录 startup_backfill 触发次数（F6 收尾链）。"""

    def __init__(self):
        self.calls = 0

    async def startup_backfill(self):
        self.calls += 1


class TestSaverGate(unittest.TestCase):
    """B 组：按群门控缓冲 / 去重 flush / 收尾链 / 容量。"""

    @async_test
    async def test_b01_gating_per_group(self):
        # begin_backfill("111") 后仅群 111 缓冲；群 222 正常实时落库（按群门控）
        mysql = _FakeMySQLMgr()
        saver = MessageSaver(mysql, _FakeGatingConfig())
        saver.set_initialized()
        saver.begin_backfill("111")
        await saver.handle_group_message(
            _StubEvent([Comp.Plain("111缓冲")], "m_g1", group_id="111")
        )
        await saver.handle_group_message(
            _StubEvent([Comp.Plain("222实时")], "m_g2", group_id="222")
        )
        self.assertEqual([c["message_id"] for c in mysql.chat_calls], ["m_g2"])
        self.assertTrue(saver.has_pending)
        n = await saver.end_backfill("111")
        self.assertEqual(n, 1)
        self.assertEqual([c["message_id"] for c in mysql.chat_calls], ["m_g2", "m_g1"])
        # 门控解除后群 111 恢复实时落库
        await saver.handle_group_message(
            _StubEvent([Comp.Plain("111实时")], "m_g3", group_id="111")
        )
        self.assertEqual(len(mysql.chat_calls), 3)

    @async_test
    async def test_b01_backfill_group_finally_calls_end_backfill(self):
        # _backfill_group 收尾链（F3/F6）：完成后调用 saver.end_backfill(该群)
        client = _FakeClientAPI({})
        ctx = _FakeContext([_FakeInst(types.SimpleNamespace(api=client))])
        saver = _RecordingSaver()
        bf = ReloadBackfill(ctx, _FakeMySQLMgr(), _FakeConfigMgr(), saver=saver)
        result = await bf._backfill_group(
            "111", 300, datetime(2026, 8, 1, 12, 0), 200, 5
        )
        self.assertEqual(result["pulled"], 0)
        self.assertEqual(saver.ended, ["111"])

    @async_test
    async def test_b01_stop_cancel_still_flushes(self):
        # terminate 取消补库任务（R1）：任务进入协议端调用（sleep 挂起）后被取消，
        # finally 仍执行 end_backfill 保全缓冲数据
        entered = asyncio.Event()

        class _BlockingClient:
            async def call_action(self, action, **kwargs):
                entered.set()
                await asyncio.sleep(3600)
                return {"messages": []}

        ctx = _FakeContext([_FakeInst(types.SimpleNamespace(api=_BlockingClient()))])
        saver = _RecordingSaver()
        bf = ReloadBackfill(ctx, _FakeMySQLMgr(), _FakeConfigMgr(), saver=saver)
        event = types.SimpleNamespace(
            get_group_id=lambda: "111",
            message_obj=types.SimpleNamespace(raw_message={"message_seq": 100}),
        )
        await bf.maybe_trigger(event)
        self.assertEqual(saver.begun, ["111"], "触发应先 begin_backfill 门控")
        await entered.wait()  # 等任务真正进入协议端挂起点后再取消（确定性）
        await bf.stop()
        self.assertEqual(saver.ended, ["111"], "取消后 finally 仍应 end_backfill")

    @async_test
    async def test_b02_flush_dedup_skips_existing_id_filters_url(self):
        # 去重 flush：已存在 message_id 整条跳过；按群 flush 只处理目标群缓冲
        mysql = _FakeMySQLMgr(existing_ids={"m_dup"})
        saver = MessageSaver(mysql, _FakeGatingConfig())
        saver.set_initialized()
        saver.begin_backfill("111")
        saver.begin_backfill("222")
        await saver.handle_group_message(
            _StubEvent([Comp.Plain("重复")], "m_dup", group_id="111")
        )
        await saver.handle_group_message(
            _StubEvent([Comp.Plain("111新")], "m_new", group_id="111")
        )
        await saver.handle_group_message(
            _StubEvent([Comp.Plain("222未完成")], "m_222", group_id="222")
        )
        # end_backfill("111") 只 flush 群 111：m_dup 跳过、m_new 落库；群 222 缓冲保留
        n = await saver.end_backfill("111")
        self.assertEqual(n, 1)
        self.assertEqual([c["message_id"] for c in mysql.chat_calls], ["m_new"])
        self.assertTrue(saver.has_pending, "群 222 缓冲不应被 flush")
        n2 = await saver.end_backfill("222")
        self.assertEqual(n2, 1)

    @async_test
    async def test_b02_flush_no_dedup_legacy_behavior(self):
        # dedup=False（初始化窗口）行为不变：逐条落库、无查重、空缓冲返回 0
        mysql = _FakeMySQLMgr()
        saver = MessageSaver(mysql, _FakeGatingConfig())
        saver.set_initialized()
        saver.begin_backfill("111")
        await saver.handle_group_message(
            _StubEvent([Comp.Plain("缓冲1")], "m1", group_id="111")
        )
        await saver.flush_pending()
        self.assertEqual([c["message_id"] for c in mysql.chat_calls], ["m1"])
        self.assertFalse(saver.has_pending)
        self.assertEqual(await saver.flush_pending(), 0)
        self.assertEqual(await saver.flush_pending(dedup=True), 0)

    def test_b03_buffer_maxlen_is_5000(self):
        # 缓冲容量（R3）
        saver = MessageSaver(_FakeMySQLMgr(), _FakeGatingConfig())
        self.assertEqual(saver._pending_records.maxlen, 5000)


class TestParsing(unittest.TestCase):
    """C-1/2：parse_onebot_raw_message 文本拼接与 time 防御。"""

    def test_c01_multi_text_joined_with_newline(self):
        # 多段文本以 "\n" 拼接（与实时路径 core/saver.py 口径一致，F5）
        raw = {
            "time": 1700000000,
            "message_id": "m1",
            "sender": {"user_id": 1, "nickname": "甲"},
            "message": [
                {"type": "text", "data": {"text": "第一段"}},
                {"type": "text", "data": {"text": "第二段"}},
            ],
        }
        item = parse_onebot_raw_message(raw, "111")
        self.assertEqual(item["text"], "第一段\n第二段")

    def test_c02_time_missing_or_invalid_returns_none_with_warning(self):
        base = {
            "message_id": "m1",
            "sender": {"user_id": 1, "nickname": "甲"},
            "message": [{"type": "text", "data": {"text": "hi"}}],
        }
        # 缺 time：返回 None 且记 warning（不抛异常，F8）
        _STUB_LOGGER.clear()
        self.assertIsNone(parse_onebot_raw_message(dict(base), "111"))
        self.assertTrue(
            any("缺 time" in m or "非法" in m for m in _STUB_LOGGER.records["warning"]),
            f"缺 time 应记 warning: {_STUB_LOGGER.records['warning']}",
        )
        # 非数值 time：同样返回 None + warning
        _STUB_LOGGER.clear()
        raw = dict(base)
        raw["time"] = "abc"
        self.assertIsNone(parse_onebot_raw_message(raw, "111"))
        self.assertTrue(
            any("缺 time" in m or "非法" in m for m in _STUB_LOGGER.records["warning"]),
            f"非数值 time 应记 warning: {_STUB_LOGGER.records['warning']}",
        )
        # 正常 time 正常解析
        _STUB_LOGGER.clear()
        raw = dict(base)
        raw["time"] = 1700000000
        item = parse_onebot_raw_message(raw, "111")
        self.assertIsNotNone(item)
        self.assertEqual(item["timestamp"], datetime.fromtimestamp(1700000000))


class TestBackfillTrigger(unittest.TestCase):
    """C 组：maybe_trigger 消息触发式补库（起点 seq / 幂等 / 未就绪 / 窗口 / 节流）。"""

    def _event(self, group_id="111", seq=3000):
        return types.SimpleNamespace(
            get_group_id=lambda: group_id,
            message_obj=types.SimpleNamespace(raw_message={"message_seq": seq}),
        )

    @async_test
    async def test_c01_trigger_once_per_group_with_start_seq(self):
        # 同群两条消息只触发一次补库；round 1 不传 message_seq（最新视图），为空时
        # 回退用触发消息真实 seq 作锚点 + reverse_order=true；完成后 end_backfill
        client = _FakeClientAPI({})
        ctx = _FakeContext([_FakeInst(types.SimpleNamespace(api=client))])
        saver = _RecordingSaver()
        bf = ReloadBackfill(ctx, _FakeMySQLMgr(), _FakeConfigMgr(), saver=saver)
        await bf.maybe_trigger(self._event(seq=3000))
        await bf.maybe_trigger(self._event(seq=3000))  # 第二次不触发
        self.assertEqual(saver.begun, ["111"])
        for t in list(bf._tasks):
            await t
        # round 1 无 message_seq；最新视图为空 → 回退锚点 = 触发消息真实 seq
        self.assertIsNone(client.calls[0].get("message_seq"))
        self.assertEqual(client.calls[1]["message_seq"], 3000)
        self.assertTrue(client.calls[1]["reverse_order"])
        self.assertEqual(saver.ended, ["111"])
        self.assertEqual(len(bf._backfilled_groups), 1)

    @async_test
    async def test_c02_mysql_not_ready_skips_without_mark(self):
        # MySQL 未就绪不触发、不标记（后续消息再试）
        saver = _RecordingSaver(initialized=False)
        bf = ReloadBackfill(
            _FakeContext(), _FakeMySQLMgr(), _FakeConfigMgr(), saver=saver
        )
        await bf.maybe_trigger(self._event())
        self.assertEqual(saver.begun, [])
        self.assertEqual(bf._backfilled_groups, set())

    @async_test
    async def test_c02_no_message_seq_skips_and_marks(self):
        # 无 message_seq 无法定位起点：跳过并标记（避免每次消息都尝试）
        _STUB_LOGGER.clear()
        saver = _RecordingSaver()
        bf = ReloadBackfill(
            _FakeContext(), _FakeMySQLMgr(), _FakeConfigMgr(), saver=saver
        )
        event = types.SimpleNamespace(
            get_group_id=lambda: "111",
            message_obj=types.SimpleNamespace(raw_message={}),
        )
        await bf.maybe_trigger(event)
        self.assertEqual(saver.begun, [])
        self.assertIn("111", bf._backfilled_groups)
        self.assertTrue(
            any("无 message_seq" in m for m in _STUB_LOGGER.records["warning"])
        )

    @async_test
    async def test_c03_snapshot_throttled(self):
        # 快照回填节流（F6）：60s 内多次补库完成只触发一次
        stats = _RecordingStats()
        bf = ReloadBackfill(
            _FakeContext(), _FakeMySQLMgr(), _FakeConfigMgr(), stats_service=stats
        )
        await bf._maybe_snapshot_backfill()
        await bf._maybe_snapshot_backfill()
        self.assertEqual(stats.calls, 1)

    @async_test
    async def test_c03_window_from_last_record_minus_overlap(self):
        # 窗口起点 v0.8.0：有已记录消息 → 最后记录时间 − BACKFILL_OVERLAP_MINUTES
        now = datetime.now()
        last_rec = now - timedelta(hours=6)
        mysql = _FakeMySQLMgr(last_message_time=last_rec)
        bf = ReloadBackfill(_FakeContext(), mysql, _FakeConfigMgr())
        ws = await bf._compute_window_start("111")
        expected = last_rec - timedelta(minutes=BACKFILL_OVERLAP_MINUTES)
        self.assertAlmostEqual(ws.timestamp(), expected.timestamp(), delta=1)

    @async_test
    async def test_c03_no_record_falls_back_to_hours(self):
        # 群无任何已记录消息 → 回退 now - backfill_hours（默认 12h）
        now = datetime.now()
        bf = ReloadBackfill(_FakeContext(), _FakeMySQLMgr(), _FakeConfigMgr())
        ws = await bf._compute_window_start("111")
        self.assertGreaterEqual(ws, now - timedelta(hours=12, minutes=1))
        self.assertLessEqual(ws, now)

    @async_test
    async def test_c03_force_hours_window(self):
        # 强制补库显式 hours → now - hours（v0.8.0 force 指令；不再依赖 last_terminate_time）
        now = datetime.now()
        bf = ReloadBackfill(_FakeContext(), _FakeMySQLMgr(), _FakeConfigMgr())
        ws = await bf._compute_window_start(None, hours=3)
        self.assertGreaterEqual(ws, now - timedelta(hours=3, minutes=1))
        self.assertLessEqual(ws, now - timedelta(hours=2, minutes=59))

    @async_test
    async def test_c04_force_backfill_bypasses_once_per_restart(self):
        # /补库 强制补库（v0.8.0）：绕过「每群每重启一次」，本群已自动补过也能再触发
        client = _FakeClientAPI({})
        ctx = _FakeContext([_FakeInst(types.SimpleNamespace(api=client))])
        saver = _RecordingSaver()
        bf = ReloadBackfill(ctx, _FakeMySQLMgr(), _FakeConfigMgr(), saver=saver)
        bf._backfilled_groups.add("111")  # 模拟本群本重启周期已自动补过
        started = await bf.force_backfill("111")
        self.assertTrue(started)
        self.assertIn("111", saver.begun)
        for t in list(bf._tasks):
            await t
        self.assertIn("111", saver.ended)
        self.assertNotIn("111", bf._active_groups)

    @async_test
    async def test_c04_force_backfill_skips_when_active(self):
        # 该群补库已在跑（force 与自动触发并发）→ 第二个 force 返回 False 不重复
        entered = asyncio.Event()

        class _BlockingClient:
            async def call_action(self, action, **kwargs):
                entered.set()
                await asyncio.sleep(3600)
                return {"messages": []}

        ctx = _FakeContext([_FakeInst(types.SimpleNamespace(api=_BlockingClient()))])
        saver = _RecordingSaver()
        bf = ReloadBackfill(ctx, _FakeMySQLMgr(), _FakeConfigMgr(), saver=saver)
        started = await bf.force_backfill("111")
        self.assertTrue(started)
        self.assertIn("111", bf._active_groups)
        # 任务仍运行时再 force：拒绝且不重复 begin_backfill
        second = await bf.force_backfill("111")
        self.assertFalse(second)
        self.assertEqual(saver.begun.count("111"), 1)
        await bf.stop()
        self.assertEqual(saver.ended, ["111"])  # 收尾链仍执行


class TestBackfillHoursValidation(unittest.TestCase):
    """D-1：api_save_settings backfill_hours 校验（F9）。"""

    def setUp(self):
        self.storage_mod = importlib.import_module(_PKG + ".core.webapi.storage")
        self.writes: dict[str, str] = {}

        class _Cfg:
            def __init__(self, writes):
                self.writes = writes

            async def set_setting(self, key, value):
                self.writes[key] = value

            async def get_all_settings(self):
                return dict(self.writes)

        class _Ctx:
            def register_web_api(self, *a, **k):
                pass

        self.api = WebAPI(_Ctx(), None, _Cfg(self.writes), None)

    def tearDown(self):
        self.storage_mod.request = None

    def _set_payload(self, payload):
        class _Req:
            def __init__(self, payload):
                self._payload = payload

            async def json(self, default=None):
                return self._payload

        self.storage_mod.request = _Req(payload)

    @async_test
    async def test_d01_int_and_numeric_string_pass(self):
        for good in (12, "12"):
            self.writes.clear()
            self._set_payload({"backfill_hours": good})
            resp = await self.api.api_save_settings()
            self.assertIn("json", resp, f"backfill_hours={good!r} 应通过校验")
            self.assertEqual(self.writes, {"backfill_hours": "12"})

    @async_test
    async def test_d01_float_bool_string_rejected_zero_write(self):
        for bad in (12.9, True, "abc"):
            self.writes.clear()
            self._set_payload({"backfill_hours": bad})
            resp = await self.api.api_save_settings()
            self.assertIn("error", resp, f"backfill_hours={bad!r} 应 400 拒绝")
            self.assertEqual(self.writes, {}, f"backfill_hours={bad!r} 应零写入")


class TestCircularImportAndVersion(unittest.TestCase):
    """D-2/D-3：循环导入消除 + 版本号与接线。"""

    def test_d02_direct_pool_import_and_constants(self):
        # R4：from core.db_mysql.pool import DynamicPool 直连导入通过
        pool_mod = importlib.import_module(_PKG + ".core.db_mysql.pool")
        self.assertIs(pool_mod.DynamicPool, DynamicPool)
        self.assertEqual(
            pool_mod.CREATE_RETRY_BACKOFF_SECONDS, CREATE_RETRY_BACKOFF_SECONDS
        )
        self.assertEqual(
            pool_mod.RESET_PENDING_WAIT_SECONDS, RESET_PENDING_WAIT_SECONDS
        )
        # base 从 pool 再导出同一对象（消除循环导入；包级导出路径保持不变）
        base_mod = importlib.import_module(_PKG + ".core.db_mysql.base")
        self.assertIs(
            base_mod.CREATE_RETRY_BACKOFF_SECONDS, pool_mod.CREATE_RETRY_BACKOFF_SECONDS
        )
        self.assertIs(
            base_mod.RESET_PENDING_WAIT_SECONDS, pool_mod.RESET_PENDING_WAIT_SECONDS
        )
        pkg = importlib.import_module(_PKG + ".core.db_mysql")
        for name in (
            "MySQLManager",
            "DynamicPool",
            "CREATE_RETRY_BACKOFF_SECONDS",
            "RESET_PENDING_WAIT_SECONDS",
        ):
            self.assertIn(name, pkg.__all__)
        self.assertIs(pkg.MySQLManager, MySQLManager)

    def test_d03_version_and_wiring(self):
        # v0.9.0 漂移同步：@register/metadata 版本文本随版本演进（0.8.0 → 0.9.0）
        main = importlib.import_module(_PKG + ".main")
        self.assertEqual(main.GroupHistoryPlugin._registered[3], "0.9.0")
        meta = (_PLUGIN_ROOT / "metadata.yaml").read_text(encoding="utf-8")
        self.assertIn("version: v0.9.0", meta)
        # ReloadBackfill 构造注入 saver / stats_service
        sig = inspect.signature(ReloadBackfill.__init__)
        self.assertIn("saver", sig.parameters)
        self.assertIn("stats_service", sig.parameters)
        main_src = (_PLUGIN_ROOT / "main.py").read_text(encoding="utf-8")
        # v0.8.1 装配下沉 core/bootstrap.py：ReloadBackfill 构造注入 saver /
        # stats_service 的接线点移至 bootstrap（行为不变，断言随演进）
        bootstrap_src = (_PLUGIN_ROOT / "core" / "bootstrap.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("saver=self.saver", bootstrap_src)
        self.assertIn("stats_service=self.stats_service", bootstrap_src)
        self.assertIn('filter.command("补库")', main_src)
        # MessageSaver 门控与去重 flush 方法面
        self.assertTrue(callable(getattr(MessageSaver, "begin_backfill", None)))
        self.assertTrue(callable(getattr(MessageSaver, "end_backfill", None)))
        fp_sig = inspect.signature(MessageSaver.flush_pending)
        self.assertIn("dedup", fp_sig.parameters)
        # v0.8.0 强制补库 / 窗口方法面
        self.assertTrue(callable(getattr(ReloadBackfill, "force_backfill", None)))
        backfill_src = (_PLUGIN_ROOT / "core" / "backfill.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("get_last_message_time", backfill_src)
        self.assertEqual(BACKFILL_OVERLAP_MINUTES, 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
