# ruff: noqa: I001
"""astrbot_plugin_group_history_save_mysql v0.4.0 Module J 离线单元测试。

覆盖「人物分析」编排层（profile/service.py，ProfileService）：

- 校验链（严格按序）：总开关 profile_enabled → 权限 profile_permission
  （admin 运行时 event.is_admin() 判定、未知值按 admin / all 放行）→ 群环境 →
  限流（用户/群双冷却，检查与盖章分离）。
- 目标解析：@ 优先（剔除 "all" 与 bot 自身）→ 纯数字 QQ号 → 皆无用法提示。
- run_analysis：单群 vs 全局（scope/group_id 推导，event=None 全局透传 fetcher）。
- result 注入：sources / scope_desc / created_at / relation_context_complete。
- storage.save 容错：返回空串 / ValueError / 其他异常均不阻断。
- 触发反馈：reaction（成功/失败降级文字）/ text / none。
- 生命周期 start/stop（失败吞异常）。
- 端到端 mock（fetch→stats→analyze→save→format→send）走通。
- 异常兜底不冒泡（fetcher/analyzer/formatter 抛异常均降级）。

运行方式（插件根目录，二者皆可）：
    python -m pytest "tests/v0.4.0/test_profile_service.py" -v
    python "tests/v0.4.0/test_profile_service.py"

范式沿用 tests/v0.4.0/test_profile_analyzer.py（Agent-C stub 隔离技巧）：
astrbot.* 全部 sys.modules stub；注入前仅剔除 profile 子模块；
**profile/formatter.py 由 Agent-H 并行开发（尚未落地），本测试以 fake 模块注入
sys.modules 满足 service.py 的 ``from .formatter import ProfileFormatter``**，
按固定契约（render(result, mode) -> MessageChain）编码，不依赖其真实实现。
"""

import asyncio
import os
import sys
import types
import unittest
from datetime import datetime


# ============================================================
# 一、stub 注入（必须在任何 from astrbot_plugin_group_history_save_mysql... import 之前）
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

    def joined(self, level: str) -> str:
        return "\n".join(self.records[level])


_STUB_LOGGER = _StubLogger()
_astrbot_api.logger = _STUB_LOGGER

# ---- aiomysql（db_mysql.py 顶层导入；fetcher 经 QUERY_TIMEOUT_SECONDS 引入）----
_aiomysql = _new_module("aiomysql")
_aiomysql.connect = None
_aiomysql.DictCursor = "DictCursor"
_aiomysql.Connection = object


# ---- astrbot.api.event: AstrMessageEvent（类型引用）+ MessageChain（t2i_render 引用）----
class _StubAstrMessageEvent:
    pass


class _StubMessageChain:
    def __init__(self, chain=None):
        self.chain = chain or []


_astrbot_event.AstrMessageEvent = _StubAstrMessageEvent
_astrbot_event.MessageChain = _StubMessageChain


# ---- astrbot.api.star: Context / Star / StarTools ----
class _StubContext:
    pass


class _StubStar:
    pass


class _StubStarTools:
    @classmethod
    def get_data_dir(cls, plugin_name=None):
        import tempfile
        from pathlib import Path

        return Path(tempfile.gettempdir()) / (plugin_name or "test")


_astrbot_star.Context = _StubContext
_astrbot_star.Star = _StubStar
_astrbot_star.StarTools = _StubStarTools


# ---- astrbot.api.message_components: At / AtAll / Reply / Plain ----
# 字段名经核实 astrbot/core/message/components.py（同 test_models_capture.py）
class _StubPlain:
    def __init__(self, text=""):
        self.text = text


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
_astrbot_mc.At = _StubAt
_astrbot_mc.AtAll = _StubAtAll
_astrbot_mc.Reply = _StubReply


# ---- 多测试文件合跑兼容：仅剔除 profile 子模块（Agent-C stub 隔离技巧）----
for _name in list(sys.modules):
    if _name.startswith("astrbot_plugin_group_history_save_mysql.core.profile"):
        del sys.modules[_name]

# ---- 注入 sys.modules ----
sys.modules["astrbot"] = _astrbot
sys.modules["astrbot.api"] = _astrbot_api
sys.modules["astrbot.api.event"] = _astrbot_event
sys.modules["astrbot.api.star"] = _astrbot_star
sys.modules["astrbot.api.message_components"] = _astrbot_mc
sys.modules["aiomysql"] = _aiomysql

# 让被测包可被导入：<plugins 目录> 加入 sys.path
_PLUGINS_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..")
)
if _PLUGINS_DIR not in sys.path:
    sys.path.insert(0, _PLUGINS_DIR)


# ============================================================
# 二、fake formatter 模块注入（Agent-H 尚未交付 profile/formatter.py）
#      必须在 import service 之前，使 service.py 的 from .formatter import 可解析
# ============================================================

# 渲染链返回的消息链哨兵（端到端断言 event.send 收到它）
SENTINEL_CHAIN = _StubMessageChain([_StubPlain("profile-report")])


class FakeFormatter:
    """按固定契约模拟 ProfileFormatter：render(result, mode) -> MessageChain。"""

    def __init__(self, star, config_mgr, renderer):
        self.star = star
        self.config_mgr = config_mgr
        self.renderer = renderer  # ProfileT2IRenderer 实例（service 注入）
        self.render_calls = []  # [(result, mode)]
        self.render_result = SENTINEL_CHAIN
        self.fail = None  # 设置异常实例 → render 抛出

    async def render(self, result, output_mode):
        self.render_calls.append((result, output_mode))
        if self.fail is not None:
            raise self.fail
        return self.render_result


_fake_formatter_mod = _new_module(
    "astrbot_plugin_group_history_save_mysql.core.profile.formatter"
)
_fake_formatter_mod.ProfileFormatter = FakeFormatter
sys.modules["astrbot_plugin_group_history_save_mysql.core.profile.formatter"] = (
    _fake_formatter_mod
)


# ============================================================
# 三、导入被测代码（stub 已就位）
# ============================================================

from astrbot_plugin_group_history_save_mysql.core.profile.models import (  # noqa: E402
    ProfileFetchOutcome,
    ProfileMessage,
    ProfileResult,
    ProfileTarget,
)
from astrbot_plugin_group_history_save_mysql.core.profile.service import (  # noqa: E402
    ProfileService,
)
from astrbot_plugin_group_history_save_mysql.core.profile.stats import (  # noqa: E402
    ProfileStatsBuilder,
)
from astrbot_plugin_group_history_save_mysql.core.profile.storage import (  # noqa: E402
    ProfileStorage,
)


# ============================================================
# 四、fake 依赖（context / config_mgr / mysql_mgr / star / event / 下游模块）
# ============================================================


def _default_settings(**overrides):
    settings = {
        "profile_enabled": True,
        "profile_permission": "admin",
        "profile_user_cooldown": 60,
        "profile_group_cooldown": 30,
        "profile_feedback_mode": "none",  # 默认关闭，避免多数测试触发反馈路径
        "profile_feedback_text": "生成中…",
        "profile_output_mode": "forward",
    }
    settings.update(overrides)
    return settings


class FakeConfigMgr:
    def __init__(self, settings=None):
        # 部分覆盖合并到默认值之上（避免漏项导致总开关等缺失被误判为关闭）
        self.settings = _default_settings(**(settings or {}))
        self.groups = []  # 白名单群（get_groups 返回）
        self.fail_groups = False
        self.fail_all_settings = False

    async def get_profile_setting(self, key):
        value = self.settings.get(key, "")
        return "" if value is None else str(value)

    async def get_profile_setting_typed(self, key):
        return self.settings.get(key)

    async def get_all_settings(self):
        if self.fail_all_settings:
            raise RuntimeError("boom")
        return {
            key: "true" if value is True else ("false" if value is False else str(value))
            for key, value in self.settings.items()
        }

    async def get_groups(self):
        if self.fail_groups:
            raise RuntimeError("boom")
        return list(self.groups)


class FakeContext:
    pass


class FakeStar:
    pass


class FakeMySQL:
    pass


class FakeEvent:
    """模拟 AstrMessageEvent：承载校验链/目标解析/发送所需的最小接口。"""

    def __init__(
        self,
        group_id="9001",
        sender_id="111",
        self_id="999",
        role_admin=True,
        messages=None,
        session_id="sess-1",
    ):
        self._group_id = group_id
        self._sender_id = sender_id
        self._self_id = self_id
        self._role_admin = role_admin
        self._messages = messages or []
        self._session_id = session_id
        self.sent = []  # send() 收到的全部消息链（含提示性回复）
        self.result_sent = []  # 仅分析结果消息链（排除 plain_result 提示）
        self.replies = []  # plain_result() 生成的提示文本
        # message_id 用数字串（OneBot 实际为整数，贴表情路径需 int() 可转）
        self.message_obj = types.SimpleNamespace(message_id="12345")
        self.bot = None
        self.unified_msg_origin = f"aiocqhttp:GroupMessage:{group_id}"

    def get_group_id(self):
        return self._group_id

    def get_sender_id(self):
        return self._sender_id

    def get_self_id(self):
        return self._self_id

    def get_session_id(self):
        return self._session_id

    def get_messages(self):
        return self._messages

    def is_admin(self):
        return self._role_admin

    def plain_result(self, text):
        self.replies.append(text)
        chain = _StubMessageChain([_StubPlain(text)])
        chain._is_reply = True  # 标记为提示性回复，send 时不计入 result_sent
        return chain

    async def send(self, chain):
        self.sent.append(chain)
        if not getattr(chain, "_is_reply", False):
            self.result_sent.append(chain)


class FakeFetcher:
    """脚本化 ProfileFetcher.fetch：返回预设 outcome / 抛异常；记录入参。"""

    def __init__(self, outcome=None, fail=None, groups=None, fail_groups=False):
        self.outcome = outcome if outcome is not None else _empty_outcome()
        self.fail = fail
        self.calls = []  # [(target, event)]
        self.groups = list(groups or [])  # get_all_groups_summary 返回
        self.fail_groups = fail_groups

    async def fetch(self, target, event):
        self.calls.append((target, event))
        if self.fail is not None:
            raise self.fail
        return self.outcome

    async def get_all_groups_summary(self):
        if self.fail_groups:
            raise RuntimeError("groups boom")
        return [dict(g) for g in self.groups]


class FakeStatsBuilder:
    """脚本化 ProfileStatsBuilder.build：返回预设 stats；记录入参。"""

    def __init__(self, stats=None):
        self.stats = stats if stats is not None else _empty_stats()
        self.calls = []

    def build(self, target_messages, partners, truncated=False):
        self.calls.append((target_messages, partners, truncated))
        return self.stats


class FakeAnalyzer:
    """脚本化 ProfileAnalyzer.analyze：返回预设 result / 抛异常；记录入参。"""

    def __init__(self, result=None, fail=None):
        self._result = result
        self.fail = fail
        self.calls = []

    async def analyze(
        self, event, target, stats, target_messages, context_messages, output_mode
    ):
        self.calls.append(
            (event, target, stats, target_messages, context_messages, output_mode)
        )
        if self.fail is not None:
            raise self.fail
        if self._result is not None:
            return self._result
        # 默认返回一个「成功」结果（provider_id 非空，sources 等保持默认待注入）
        return ProfileResult(
            target=target,
            stats=stats,
            sections=[("发言习惯", "示例")],
            raw_llm_text="## 发言习惯\n示例",
            provider_id="test-provider",
            messages_used=len(target_messages),
        )


class FakeStorage:
    """脚本化 ProfileStorage.save：返回相对名 / 空串 / 抛异常；记录入参。"""

    def __init__(self, save_return="group_9001/20260802_103045_123.json", fail=None):
        self.save_return = save_return
        self.fail = fail
        self.save_calls = []

    async def save(self, result):
        self.save_calls.append(result)
        if self.fail is not None:
            raise self.fail
        return self.save_return


class FakeScheduler:
    def __init__(self, start_fail=None, stop_fail=None):
        self.start_fail = start_fail
        self.stop_fail = stop_fail
        self.started = 0
        self.stopped = 0

    async def start(self):
        self.started += 1
        if self.start_fail is not None:
            raise self.start_fail

    async def stop(self):
        self.stopped += 1
        if self.stop_fail is not None:
            raise self.stop_fail


class FakeBotAPI:
    def __init__(self, fail=None):
        self.fail = fail
        self.actions = []

    async def call_action(self, action, **kwargs):
        self.actions.append((action, kwargs))
        if self.fail is not None:
            raise self.fail
        return {"status": "ok"}


# ============================================================
# 五、构造辅助
# ============================================================


def _msg(mid, sid, name, minute, content="hello", gid="9001"):
    return ProfileMessage(
        timestamp=datetime(2026, 8, 1, 10 + minute // 60, minute % 60),
        group_id=gid,
        sender_id=sid,
        sender_name=name,
        content=content,
        message_id=mid,
        source="mysql",
    )


def _empty_stats():
    return ProfileStatsBuilder().build([], [])


def _empty_outcome():
    return ProfileFetchOutcome(target_messages=[], context_messages=[], partners=[])


def _outcome_with_messages(messages, partners=None, sources=None):
    return ProfileFetchOutcome(
        target_messages=messages,
        context_messages=[],
        partners=partners or [],
        sources=sources or {"mysql": len(messages), "onebot": 0},
    )


def make_service(
    settings=None,
    fetcher=None,
    stats_builder=None,
    analyzer=None,
    storage=None,
    scheduler=None,
):
    """构造 ProfileService（fake context/config/mysql/star），并按需替换下游实例。"""
    config_mgr = FakeConfigMgr(settings)
    svc = ProfileService(
        context=FakeContext(),
        config_mgr=config_mgr,
        mysql_mgr=FakeMySQL(),
        star=FakeStar(),
    )
    if fetcher is not None:
        svc._fetcher = fetcher
    if stats_builder is not None:
        svc._stats_builder = stats_builder
    if analyzer is not None:
        svc._analyzer = analyzer
    if storage is not None:
        svc.storage = storage
    if scheduler is not None:
        svc._scheduler = scheduler
    return svc


def run(coro):
    return asyncio.run(coro)


# ============================================================
# 六、测试用例
# ============================================================


class TestValidationChain(unittest.TestCase):
    """校验链：总开关 → 权限 → 群环境 → 限流（检查与盖章分离）。"""

    def test_disabled(self):
        svc = make_service(settings={"profile_enabled": False})
        event = FakeEvent()
        run(svc.handle_command(event, "123"))
        self.assertIn("人物分析功能未开启", event.replies)
        self.assertEqual(event.result_sent, [])

    def test_permission_admin_nonadmin_rejected(self):
        svc = make_service(settings={"profile_permission": "admin"})
        event = FakeEvent(role_admin=False)
        run(svc.handle_command(event, "123"))
        self.assertIn("该指令仅管理员可用", event.replies)
        self.assertEqual(event.result_sent, [])

    def test_permission_admin_admin_allowed(self):
        fetcher = FakeFetcher()
        svc = make_service(
            settings={"profile_permission": "admin"},
            fetcher=fetcher,
            storage=FakeStorage(),
        )
        event = FakeEvent(role_admin=True, messages=[_StubAt(qq="123")])
        run(svc.handle_command(event, ""))
        self.assertNotIn("该指令仅管理员可用", event.replies)
        self.assertEqual(len(fetcher.calls), 1)

    def test_permission_all_nonadmin_allowed(self):
        fetcher = FakeFetcher()
        svc = make_service(
            settings={"profile_permission": "all"},
            fetcher=fetcher,
            storage=FakeStorage(),
        )
        event = FakeEvent(role_admin=False, messages=[_StubAt(qq="123")])
        run(svc.handle_command(event, ""))
        self.assertNotIn("该指令仅管理员可用", event.replies)
        self.assertEqual(len(fetcher.calls), 1)

    def test_permission_unknown_treated_as_admin(self):
        svc = make_service(settings={"profile_permission": "weird"})
        event = FakeEvent(role_admin=False)
        run(svc.handle_command(event, "123"))
        self.assertIn("该指令仅管理员可用", event.replies)

    def test_is_admin_exception_safe_nonadmin(self):
        svc = make_service(settings={"profile_permission": "admin"})

        class _BoomEvent(FakeEvent):
            def is_admin(self):
                raise RuntimeError("boom")

        event = _BoomEvent()
        run(svc.handle_command(event, "123"))
        self.assertIn("该指令仅管理员可用", event.replies)

    def test_not_group(self):
        svc = make_service()
        event = FakeEvent(group_id="")  # 非群消息
        run(svc.handle_command(event, "123"))
        self.assertIn("请在群内使用", event.replies)

    def test_cooldown_user(self):
        fetcher = FakeFetcher()
        svc = make_service(
            settings={"profile_user_cooldown": 60, "profile_group_cooldown": 0},
            fetcher=fetcher,
            storage=FakeStorage(),
        )
        event1 = FakeEvent(sender_id="111", messages=[_StubAt(qq="123")])
        run(svc.handle_command(event1, ""))
        self.assertEqual(len(fetcher.calls), 1)
        # 同一用户立即再次触发 → 用户冷却拦截
        event2 = FakeEvent(sender_id="111", messages=[_StubAt(qq="123")])
        run(svc.handle_command(event2, ""))
        self.assertTrue(any("操作太频繁" in r for r in event2.replies))
        self.assertEqual(len(fetcher.calls), 1)  # 未执行第二次分析

    def test_cooldown_group(self):
        fetcher = FakeFetcher()
        svc = make_service(
            settings={"profile_user_cooldown": 0, "profile_group_cooldown": 30},
            fetcher=fetcher,
            storage=FakeStorage(),
        )
        run(
            svc.handle_command(
                FakeEvent(sender_id="111", messages=[_StubAt(qq="123")]), ""
            )
        )
        # 不同用户、同群立即触发 → 群冷却拦截
        event2 = FakeEvent(sender_id="222", messages=[_StubAt(qq="123")])
        run(svc.handle_command(event2, ""))
        self.assertTrue(any("操作太频繁" in r for r in event2.replies))
        self.assertEqual(len(fetcher.calls), 1)

    def test_check_stamp_separation(self):
        """无效目标（用法提示）不盖章：随后的合法指令不受冷却影响。"""
        fetcher = FakeFetcher()
        svc = make_service(
            settings={"profile_user_cooldown": 60, "profile_group_cooldown": 60},
            fetcher=fetcher,
            storage=FakeStorage(),
        )
        # 第一次：无 @ 无数字参数 → 用法提示，不应盖章
        event1 = FakeEvent(sender_id="111", messages=[])
        run(svc.handle_command(event1, "not-a-qq"))
        self.assertTrue(any("请 @" in r for r in event1.replies))
        self.assertEqual(svc._user_last, {})  # 未盖章
        self.assertEqual(svc._group_last, {})
        # 第二次：合法目标 → 不应被冷却拦截
        event2 = FakeEvent(sender_id="111", messages=[_StubAt(qq="123")])
        run(svc.handle_command(event2, ""))
        self.assertFalse(any("操作太频繁" in r for r in event2.replies))
        self.assertEqual(len(fetcher.calls), 1)
        # 此时才盖章
        self.assertIn("111", svc._user_last)


class TestTargetResolution(unittest.TestCase):
    """目标解析：@ 优先（剔除 all/bot）→ 纯数字 QQ → 皆无用法提示。"""

    def _run_and_get_target(self, messages, arg, self_id="999"):
        fetcher = FakeFetcher()
        svc = make_service(
            settings={"profile_permission": "all"},
            fetcher=fetcher,
            storage=FakeStorage(),
        )
        event = FakeEvent(self_id=self_id, messages=messages)
        run(svc.handle_command(event, arg))
        if not fetcher.calls:
            return None, event
        return fetcher.calls[0][0].sender_id, event

    def test_at_priority_over_arg(self):
        target, _ = self._run_and_get_target([_StubAt(qq="123")], "456")
        self.assertEqual(target, "123")

    def test_at_strips_all_and_bot(self):
        target, _ = self._run_and_get_target(
            [_StubAtAll(), _StubAt(qq="999"), _StubAt(qq="777")], "", self_id="999"
        )
        self.assertEqual(target, "777")

    def test_at_only_bot_falls_back_to_arg(self):
        target, _ = self._run_and_get_target([_StubAt(qq="999")], "555", self_id="999")
        self.assertEqual(target, "555")

    def test_qq_arg(self):
        target, _ = self._run_and_get_target([], "12345678")
        self.assertEqual(target, "12345678")

    def test_none_usage(self):
        target, event = self._run_and_get_target([], "abc")
        self.assertIsNone(target)
        self.assertTrue(any("请 @" in r for r in event.replies))


class TestRunAnalysisScope(unittest.TestCase):
    """run_analysis：单群 vs 全局 scope/group_id 推导 + event 透传。"""

    def test_scope_group(self):
        fetcher = FakeFetcher()
        svc = make_service(fetcher=fetcher, storage=FakeStorage())
        run(svc.run_analysis("123", "9001"))
        target, _event = fetcher.calls[0]
        self.assertEqual(target.scope, "group")
        self.assertEqual(target.group_id, "9001")
        self.assertEqual(target.sender_id, "123")

    def test_scope_all_empty(self):
        fetcher = FakeFetcher()
        svc = make_service(fetcher=fetcher, storage=FakeStorage())
        run(svc.run_analysis("123", ""))
        target, _ = fetcher.calls[0]
        self.assertEqual(target.scope, "all")
        self.assertEqual(target.group_id, "")

    def test_scope_all_literal(self):
        fetcher = FakeFetcher()
        svc = make_service(fetcher=fetcher, storage=FakeStorage())
        run(svc.run_analysis("123", "all"))
        self.assertEqual(fetcher.calls[0][0].scope, "all")

    def test_scope_all_case_insensitive(self):
        fetcher = FakeFetcher()
        svc = make_service(fetcher=fetcher, storage=FakeStorage())
        run(svc.run_analysis("123", "ALL"))
        self.assertEqual(fetcher.calls[0][0].scope, "all")

    def test_web_event_none_passthrough(self):
        fetcher = FakeFetcher()
        svc = make_service(fetcher=fetcher, storage=FakeStorage())
        run(svc.run_analysis("123", "", event=None))
        _, event = fetcher.calls[0]
        self.assertIsNone(event)

    def test_sender_name_backfilled_from_latest_message(self):
        msgs = [_msg("m1", "123", "旧昵称", 0), _msg("m2", "123", "新昵称", 5)]
        fetcher = FakeFetcher(outcome=_outcome_with_messages(msgs))
        analyzer = FakeAnalyzer()
        svc = make_service(fetcher=fetcher, analyzer=analyzer, storage=FakeStorage())
        result = run(svc.run_analysis("123", "9001"))
        self.assertEqual(result.target.sender_name, "新昵称")


class TestResultInjection(unittest.TestCase):
    """run_analysis 注入 sources / scope_desc / created_at / relation_context_complete。"""

    def test_injection_group(self):
        msgs = [_msg("m1", "123", "某人", 0)]
        outcome = _outcome_with_messages(msgs, sources={"mysql": 1, "onebot": 2})
        outcome.relation_context_complete = False
        svc = make_service(
            fetcher=FakeFetcher(outcome=outcome),
            analyzer=FakeAnalyzer(),
            storage=FakeStorage(),
        )
        result = run(svc.run_analysis("123", "9001"))
        self.assertEqual(result.sources, {"mysql": 1, "onebot": 2})
        self.assertEqual(result.scope_desc, "群 9001")
        self.assertFalse(result.relation_context_complete)
        self.assertTrue(result.created_at)  # 非空 ISO 时间串

    def test_injection_all_scope_desc(self):
        msgs = [
            _msg("m1", "123", "某人", 0, gid="9001"),
            _msg("m2", "123", "某人", 5, gid="9002"),
        ]
        # 用真实 stats builder 以得到 group_count=2
        svc = make_service(
            fetcher=FakeFetcher(outcome=_outcome_with_messages(msgs)),
            stats_builder=ProfileStatsBuilder(),
            analyzer=FakeAnalyzer(),
            storage=FakeStorage(),
        )
        result = run(svc.run_analysis("123", "all"))
        self.assertEqual(result.scope_desc, "全部已保存群（2 个）")


class TestStorageTolerance(unittest.TestCase):
    """storage.save 返回空串 / ValueError / 其他异常均不阻断结果返回。"""

    def test_save_empty_not_blocking(self):
        msgs = [_msg("m1", "123", "某人", 0)]
        svc = make_service(
            fetcher=FakeFetcher(outcome=_outcome_with_messages(msgs)),
            analyzer=FakeAnalyzer(),
            storage=FakeStorage(save_return=""),
        )
        result = run(svc.run_analysis("123", "9001"))
        self.assertEqual(result.provider_id, "test-provider")  # 正常返回

    def test_save_valueerror_not_blocking(self):
        msgs = [_msg("m1", "123", "某人", 0)]
        svc = make_service(
            fetcher=FakeFetcher(outcome=_outcome_with_messages(msgs)),
            analyzer=FakeAnalyzer(),
            storage=FakeStorage(fail=ValueError("非法 scope")),
        )
        result = run(svc.run_analysis("123", "9001"))
        self.assertEqual(result.provider_id, "test-provider")

    def test_save_exception_not_blocking(self):
        msgs = [_msg("m1", "123", "某人", 0)]
        svc = make_service(
            fetcher=FakeFetcher(outcome=_outcome_with_messages(msgs)),
            analyzer=FakeAnalyzer(),
            storage=FakeStorage(fail=RuntimeError("disk error")),
        )
        result = run(svc.run_analysis("123", "9001"))
        self.assertEqual(result.provider_id, "test-provider")

    def test_storage_property_is_profile_storage(self):
        svc = make_service()
        self.assertIsInstance(svc.storage, ProfileStorage)


class TestEndToEnd(unittest.TestCase):
    """端到端：fetch → stats(真实) → analyze → save → format → send。"""

    def test_end_to_end_command(self):
        msgs = [
            _msg("m1", "123", "某人", 0, content="你好呀"),
            _msg("m2", "123", "某人", 5, content="今天天气不错？"),
        ]
        outcome = _outcome_with_messages(msgs, partners=[("222", "伙伴", 3)])
        fetcher = FakeFetcher(outcome=outcome)
        storage = FakeStorage(save_return="group_9001/20260802_103045_123.json")
        analyzer = FakeAnalyzer()
        svc = make_service(
            settings={"profile_permission": "all", "profile_feedback_mode": "none"},
            fetcher=fetcher,
            stats_builder=ProfileStatsBuilder(),  # 真实统计
            analyzer=analyzer,
            storage=storage,
        )
        event = FakeEvent(messages=[_StubAt(qq="123")])
        run(svc.handle_command(event, ""))

        # 各环节均被调用
        self.assertEqual(len(fetcher.calls), 1)
        self.assertEqual(len(analyzer.calls), 1)
        self.assertEqual(len(storage.save_calls), 1)
        self.assertEqual(len(svc._formatter.render_calls), 1)
        # 最终发送的是 formatter 产出的消息链
        self.assertEqual(event.result_sent, [SENTINEL_CHAIN])
        # render 收到注入后的 result 与 forward 模式
        rendered_result, mode = svc._formatter.render_calls[0]
        self.assertEqual(mode, "forward")
        self.assertEqual(rendered_result.sources, {"mysql": 2, "onebot": 0})
        self.assertEqual(rendered_result.scope_desc, "群 9001")
        self.assertEqual(rendered_result.stats.total, 2)  # 真实统计
        # 无提示性错误文案
        self.assertEqual(event.replies, [])

    def test_no_material_short_circuit(self):
        """空素材：不调 analyzer，回复「没有可分析的消息」。"""
        analyzer = FakeAnalyzer()
        svc = make_service(
            settings={"profile_permission": "all"},
            fetcher=FakeFetcher(outcome=_empty_outcome()),
            analyzer=analyzer,
            storage=FakeStorage(),
        )
        event = FakeEvent(messages=[_StubAt(qq="123")])
        run(svc.handle_command(event, ""))
        self.assertEqual(analyzer.calls, [])  # 未调 LLM
        self.assertTrue(any("没有可分析的消息" in r for r in event.replies))
        self.assertEqual(event.result_sent, [])

    def test_llm_failed_fallback_text(self):
        """LLM 整链耗尽（provider_id 空但素材非空）：回复「分析失败」，不渲染。"""
        msgs = [_msg("m1", "123", "某人", 0)]
        # analyzer 返回的 result 携带其收到的统计（total>0），仅 provider_id 为空
        failed = ProfileResult(
            target=ProfileTarget("123", "某人", "group", "9001"),
            stats=ProfileStatsBuilder().build(msgs, []),
            sections=[("分析失败", "x")],
            raw_llm_text="",
            provider_id="",
            messages_used=1,
        )
        svc = make_service(
            settings={"profile_permission": "all"},
            fetcher=FakeFetcher(outcome=_outcome_with_messages(msgs)),
            stats_builder=ProfileStatsBuilder(),
            analyzer=FakeAnalyzer(result=failed),
            storage=FakeStorage(),
        )
        event = FakeEvent(messages=[_StubAt(qq="123")])
        run(svc.handle_command(event, ""))
        self.assertTrue(any("生成失败" in r for r in event.replies))
        self.assertEqual(svc._formatter.render_calls, [])


class TestExceptionFallback(unittest.TestCase):
    """异常兜底：fetcher/analyzer/formatter 抛异常均降级，绝不冒泡。"""

    def test_run_analysis_fetcher_raises_returns_degraded(self):
        svc = make_service(
            fetcher=FakeFetcher(fail=RuntimeError("db down")),
            storage=FakeStorage(),
        )
        result = run(svc.run_analysis("123", "9001"))  # 不应抛
        self.assertIsInstance(result, ProfileResult)
        self.assertEqual(result.provider_id, "")
        self.assertEqual(result.sections[0][0], "分析失败")
        self.assertTrue(result.created_at)
        self.assertEqual(result.scope_desc, "群 9001")

    def test_run_analysis_all_scope_degraded_desc(self):
        svc = make_service(
            fetcher=FakeFetcher(fail=RuntimeError("boom")), storage=FakeStorage()
        )
        result = run(svc.run_analysis("123", "all"))
        self.assertEqual(result.scope_desc, "全部已保存群（0 个）")

    def test_handle_command_formatter_raises_no_bubble(self):
        msgs = [_msg("m1", "123", "某人", 0)]
        svc = make_service(
            settings={"profile_permission": "all"},
            fetcher=FakeFetcher(outcome=_outcome_with_messages(msgs)),
            stats_builder=ProfileStatsBuilder(),
            analyzer=FakeAnalyzer(),
            storage=FakeStorage(),
        )
        svc._formatter.fail = RuntimeError("render boom")
        event = FakeEvent(messages=[_StubAt(qq="123")])
        run(svc.handle_command(event, ""))  # 不应抛
        self.assertTrue(any("生成失败" in r for r in event.replies))

    def test_handle_command_analyzer_raises_no_bubble(self):
        msgs = [_msg("m1", "123", "某人", 0)]
        svc = make_service(
            settings={"profile_permission": "all"},
            fetcher=FakeFetcher(outcome=_outcome_with_messages(msgs)),
            stats_builder=ProfileStatsBuilder(),
            analyzer=FakeAnalyzer(fail=RuntimeError("llm boom")),
            storage=FakeStorage(),
        )
        event = FakeEvent(messages=[_StubAt(qq="123")])
        run(svc.handle_command(event, ""))  # run_analysis 兜底 → provider_id 空
        self.assertTrue(any("生成失败" in r for r in event.replies))


class TestFeedback(unittest.TestCase):
    """触发反馈：reaction（成功/降级文字）/ text / none。"""

    def _make(self, mode):
        # 使用有素材的 fetcher，让流程走到渲染（避免「没有可分析的消息」干扰反馈断言）
        msgs = [_msg("m1", "123", "某人", 0)]
        return make_service(
            settings={"profile_permission": "all", "profile_feedback_mode": mode},
            fetcher=FakeFetcher(outcome=_outcome_with_messages(msgs)),
            stats_builder=ProfileStatsBuilder(),
            analyzer=FakeAnalyzer(),
            storage=FakeStorage(),
        )

    def test_reaction_success_no_text(self):
        svc = self._make("reaction")
        api = FakeBotAPI()
        event = FakeEvent(messages=[_StubAt(qq="123")])
        event.bot = types.SimpleNamespace(api=api)
        run(svc.handle_command(event, ""))
        self.assertEqual(len(api.actions), 1)
        self.assertEqual(api.actions[0][0], "set_msg_emoji_like")
        self.assertEqual(event.replies, [])  # 未降级文字

    def test_reaction_fail_fallback_text(self):
        svc = self._make("reaction")
        event = FakeEvent(messages=[_StubAt(qq="123")])  # 无 bot → 贴表情失败
        run(svc.handle_command(event, ""))
        self.assertTrue(any("生成中…" in r for r in event.replies))

    def test_reaction_api_exception_fallback_text(self):
        svc = self._make("reaction")
        event = FakeEvent(messages=[_StubAt(qq="123")])
        event.bot = types.SimpleNamespace(api=FakeBotAPI(fail=RuntimeError("nope")))
        run(svc.handle_command(event, ""))
        self.assertTrue(any("生成中…" in r for r in event.replies))

    def test_text_mode(self):
        svc = self._make("text")
        event = FakeEvent(messages=[_StubAt(qq="123")])
        run(svc.handle_command(event, ""))
        self.assertTrue(any("生成中…" in r for r in event.replies))

    def test_none_mode(self):
        svc = self._make("none")
        event = FakeEvent(messages=[_StubAt(qq="123")])
        run(svc.handle_command(event, ""))
        self.assertEqual(event.replies, [])


class TestLifecycle(unittest.TestCase):
    """生命周期 start/stop：失败吞异常；stop 吞 CancelledError。"""

    def test_start_ok(self):
        sched = FakeScheduler()
        svc = make_service(scheduler=sched)
        run(svc.start())
        self.assertEqual(sched.started, 1)

    def test_start_failure_swallowed(self):
        sched = FakeScheduler(start_fail=RuntimeError("start boom"))
        svc = make_service(scheduler=sched)
        run(svc.start())  # 不应抛

    def test_stop_ok(self):
        sched = FakeScheduler()
        svc = make_service(scheduler=sched)
        run(svc.stop())
        self.assertEqual(sched.stopped, 1)

    def test_stop_cancelled_swallowed(self):
        sched = FakeScheduler(stop_fail=asyncio.CancelledError())
        svc = make_service(scheduler=sched)
        run(svc.stop())  # 不应抛


class TestLaunchGroups(unittest.TestCase):
    """Web 群下拉模式感知（v0.5.6）：is_all_mode / resolve_launch_groups。"""

    def test_is_all_mode_true_when_enabled(self):
        svc = make_service(settings={"all_mode": "true"})
        self.assertTrue(run(svc.is_all_mode()))

    def test_is_all_mode_false_default_and_exception(self):
        self.assertFalse(run(make_service(settings={}).is_all_mode()))
        cfg = make_service(settings={"all_mode": "true"})
        cfg._config_mgr.fail_all_settings = True
        self.assertFalse(run(cfg.is_all_mode()))  # 异常按白名单模式保守处理

    def test_whitelist_mode_merges_whitelist_and_data_groups(self):
        svc = make_service()
        svc._config_mgr.groups = [
            {"group_id": "9001", "enabled": True},
            {"group_id": "9003", "enabled": False},
        ]
        svc._fetcher = FakeFetcher(
            groups=[
                {"group_id": "9001", "count": 5, "last_active": None},
                {"group_id": "9002", "count": 9, "last_active": None},
            ]
        )
        got = run(svc.resolve_launch_groups())
        # 消息数降序；白名单无数据群补 count=None 但仍保留
        self.assertEqual(
            [g["group_id"] for g in got], ["9002", "9001", "9003"]
        )
        self.assertEqual(got[0], {"group_id": "9002", "enabled": True, "count": 9})
        self.assertEqual(got[1], {"group_id": "9001", "enabled": True, "count": 5})
        self.assertEqual(
            got[2], {"group_id": "9003", "enabled": False, "count": None}
        )

    def test_all_mode_data_only_groups(self):
        svc = make_service(settings={"all_mode": "true"})
        svc._fetcher = FakeFetcher(
            groups=[
                {"group_id": "200", "count": 3, "last_active": None},
                {"group_id": "100", "count": 7, "last_active": None},
            ]
        )
        got = run(svc.resolve_launch_groups())
        self.assertEqual([g["group_id"] for g in got], ["100", "200"])
        # 非白名单群 enabled 记 True（all_mode 无白名单概念）
        self.assertEqual(got[0]["enabled"], True)

    def test_whitelist_failure_degrades_to_data_groups(self):
        svc = make_service()
        svc._config_mgr.fail_groups = True
        svc._fetcher = FakeFetcher(
            groups=[{"group_id": "100", "count": 7, "last_active": None}]
        )
        got = run(svc.resolve_launch_groups())
        self.assertEqual([g["group_id"] for g in got], ["100"])

    def test_data_groups_failure_degrades_to_whitelist(self):
        svc = make_service()
        svc._config_mgr.groups = [{"group_id": "9001", "enabled": True}]
        svc._fetcher = FakeFetcher(fail_groups=True)
        got = run(svc.resolve_launch_groups())
        self.assertEqual(
            got, [{"group_id": "9001", "enabled": True, "count": None}]
        )

    def test_both_fail_returns_empty(self):
        svc = make_service()
        svc._config_mgr.fail_groups = True
        svc._fetcher = FakeFetcher(fail_groups=True)
        self.assertEqual(run(svc.resolve_launch_groups()), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
