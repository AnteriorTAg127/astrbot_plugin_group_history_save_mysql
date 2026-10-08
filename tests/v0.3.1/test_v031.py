# ruff: noqa: I001
"""astrbot_plugin_group_history_save_mysql v0.3.1 离线单元测试。

覆盖 v0.3.1 两个新功能的可离线验证行为：

- F1 备用模型降级链（summary/summarizer.py）：
  「主选 summary_provider_id → 备用列表 summary_fallback_providers → 会话 provider」
  三段按序降级；任一节点调用异常或返回空文本即降级下一节点；会话 provider
  惰性解析（主选成功时零查询）；链构建过滤非法条目并保序去重；
  三段皆空抛 SummaryProviderError；整链耗尽按末次失败类型原样上抛
  （异常原样 / 空文本 RuntimeError）。
- F2 触发反馈（summary/service.py）：
  summary_feedback_mode = reaction（默认，经 OneBot 扩展 action set_msg_emoji_like
  贴 👍，四种失败路径降级文字）/ text（文案可配，清空回退内置默认）/ none；
  未知值仅 warning 不发消息；反馈失败绝不冒泡、绝不阻断总结主流程。

运行方式（插件根目录）：
    python -m pytest "tests/v0.3.1/test_v031.py" -v

范式沿用 tests/v0.3/test_v03.py（不 import 它，fake 自带）：异步用例用内置
async_test 装饰器经 asyncio.run 驱动（不依赖 pytest-asyncio / conftest.py）；
astrbot.* 全部 sys.modules stub，且 stub 必须在 import 被测包之前完成。
"""

import asyncio
import functools
import os
import sys
import tempfile
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


# ---- astrbot.api: logger（带记录能力，供 warning/error 断言）----
class _StubLogger:
    """记录各级日志内容，便于断言（如降级 warning / 反馈 warning）。"""

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


# ---- astrbot.api.message_components: Plain / Image / Node / Nodes ----
class _StubPlain:
    def __init__(self, text=""):
        self.text = text


class _StubImage:
    def __init__(self, url=None, file=None):
        self.url = url
        self.file = file


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


# ---- astrbot.api.event: AstrMessageEvent / MessageChain ----
class _StubAstrMessageEvent:
    pass


class _StubMessageChain:
    """MessageChain 替身：仅承载 chain 列表（MessageEventResult 亦继承它）。"""

    def __init__(self, chain=None):
        self.chain = chain or []


_astrbot_event.AstrMessageEvent = _StubAstrMessageEvent
_astrbot_event.MessageChain = _StubMessageChain


# ---- astrbot.api.star: Context / Star / StarTools ----
class _StubContext:
    pass


class _StubStar:
    pass


_TEMP_DATA_DIR = tempfile.mkdtemp(prefix="astrbot_hist_test_v031_")


class _StubStarTools:
    @classmethod
    def get_data_dir(cls, plugin_name=None):
        return Path(_TEMP_DATA_DIR) / (plugin_name or "test")


_astrbot_star.Context = _StubContext
_astrbot_star.Star = _StubStar
_astrbot_star.StarTools = _StubStarTools

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

# 让被测包可被导入：<plugins 目录> 加入 sys.path
_PLUGINS_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..")
)
if _PLUGINS_DIR not in sys.path:
    sys.path.insert(0, _PLUGINS_DIR)

# ============================================================
# 二、导入被测代码（stub 已就位）
# ============================================================

from astrbot_plugin_group_history_save_mysql.core.db_config import ConfigManager  # noqa: E402
from astrbot_plugin_group_history_save_mysql.core.summary import fetcher as fetcher_mod  # noqa: E402
from astrbot_plugin_group_history_save_mysql.core.summary import service as service_mod  # noqa: E402
from astrbot_plugin_group_history_save_mysql.core.summary.models import ChatMessage  # noqa: E402
from astrbot_plugin_group_history_save_mysql.core.summary.service import SummaryService  # noqa: E402
from astrbot_plugin_group_history_save_mysql.core.summary.summarizer import (  # noqa: E402
    Summarizer,
    SummaryProviderError,
)

MessageChain = _StubMessageChain

# 降级链测试的统一成功文本（无板块关键词，板块解析走单段兜底，不影响断言）
CHAIN_OK_TEXT = "总结成功：这是一段用于降级链测试的纯文本。"

# 4 板块 LLM 输出文本（service 端到端用例复用，走标准板块解析路径）
FOUR_SECTION_LLM_TEXT = """📢 重要通知与结论
- 通知 A：周五发布

💬 讨论要点 / 争议
- 讨论 B：方案选型

🎉 有趣片段
- 片段 C：表情包大战

✅ TODO / 待跟进
- 任务 D：补充文档"""

# 内置默认反馈文案（与 service._DEFAULT_FEEDBACK_TEXT 一致，供断言）
DEFAULT_FEEDBACK_TEXT = "📝 收到！正在总结中，请稍候…"


class _ProviderDownError(Exception):
    """自定义异常：断言降级链耗尽时「最后遇到的异常原样上抛」。"""


# ============================================================
# 三、测试辅助（假基础设施，自包含，不从 test_v03.py import）
# ============================================================


class FakeSummaryConfig:
    """ConfigManager 内存替身（覆盖总结功能用到的全部读取接口）。

    以真实 SUMMARY_DEFAULTS 为底（含 v0.3.1 新增 3 项共 19 项），typed 读取
    复用生产侧 ConfigManager._convert_summary_value 转换逻辑，保证假件行为
    与真实配置层一致；``typed_overrides`` 可直接指定 typed 返回值（模拟
    「typed 返回非 list」等异常形状，绕过正常转换路径）。
    """

    def __init__(self, overrides=None, typed_overrides=None):
        self.store = dict(ConfigManager.SUMMARY_DEFAULTS)
        if overrides:
            self.store.update(overrides)
        self.typed_overrides = dict(typed_overrides or {})

    async def get_summary_setting(self, key, default=None):
        if key in self.store:
            return self.store[key]
        fallback = ConfigManager.SUMMARY_DEFAULTS.get(key, "")
        return default if default is not None else fallback

    async def get_summary_setting_typed(self, key):
        if key in self.typed_overrides:
            return self.typed_overrides[key]
        raw = await self.get_summary_setting(key)
        target = ConfigManager.SUMMARY_TYPES.get(key, str)
        try:
            return ConfigManager._convert_summary_value(raw, target)
        except Exception:
            default_raw = ConfigManager.SUMMARY_DEFAULTS.get(key, "")
            return ConfigManager._convert_summary_value(default_raw, target)

    async def get_ignore_senders(self, group_id):
        """忽略名单恒为空（本套件不测过滤逻辑，v0.3 套件已覆盖）。"""
        return []


class ChainLLMContext:
    """Context 替身：llm_generate 按 provider id 剧本化返回。

    behaviors: {provider_id: (kind, value)}
        - ("text", 文本)   → 返回 completion_text=文本
        - ("empty", None)  → 返回 completion_text 为空白串（模拟空文本失败）
        - ("exc", 异常实例) → 抛出该异常（模拟调用失败）
    未登记的 provider 走默认行为（返回 default_text）。
    get_current_chat_provider_id 返回可配值或抛异常，并记录查询次数。
    """

    def __init__(
        self,
        behaviors=None,
        chat_provider="chat-prov",
        chat_provider_exc=None,
        default_text=CHAIN_OK_TEXT,
    ):
        self.behaviors = behaviors or {}
        self._chat_provider = chat_provider
        self._chat_provider_exc = chat_provider_exc
        self._default_text = default_text
        self.llm_calls = []
        self.provider_queries = []

    async def get_current_chat_provider_id(self, umo):
        self.provider_queries.append(umo)
        if self._chat_provider_exc is not None:
            raise self._chat_provider_exc
        return self._chat_provider

    async def llm_generate(self, chat_provider_id=None, prompt=None):
        self.llm_calls.append({"provider": chat_provider_id, "prompt": prompt})
        kind, value = self.behaviors.get(chat_provider_id, ("text", self._default_text))
        if kind == "exc":
            raise value
        if kind == "empty":
            return SimpleNamespace(completion_text="   ")
        return SimpleNamespace(completion_text=value)

    def called_providers(self):
        """实际调用序列（provider id 列表，含重复）。"""
        return [c["provider"] for c in self.llm_calls]


class SimpleLLMContext:
    """Context 替身（单一文本版）：service 端到端用例用。"""

    def __init__(self, llm_text=FOUR_SECTION_LLM_TEXT, chat_provider="chat-prov"):
        self._llm_text = llm_text
        self._chat_provider = chat_provider
        self.llm_calls = []
        self.provider_queries = []

    async def get_current_chat_provider_id(self, umo):
        self.provider_queries.append(umo)
        return self._chat_provider

    async def llm_generate(self, chat_provider_id=None, prompt=None):
        self.llm_calls.append({"provider": chat_provider_id, "prompt": prompt})
        return SimpleNamespace(completion_text=self._llm_text)


class FakeMySQLMgr:
    """MySQLManager 替身：query_messages 返回预制行。"""

    def __init__(self, rows=None, total=None):
        self.rows = rows or []
        self.total = len(self.rows) if total is None else total
        self.calls = []

    async def query_messages(self, **kwargs):
        self.calls.append(kwargs)
        return {"records": self.rows, "total": self.total}


class FakeOneBotFetch:
    """summary.onebot.fetch_group_history 替身（记录调用、默认返回空）。"""

    def __init__(self, msgs=None, exc=None):
        self.msgs = msgs or []
        self.exc = exc
        self.calls = []

    async def __call__(self, event, group_id, count):
        self.calls.append({"event": event, "group_id": group_id, "count": count})
        if self.exc is not None:
            raise self.exc
        return list(self.msgs)


class FakeStar:
    """Star 替身：text_to_image / html_render 最小实现（forward 模式不触达）。"""

    def __init__(self, t2i_url="https://render.example/sum.png"):
        self._t2i_url = t2i_url
        self.t2i_calls = []

    async def text_to_image(self, text, return_url=True):
        self.t2i_calls.append(text)
        return self._t2i_url

    async def html_render(self, tmpl, data, return_url=True, options=None):
        return None


class FakeBotAPI:
    """event.bot.api 替身：记录 call_action 调用，可配抛异常。"""

    def __init__(self, exc=None):
        self.exc = exc
        self.calls = []

    async def call_action(self, action, **kwargs):
        self.calls.append({"action": action, **kwargs})
        if self.exc is not None:
            raise self.exc
        return {"status": "ok"}


class FakeEvent:
    """AstrMessageEvent 替身：群/用户/bot 身份 + send 记录。

    v0.3.1 反馈相关扩展：
    - message_obj.message_id 可配（None / 非数字串 / 数字 / 数字串）；
      with_message_obj=False 时 message_obj 为 None
    - bot 三态："__auto__" 自动挂 FakeBotAPI(exc=api_exc) / None / 自定义（可无 api）
    - send_fail_on_plain=True 时仅对 plain_result 元组发送抛异常
      （模拟「反馈文字发送失败但消息链发送正常」）
    """

    def __init__(
        self,
        group_id="12345",
        sender_id="111",
        self_id="999",
        session_id="sess-1",
        umo="umo-1",
        message_id=12345,
        with_message_obj=True,
        bot="__auto__",
        api_exc=None,
        send_fail_on_plain=False,
    ):
        self._group_id = group_id
        self._sender_id = sender_id
        self._self_id = self_id
        self._session_id = session_id
        self.unified_msg_origin = umo
        self.message_obj = (
            SimpleNamespace(message_id=message_id) if with_message_obj else None
        )
        if bot == "__auto__":
            self.bot = SimpleNamespace(api=FakeBotAPI(exc=api_exc))
        else:
            self.bot = bot
        self._send_fail_on_plain = send_fail_on_plain
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
        if (
            self._send_fail_on_plain
            and isinstance(chain, tuple)
            and chain[:1] == ("plain",)
        ):
            raise RuntimeError("send backend down")
        self.sent.append(chain)

    # ---- 断言辅助 ----
    def plain_texts(self):
        return [c[1] for c in self.sent if isinstance(c, tuple) and c[0] == "plain"]

    def chains(self):
        return [c for c in self.sent if isinstance(c, MessageChain)]

    def api_calls(self):
        """bot.api.call_action 调用记录（bot/api 缺失时返回空列表）。"""
        api = getattr(self.bot, "api", None) if self.bot is not None else None
        return api.calls if api is not None else []


def cm(
    ts,
    sender="333",
    name="Carol",
    content="msg",
    gid="12345",
    mid="m-1",
    source="mysql",
):
    """构造 ChatMessage（降级链/统计用例的最小素材单元）。"""
    return ChatMessage(
        timestamp=ts,
        group_id=gid,
        sender_id=sender,
        sender_name=name,
        content=content,
        message_id=mid,
        source=source,
    )


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
    base = base or datetime(2026, 7, 31, 12, 0, 0)
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


def _new_warnings():
    """取本次用例期间新增的 warning 日志（切片，避免跨用例串扰）。"""
    return list(_STUB_LOGGER.records["warning"])


# ============================================================
# 四、TestProviderFallbackChain：summarizer 备用模型降级链（F1）
# ============================================================


class TestProviderFallbackChain:
    """以公开接口 summarize() 驱动，断言 context 的 LLM 调用序列与 provider_id。"""

    def _messages(self):
        """3 条消息的最小素材列表（时间升序）。"""
        base = datetime(2026, 7, 31, 10, 0, 0)
        return [
            cm(
                base + timedelta(minutes=i),
                sender="111",
                name="Alice",
                content=f"消息{i}",
                mid=f"m{i}",
            )
            for i in range(3)
        ]

    def _make(
        self,
        primary="",
        fallbacks_raw=None,
        typed_overrides=None,
        behaviors=None,
        chat_provider="chat-prov",
        chat_provider_exc=None,
    ):
        overrides = {}
        if primary:
            overrides["summary_provider_id"] = primary
        if fallbacks_raw is not None:
            overrides["summary_fallback_providers"] = fallbacks_raw
        config = FakeSummaryConfig(overrides=overrides, typed_overrides=typed_overrides)
        context = ChainLLMContext(
            behaviors=behaviors,
            chat_provider=chat_provider,
            chat_provider_exc=chat_provider_exc,
        )
        return Summarizer(context, config), context

    async def _summarize(self, summarizer, event=None):
        return await summarizer.summarize(
            event or FakeEvent(), self._messages(), "最近 3 条消息", "forward"
        )

    @async_test
    async def test_primary_success_no_session_query(self):
        """主选成功 → 调用序仅 [主选]，provider_id==主选，会话 provider 零查询（惰性）。"""
        summarizer, context = self._make(
            primary="p-main", behaviors={"p-main": ("text", CHAIN_OK_TEXT)}
        )
        result = await self._summarize(summarizer)
        assert context.called_providers() == ["p-main"]
        assert result.provider_id == "p-main"
        assert context.provider_queries == []  # 主选成功全程不查会话
        assert result.raw_llm_text == CHAIN_OK_TEXT

    @async_test
    async def test_primary_exception_fallback_success(self):
        """主选异常 → 备用成功：调用序 [主选, 备用]，provider_id==备用，会话零查询。"""
        summarizer, context = self._make(
            primary="p-main",
            fallbacks_raw='["p-back"]',
            behaviors={
                "p-main": ("exc", ValueError("主选限流")),
                "p-back": ("text", CHAIN_OK_TEXT),
            },
        )
        before = _new_warnings()
        result = await self._summarize(summarizer)
        assert context.called_providers() == ["p-main", "p-back"]
        assert result.provider_id == "p-back"
        assert context.provider_queries == []
        # 非末尾节点失败记 warning（含 provider id + 失败原因）
        new_warn = "\n".join(_new_warnings()[len(before) :])
        assert "降级下一节点" in new_warn
        assert "provider=p-main" in new_warn
        assert "ValueError" in new_warn and "主选限流" in new_warn

    @async_test
    async def test_primary_empty_text_fallback_success(self):
        """主选返回空文本也触发降级 → 备用成功接管。"""
        summarizer, context = self._make(
            primary="p-main",
            fallbacks_raw='["p-back"]',
            behaviors={
                "p-main": ("empty", None),
                "p-back": ("text", CHAIN_OK_TEXT),
            },
        )
        before = _new_warnings()
        result = await self._summarize(summarizer)
        assert context.called_providers() == ["p-main", "p-back"]
        assert result.provider_id == "p-back"
        new_warn = "\n".join(_new_warnings()[len(before) :])
        assert "provider=p-main" in new_warn
        assert "返回文本为空" in new_warn

    @async_test
    async def test_all_configured_fail_session_success(self):
        """主选异常 + 备用空文本 → 会话成功：调用序含会话节点，会话仅解析一次。"""
        summarizer, context = self._make(
            primary="p1",
            fallbacks_raw='["p2"]',
            behaviors={
                "p1": ("exc", RuntimeError("p1 故障")),
                "p2": ("empty", None),
            },
            chat_provider="chat-prov",
        )
        before = _new_warnings()
        result = await self._summarize(summarizer)
        assert context.called_providers() == ["p1", "p2", "chat-prov"]
        assert result.provider_id == "chat-prov"
        # 会话 provider 惰性解析且仅解析一次
        assert context.provider_queries == ["umo-1"]
        new_warn = "\n".join(_new_warnings()[len(before) :])
        assert "降级会话模型" in new_warn

    @async_test
    async def test_empty_chain_raises_provider_error(self):
        """三段皆空（主选空 + 列表 [] + 会话抛异常）→ SummaryProviderError。"""
        summarizer, context = self._make(
            chat_provider_exc=Exception("ProviderNotFoundError"),
        )
        with pytest.raises(SummaryProviderError):
            await self._summarize(summarizer)
        assert context.llm_calls == []  # 无任何 LLM 调用
        assert context.provider_queries == ["umo-1"]  # 会话解析过一次（取不到）

    @async_test
    async def test_chain_exhausted_raises_last_exception(self):
        """已配置段全失败（异常）+ 会话取不到 → 抛最后遇到的异常（原样对象）。"""
        last_boom = _ProviderDownError("p2 账户欠费")
        summarizer, context = self._make(
            primary="p1",
            fallbacks_raw='["p2"]',
            behaviors={
                "p1": ("exc", ValueError("p1 限流")),
                "p2": ("exc", last_boom),
            },
            chat_provider_exc=Exception("ProviderNotFoundError"),
        )
        before_err = list(_STUB_LOGGER.records["error"])
        with pytest.raises(_ProviderDownError) as exc_info:
            await self._summarize(summarizer)
        assert exc_info.value is last_boom  # 原样上抛（同一对象，未包装）
        assert context.called_providers() == ["p1", "p2"]
        # 整链耗尽记 error（含末次失败 provider）
        new_err = "\n".join(_STUB_LOGGER.records["error"][len(before_err) :])
        assert "降级链耗尽" in new_err and "p2" in new_err

    @async_test
    async def test_chain_exhausted_empty_text_raises_runtime_error(self):
        """已配置段末次失败为空文本 + 会话取不到 → RuntimeError。"""
        summarizer, context = self._make(
            primary="p1",
            behaviors={"p1": ("empty", None)},
            chat_provider_exc=Exception("ProviderNotFoundError"),
        )
        with pytest.raises(RuntimeError, match="空总结文本"):
            await self._summarize(summarizer)
        assert context.called_providers() == ["p1"]

    @async_test
    async def test_chain_build_filter_dedup(self):
        """链构建：strip / 非 str 丢弃 / 保序去重 / 与主选去重（以调用序验证）。"""
        # 列表含：空串 / 纯空白 / 数字 / null / 与主选重复 / 带空白重复项
        summarizer, context = self._make(
            primary="a",
            fallbacks_raw='["", "  ", 42, null, "a", " b ", "b", "a"]',
            behaviors={
                "a": ("exc", ValueError("a down")),
                "b": ("exc", _ProviderDownError("b down")),
            },
            chat_provider_exc=Exception("no session"),
        )
        with pytest.raises(_ProviderDownError):
            await self._summarize(summarizer)
        # 实际链 == ["a", "b"]：42/null/空串被过滤，" b " strip 后与 "b" 去重，
        # 两个 "a" 与主选去重
        assert context.called_providers() == ["a", "b"]
        assert context.provider_queries == ["umo-1"]

    @async_test
    async def test_typed_non_list_treated_as_empty(self):
        """typed 返回非 list（如字符串）→ 视为空列表，不崩，会话兜底接管。"""
        summarizer, context = self._make(
            typed_overrides={"summary_fallback_providers": "not-a-list"},
            chat_provider="chat-prov",
        )
        result = await self._summarize(summarizer)
        # 已配置段为空 → 会话 provider 立即解析为唯一节点
        assert context.called_providers() == ["chat-prov"]
        assert context.provider_queries == ["umo-1"]
        assert result.provider_id == "chat-prov"

    @async_test
    async def test_session_dup_tried_no_recall(self):
        """会话 provider 与已尝试节点重复 → 不重复调用，链耗尽抛末次异常。"""
        boom = _ProviderDownError("会话节点也挂了")
        summarizer, context = self._make(
            primary="chat-prov",  # 主选 id 与会话 provider 相同
            behaviors={"chat-prov": ("exc", boom)},
            chat_provider="chat-prov",
        )
        with pytest.raises(_ProviderDownError) as exc_info:
            await self._summarize(summarizer)
        assert exc_info.value is boom
        assert context.called_providers() == ["chat-prov"]  # 仅调用一次，未重复
        assert context.provider_queries == ["umo-1"]  # 会话仅解析一次

    @async_test
    async def test_session_dup_in_fallback_list_empty_text_exhausted(self):
        """备用列表含会话 provider（末次空文本失败）→ 会话重复被丢弃，抛 RuntimeError。"""
        summarizer, context = self._make(
            primary="p1",
            fallbacks_raw='["chat-prov"]',
            behaviors={
                "p1": ("exc", ValueError("p1 down")),
                "chat-prov": ("empty", None),
            },
            chat_provider="chat-prov",
        )
        with pytest.raises(RuntimeError, match="空总结文本"):
            await self._summarize(summarizer)
        # chat-prov 作为备用节点只调用一次，会话兜底因重复直接放弃
        assert context.called_providers() == ["p1", "chat-prov"]
        assert context.provider_queries == ["umo-1"]


# ============================================================
# 五、TestFeedback：service 触发反馈（F2）
# ============================================================


class TestFeedback:
    """私有方法直测（_send_feedback/_feedback_text/_react_to_trigger）+ 端到端。"""

    def _make_service(self, monkeypatch, tmp_path, overrides=None, rows=None):
        """组装 SummaryService（真实上游模块 + 假基础设施），数据目录重定向 tmp。"""
        monkeypatch.setattr(
            service_mod.StarTools,
            "get_data_dir",
            staticmethod(lambda plugin_name=None: tmp_path),
        )
        config = FakeSummaryConfig(overrides=overrides)
        mysql = FakeMySQLMgr(rows=rows)
        context = SimpleLLMContext()
        svc = SummaryService(context, config, mysql, FakeStar())
        return svc, config, context

    def _make_e2e(self, monkeypatch, tmp_path, overrides=None, rows=None):
        """端到端组装：在 _make_service 基础上放行白名单 + 假 OneBot 补齐桩。"""
        merged = {
            "summary_group_whitelist": '["12345"]',
            **(overrides or {}),
        }
        svc, config, context = self._make_service(
            monkeypatch, tmp_path, merged, rows=rows
        )
        onebot = FakeOneBotFetch()
        monkeypatch.setattr(fetcher_mod, "fetch_group_history", onebot)
        return svc, config, context, onebot

    # ---- 私有方法直测 ----

    @async_test
    async def test_mode_none_silent(self, monkeypatch, tmp_path):
        """mode=none → 无任何发送、无 call_action。"""
        svc, _, _ = self._make_service(
            monkeypatch, tmp_path, {"summary_feedback_mode": "none"}
        )
        event = FakeEvent()
        await svc._send_feedback(event)
        assert event.sent == []
        assert event.api_calls() == []

    @async_test
    async def test_mode_text_sends_configured_text(self, monkeypatch, tmp_path):
        """mode=text → 发送 summary_feedback_text 配置文案，不触达协议端。"""
        svc, _, _ = self._make_service(
            monkeypatch,
            tmp_path,
            {
                "summary_feedback_mode": "text",
                "summary_feedback_text": "自定义反馈文案",
            },
        )
        event = FakeEvent()
        await svc._send_feedback(event)
        assert event.plain_texts() == ["自定义反馈文案"]
        assert event.api_calls() == []

    @async_test
    async def test_mode_text_blank_falls_back_default(self, monkeypatch, tmp_path):
        """mode=text 且文案被清空（空白）→ 回退内置默认文案。"""
        svc, _, _ = self._make_service(
            monkeypatch,
            tmp_path,
            {"summary_feedback_mode": "text", "summary_feedback_text": "   "},
        )
        event = FakeEvent()
        await svc._send_feedback(event)
        assert event.plain_texts() == [DEFAULT_FEEDBACK_TEXT]

    @async_test
    async def test_reaction_success_calls_emoji_like(self, monkeypatch, tmp_path):
        """mode=reaction 成功 → set_msg_emoji_like(message_id=int, emoji_id='128077')，无文字。"""
        svc, _, _ = self._make_service(
            monkeypatch, tmp_path, {"summary_feedback_mode": "reaction"}
        )
        event = FakeEvent(message_id=12345)
        await svc._send_feedback(event)
        assert event.api_calls() == [
            {"action": "set_msg_emoji_like", "message_id": 12345, "emoji_id": "128077"}
        ]
        assert event.sent == []  # 贴表情成功不发文字

        # message_id 为数字字符串 → int 化后透传
        event2 = FakeEvent(message_id="9876")
        await svc._send_feedback(event2)
        assert event2.api_calls()[0]["message_id"] == 9876
        assert event2.sent == []

    @async_test
    async def test_reaction_no_message_id_degrades_text(self, monkeypatch, tmp_path):
        """mode=reaction + message_id 为 None → 降级文字，不尝试 call_action。"""
        svc, _, _ = self._make_service(
            monkeypatch, tmp_path, {"summary_feedback_mode": "reaction"}
        )
        event = FakeEvent(message_id=None)
        await svc._send_feedback(event)
        assert event.plain_texts() == [DEFAULT_FEEDBACK_TEXT]
        assert event.api_calls() == []

        # message_obj 整体缺失（None）同样降级
        event2 = FakeEvent(with_message_obj=False)
        await svc._send_feedback(event2)
        assert event2.plain_texts() == [DEFAULT_FEEDBACK_TEXT]
        assert event2.api_calls() == []

    @async_test
    async def test_reaction_no_bot_degrades_text(self, monkeypatch, tmp_path):
        """mode=reaction + event.bot 为 None（或无 api 属性）→ 降级文字。"""
        svc, _, _ = self._make_service(
            monkeypatch, tmp_path, {"summary_feedback_mode": "reaction"}
        )
        event = FakeEvent(bot=None)
        await svc._send_feedback(event)
        assert event.plain_texts() == [DEFAULT_FEEDBACK_TEXT]
        assert event.api_calls() == []

        # bot 存在但无 api 属性（非 aiocqhttp 平台模拟）
        event2 = FakeEvent(bot=SimpleNamespace())
        await svc._send_feedback(event2)
        assert event2.plain_texts() == [DEFAULT_FEEDBACK_TEXT]
        assert event2.api_calls() == []

    @async_test
    async def test_reaction_non_numeric_message_id_degrades_text(
        self, monkeypatch, tmp_path
    ):
        """mode=reaction + message_id='abc'（int 转换失败）→ 降级文字。"""
        svc, _, _ = self._make_service(
            monkeypatch, tmp_path, {"summary_feedback_mode": "reaction"}
        )
        event = FakeEvent(message_id="abc")
        await svc._send_feedback(event)
        assert event.plain_texts() == [DEFAULT_FEEDBACK_TEXT]
        assert event.api_calls() == []  # 转换失败在 call_action 之前

    @async_test
    async def test_reaction_call_action_failure_degrades_text(
        self, monkeypatch, tmp_path
    ):
        """mode=reaction + call_action 抛异常（协议端不支持）→ 降级配置文案。"""
        svc, _, _ = self._make_service(
            monkeypatch,
            tmp_path,
            {
                "summary_feedback_mode": "reaction",
                "summary_feedback_text": "降级文案",
            },
        )
        event = FakeEvent(message_id=12345, api_exc=RuntimeError("ActionFailed"))
        await svc._send_feedback(event)
        assert event.plain_texts() == ["降级文案"]  # 降级走配置文案
        assert len(event.api_calls()) == 1  # 已尝试过一次 call_action

    @async_test
    async def test_unknown_mode_no_send_with_warning(self, monkeypatch, tmp_path):
        """mode='garbage'（未知值）→ 无发送（仅 warning，不崩）。"""
        svc, _, _ = self._make_service(
            monkeypatch, tmp_path, {"summary_feedback_mode": "garbage"}
        )
        event = FakeEvent()
        before = _new_warnings()
        await svc._send_feedback(event)  # 不得抛异常
        assert event.sent == []
        assert event.api_calls() == []
        new_warn = "\n".join(_new_warnings()[len(before) :])
        assert "未知的 summary_feedback_mode" in new_warn

    # ---- 端到端（经 handler）----

    @async_test
    async def test_e2e_count_command_feedback_text_first(self, monkeypatch, tmp_path):
        """handle_count_command 成功路径（mode=text）→ 首条为反馈文案，总结照常产出。"""
        rows = make_rows_desc(3)
        svc, _, context, _ = self._make_e2e(
            monkeypatch,
            tmp_path,
            {
                "summary_feedback_mode": "text",
                "summary_feedback_text": "开工啦",
            },
            rows=rows,
        )
        event = FakeEvent()
        await svc.handle_count_command(event, "5")  # 不得冒泡
        assert event.plain_texts()[0] == "开工啦"  # 反馈先于总结发出
        assert len(event.chains()) == 1  # 总结消息链照常发送
        assert len(context.llm_calls) == 1

    @async_test
    async def test_e2e_reaction_degrades_then_summary(self, monkeypatch, tmp_path):
        """端到端：默认 reaction 但 message_id 缺失 → 降级默认文案 + 总结照常。"""
        rows = make_rows_desc(3)
        svc, _, context, _ = self._make_e2e(monkeypatch, tmp_path, rows=rows)
        event = FakeEvent(message_id=None)  # 默认 mode=reaction
        await svc.handle_count_command(event, "5")
        assert event.plain_texts()[0] == DEFAULT_FEEDBACK_TEXT
        assert len(event.chains()) == 1
        assert len(context.llm_calls) == 1

    @async_test
    async def test_feedback_send_failure_does_not_block_main(
        self, monkeypatch, tmp_path
    ):
        """反馈发送本身抛异常（send 炸）→ handler 不冒泡，总结主流程不受影响。"""
        rows = make_rows_desc(3)
        svc, _, context, _ = self._make_e2e(
            monkeypatch,
            tmp_path,
            {"summary_feedback_mode": "text", "summary_feedback_text": "开工啦"},
            rows=rows,
        )
        event = FakeEvent(send_fail_on_plain=True)  # 仅文字发送失败，消息链正常
        before = _new_warnings()
        await svc.handle_count_command(event, "5")  # 绝不冒泡
        assert event.plain_texts() == []  # 反馈文字发送失败
        assert len(event.chains()) == 1  # 总结消息链仍成功发出
        assert len(context.llm_calls) == 1  # LLM 总结照常执行
        new_warn = "\n".join(_new_warnings()[len(before) :])
        assert "发送提示消息失败" in new_warn

    @async_test
    async def test_invalid_command_no_feedback(self, monkeypatch, tmp_path):
        """校验未通过（参数非法）不发反馈——仅用法提示。"""
        svc, _, context, _ = self._make_e2e(
            monkeypatch,
            tmp_path,
            {"summary_feedback_mode": "text", "summary_feedback_text": "开工啦"},
        )
        event = FakeEvent()
        await svc.handle_count_command(event, "abc")
        assert event.plain_texts() == ["用法：/消息总结 <数量>，如 /消息总结 512"]
        assert "开工啦" not in event.plain_texts()
        assert context.llm_calls == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
