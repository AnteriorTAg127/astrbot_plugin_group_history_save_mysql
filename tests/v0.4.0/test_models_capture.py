# ruff: noqa: I001
"""astrbot_plugin_group_history_save_mysql v0.4.0 Module C 离线单元测试。

覆盖「人物分析」数据模型与捕获函数（纯函数/数据类，无副作用）：

- profile/models.py：
  - 5 个 dataclass 字段默认值（ProfileTarget / ProfileMessage / ProfileStats /
    ProfileResult / ProfileFetchOutcome）；
  - ``mysql_row_to_profile_message``：正常行、at_list 解析（逗号分隔/strip/去空）、
    NULL 兜底、坏时间戳三级回退（epoch）、ISO 时间戳回退。
- profile/capture.py：
  - ``extract_at_targets``：多 At 去重保序、剔除 @全体成员(all)、无 At 返回 []、
    异常/垃圾组件跳过、None 链返回 []；
  - ``extract_reply_id``：消息链 Reply 组件优先、raw_message 回退、
    Reply id 为空时穿透到 raw_message、皆无返回 ""、event 为 None 返回 ""。

运行方式（插件根目录，二者皆可）：
    python -m pytest "tests/v0.4.0/test_models_capture.py" -v
    python "tests/v0.4.0/test_models_capture.py"

范式沿用 tests/v0.3.2/test_v032.py：astrbot.* 全部 sys.modules stub，且 stub
必须在 import 被测包之前完成；不依赖 AstrBot 运行时，消息链/event 用 fake 对象模拟。
"""

import os
import sys
import types
import unittest
from datetime import datetime


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


# ---- astrbot.api: logger（带记录能力，供 warning/debug 断言）----
class _StubLogger:
    """记录各级日志内容，便于断言（如时间戳回退 warning）。"""

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


# ---- astrbot.api.message_components: At / AtAll / Reply / Plain ----
# 字段名经核实 astrbot/core/message/components.py：
#   At.qq (int|str，"all" 代表所有人)；Reply.id (str|int，所引用的消息 ID)。
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


class _StubAtBoom(_StubAt):
    """qq 访问即抛异常的坏组件，用于验证「异常组件跳过」防御路径。"""

    def __init__(self):
        # 绕过父类 __init__ 的 self.qq 赋值（下方以只读 property 覆盖 qq）
        pass

    @property
    def qq(self):
        raise RuntimeError("boom")


class _StubReply:
    def __init__(self, id=""):
        self.id = id


_astrbot_mc.Plain = _StubPlain
_astrbot_mc.At = _StubAt
_astrbot_mc.AtAll = _StubAtAll
_astrbot_mc.Reply = _StubReply


# ---- 多测试文件合跑兼容：仅剔除本文件需要的 profile 子模块（而非整个插件包），
#      使其重新执行并绑定到本文件的 stub。切忌删除 summary.* 等兄弟子包——
#      它们可能已被其他版本测试文件（如 v0.3.2）导入并缓存，误删会触发二次导入
#      产生重复类对象，破坏其 isinstance 断言（如 T2IRenderer）。 ----
for _name in list(sys.modules):
    if _name.startswith("astrbot_plugin_group_history_save_mysql.core.profile"):
        del sys.modules[_name]

# ---- 注入 sys.modules ----
sys.modules["astrbot"] = _astrbot
sys.modules["astrbot.api"] = _astrbot_api
sys.modules["astrbot.api.message_components"] = _astrbot_mc

# 让被测包可被导入：<plugins 目录> 加入 sys.path
_PLUGINS_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..")
)
if _PLUGINS_DIR not in sys.path:
    sys.path.insert(0, _PLUGINS_DIR)


# ============================================================
# 二、导入被测代码（stub 已就位）
# ============================================================

from astrbot_plugin_group_history_save_mysql.core.profile import capture as capture_mod  # noqa: E402
from astrbot_plugin_group_history_save_mysql.core.profile.models import (  # noqa: E402
    ProfileFetchOutcome,
    ProfileMessage,
    ProfileResult,
    ProfileStats,
    ProfileTarget,
    _parse_at_list,
    mysql_row_to_profile_message,
)

extract_at_targets = capture_mod.extract_at_targets
extract_reply_id = capture_mod.extract_reply_id


# ============================================================
# 三、fake event（模拟 AstrMessageEvent：get_messages + message_obj.raw_message）
# ============================================================


class _FakeMessageObj:
    def __init__(self, raw_message=None):
        self.raw_message = raw_message


class _FakeEvent:
    def __init__(self, chain=None, raw_message=None):
        self._chain = chain or []
        self.message_obj = _FakeMessageObj(raw_message)

    def get_messages(self):
        return self._chain


# ============================================================
# 四、测试用例
# ============================================================


class TestDataclassDefaults(unittest.TestCase):
    """5 个 dataclass 的字段与默认值契约。"""

    def test_profile_target_fields(self):
        t = ProfileTarget(sender_id="123", sender_name="张三", scope="all", group_id="")
        self.assertEqual(t.sender_id, "123")
        self.assertEqual(t.scope, "all")
        self.assertEqual(t.group_id, "")

    def test_profile_message_defaults(self):
        m = ProfileMessage(
            timestamp=datetime(2026, 8, 2, 10, 0, 0),
            group_id="999",
            sender_id="123",
            sender_name="张三",
            content="hi",
            message_id="m1",
            source="mysql",
        )
        self.assertEqual(m.at_list, [])  # default_factory=list
        self.assertEqual(m.reply_id, "")

    def test_profile_message_at_list_independent(self):
        # default_factory 应产出相互独立的 list，避免共享默认值陷阱
        a = ProfileMessage(datetime.now(), "", "", "", "", "", "mysql")
        b = ProfileMessage(datetime.now(), "", "", "", "", "", "mysql")
        a.at_list.append("x")
        self.assertEqual(b.at_list, [])

    def test_profile_stats_truncated_default(self):
        s = ProfileStats(
            total=0,
            group_count=0,
            group_breakdown=[],
            time_start=None,
            time_end=None,
            active_days=0,
            hour_dist=[0] * 24,
            weekday_dist=[0] * 7,
            peak_hour=0,
            peak_weekday=0,
            avg_length=0.0,
            total_chars=0,
            emoji_ratio=0.0,
            question_ratio=0.0,
            top_partners=[],
        )
        self.assertFalse(s.truncated)
        self.assertEqual(len(s.hour_dist), 24)
        self.assertEqual(len(s.weekday_dist), 7)

    def test_profile_result_defaults(self):
        target = ProfileTarget("1", "n", "group", "999")
        stats = ProfileStats(
            0,
            0,
            [],
            None,
            None,
            0,
            [0] * 24,
            [0] * 7,
            0,
            0,
            0.0,
            0,
            0.0,
            0.0,
            [],
        )
        r = ProfileResult(
            target=target,
            stats=stats,
            sections=[],
            raw_llm_text="",
            provider_id="p",
            messages_used=0,
        )
        self.assertEqual(r.sources, {})
        self.assertTrue(r.relation_context_complete)
        self.assertEqual(r.scope_desc, "")
        self.assertEqual(r.created_at, "")

    def test_profile_fetch_outcome_defaults(self):
        o = ProfileFetchOutcome(target_messages=[], context_messages=[], partners=[])
        self.assertEqual(o.sources, {})
        self.assertFalse(o.onebot_attempted)
        self.assertIsNone(o.onebot_error)
        self.assertTrue(o.relation_context_complete)


class TestMysqlRowToProfileMessage(unittest.TestCase):
    """mysql_row_to_profile_message 归一化。"""

    def _row(self, **overrides):
        base = {
            "id": 1,
            "timestamp": "2026-08-02 10:30:45",
            "group_id": "987654",
            "sender_id": "123456",
            "sender_name": "张三",
            "message_type": "text",
            "content": "hello world",
            "message_id": "m-001",
            "at_list": "111,222",
            "reply_id": "r-001",
        }
        base.update(overrides)
        return base

    def test_normal_row(self):
        m = mysql_row_to_profile_message(self._row())
        self.assertEqual(m.timestamp, datetime(2026, 8, 2, 10, 30, 45))
        self.assertEqual(m.group_id, "987654")
        self.assertEqual(m.sender_id, "123456")
        self.assertEqual(m.sender_name, "张三")
        self.assertEqual(m.content, "hello world")
        self.assertEqual(m.message_id, "m-001")
        self.assertEqual(m.source, "mysql")
        self.assertEqual(m.at_list, ["111", "222"])
        self.assertEqual(m.reply_id, "r-001")

    def test_at_list_parse_strip_and_drop_empty(self):
        m = mysql_row_to_profile_message(self._row(at_list=" 111 , ,222 ,"))
        self.assertEqual(m.at_list, ["111", "222"])

    def test_at_list_empty_and_null(self):
        self.assertEqual(
            mysql_row_to_profile_message(self._row(at_list="")).at_list, []
        )
        self.assertEqual(
            mysql_row_to_profile_message(self._row(at_list=None)).at_list, []
        )

    def test_null_fields_fallback(self):
        row = {
            "timestamp": "2026-08-02 10:30:45",
            "group_id": None,
            "sender_id": None,
            "sender_name": None,
            "content": None,
            "message_id": None,
            "at_list": None,
            "reply_id": None,
        }
        m = mysql_row_to_profile_message(row)
        self.assertEqual(m.group_id, "")
        self.assertEqual(m.sender_id, "")
        self.assertEqual(m.sender_name, "")
        self.assertEqual(m.content, "")
        self.assertEqual(m.message_id, "")
        self.assertEqual(m.at_list, [])
        self.assertEqual(m.reply_id, "")

    def test_missing_keys_fallback(self):
        # 完全缺失新增列（旧 SELECT 未追加 at_list/reply_id）也应安全兜底
        m = mysql_row_to_profile_message({"timestamp": "2026-08-02 10:30:45"})
        self.assertEqual(m.at_list, [])
        self.assertEqual(m.reply_id, "")

    def test_bad_timestamp_fallback_epoch_with_warning(self):
        _STUB_LOGGER.records["warning"].clear()
        m = mysql_row_to_profile_message(self._row(timestamp="not-a-date"))
        self.assertEqual(m.timestamp, datetime.fromtimestamp(0))
        self.assertIn("无法解析 MySQL 消息时间戳", _STUB_LOGGER.joined("warning"))

    def test_iso_timestamp_second_fallback(self):
        # 非 "%Y-%m-%d %H:%M:%S" 但合法 ISO → fromisoformat 成功，不回退 epoch
        _STUB_LOGGER.records["warning"].clear()
        m = mysql_row_to_profile_message(self._row(timestamp="2026-08-02T10:30:45"))
        self.assertEqual(m.timestamp, datetime(2026, 8, 2, 10, 30, 45))
        self.assertEqual(_STUB_LOGGER.records["warning"], [])

    def test_none_timestamp_fallback_epoch(self):
        m = mysql_row_to_profile_message(self._row(timestamp=None))
        self.assertEqual(m.timestamp, datetime.fromtimestamp(0))


class TestParseAtList(unittest.TestCase):
    """_parse_at_list 私有辅助的边界。"""

    def test_variants(self):
        self.assertEqual(_parse_at_list("123,456"), ["123", "456"])
        self.assertEqual(_parse_at_list(" 123 ,, 456 "), ["123", "456"])
        self.assertEqual(_parse_at_list(""), [])
        self.assertEqual(_parse_at_list("   "), [])
        self.assertEqual(_parse_at_list(None), [])
        self.assertEqual(_parse_at_list("solo"), ["solo"])


class TestExtractAtTargets(unittest.TestCase):
    """extract_at_targets 防御式提取。"""

    def test_dedup_preserve_order(self):
        chain = [
            _StubAt(qq=111),
            _StubPlain("hi"),
            _StubAt(qq="222"),
            _StubAt(qq=111),  # 重复
            _StubAt(qq="333"),
        ]
        self.assertEqual(extract_at_targets(chain), ["111", "222", "333"])

    def test_exclude_at_all(self):
        chain = [_StubAt(qq=111), _StubAtAll(), _StubAt(qq="all"), _StubAt(qq=222)]
        # @全体成员（AtAll / qq=="all"）非真实用户，应剔除
        self.assertEqual(extract_at_targets(chain), ["111", "222"])

    def test_no_at_returns_empty(self):
        chain = [_StubPlain("just text"), _StubReply(id="x")]
        self.assertEqual(extract_at_targets(chain), [])

    def test_none_chain_returns_empty(self):
        self.assertEqual(extract_at_targets(None), [])

    def test_empty_chain_returns_empty(self):
        self.assertEqual(extract_at_targets([]), [])

    def test_skip_garbage_components(self):
        chain = [None, "junk", 42, _StubAt(qq=123), {"not": "a comp"}]
        self.assertEqual(extract_at_targets(chain), ["123"])

    def test_skip_boom_component(self):
        # qq 访问抛异常的坏组件被跳过，其余正常提取
        _STUB_LOGGER.records["debug"].clear()
        chain = [_StubAtBoom(), _StubAt(qq=999)]
        self.assertEqual(extract_at_targets(chain), ["999"])
        self.assertIn("解析 At 组件失败", _STUB_LOGGER.joined("debug"))

    def test_skip_empty_qq(self):
        chain = [_StubAt(qq=""), _StubAt(qq="   "), _StubAt(qq=7)]
        self.assertEqual(extract_at_targets(chain), ["7"])


class TestExtractReplyId(unittest.TestCase):
    """extract_reply_id 两级来源与降级。"""

    def test_reply_component_priority(self):
        chain = [_StubPlain("hi"), _StubReply(id="m-123")]
        event = _FakeEvent(chain=chain)
        self.assertEqual(extract_reply_id(event), "m-123")

    def test_reply_component_int_id_stringified(self):
        event = _FakeEvent(chain=[_StubReply(id=456)])
        self.assertEqual(extract_reply_id(event), "456")

    def test_raw_message_fallback(self):
        # 消息链无 Reply → 回退 raw_message 的 reply 段 data.id
        raw = {
            "message": [
                {"type": "text", "data": {"text": "hi"}},
                {"type": "reply", "data": {"id": "m-999"}},
            ]
        }
        event = _FakeEvent(chain=[_StubPlain("hi")], raw_message=raw)
        self.assertEqual(extract_reply_id(event), "m-999")

    def test_empty_component_id_falls_through_to_raw(self):
        # Reply 组件 id 为空 → 穿透到 raw_message
        raw = {"message": [{"type": "reply", "data": {"id": "m-5"}}]}
        event = _FakeEvent(chain=[_StubReply(id="")], raw_message=raw)
        self.assertEqual(extract_reply_id(event), "m-5")

    def test_neither_returns_empty(self):
        event = _FakeEvent(chain=[_StubPlain("hi")], raw_message=None)
        self.assertEqual(extract_reply_id(event), "")

    def test_raw_message_not_dict_returns_empty(self):
        event = _FakeEvent(chain=[_StubPlain("hi")], raw_message="garbage")
        self.assertEqual(extract_reply_id(event), "")

    def test_raw_message_no_reply_segment_returns_empty(self):
        raw = {"message": [{"type": "text", "data": {"text": "hi"}}]}
        event = _FakeEvent(chain=[_StubPlain("hi")], raw_message=raw)
        self.assertEqual(extract_reply_id(event), "")

    def test_raw_reply_empty_id_returns_empty(self):
        raw = {"message": [{"type": "reply", "data": {"id": ""}}]}
        event = _FakeEvent(chain=[], raw_message=raw)
        self.assertEqual(extract_reply_id(event), "")

    def test_event_none_returns_empty(self):
        self.assertEqual(extract_reply_id(None), "")

    def test_event_without_message_obj_returns_empty(self):
        class _Bare:
            def get_messages(self):
                return []

        self.assertEqual(extract_reply_id(_Bare()), "")

    def test_malformed_segments_skipped(self):
        # 段数组中混杂非 dict / 缺 data，不崩，取到合法 reply
        raw = {
            "message": [
                "junk",
                None,
                {"type": "reply"},  # 无 data
                {"type": "reply", "data": {"id": "m-ok"}},
            ]
        }
        event = _FakeEvent(chain=[], raw_message=raw)
        self.assertEqual(extract_reply_id(event), "m-ok")


if __name__ == "__main__":
    unittest.main(verbosity=2)
