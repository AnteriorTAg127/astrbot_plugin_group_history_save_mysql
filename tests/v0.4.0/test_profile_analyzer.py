# ruff: noqa: I001
"""astrbot_plugin_group_history_save_mysql v0.4.0 Module F 离线单元测试。

覆盖「人物分析」AI 分析引擎（profile/analyzer.py，ProfileAnalyzer）：

- 独立 provider 降级链（读 profile_* 配置，不复用 summary 配置）：
  主选 profile_provider（空串跳过）→ 备用列表 profile_fallback_providers（按序、
  过滤非法项、保序去重）→ 会话模型兜底（惰性解析，主选成功零会话查询）；
  任一节点异常/空文本/非字符串 completion_text 即降级；provider_id 记实际成功者；
  全失败 → 「分析失败」兜底单段 + provider_id=""，绝不抛异常。
- event=None（Web 全局场景）：会话兜底跳过路径。
- 长度预算截断：保最近、上下文消息优先级低于目标消息、truncated 不触及 stats。
- 维度开关：关闭维度不进 prompt；活动时间并入「发言习惯」不单独成块；
  关系维度关闭则无互动上下文块；image 模式追加表格提示。
- 4 板块宽松切分：正常 4 块（乱序归一排序）/ 宽松标题归一 / 去重 / 无 ## 单段兜底。
- ProfileResult 契约字段。

运行方式（插件根目录，二者皆可）：
    python -m pytest "tests/v0.4.0/test_profile_analyzer.py" -v
    python "tests/v0.4.0/test_profile_analyzer.py"

范式沿用 tests/v0.4.0/test_models_capture.py（Agent-C stub 隔离技巧）：
astrbot.* 全部 sys.modules stub，注入前仅剔除 profile 子模块（不删 summary.* 等
兄弟子包，避免破坏多文件合跑时其他版本测试的缓存类对象）；不依赖真实 LLM，
context.llm_generate / get_current_chat_provider_id / get_all_providers 全 mock。
"""

import asyncio
import os
import sys
import types
import unittest
from datetime import datetime, timedelta


# ============================================================
# 一、stub 注入（必须在任何 from astrbot_plugin_group_history_save_mysql... import 之前）
# ============================================================


def _new_module(name: str) -> types.ModuleType:
    """创建一个空模块对象。"""
    return types.ModuleType(name)


# ---- astrbot 主包及其子包 ----
_astrbot = _new_module("astrbot")
_astrbot_api = _new_module("astrbot.api")
_astrbot_event = _new_module("astrbot.api.event")
_astrbot_star = _new_module("astrbot.api.star")


# ---- astrbot.api: logger（带记录能力，供降级 warning/error 断言）----
class _StubLogger:
    """记录各级日志内容，便于断言（如降级 warning / 链耗尽 error）。"""

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


# ---- astrbot.api.event: AstrMessageEvent（仅类型引用）----
class _StubAstrMessageEvent:
    pass


_astrbot_event.AstrMessageEvent = _StubAstrMessageEvent


# ---- astrbot.api.star: Context / Star / StarTools（analyzer 类型引用 +
#      db_config 间接依赖 StarTools.get_data_dir）----
class _StubContext:
    pass


class _StubStar:
    pass


class _StubStarTools:
    @classmethod
    def get_data_dir(cls, plugin_name=None):
        # db_config 仅在实例化时使用；本测试只用 ConfigManager 类做类型引用，
        # 不会被调用。返回任意合法路径即可（防御性）。
        import tempfile
        from pathlib import Path

        return Path(tempfile.gettempdir()) / (plugin_name or "test")


_astrbot_star.Context = _StubContext
_astrbot_star.Star = _StubStar
_astrbot_star.StarTools = _StubStarTools


# ---- 多测试文件合跑兼容：仅剔除 profile 子模块（而非整个插件包），使其重新
#      执行并绑定到本文件的 stub。切忌删除 summary.* / db_config 等兄弟模块——
#      它们可能已被其他版本测试文件导入并缓存，误删会触发二次导入产生重复类
#      对象，破坏其 isinstance 断言（Agent-C stub 隔离技巧）。 ----
for _name in list(sys.modules):
    if _name.startswith("astrbot_plugin_group_history_save_mysql.core.profile"):
        del sys.modules[_name]

# ---- 注入 sys.modules ----
sys.modules["astrbot"] = _astrbot
sys.modules["astrbot.api"] = _astrbot_api
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

from astrbot_plugin_group_history_save_mysql.core.profile.analyzer import (  # noqa: E402
    ProfileAnalyzer,
)
from astrbot_plugin_group_history_save_mysql.core.profile.models import (  # noqa: E402
    ProfileMessage,
    ProfileStats,
    ProfileTarget,
)


# ============================================================
# 三、fake 依赖（context / config_mgr / event）
# ============================================================


class _LLMResponse:
    """模拟 AstrBot LLMResponse：仅承载 completion_text。"""

    def __init__(self, completion_text=""):
        self.completion_text = completion_text


class _NoStripObj:
    """无 strip 方法的对象：验证非字符串 completion_text 被视为空文本降级。"""


class _FakeContext:
    """模拟 Context：llm_generate 按 provider 脚本化返回/抛异常。

    behaviors: {provider_id: str（成功文本）| BaseException 实例（抛异常）
                | callable(prompt)->任意值（包装为 completion_text）}
    session_provider: str（会话 provider）| BaseException 实例（解析抛异常）
    """

    def __init__(self, behaviors=None, session_provider=""):
        self.behaviors = behaviors or {}
        self.session_provider = session_provider
        self.llm_calls = []  # [(provider_id, prompt)]
        self.session_calls = []  # [umo]
        self.all_providers_calls = 0

    async def llm_generate(self, chat_provider_id=None, prompt=None, **kwargs):
        self.llm_calls.append((chat_provider_id, prompt))
        if chat_provider_id not in self.behaviors:
            raise RuntimeError(f"未脚本化的 provider: {chat_provider_id}")
        behavior = self.behaviors[chat_provider_id]
        if isinstance(behavior, BaseException):
            raise behavior
        if callable(behavior):
            return _LLMResponse(behavior(prompt))
        return _LLMResponse(behavior)

    async def get_current_chat_provider_id(self, umo):
        self.session_calls.append(umo)
        if isinstance(self.session_provider, BaseException):
            raise self.session_provider
        return self.session_provider

    async def get_all_providers(self):
        self.all_providers_calls += 1
        return []


def _default_settings(**overrides):
    """默认 profile 配置（typed 值，等价 PROFILE_DEFAULTS 的语义）。"""
    settings = {
        "profile_provider": "",
        "profile_fallback_providers": [],
        "profile_max_prompt_chars": 60000,
        "profile_dim_habits": True,
        "profile_dim_activity": True,
        "profile_dim_personality": True,
        "profile_dim_hobbies": True,
        "profile_dim_relations": True,
    }
    settings.update(overrides)
    return settings


class _FakeConfigMgr:
    """模拟 ConfigManager：仅实现 analyzer 用到的两个读取方法。"""

    def __init__(self, settings=None):
        self.settings = _default_settings() if settings is None else settings

    async def get_profile_setting(self, key):
        value = self.settings.get(key, "")
        return "" if value is None else str(value)

    async def get_profile_setting_typed(self, key):
        return self.settings.get(key)


class _FakeEvent:
    """模拟 AstrMessageEvent：仅承载 unified_msg_origin。"""

    def __init__(self, umo="aiocqhttp:GroupMessage:987654"):
        self.unified_msg_origin = umo


# ============================================================
# 四、构造辅助
# ============================================================

_BASE_TS = datetime(2026, 8, 1, 20, 0, 0)

# 标准 4 板块 LLM 输出（规范顺序）
FOUR_SECTIONS = (
    "## 发言习惯\n习惯内容\n\n"
    "## 性格分析\n性格内容\n\n"
    "## 兴趣爱好\n爱好内容\n\n"
    "## 人物关系\n关系内容"
)


def _msg(index, content=None, group_id="987654", sender_id="123", sender_name="张三"):
    """构造目标消息（时间按 index 分钟递增，内容默认 msg-NNN 便于截断断言）。"""
    return ProfileMessage(
        timestamp=_BASE_TS + timedelta(minutes=index),
        group_id=group_id,
        sender_id=sender_id,
        sender_name=sender_name,
        content=content if content is not None else f"msg-{index:03d}",
        message_id=f"m-{index}",
        source="mysql",
    )


def _ctx_msg(index, sender_id="222", sender_name="李四"):
    """构造互动上下文消息（内容默认 ctx-NNN）。"""
    return ProfileMessage(
        timestamp=_BASE_TS + timedelta(minutes=index),
        group_id="987654",
        sender_id=sender_id,
        sender_name=sender_name,
        content=f"ctx-{index:03d}",
        message_id=f"c-{index}",
        source="mysql",
    )


def _stats(**overrides):
    """构造 ProfileStats（全字段默认值 + overrides）。"""
    base = {
        "total": 20,
        "group_count": 1,
        "group_breakdown": [("987654", 20)],
        "time_start": _BASE_TS,
        "time_end": _BASE_TS + timedelta(minutes=19),
        "active_days": 3,
        "hour_dist": [0] * 24,
        "weekday_dist": [0] * 7,
        "peak_hour": 23,
        "peak_weekday": 4,  # 周五
        "avg_length": 12.5,
        "total_chars": 250,
        "emoji_ratio": 0.1,
        "question_ratio": 0.2,
        "top_partners": [("222", "李四", 8), ("333", "王五", 3)],
    }
    base.update(overrides)
    return ProfileStats(**base)


def _target(scope="group", group_id="987654"):
    return ProfileTarget(
        sender_id="123", sender_name="张三", scope=scope, group_id=group_id
    )


def _make_analyzer(context, settings=None):
    return ProfileAnalyzer(context, _FakeConfigMgr(settings))


def _run(coro):
    """同步驱动异步用例（环境无 pytest-asyncio）。"""
    return asyncio.run(coro)


# ============================================================
# 五、测试用例
# ============================================================


class TestProviderChain(unittest.TestCase):
    """独立 provider 降级链（profile_* 配置）。"""

    def setUp(self):
        for level in ("info", "warning", "error"):
            _STUB_LOGGER.records[level].clear()

    def test_primary_exception_falls_back_to_next(self):
        """主选抛异常 → 备用成功，provider_id=备用。"""
        ctx = _FakeContext(behaviors={"p1": RuntimeError("boom"), "p2": FOUR_SECTIONS})
        analyzer = _make_analyzer(
            ctx,
            _default_settings(
                profile_provider="p1", profile_fallback_providers=["p2", "p3"]
            ),
        )
        result = _run(
            analyzer.analyze(
                _FakeEvent(), _target(), _stats(), [_msg(0)], [], "forward"
            )
        )
        self.assertEqual(result.provider_id, "p2")
        self.assertEqual([pid for pid, _ in ctx.llm_calls], ["p1", "p2"])
        self.assertEqual(result.sections[0][0], "发言习惯")

    def test_primary_empty_text_falls_back(self):
        """主选返回空文本（纯空白）→ 降级备用。"""
        ctx = _FakeContext(behaviors={"p1": "   \n  ", "p2": FOUR_SECTIONS})
        analyzer = _make_analyzer(
            ctx,
            _default_settings(profile_provider="p1", profile_fallback_providers=["p2"]),
        )
        result = _run(
            analyzer.analyze(
                _FakeEvent(), _target(), _stats(), [_msg(0)], [], "forward"
            )
        )
        self.assertEqual(result.provider_id, "p2")
        self.assertIn("返回文本为空", _STUB_LOGGER.joined("warning"))

    def test_non_string_completion_treated_as_empty(self):
        """completion_text 非字符串（无 strip）→ 视为空文本降级，不抛异常。"""
        ctx = _FakeContext(
            behaviors={"p1": lambda prompt: _NoStripObj(), "p2": FOUR_SECTIONS}
        )
        analyzer = _make_analyzer(
            ctx,
            _default_settings(profile_provider="p1", profile_fallback_providers=["p2"]),
        )
        result = _run(
            analyzer.analyze(
                _FakeEvent(), _target(), _stats(), [_msg(0)], [], "forward"
            )
        )
        self.assertEqual(result.provider_id, "p2")

    def test_all_fail_returns_failure_result_without_raise(self):
        """全失败 → 「分析失败」兜底单段 + provider_id=""，绝不抛异常。"""
        ctx = _FakeContext(behaviors={"p1": RuntimeError("boom"), "p2": ""})
        analyzer = _make_analyzer(
            ctx,
            _default_settings(profile_provider="p1", profile_fallback_providers=["p2"]),
        )
        # event 非 None 但会话 provider 取不到 → 三段皆空路径
        ctx.session_provider = RuntimeError("ProviderNotFoundError")
        result = _run(
            analyzer.analyze(
                _FakeEvent(), _target(), _stats(), [_msg(0)], [], "forward"
            )
        )
        self.assertEqual(result.provider_id, "")
        self.assertEqual(result.raw_llm_text, "")
        self.assertEqual(len(result.sections), 1)
        self.assertEqual(result.sections[0][0], "分析失败")
        self.assertIn("未能生成人物画像叙述", result.sections[0][1])
        self.assertIn("降级链耗尽", _STUB_LOGGER.joined("error"))

    def test_session_fallback_after_configured_fail(self):
        """已配置段全失败 → 惰性降级会话模型，provider_id=会话。"""
        ctx = _FakeContext(
            behaviors={"p1": RuntimeError("boom"), "sess": FOUR_SECTIONS},
            session_provider="sess",
        )
        analyzer = _make_analyzer(ctx, _default_settings(profile_provider="p1"))
        event = _FakeEvent(umo="aiocqhttp:GroupMessage:555")
        result = _run(
            analyzer.analyze(event, _target(), _stats(), [_msg(0)], [], "forward")
        )
        self.assertEqual(result.provider_id, "sess")
        self.assertEqual(ctx.session_calls, ["aiocqhttp:GroupMessage:555"])
        self.assertIn("降级会话模型", _STUB_LOGGER.joined("warning"))

    def test_primary_success_skips_session_query(self):
        """主选成功 → 全程零会话查询（惰性设计）。"""
        ctx = _FakeContext(behaviors={"p1": FOUR_SECTIONS}, session_provider="sess")
        analyzer = _make_analyzer(ctx, _default_settings(profile_provider="p1"))
        result = _run(
            analyzer.analyze(
                _FakeEvent(), _target(), _stats(), [_msg(0)], [], "forward"
            )
        )
        self.assertEqual(result.provider_id, "p1")
        self.assertEqual(ctx.session_calls, [])

    def test_empty_config_uses_session_directly(self):
        """主选空串 + 备用空列表 → 会话模型作为唯一节点。"""
        ctx = _FakeContext(behaviors={"sess": FOUR_SECTIONS}, session_provider="sess")
        analyzer = _make_analyzer(ctx)  # 默认 provider="" fallback=[]
        result = _run(
            analyzer.analyze(
                _FakeEvent(), _target(), _stats(), [_msg(0)], [], "forward"
            )
        )
        self.assertEqual(result.provider_id, "sess")
        self.assertEqual([pid for pid, _ in ctx.llm_calls], ["sess"])

    def test_chain_filters_illegal_items_and_dedups(self):
        """备用列表过滤非 str/空白项，并与主选保序去重。"""
        ctx = _FakeContext(behaviors={"p1": RuntimeError("boom"), "p2": FOUR_SECTIONS})
        analyzer = _make_analyzer(
            ctx,
            _default_settings(
                profile_provider="p1",
                profile_fallback_providers=["", "   ", 42, None, "p1", "p2"],
            ),
        )
        result = _run(
            analyzer.analyze(
                _FakeEvent(), _target(), _stats(), [_msg(0)], [], "forward"
            )
        )
        self.assertEqual(result.provider_id, "p2")
        # 链应为 [p1, p2]：非法项剔除、p1 去重
        self.assertEqual([pid for pid, _ in ctx.llm_calls], ["p1", "p2"])

    def test_fallback_not_list_treated_as_empty(self):
        """备用配置非 list（异常类型）→ 视为空列表，直落会话兜底。"""
        ctx = _FakeContext(behaviors={"sess": FOUR_SECTIONS}, session_provider="sess")
        analyzer = _make_analyzer(
            ctx,
            _default_settings(
                profile_provider="", profile_fallback_providers="not-a-list"
            ),
        )
        result = _run(
            analyzer.analyze(
                _FakeEvent(), _target(), _stats(), [_msg(0)], [], "forward"
            )
        )
        self.assertEqual(result.provider_id, "sess")


class TestEventNoneWebGlobal(unittest.TestCase):
    """event=None（Web 全局分析）：无会话，会话兜底跳过。"""

    def setUp(self):
        for level in ("info", "warning", "error"):
            _STUB_LOGGER.records[level].clear()

    def test_event_none_skips_session_fallback(self):
        """已配置段全失败且 event=None → 跳过会话兜底，失败结果且不查询会话。"""
        ctx = _FakeContext(
            behaviors={"p1": RuntimeError("boom")}, session_provider="sess"
        )
        analyzer = _make_analyzer(ctx, _default_settings(profile_provider="p1"))
        result = _run(
            analyzer.analyze(
                None,
                _target(scope="all", group_id=""),
                _stats(),
                [_msg(0)],
                [],
                "forward",
            )
        )
        self.assertEqual(ctx.session_calls, [])  # 从未查询会话
        self.assertEqual(result.provider_id, "")
        self.assertEqual(result.sections[0][0], "分析失败")

    def test_event_none_configured_chain_works(self):
        """event=None 但主选可用 → 正常成功。"""
        ctx = _FakeContext(behaviors={"p1": FOUR_SECTIONS})
        analyzer = _make_analyzer(ctx, _default_settings(profile_provider="p1"))
        result = _run(
            analyzer.analyze(
                None,
                _target(scope="all", group_id=""),
                _stats(),
                [_msg(0)],
                [],
                "forward",
            )
        )
        self.assertEqual(result.provider_id, "p1")
        self.assertEqual(ctx.session_calls, [])


class TestLengthBudget(unittest.TestCase):
    """长度预算截断：保最近、上下文低优先级、stats 不受影响。"""

    def setUp(self):
        for level in ("info", "warning", "error"):
            _STUB_LOGGER.records[level].clear()

    def test_truncation_keeps_most_recent(self):
        """预算不足 → 丢弃最旧、保留最近；messages_used < 全量。"""
        messages = [_msg(i, content=f"msg-{i:03d}" + "x" * 40) for i in range(40)]
        ctx = _FakeContext(behaviors={"p1": FOUR_SECTIONS})
        analyzer = _make_analyzer(
            ctx, _default_settings(profile_provider="p1", profile_max_prompt_chars=1600)
        )
        stats = _stats(total=40)
        result = _run(
            analyzer.analyze(_FakeEvent(), _target(), stats, messages, [], "forward")
        )
        prompt = ctx.llm_calls[0][1]
        self.assertLess(result.messages_used, 40)
        self.assertGreater(result.messages_used, 0)
        self.assertIn("msg-039", prompt)  # 最新保留
        self.assertNotIn("msg-000", prompt)  # 最旧丢弃
        self.assertIn("已截断", _STUB_LOGGER.joined("info"))

    def test_no_truncation_within_budget(self):
        """预算充足 → 全量送入，无截断日志。"""
        messages = [_msg(i) for i in range(10)]
        ctx = _FakeContext(behaviors={"p1": FOUR_SECTIONS})
        analyzer = _make_analyzer(ctx, _default_settings(profile_provider="p1"))
        result = _run(
            analyzer.analyze(_FakeEvent(), _target(), _stats(), messages, [], "forward")
        )
        self.assertEqual(result.messages_used, 10)
        self.assertNotIn("已截断", _STUB_LOGGER.joined("info"))

    def test_stats_not_mutated_by_truncation(self):
        """截断只记日志：传入的 stats 不被修改（统计仍全量）。"""
        messages = [_msg(i, content=f"msg-{i:03d}" + "x" * 40) for i in range(40)]
        ctx = _FakeContext(behaviors={"p1": FOUR_SECTIONS})
        analyzer = _make_analyzer(
            ctx, _default_settings(profile_provider="p1", profile_max_prompt_chars=1600)
        )
        stats = _stats(total=40)
        result = _run(
            analyzer.analyze(_FakeEvent(), _target(), stats, messages, [], "forward")
        )
        self.assertLess(result.messages_used, 40)  # 确实发生截断
        self.assertFalse(stats.truncated)  # stats 未被触及
        self.assertIs(result.stats, stats)  # 原样透传

    def test_context_messages_lower_priority(self):
        """预算先满足目标消息，剩余才给上下文（均保最近）。"""
        targets = [_msg(i, content=f"msg-{i:03d}") for i in range(5)]
        # 50 条上下文（每条 ~50 字符），总量远超剩余预算以触发上下文侧截断
        contexts = [
            ProfileMessage(
                timestamp=_BASE_TS + timedelta(minutes=i),
                group_id="987654",
                sender_id="222",
                sender_name="李四",
                content=f"ctx-{i:03d}" + "y" * 40,
                message_id=f"c-{i}",
                source="mysql",
            )
            for i in range(50)
        ]
        ctx = _FakeContext(behaviors={"p1": FOUR_SECTIONS})
        analyzer = _make_analyzer(
            ctx, _default_settings(profile_provider="p1", profile_max_prompt_chars=1800)
        )
        result = _run(
            analyzer.analyze(
                _FakeEvent(), _target(), _stats(), targets, contexts, "forward"
            )
        )
        prompt = ctx.llm_calls[0][1]
        # 目标消息全量保留（优先级高）
        self.assertEqual(result.messages_used, 5)
        self.assertIn("msg-000", prompt)
        # 上下文保最近：最新保留、最旧丢弃
        self.assertIn("ctx-049", prompt)
        self.assertNotIn("ctx-000", prompt)

    def test_empty_target_messages_placeholder(self):
        """无目标消息 → 样本占位文案，不崩。"""
        ctx = _FakeContext(behaviors={"p1": FOUR_SECTIONS})
        analyzer = _make_analyzer(ctx, _default_settings(profile_provider="p1"))
        result = _run(
            analyzer.analyze(
                _FakeEvent(), _target(), _stats(total=0), [], [], "forward"
            )
        )
        self.assertEqual(result.messages_used, 0)
        self.assertIn("无目标发言样本", ctx.llm_calls[0][1])


class TestDimensionSwitch(unittest.TestCase):
    """维度开关：仅启用维度进 prompt；activity 并入发言习惯。"""

    def setUp(self):
        for level in ("info", "warning", "error"):
            _STUB_LOGGER.records[level].clear()

    def _prompt_of(self, settings, contexts=None, output_mode="forward"):
        settings["profile_provider"] = "p1"  # 固定主选，确保产生 llm 调用
        ctx = _FakeContext(behaviors={"p1": FOUR_SECTIONS})
        analyzer = _make_analyzer(ctx, settings)
        _run(
            analyzer.analyze(
                _FakeEvent(),
                _target(),
                _stats(),
                [_msg(0)],
                contexts or [],
                output_mode,
            )
        )
        return ctx.llm_calls[0][1]

    def test_disabled_dimension_absent_from_prompt(self):
        """关闭性格维度 → prompt 不含该板块要求，其余维度在。"""
        prompt = self._prompt_of(_default_settings(profile_dim_personality=False))
        self.assertNotIn("性格分析", prompt)
        self.assertIn("## 发言习惯", prompt)
        self.assertIn("## 兴趣爱好", prompt)
        self.assertIn("## 人物关系", prompt)

    def test_activity_merged_into_habits_not_standalone(self):
        """活动时间解读并入发言习惯，绝不单独成块。"""
        prompt = self._prompt_of(_default_settings())
        self.assertIn("活动时间规律", prompt)
        self.assertNotIn("## 活动时间", prompt)  # 无独立标题

    def test_habits_off_activity_on_still_habits_section(self):
        """habits 关、activity 开 → 仍产出「## 发言习惯」但只含活动解读。"""
        prompt = self._prompt_of(
            _default_settings(profile_dim_habits=False, profile_dim_activity=True)
        )
        self.assertIn("## 发言习惯", prompt)
        self.assertIn("活动时间规律", prompt)
        self.assertNotIn("口头禅", prompt)  # 习惯描述不出现

    def test_habits_and_activity_both_off_no_habits_section(self):
        """habits 与 activity 均关 → 无发言习惯板块。"""
        prompt = self._prompt_of(
            _default_settings(profile_dim_habits=False, profile_dim_activity=False)
        )
        self.assertNotIn("## 发言习惯", prompt)
        self.assertNotIn("活动时间规律", prompt)

    def test_relations_off_drops_context_block(self):
        """关系维度关闭 → 无互动上下文块，上下文消息不进 prompt。"""
        contexts = [_ctx_msg(0)]
        prompt = self._prompt_of(
            _default_settings(profile_dim_relations=False), contexts=contexts
        )
        self.assertNotIn("【互动上下文】", prompt)
        self.assertNotIn("ctx-000", prompt)
        self.assertNotIn("## 人物关系", prompt)

    def test_relations_on_includes_context_block(self):
        """关系维度开启且有上下文 → 互动上下文块含样本。"""
        contexts = [_ctx_msg(0)]
        prompt = self._prompt_of(_default_settings(), contexts=contexts)
        self.assertIn("【互动上下文】", prompt)
        self.assertIn("ctx-000", prompt)
        self.assertIn("李四", prompt)  # 上下文行标注发言人

    def test_all_dims_off_fallback_overview(self):
        """全维度关闭 → 「## 人物画像」简要总评指示兜底。"""
        prompt = self._prompt_of(
            _default_settings(
                profile_dim_habits=False,
                profile_dim_activity=False,
                profile_dim_personality=False,
                profile_dim_hobbies=False,
                profile_dim_relations=False,
            )
        )
        self.assertIn("## 人物画像", prompt)
        self.assertIn("简要总评", prompt)

    def test_wording_constraint_always_present(self):
        """措辞约束（推测/避免绝对化/无需免责声明）恒定注入。"""
        prompt = self._prompt_of(_default_settings())
        self.assertIn("避免绝对化断言", prompt)
        self.assertIn("均为推测", prompt)
        self.assertIn("文末无需重复免责声明", prompt)
        self.assertIn("社群发言画像分析助手", prompt)  # 角色设定

    def test_image_mode_table_hint(self):
        """image 模式追加表格提示；forward 模式无。"""
        prompt_img = self._prompt_of(_default_settings(), output_mode="image")
        prompt_fwd = self._prompt_of(_default_settings(), output_mode="forward")
        self.assertIn("Markdown 表格", prompt_img)
        self.assertNotIn("Markdown 表格", prompt_fwd)


class TestSectionParsing(unittest.TestCase):
    """4 板块宽松切分与兜底。"""

    def setUp(self):
        _STUB_LOGGER.records["warning"].clear()
        self.analyzer = _make_analyzer(_FakeContext())

    def test_four_sections_parsed(self):
        raw = (
            "## 发言习惯\n习惯内容\n\n## 性格分析\n性格内容\n\n"
            "## 兴趣爱好\n爱好内容\n\n## 人物关系\n关系内容"
        )
        sections = self.analyzer._parse_sections(raw)
        self.assertEqual(
            [t for t, _ in sections], ["发言习惯", "性格分析", "兴趣爱好", "人物关系"]
        )
        self.assertEqual(sections[0][1], "习惯内容")
        self.assertEqual(sections[3][1], "关系内容")

    def test_out_of_order_normalized_to_canonical(self):
        """乱序输出 → 按规范顺序归一。"""
        raw = "## 人物关系\nR\n\n## 性格分析\nP\n\n## 发言习惯\nH\n\n## 兴趣爱好\nI"
        sections = self.analyzer._parse_sections(raw)
        self.assertEqual(
            [t for t, _ in sections], ["发言习惯", "性格分析", "兴趣爱好", "人物关系"]
        )

    def test_lenient_title_keywords(self):
        """标题被 LLM 微调（含关键词）→ 归一为规范标题。"""
        raw = "## 发言习惯与活跃时间\nH\n\n## 性格\nP"
        sections = self.analyzer._parse_sections(raw)
        self.assertEqual([t for t, _ in sections], ["发言习惯", "性格分析"])

    def test_duplicate_title_keeps_first(self):
        raw = "## 发言习惯\n第一份\n\n## 发言习惯\n第二份\n\n## 性格分析\nP"
        sections = self.analyzer._parse_sections(raw)
        self.assertEqual([t for t, _ in sections], ["发言习惯", "性格分析"])
        self.assertEqual(sections[0][1], "第一份")

    def test_single_heading_kept_not_fallback(self):
        """单个 ## 板块也算切分成功（仅启用单维度场景）。"""
        sections = self.analyzer._parse_sections("## 人物关系\n关系内容")
        self.assertEqual(sections, [("人物关系", "关系内容")])

    def test_no_heading_single_section_fallback(self):
        """无 ## 标题 → 单段 [("人物画像", 全文)]。"""
        raw = "这是一段没有板块标题的纯文本\n第二行"
        sections = self.analyzer._parse_sections(raw)
        self.assertEqual(sections, [("人物画像", raw)])
        self.assertIn("板块切分失败", _STUB_LOGGER.joined("warning"))

    def test_empty_raw_fallback(self):
        sections = self.analyzer._parse_sections("")
        self.assertEqual(sections, [("人物画像", "")])


class TestResultContract(unittest.TestCase):
    """ProfileResult 字段契约。"""

    def setUp(self):
        for level in ("info", "warning", "error"):
            _STUB_LOGGER.records[level].clear()

    def test_success_result_fields(self):
        messages = [_msg(i) for i in range(7)]
        ctx = _FakeContext(behaviors={"p1": FOUR_SECTIONS})
        analyzer = _make_analyzer(ctx, _default_settings(profile_provider="p1"))
        target = _target()
        stats = _stats()
        result = _run(
            analyzer.analyze(_FakeEvent(), target, stats, messages, [], "forward")
        )
        self.assertIs(result.target, target)  # 原样透传
        self.assertIs(result.stats, stats)
        self.assertEqual(result.raw_llm_text, FOUR_SECTIONS)
        self.assertEqual(result.messages_used, 7)
        # service 层填充字段保持默认
        self.assertEqual(result.sources, {})
        self.assertTrue(result.relation_context_complete)
        self.assertEqual(result.scope_desc, "")
        self.assertEqual(result.created_at, "")

    def test_failure_result_fields(self):
        ctx = _FakeContext(behaviors={"p1": RuntimeError("boom")})
        analyzer = _make_analyzer(ctx, _default_settings(profile_provider="p1"))
        target = _target()
        result = _run(
            analyzer.analyze(None, target, _stats(), [_msg(0)], [], "forward")
        )
        self.assertIs(result.target, target)
        self.assertEqual(result.provider_id, "")
        self.assertEqual(result.raw_llm_text, "")
        self.assertEqual(result.sections[0][0], "分析失败")
        self.assertEqual(result.sources, {})  # 默认保持

    def test_prompt_carries_structured_input(self):
        """结构化输入：基本信息/关键统计/互动排行/样本格式齐备。"""
        ctx = _FakeContext(behaviors={"p1": FOUR_SECTIONS})
        analyzer = _make_analyzer(ctx, _default_settings(profile_provider="p1"))
        messages = [_msg(0, content="大家好")]
        _run(
            analyzer.analyze(_FakeEvent(), _target(), _stats(), messages, [], "forward")
        )
        prompt = ctx.llm_calls[0][1]
        self.assertIn("昵称: 张三", prompt)
        self.assertIn("QQ: 123", prompt)
        self.assertIn("消息总数: 20", prompt)
        self.assertIn("活跃天数: 3", prompt)
        self.assertIn("23 点", prompt)  # peak_hour
        self.assertIn("周五", prompt)  # peak_weekday
        self.assertIn("12.5 字符", prompt)  # avg_length
        self.assertIn("10.0%", prompt)  # emoji_ratio
        self.assertIn("群 987654(20 条)", prompt)  # 各群分布
        self.assertIn("李四(QQ 222): 8 条", prompt)  # 互动排行
        self.assertIn("[987654][2026-08-01 20:00] 大家好", prompt)  # 样本格式

    def test_global_scope_desc_from_stats(self):
        """全局范围：scope=all 时范围描述取 stats.group_count。"""
        ctx = _FakeContext(behaviors={"p1": FOUR_SECTIONS})
        analyzer = _make_analyzer(ctx, _default_settings(profile_provider="p1"))
        stats = _stats(group_count=5)
        _run(
            analyzer.analyze(
                None, _target(scope="all", group_id=""), stats, [_msg(0)], [], "forward"
            )
        )
        self.assertIn("全部已保存群（共 5 个群）", ctx.llm_calls[0][1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
