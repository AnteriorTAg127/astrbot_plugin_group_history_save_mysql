"""/群统计 指令参数解析（v0.5.0 数据分析）。

将指令名之后的自由参数文本解析为 ``(member_id, StatsTimeRange)`` 二元组，供
``main.py`` 指令 handler 与 ``stats.service.StatsService`` 使用。

解析规则（契约见 ``开发/v0.5.0/分工.md``「接口契约 → StatsQuery 时间范围解析」，
与 PRD 2.1 F2 / 2.2 一致）：

- **时间关键词**（大小写不敏感、容忍全角/半角空白）：
  今日/今天(默认) / 昨日/昨天 / 7天/近7天 / 30天/近30天 / 全部/所有
- **自定义日期**：单个 ``YYYY-MM-DD``（当日 00:00 ~ 次日 00:00）；
  区间 ``A到B`` / ``A至B`` / ``A-B`` / ``A B``（A 00:00 ~ B 次日 00:00，
  要求 B>=A，跨度按含首尾的自然日计，>366 天抛 :class:`StatsParseError`）
- **成员**：``at_targets`` 非空取第一个；否则文本中 7–20 位纯数字视为 QQ 号；
  @ 与数字并存时 @ 优先
- 无法识别的 token → :class:`StatsParseError`；空串 → ``(None, 今日)``

纯函数，无副作用，不 import 任何框架模块，便于离线测试。
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from .models import StatsTimeRange

__all__ = ["USAGE_TEXT", "StatsParseError", "parse_stats_args"]

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

USAGE_TEXT = "\n".join(
    [
        "用法：/群统计 [@某人 | QQ号] [时间范围]（参数顺序自由，均可省略）",
        "时间范围：",
        "  关键词：今日（默认）/ 昨日 / 7天 / 30天 / 全部",
        "  单日：2026-08-01",
        "  区间：2026-08-01到2026-08-04（分隔符支持 到/至/-/空格，最长 366 天）",
        "成员：@某人 或 7–20 位 QQ 号；@ 与 QQ 号同时出现时 @ 优先",
        "示例：",
        "  /群统计 → 本群今日数据",
        "  /群统计 7天 → 本群近 7 天数据",
        "  /群统计 @某人 30天 → 某人近 30 天个人卡片",
        "  /群统计 123456789 2026-08-01到2026-08-04 → 指定成员的自定义区间",
    ]
)
"""用法提示常量（模块 C 导出，main/service 共用）。"""

# 「全部」时间范围起点（契约固定值）
_ALL_TIME_START = datetime(2000, 1, 1)

# 自定义日期区间最大跨度（含首尾的自然日天数）
_MAX_RANGE_DAYS = 366

# 时间关键词 → 内部类别
_TIME_KEYWORDS: dict[str, str] = {
    "今日": "today",
    "今天": "today",
    "昨日": "yesterday",
    "昨天": "yesterday",
    "7天": "7d",
    "近7天": "7d",
    "30天": "30d",
    "近30天": "30d",
    "全部": "all",
    "所有": "all",
}

# 类别 → 展示文案（label）
_KEYWORD_LABELS: dict[str, str] = {
    "today": "今日",
    "yesterday": "昨日",
    "7d": "近7天",
    "30d": "近30天",
    "all": "全部",
}

# 单个日期：YYYY-MM-DD（严格 4-2-2 位数字；值合法性另行校验）
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# 连字符分隔的日期区间：A-B（整体一个 token，如 2026-08-01-2026-08-04）
_DATE_RANGE_DASH_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-(\d{4}-\d{2}-\d{2})$")

# QQ 号：7–20 位纯数字
_QQ_RE = re.compile(r"^\d{7,20}$")


class StatsParseError(Exception):
    """指令参数解析失败；实例附 ``usage`` 属性（用法提示文案）。

    main.py / service 捕获后直接将 :attr:`usage` 回给用户即可，无需另行拼装
    帮助文案。``usage`` 默认取模块级 :data:`USAGE_TEXT`，可按需覆盖。
    """

    def __init__(self, message: str, usage: str | None = None) -> None:
        super().__init__(message)
        self.usage = usage if usage is not None else USAGE_TEXT


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _parse_date(text: str, origin: str) -> date:
    """将 ``YYYY-MM-DD`` 文本解析为 :class:`datetime.date`。

    两段校验：先按正则校验形状（严格 4-2-2 位），再用 ``strptime`` 校验值
    合法性（如 ``2026-13-40`` / ``2026-02-30`` 这类不存在的日期）。任何一步
    失败均抛 :class:`StatsParseError`（附用法提示）。

    Args:
        text: 待解析的日期文本。
        origin: 来源 token 原文（仅用于错误提示，便于用户定位）。
    """
    if not _DATE_RE.match(text):
        raise StatsParseError(f"无法识别的日期：{origin}（日期格式应为 YYYY-MM-DD）")
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        raise StatsParseError(f"无效的日期：{text}（该日期不存在）") from None


def _keyword_range(kind: str, now: datetime) -> StatsTimeRange:
    """按关键词类别构造时间范围（半开区间 [start, end)）。

    - today：今日 [今日 00:00, 明日 00:00)
    - yesterday：昨日 [昨日 00:00, 今日 00:00)
    - 7d：近 7 天 [今日-6天 00:00, 明日 00:00)（含今日共 7 天）
    - 30d：近 30 天 [今日-29天 00:00, 明日 00:00)（含今日共 30 天）
    - all：全部 [2000-01-01 00:00, 明日 00:00)

    label 统一取 :data:`_KEYWORD_LABELS`（契约：今日/昨日/近7天/近30天/全部）。
    """
    today = datetime(now.year, now.month, now.day)  # 今日 00:00（服务器本地）
    tomorrow = today + timedelta(days=1)  # 明日 00:00
    label = _KEYWORD_LABELS[kind]
    if kind == "yesterday":
        return StatsTimeRange(start=today - timedelta(days=1), end=today, label=label)
    if kind == "7d":
        return StatsTimeRange(
            start=today - timedelta(days=6), end=tomorrow, label=label
        )
    if kind == "30d":
        return StatsTimeRange(
            start=today - timedelta(days=29), end=tomorrow, label=label
        )
    if kind == "all":
        return StatsTimeRange(start=_ALL_TIME_START, end=tomorrow, label=label)
    # today（默认）
    return StatsTimeRange(start=today, end=tomorrow, label=label)


def _single_date_range(text: str) -> StatsTimeRange:
    """构造单日时间范围：[当日 00:00, 次日 00:00)，label 为该日期。"""
    day = _parse_date(text, text)
    start = datetime(day.year, day.month, day.day)
    return StatsTimeRange(start=start, end=start + timedelta(days=1), label=text)


def _date_range(a: str, b: str, origin: str) -> StatsTimeRange:
    """构造日期区间范围：[A 00:00, B 次日 00:00)，label 为 ``A ~ B``。

    校验：B >= A；跨度（含首尾的自然日天数）<= 366 天。违反任一条抛
    :class:`StatsParseError`。
    """
    day_a = _parse_date(a, origin)
    day_b = _parse_date(b, origin)
    if day_b < day_a:
        raise StatsParseError(f"日期区间的结束日期不能早于开始日期：{origin}")
    days = (day_b - day_a).days + 1
    if days > _MAX_RANGE_DAYS:
        raise StatsParseError(
            f"日期区间最长 {_MAX_RANGE_DAYS} 天，当前 {days} 天：{origin}"
        )
    start = datetime(day_a.year, day_a.month, day_a.day)
    end = datetime(day_b.year, day_b.month, day_b.day) + timedelta(days=1)
    return StatsTimeRange(start=start, end=end, label=f"{a} ~ {b}")


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------


def parse_stats_args(
    message_str: str,
    at_targets: list[str],
    now: datetime | None = None,
) -> tuple[str | None, StatsTimeRange]:
    """解析 /群统计 指令参数，返回 ``(member_id, time_range)``。

    Args:
        message_str: 指令名之后的剩余文本（已 strip）。
        at_targets: 消息链中提取的 @ 目标 QQ 列表（已剔除 ``"all"`` 与 bot 自身，
            复用 ``profile/capture.extract_at_targets`` 的产物口径）。非空时取
            第一个作为个人维度目标。
        now: 当前时间（服务器本地时间）；缺省 ``datetime.now()``。独立成参数
            便于离线测试注入固定时刻。

    Returns:
        ``(member_id, time_range)`` 二元组：

        - ``member_id``：个人维度目标 QQ；``None`` 表示群维度。
        - ``time_range``：:class:`StatsTimeRange`（半开区间 [start, end)）。
          未指定时间时默认「今日」。

    Raises:
        StatsParseError: 参数无法解析（无法识别的 token / 无效日期 / 区间结束
            早于开始 / 跨度超 366 天 / 重复指定时间范围 / 多个 QQ 号等）。
            实例附 ``usage`` 用法文案。

    解析策略：按空白（含全角空格）切分为 token 序列，逐个按类别识别——
    时间关键词 → 日期（含四种区间分隔形式）→ QQ 号 → 无法识别则抛错。
    参数顺序自由；时间范围至多一个；@ 与数字 QQ 并存时 @ 优先。
    """
    if now is None:
        now = datetime.now()

    # 容忍全角空格（U+3000）：统一替换为半角后按任意空白切分
    text = (message_str or "").replace("　", " ").strip()
    tokens = text.split()

    time_range: StatsTimeRange | None = None
    qq_candidates: list[str] = []

    def _set_time_range(candidate: StatsTimeRange) -> None:
        """登记时间范围；重复指定视为冲突，抛错。"""
        nonlocal time_range
        if time_range is not None:
            raise StatsParseError(
                f"时间范围只能指定一次（已指定「{time_range.label}」，又出现「{candidate.label}」）"
            )
        time_range = candidate

    i = 0
    while i < len(tokens):
        token = tokens[i]

        # 1) 时间关键词
        kind = _TIME_KEYWORDS.get(token)
        if kind is not None:
            _set_time_range(_keyword_range(kind, now))
            i += 1
            continue

        # 2) 「A到B」/「A至B」区间（到/至 分隔，同一个 token 内）
        if "到" in token or "至" in token:
            parts = re.split(r"[到至]", token)
            if len(parts) == 2:
                _set_time_range(_date_range(parts[0], parts[1], token))
                i += 1
                continue
            raise StatsParseError(f"无法识别的参数：{token}")

        # 3) 「A-B」区间（连字符分隔，整体一个 token）
        dash_match = _DATE_RANGE_DASH_RE.match(token)
        if dash_match:
            _set_time_range(
                _date_range(dash_match.group(1), dash_match.group(2), token)
            )
            i += 1
            continue

        # 4) 日期：先看是否为「A B」空格分隔区间（本 token 与下一个 token 均为日期）
        if _DATE_RE.match(token):
            if i + 1 < len(tokens) and _DATE_RE.match(tokens[i + 1]):
                _set_time_range(
                    _date_range(token, tokens[i + 1], f"{token} {tokens[i + 1]}")
                )
                i += 2
            else:
                _set_time_range(_single_date_range(token))
                i += 1
            continue

        # 5) QQ 号（7–20 位纯数字）
        if _QQ_RE.match(token):
            qq_candidates.append(token)
            i += 1
            continue

        # 6) 无法识别
        raise StatsParseError(f"无法识别的参数：{token}")

    # 未指定时间范围 → 默认「今日」
    if time_range is None:
        time_range = _keyword_range("today", now)

    # 成员解析：@ 优先（at_targets 已剔除 "all" 与 bot 自身），其次纯数字 QQ 号
    member_id: str | None = None
    if at_targets:
        member_id = str(at_targets[0])
    else:
        if len(qq_candidates) > 1:
            raise StatsParseError(
                f"只能指定一个 QQ 号（出现了多个：{'、'.join(qq_candidates)}）"
            )
        if qq_candidates:
            member_id = qq_candidates[0]

    return member_id, time_range
