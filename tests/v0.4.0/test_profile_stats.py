# ruff: noqa: I001
"""astrbot_plugin_group_history_save_mysql v0.4.0 Module E 离线单元测试。

覆盖「人物分析」确定性统计引擎（profile/stats.py，纯 Python 计算，无 AI/无副作用）：

- ``contains_emoji``：各区段命中（象形文字/杂项符号/箭头/区域指示符/FE0F 单例）与纯文本不误报；
- ``ProfileStatsBuilder.build``：
  - 构造已知消息集（跨 3 天/4 小时段/3 群/含 emoji/含 ASCII 与全中文问号/含中间问号干扰项），
    精确断言 hour_dist/weekday_dist/peak/active_days/group_breakdown/avg_length/
    emoji_ratio/question_ratio/time_start/time_end；
  - weekday 约定 Mon=0（用确定日期 2026-08-03 周一 / 2026-08-02 周日）；
  - 平局时 peak 取首个最大索引；
  - 空消息列表兜底（total=0、peak=0、ratio=0.0、time_start=None、分布全 0）；
  - partners 原样透传（不排序不修改）+ truncated 透传。

运行方式（插件根目录，二者皆可）：
    python -m pytest "tests/v0.4.0/test_profile_stats.py" -v
    python "tests/v0.4.0/test_profile_stats.py"

范式沿用 test_models_capture.py：astrbot.* 全部 sys.modules stub，且 stub 必须在
import 被测包之前完成；不依赖 AstrBot 运行时。
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


class _StubLogger:
    """最小日志桩：stats 模块不打日志，仅为满足 models.py 的 import。"""

    def info(self, msg, *args, **kwargs):
        pass

    def warning(self, msg, *args, **kwargs):
        pass

    def error(self, msg, *args, **kwargs):
        pass

    def debug(self, msg, *args, **kwargs):
        pass


_astrbot_api.logger = _StubLogger()


# ---- 多测试文件合跑兼容：仅剔除 profile 子模块使其重新绑定到本文件 stub，
#      不动 summary.* 等兄弟子包（避免破坏其他版本测试的缓存类对象）。 ----
for _name in list(sys.modules):
    if _name.startswith("astrbot_plugin_group_history_save_mysql.core.profile"):
        del sys.modules[_name]

sys.modules["astrbot"] = _astrbot
sys.modules["astrbot.api"] = _astrbot_api

# 让被测包可被导入：<plugins 目录> 加入 sys.path
_PLUGINS_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..")
)
if _PLUGINS_DIR not in sys.path:
    sys.path.insert(0, _PLUGINS_DIR)


# ============================================================
# 二、导入被测代码（stub 已就位）
# ============================================================

from astrbot_plugin_group_history_save_mysql.core.profile.models import (  # noqa: E402
    ProfileMessage,
)
from astrbot_plugin_group_history_save_mysql.core.profile.stats import (  # noqa: E402
    ProfileStatsBuilder,
    contains_emoji,
)


# ============================================================
# 三、构造辅助
# ============================================================


def _msg(ts: datetime, group_id: str, content: str) -> ProfileMessage:
    """快捷构造目标消息（统计只用到 timestamp/group_id/content，其余给占位值）。"""
    return ProfileMessage(
        timestamp=ts,
        group_id=group_id,
        sender_id="10001",
        sender_name="目标用户",
        content=content,
        message_id=f"m-{ts:%Y%m%d%H%M%S}-{group_id}",
        source="mysql",
    )


# 已知消息集（跨 3 天 / 小时段 3、9、23 / 3 个群；含 emoji、ASCII/全角问号、中间问号干扰）：
#   日期锚点：2026-07-27=周一(weekday 0)、2026-07-28=周二(1)、2026-08-01=周六(5)
#   m1 07-27 09:15 G1 "早上好"          len=3  普通
#   m2 07-27 09:40 G1 "吃了吗?"         len=4  ASCII 问号结尾 → question
#   m3 07-27 23:30 G1 "熬夜真爽😂"      len=5  U+1F602 → emoji
#   m4 07-28 23:05 G2 "晚安？"          len=3  全角问号结尾 → question
#   m5 07-28 23:50 G2 "☀️今天不错"      len=6  U+2600+U+FE0F → emoji
#   m6 08-01 03:00 G1 "hi"             len=2  普通
#   m7 08-01 09:10 G3 "真的吗"          len=3  无问号（吗字不算）
#   m8 08-01 09:30 G3 "为什么? 不知道"   len=8  问号在中间 → 不算 question
_KNOWN_MESSAGES = [
    _msg(datetime(2026, 7, 27, 9, 15), "G1", "早上好"),
    _msg(datetime(2026, 7, 27, 9, 40), "G1", "吃了吗?"),
    _msg(datetime(2026, 7, 27, 23, 30), "G1", "熬夜真爽😂"),
    _msg(datetime(2026, 7, 28, 23, 5), "G2", "晚安？"),
    _msg(datetime(2026, 7, 28, 23, 50), "G2", "☀️今天不错"),
    _msg(datetime(2026, 8, 1, 3, 0), "G1", "hi"),
    _msg(datetime(2026, 8, 1, 9, 10), "G3", "真的吗"),
    _msg(datetime(2026, 8, 1, 9, 30), "G3", "为什么? 不知道"),
]


# ============================================================
# 四、测试用例
# ============================================================


class TestContainsEmoji(unittest.TestCase):
    """emoji 区段判定的命中与不误报。"""

    def test_hits(self):
        self.assertTrue(contains_emoji("哈哈😄"))  # U+1F604 象形文字区
        self.assertTrue(contains_emoji("熬夜真爽😂"))  # U+1F602
        self.assertTrue(contains_emoji("❤"))  # U+2764 装饰符号区
        self.assertTrue(contains_emoji("☀今天"))  # U+2600 杂项符号区
        self.assertTrue(contains_emoji("看箭头→"))  # U+2192 箭头区
        self.assertTrue(contains_emoji("⭐"))  # U+2B50 杂项符号与箭头
        self.assertTrue(contains_emoji("🇨🇳"))  # U+1F1E8 区域指示符
        self.assertTrue(contains_emoji("1️⃣"))  # U+FE0F 单例命中
        self.assertTrue(contains_emoji("🀄"))  # U+1F004 麻将牌区
        self.assertTrue(contains_emoji("🂡"))  # U+1F0A1 扑克牌区

    def test_misses(self):
        self.assertFalse(contains_emoji(""))
        self.assertFalse(contains_emoji("hello world"))
        self.assertFalse(contains_emoji("纯中文，不含表情。"))
        self.assertFalse(contains_emoji("你好！?？"))  # 全半角标点均非 emoji
        self.assertFalse(
            contains_emoji("(^_^) y^_^y")
        )  # ASCII 颜文字不识别（有意取舍）


class TestBuildKnownSet(unittest.TestCase):
    """已知消息集的精确统计断言。"""

    @classmethod
    def setUpClass(cls):
        cls.stats = ProfileStatsBuilder().build(_KNOWN_MESSAGES, partners=[])

    def test_total_and_time_span(self):
        self.assertEqual(self.stats.total, 8)
        self.assertEqual(self.stats.time_start, datetime(2026, 7, 27, 9, 15))
        self.assertEqual(self.stats.time_end, datetime(2026, 8, 1, 9, 30))

    def test_group_breakdown_desc_with_tie_by_id(self):
        self.assertEqual(self.stats.group_count, 3)
        # G1=4；G2/G3 同数 2 → 按 group_id 升序稳定
        self.assertEqual(self.stats.group_breakdown, [("G1", 4), ("G2", 2), ("G3", 2)])

    def test_active_days(self):
        self.assertEqual(self.stats.active_days, 3)  # 07-27 / 07-28 / 08-01

    def test_hour_dist(self):
        expected = [0] * 24
        expected[3] = 1  # m6
        expected[9] = 4  # m1, m2, m7, m8
        expected[23] = 3  # m3, m4, m5
        self.assertEqual(self.stats.hour_dist, expected)
        self.assertEqual(self.stats.peak_hour, 9)  # 9 点 4 条为唯一峰值

    def test_weekday_dist_mon0_convention(self):
        # Mon(0)=3(07-27) / Tue(1)=2(07-28) / Sat(5)=3(08-01)，其余 0
        self.assertEqual(self.stats.weekday_dist, [3, 2, 0, 0, 0, 3, 0])
        # 周一=0 与周六=5 同为 3 → 首个最大索引 0
        self.assertEqual(self.stats.peak_weekday, 0)

    def test_length_stats(self):
        # 3+4+5+3+6+2+3+8 = 34
        self.assertEqual(self.stats.total_chars, 34)
        self.assertEqual(self.stats.avg_length, 34 / 8)

    def test_emoji_ratio(self):
        # m3(😂) 与 m5(☀️) 命中 → 2/8
        self.assertEqual(self.stats.emoji_ratio, 2 / 8)

    def test_question_ratio(self):
        # m2(ASCII?) 与 m4(全角？) 命中；m8 问号在中间不计 → 2/8
        self.assertEqual(self.stats.question_ratio, 2 / 8)


class TestWeekdayConvention(unittest.TestCase):
    """weekday 约定：datetime.weekday() 周一=0 … 周日=6（用确定日期锚定）。"""

    def test_monday_maps_to_index0_and_sunday_to_index6(self):
        msgs = [
            _msg(datetime(2026, 8, 3, 10, 0), "G1", "周一发言"),  # 周一
            _msg(datetime(2026, 8, 2, 10, 0), "G1", "周日发言"),  # 周日
        ]
        stats = ProfileStatsBuilder().build(msgs, partners=[])
        self.assertEqual(stats.weekday_dist, [1, 0, 0, 0, 0, 0, 1])
        self.assertEqual(stats.peak_weekday, 0)  # 平局取首个最大索引

    def test_anchor_dates_are_correct(self):
        # 测试自身的日期锚点防呆（防止将来误改测试数据）
        self.assertEqual(datetime(2026, 8, 3).weekday(), 0)
        self.assertEqual(datetime(2026, 8, 2).weekday(), 6)
        self.assertEqual(datetime(2026, 7, 27).weekday(), 0)


class TestBuildEmpty(unittest.TestCase):
    """空消息列表兜底。"""

    def test_empty_defaults(self):
        stats = ProfileStatsBuilder().build([], partners=[])
        self.assertEqual(stats.total, 0)
        self.assertEqual(stats.group_count, 0)
        self.assertEqual(stats.group_breakdown, [])
        self.assertIsNone(stats.time_start)
        self.assertIsNone(stats.time_end)
        self.assertEqual(stats.active_days, 0)
        self.assertEqual(stats.hour_dist, [0] * 24)
        self.assertEqual(stats.weekday_dist, [0] * 7)
        self.assertEqual(stats.peak_hour, 0)
        self.assertEqual(stats.peak_weekday, 0)
        self.assertEqual(stats.avg_length, 0.0)
        self.assertEqual(stats.total_chars, 0)
        self.assertEqual(stats.emoji_ratio, 0.0)
        self.assertEqual(stats.question_ratio, 0.0)
        self.assertEqual(stats.top_partners, [])
        self.assertFalse(stats.truncated)


class TestPartnersAndTruncatedPassthrough(unittest.TestCase):
    """partners 与 truncated 透传。"""

    def test_partners_passed_through_verbatim(self):
        partners = [("222", "李四", 10), ("111", "张三", 3), ("333", "王五", 3)]
        stats = ProfileStatsBuilder().build(_KNOWN_MESSAGES, partners=partners)
        # 原样透传：顺序/内容不变（builder 不做任何重排）
        self.assertEqual(stats.top_partners, partners)
        self.assertIs(stats.top_partners, partners)

    def test_truncated_passthrough(self):
        stats = ProfileStatsBuilder().build(_KNOWN_MESSAGES, [], truncated=True)
        self.assertTrue(stats.truncated)
        stats2 = ProfileStatsBuilder().build(_KNOWN_MESSAGES, [])
        self.assertFalse(stats2.truncated)  # 默认 False


class TestQuestionEdgeCases(unittest.TestCase):
    """问号判定的空白与边界。"""

    def test_strip_then_endswith(self):
        msgs = [
            _msg(datetime(2026, 8, 3, 1, 0), "G1", "  前后空白?  "),  # strip 后命中
            _msg(datetime(2026, 8, 3, 2, 0), "G1", "？"),  # 单全角问号命中
            _msg(datetime(2026, 8, 3, 3, 0), "G1", ""),  # 空内容不命中
            _msg(datetime(2026, 8, 3, 4, 0), "G1", "   "),  # 纯空白不命中
            _msg(datetime(2026, 8, 3, 5, 0), "G1", "陈述句。"),  # 句号结尾不命中
        ]
        stats = ProfileStatsBuilder().build(msgs, partners=[])
        self.assertEqual(stats.question_ratio, 2 / 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
