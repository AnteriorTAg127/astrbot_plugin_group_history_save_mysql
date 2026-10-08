"""v0.5.0 模块 C 数据模型与指令解析（stats/models.py + stats/parser.py）冒烟脚本。

纯函数/纯数据类离线测试：**无需任何 astrbot / aiomysql stub**——模块 C 契约要求
不 import 框架模块，stats/__init__.py 为 PEP 562 惰性导出（导入 stats.models /
stats.parser 不拉起 repository 依赖链），直接导入即可。

覆盖点（对照分工契约与 PRD 2.1 F2 / 2.2）：

- models.py：6 个 dataclass 字段名序 / 默认值（top_n=10、image_count=0、
  active_senders=0）/ StatsData 全量构造
- parser.py：
  - 空串/纯空白 → 默认 (None, 今日)；now 缺省 datetime.now()
  - 10 个时间关键词逐一断言 start / end / label（固定 now 注入）
  - 单日 YYYY-MM-DD（start/end 精确值 + label）
  - 区间四种分隔符：到 / 至 / - / 空格（同一区间等价）
  - B<A 报错；跨度恰 366 天通过 / 367 天报错（含首尾自然日计）
  - 非法日期报错（2026-13-40 / 2026-02-30 / 形状不符 2026-8-1）
  - member：@ 单个/多个取首、@ 与数字并存优先 @、纯数字 QQ（7/20 位边界通过，
    6/21 位报无法识别）、多个 QQ 号报错
  - 无法识别 token 报错、重复时间范围报错
  - StatsParseError.usage 默认取 USAGE_TEXT，可覆盖
  - 全角空格容忍

运行方式（插件根目录，二者皆可）：
    python -m pytest "tests/v0.5.0/smoke_c_parser.py" -v
    python "tests/v0.5.0/smoke_c_parser.py"
"""

# ruff: noqa: I001

import os
import sys
import unittest
from dataclasses import fields
from datetime import datetime

# ============================================================
# 一、导入准备：多测试文件合跑兼容——仅剔除本文件即将导入的 stats.models /
#     stats.parser 缓存（勿删 stats.repository 等兄弟模块，避免破坏其他冒烟
#     脚本已绑定的对象）；本模块不 import 框架，无需 stub。
# ============================================================

for _name in list(sys.modules):
    if _name in (
        "astrbot_plugin_group_history_save_mysql.core.stats.models",
        "astrbot_plugin_group_history_save_mysql.core.stats.parser",
    ):
        del sys.modules[_name]

# 让被测包可被导入：<plugins 目录> 加入 sys.path
_PLUGINS_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..")
)
if _PLUGINS_DIR not in sys.path:
    sys.path.insert(0, _PLUGINS_DIR)

from astrbot_plugin_group_history_save_mysql.core.stats.models import (  # noqa: E402
    GroupRankItem,
    MemberStats,
    SenderRankItem,
    StatsData,
    StatsQuery,
    StatsTimeRange,
)
from astrbot_plugin_group_history_save_mysql.core.stats.parser import (  # noqa: E402
    USAGE_TEXT,
    StatsParseError,
    parse_stats_args,
)

# 固定注入时刻：2026-08-04 15:30:45（使今日=2026-08-04，明日=2026-08-05）
NOW = datetime(2026, 8, 4, 15, 30, 45)

# 今日/明日的精确边界（反复引用）
TODAY = datetime(2026, 8, 4)
TOMORROW = datetime(2026, 8, 5)


def _assert_parse_error(test: unittest.TestCase, message_str, at_targets=None):
    """断言解析抛出 StatsParseError，且 usage 为 USAGE_TEXT；返回异常实例。"""
    with test.assertRaises(StatsParseError) as ctx:
        parse_stats_args(message_str, at_targets or [], NOW)
    test.assertEqual(ctx.exception.usage, USAGE_TEXT)
    return ctx.exception


# ============================================================
# 二、models.py — 数据模型契约
# ============================================================


class TestModels(unittest.TestCase):
    """6 个 dataclass 字段名序与默认值逐一对齐分工契约。"""

    def _names(self, cls):
        return [f.name for f in fields(cls)]

    def test_stats_time_range_fields(self):
        self.assertEqual(self._names(StatsTimeRange), ["start", "end", "label"])

    def test_stats_query_fields_and_defaults(self):
        self.assertEqual(
            self._names(StatsQuery), ["group_id", "member_id", "time_range", "top_n"]
        )
        tr = StatsTimeRange(start=TODAY, end=TOMORROW, label="今日")
        query = StatsQuery(group_id=None, member_id=None, time_range=tr)
        self.assertEqual(query.top_n, 10)  # 契约默认 10

    def test_sender_rank_item_defaults(self):
        self.assertEqual(
            self._names(SenderRankItem),
            ["sender_id", "sender_name", "count", "image_count"],
        )
        item = SenderRankItem(sender_id="1", sender_name="甲", count=5)
        self.assertEqual(item.image_count, 0)

    def test_group_rank_item_defaults(self):
        self.assertEqual(
            self._names(GroupRankItem),
            ["group_id", "count", "image_count", "active_senders"],
        )
        item = GroupRankItem(group_id="100", count=5)
        self.assertEqual(item.image_count, 0)
        self.assertEqual(item.active_senders, 0)

    def test_member_stats_fields(self):
        self.assertEqual(
            self._names(MemberStats),
            [
                "sender_id",
                "sender_name",
                "count",
                "image_count",
                "ratio",
                "rank",
                "active_days",
                "avg_per_day",
                "hourly_dist",
                "weekday_dist",
            ],
        )

    def test_stats_data_fields_and_construction(self):
        self.assertEqual(
            self._names(StatsData),
            [
                "query",
                "total_messages",
                "total_images",
                "active_senders",
                "peak_hour",
                "first_msg_time",
                "last_msg_time",
                "daily_trend",
                "hourly_dist",
                "weekday_dist",
                "sender_ranking",
                "group_ranking",
                "member",
                "generated_at",
            ],
        )
        # 全量构造冒烟（含可空字段 None 与嵌套 dataclass 列表）
        tr = StatsTimeRange(start=TODAY, end=TOMORROW, label="今日")
        query = StatsQuery(group_id="100", member_id=None, time_range=tr, top_n=10)
        data = StatsData(
            query=query,
            total_messages=42,
            total_images=3,
            active_senders=7,
            peak_hour=21,
            first_msg_time=TODAY,
            last_msg_time=TODAY,
            daily_trend=[{"date": "2026-08-04", "count": 42}],
            hourly_dist=[0] * 24,
            weekday_dist=[0] * 7,
            sender_ranking=[SenderRankItem(sender_id="1", sender_name="甲", count=42)],
            group_ranking=[],
            member=None,
            generated_at=NOW,
        )
        self.assertEqual(data.total_messages, 42)
        self.assertIsNone(data.member)
        self.assertEqual(len(data.hourly_dist), 24)
        self.assertEqual(len(data.weekday_dist), 7)


# ============================================================
# 三、parser.py — 默认行为与 USAGE_TEXT
# ============================================================


class TestDefaultAndUsage(unittest.TestCase):
    def test_empty_string_defaults_to_today(self):
        member, tr = parse_stats_args("", [], NOW)
        self.assertIsNone(member)
        self.assertEqual(tr.start, TODAY)
        self.assertEqual(tr.end, TOMORROW)
        self.assertEqual(tr.label, "今日")

    def test_whitespace_only_defaults_to_today(self):
        member, tr = parse_stats_args("    　  ", [], NOW)  # 半角+全角空白
        self.assertIsNone(member)
        self.assertEqual(tr.label, "今日")
        self.assertEqual(tr.start, TODAY)

    def test_now_defaults_to_local_now(self):
        # now 缺省 datetime.now()：无法断言精确值，仅断言「今日」语义成立
        member, tr = parse_stats_args("", [])
        self.assertIsNone(member)
        self.assertEqual(tr.label, "今日")
        self.assertEqual(tr.start.hour, 0)
        self.assertEqual(tr.start.minute, 0)
        self.assertEqual((tr.end - tr.start).days, 1)

    def test_usage_text_content(self):
        self.assertIsInstance(USAGE_TEXT, str)
        lines = USAGE_TEXT.splitlines()
        self.assertGreater(len(lines), 3)  # 多行
        for kw in ("今日", "昨日", "7天", "30天", "全部", "2026-08-01", "@", "QQ"):
            self.assertIn(kw, USAGE_TEXT)

    def test_parse_error_carries_usage(self):
        err = _assert_parse_error(self, "叽里咕噜")
        self.assertEqual(err.usage, USAGE_TEXT)

    def test_parse_error_custom_usage(self):
        try:
            raise StatsParseError("boom", usage="自定义用法")
        except StatsParseError as e:
            self.assertEqual(e.usage, "自定义用法")


# ============================================================
# 四、parser.py — 时间关键词（10 个逐一断言）
# ============================================================


class TestKeywords(unittest.TestCase):
    def _assert_keyword(self, keyword, start, end, label):
        member, tr = parse_stats_args(keyword, [], NOW)
        self.assertIsNone(member, f"关键词 {keyword} 不应解析出 member")
        self.assertEqual(tr.start, start, f"关键词 {keyword} start 不符")
        self.assertEqual(tr.end, end, f"关键词 {keyword} end 不符")
        self.assertEqual(tr.label, label, f"关键词 {keyword} label 不符")

    def test_keyword_today(self):
        self._assert_keyword("今日", TODAY, TOMORROW, "今日")

    def test_keyword_today_alias(self):
        self._assert_keyword("今天", TODAY, TOMORROW, "今日")

    def test_keyword_yesterday(self):
        self._assert_keyword("昨日", datetime(2026, 8, 3), TODAY, "昨日")

    def test_keyword_yesterday_alias(self):
        self._assert_keyword("昨天", datetime(2026, 8, 3), TODAY, "昨日")

    def test_keyword_7d(self):
        # [今日-6天 00:00, 明日 00:00)：08-04 往前 6 天 = 07-29
        self._assert_keyword("7天", datetime(2026, 7, 29), TOMORROW, "近7天")

    def test_keyword_7d_alias(self):
        self._assert_keyword("近7天", datetime(2026, 7, 29), TOMORROW, "近7天")

    def test_keyword_30d(self):
        # [今日-29天 00:00, 明日 00:00)：08-04 往前 29 天 = 07-06
        self._assert_keyword("30天", datetime(2026, 7, 6), TOMORROW, "近30天")

    def test_keyword_30d_alias(self):
        self._assert_keyword("近30天", datetime(2026, 7, 6), TOMORROW, "近30天")

    def test_keyword_all(self):
        self._assert_keyword("全部", datetime(2000, 1, 1), TOMORROW, "全部")

    def test_keyword_all_alias(self):
        self._assert_keyword("所有", datetime(2000, 1, 1), TOMORROW, "全部")


# ============================================================
# 五、parser.py — 自定义日期（单日 / 四种区间分隔符 / 边界）
# ============================================================


class TestCustomDates(unittest.TestCase):
    def test_single_day(self):
        member, tr = parse_stats_args("2026-08-01", [], NOW)
        self.assertIsNone(member)
        self.assertEqual(tr.start, datetime(2026, 8, 1))
        self.assertEqual(tr.end, datetime(2026, 8, 2))  # 次日 00:00（不含）
        self.assertEqual(tr.label, "2026-08-01")

    def test_single_day_independent_of_now(self):
        # 自定义日期与 now 无关：now 在区间之外也照常解析
        _, tr = parse_stats_args("2020-02-29", [], NOW)  # 闰日合法
        self.assertEqual(tr.start, datetime(2020, 2, 29))
        self.assertEqual(tr.end, datetime(2020, 3, 1))
        self.assertEqual(tr.label, "2020-02-29")

    def _assert_range(self, message_str):
        member, tr = parse_stats_args(message_str, [], NOW)
        self.assertIsNone(member)
        self.assertEqual(tr.start, datetime(2026, 8, 1), f"{message_str} start 不符")
        self.assertEqual(
            tr.end, datetime(2026, 8, 5), f"{message_str} end 应为 B 次日 00:00"
        )
        self.assertEqual(
            tr.label, "2026-08-01 ~ 2026-08-04", f"{message_str} label 不符"
        )

    def test_range_separator_dao(self):
        self._assert_range("2026-08-01到2026-08-04")

    def test_range_separator_zhi(self):
        self._assert_range("2026-08-01至2026-08-04")

    def test_range_separator_dash(self):
        self._assert_range("2026-08-01-2026-08-04")

    def test_range_separator_space(self):
        self._assert_range("2026-08-01 2026-08-04")

    def test_range_same_day_equals_single_day_span(self):
        _, tr = parse_stats_args("2026-08-01到2026-08-01", [], NOW)
        self.assertEqual(tr.start, datetime(2026, 8, 1))
        self.assertEqual(tr.end, datetime(2026, 8, 2))
        self.assertEqual(tr.label, "2026-08-01 ~ 2026-08-01")

    def test_range_span_366_ok(self):
        # 2024 闰年：01-01 ~ 12-31 含首尾共 366 天，恰好放行
        _, tr = parse_stats_args("2024-01-01到2024-12-31", [], NOW)
        self.assertEqual(tr.start, datetime(2024, 1, 1))
        self.assertEqual(tr.end, datetime(2025, 1, 1))

    def test_range_span_367_rejected(self):
        _assert_parse_error(self, "2024-01-01到2025-01-01")  # 含首尾 367 天

    def test_range_end_before_start_rejected(self):
        _assert_parse_error(self, "2026-08-04到2026-08-01")

    def test_invalid_date_value_month(self):
        _assert_parse_error(self, "2026-13-40")

    def test_invalid_date_value_day(self):
        _assert_parse_error(self, "2026-02-30")

    def test_invalid_date_shape_single_digit(self):
        # 形状不符（非 4-2-2 位）→ 无法识别 → 同样 StatsParseError
        _assert_parse_error(self, "2026-8-1")

    def test_invalid_date_inside_range(self):
        _assert_parse_error(self, "2026-08-01到2026-13-40")

    def test_non_leap_feb_29_rejected(self):
        _assert_parse_error(self, "2026-02-29")  # 2026 平年无 2-29


# ============================================================
# 六、parser.py — member 解析（@ 与 QQ 号）
# ============================================================


class TestMember(unittest.TestCase):
    def test_at_target_single(self):
        member, tr = parse_stats_args("", ["111222333"], NOW)
        self.assertEqual(member, "111222333")
        self.assertEqual(tr.label, "今日")

    def test_at_target_multi_takes_first(self):
        member, _ = parse_stats_args("7天", ["111", "222", "333"], NOW)
        self.assertEqual(member, "111")

    def test_at_beats_qq_number(self):
        # @ 与数字并存 → 优先 @；数字 token 本身合法（不报无法识别）
        member, tr = parse_stats_args("7天 987654321", ["111222333"], NOW)
        self.assertEqual(member, "111222333")
        self.assertEqual(tr.label, "近7天")

    def test_qq_number_only(self):
        member, tr = parse_stats_args("123456789", [], NOW)
        self.assertEqual(member, "123456789")
        self.assertEqual(tr.label, "今日")
        self.assertEqual(tr.start, TODAY)

    def test_qq_min_length_7(self):
        member, _ = parse_stats_args("1234567", [], NOW)
        self.assertEqual(member, "1234567")

    def test_qq_max_length_20(self):
        member, _ = parse_stats_args("12345678901234567890", [], NOW)
        self.assertEqual(member, "12345678901234567890")

    def test_qq_too_short_unrecognized(self):
        _assert_parse_error(self, "123456")  # 6 位

    def test_qq_too_long_unrecognized(self):
        _assert_parse_error(self, "123456789012345678901")  # 21 位

    def test_two_qq_numbers_rejected(self):
        _assert_parse_error(self, "123456789 987654321")

    def test_member_and_keyword_any_order(self):
        m1, tr1 = parse_stats_args("昨日 123456789", [], NOW)
        m2, tr2 = parse_stats_args("123456789 昨日", [], NOW)
        self.assertEqual((m1, tr1.label), ("123456789", "昨日"))
        self.assertEqual((m2, tr2.label), ("123456789", "昨日"))
        self.assertEqual(tr1.start, tr2.start)

    def test_member_with_custom_range(self):
        member, tr = parse_stats_args("2026-08-01到2026-08-04 123456789", [], NOW)
        self.assertEqual(member, "123456789")
        self.assertEqual(tr.start, datetime(2026, 8, 1))
        self.assertEqual(tr.end, datetime(2026, 8, 5))

    def test_fullwidth_space_tolerated(self):
        # 全角空格分隔：关键词 + QQ 号
        member, tr = parse_stats_args("昨日　123456789", [], NOW)
        self.assertEqual(member, "123456789")
        self.assertEqual(tr.label, "昨日")


# ============================================================
# 七、parser.py — 无法识别 / 冲突参数
# ============================================================


class TestUnrecognized(unittest.TestCase):
    def test_junk_token(self):
        _assert_parse_error(self, "哈哈")

    def test_junk_mixed_with_keyword(self):
        _assert_parse_error(self, "7天 哈哈")

    def test_duplicate_keywords_rejected(self):
        _assert_parse_error(self, "今日 昨日")

    def test_keyword_plus_date_rejected(self):
        _assert_parse_error(self, "2026-08-01 今日")

    def test_range_plus_extra_date_rejected(self):
        # 前两个日期构成区间，第三个日期 → 重复时间范围
        _assert_parse_error(self, "2026-08-01 2026-08-02 2026-08-03")

    def test_two_equal_dates_form_same_day_range(self):
        # 「A B」空格分隔合法且 B==A 允许 → 等价单日区间（label 为区间形式）
        member, tr = parse_stats_args("2026-08-01 2026-08-01", [], NOW)
        self.assertIsNone(member)
        self.assertEqual(tr.start, datetime(2026, 8, 1))
        self.assertEqual(tr.end, datetime(2026, 8, 2))
        self.assertEqual(tr.label, "2026-08-01 ~ 2026-08-01")

    def test_error_messages_readable(self):
        err = _assert_parse_error(self, "哈哈")
        self.assertIn("哈哈", str(err))


if __name__ == "__main__":
    unittest.main(verbosity=2)
