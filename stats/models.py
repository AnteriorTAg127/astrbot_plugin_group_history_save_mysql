"""公共数据模型（v0.5.0 数据分析）。

定义数据分析子包各层共享的数据结构（Web / 指令 / 定时推送三端同源契约）：

- ``StatsTimeRange``：统计时间范围（左闭右开，附展示文案）
- ``StatsQuery``：统计查询条件（群 / 成员 / 时间范围 / 排行条数）
- ``SenderRankItem``：发言人排行条目
- ``GroupRankItem``：群排行条目（仅全部群汇总视图）
- ``MemberStats``：个人维度成员详细统计
- ``StatsData``：完整组装后的统计数据（``StatsService.build_stats`` 单一出口）

纯数据模型：不 import 框架模块、无副作用，便于离线测试。
契约见 ``开发/v0.5.0/分工.md``「接口契约 → 数据模型」，字段与注释与其一字不差，
改动需同步下游（repository / snapshot / service / t2i_render / web_api / 前端）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

# ---------------------------------------------------------------------------
# 数据结构（契约见 开发/v0.5.0/分工.md「接口契约 → 数据模型」，改动需同步下游）
# ---------------------------------------------------------------------------


@dataclass
class StatsTimeRange:
    """统计时间范围（左闭右开）。"""

    start: datetime  # 含（服务器本地时间）
    end: datetime  # 不含
    label: str  # 展示文案："今日" / "昨日" / "近7天" / "近30天" / "全部" / "2026-08-01" / "2026-08-01 ~ 2026-08-04"


@dataclass
class StatsQuery:
    """统计查询条件（Web / 指令 / 推送三端共用）。"""

    group_id: str | None  # None = 全部群汇总
    member_id: str | None  # 个人维度目标 QQ；None = 群维度
    time_range: StatsTimeRange
    top_n: int = 10  # 排行条数（1–50，由调用方夹住）


@dataclass
class SenderRankItem:
    """发言人排行条目。"""

    sender_id: str
    sender_name: str  # 最近一次入库昵称
    count: int  # chat_history 消息数
    image_count: int = 0  # 快照图片数（Top K 口径）


@dataclass
class GroupRankItem:
    """群排行条目（仅全部群汇总视图非空）。"""

    group_id: str
    count: int
    image_count: int = 0
    active_senders: int = 0


@dataclass
class MemberStats:
    """个人维度成员详细统计。"""

    sender_id: str
    sender_name: str
    count: int
    image_count: int
    ratio: float  # 占所选群消息比例 0.0–1.0；全部群视图下为该成员消息/全部消息
    rank: int | None  # 在所选群排行中的名次（1 起）；全部群视图为 None
    active_days: int
    avg_per_day: float  # count / 时间范围天数（至少 1 天）
    hourly_dist: list[int]  # 24 项
    weekday_dist: list[int]  # 7 项，周一=0（与 profile StatsBuilder 锚定一致）


@dataclass
class StatsData:
    """完整组装后的统计数据（``StatsService.build_stats`` 单一出口）。"""

    query: StatsQuery
    total_messages: int
    total_images: int
    active_senders: int
    peak_hour: int | None  # 0–23；hourly_dist 全 0 时 None
    first_msg_time: datetime | None
    last_msg_time: datetime | None
    daily_trend: list[
        dict
    ]  # [{"date": "YYYY-MM-DD", "count": int}]，连续补零（start 所在日 ~ end 前一日）
    hourly_dist: list[int]  # 24 项
    weekday_dist: list[int]  # 7 项，周一=0
    sender_ranking: list[SenderRankItem]  # 群维度/个人维度都返回（所在群排行）
    group_ranking: list[GroupRankItem]  # 仅 group_id=None 时非空
    member: MemberStats | None  # 仅 member_id 非 None 时非空
    generated_at: datetime  # 生成时刻（模板页脚用）
