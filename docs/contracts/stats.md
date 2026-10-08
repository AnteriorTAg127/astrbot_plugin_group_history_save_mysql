# 契约 — 数据分析模块（core/stats）

> 本文件是数据分析功能各子模块之间的**接口契约**：数据模型字段、公开方法签名、快照/
> 回退规则一经确定，改动需同步所有下游调用方。代码内注释「契约见模块接口约定」即指向本文。

## 数据模型（core/stats/models.py）

```python
@dataclass
class StatsTimeRange:
    start: datetime          # 含（服务器本地时间）
    end: datetime            # 不含
    label: str               # 展示文案："今日"/"昨日"/"近7天"/"近30天"/"全部"/"2026-08-01"…

@dataclass
class StatsQuery:
    group_id: str | None     # None = 全部群汇总
    member_id: str | None    # 个人维度目标 QQ；None = 群维度
    time_range: StatsTimeRange
    top_n: int = 10          # 排行条数（1–50，调用方夹取）

@dataclass
class SenderRankItem:
    sender_id: str; sender_name: str; count: int; image_count: int = 0

@dataclass
class GroupRankItem:
    group_id: str; count: int; image_count: int = 0; active_senders: int = 0

@dataclass
class MemberStats:
    sender_id: str; sender_name: str; count: int; image_count: int
    ratio: float             # 0.0–1.0
    rank: int | None
    active_days: int; avg_per_day: float
    hourly_dist: list[int]   # 24 项
    weekday_dist: list[int]  # 7 项，周一=0

@dataclass
class StatsData:
    query: StatsQuery
    total_messages: int; total_images: int; active_senders: int
    peak_hour: int | None; first_msg_time: datetime | None; last_msg_time: datetime | None
    daily_trend: list[dict]  # [{"date": "YYYY-MM-DD", "count": int}]，连续补零
    hourly_dist: list[int]   # 24 项
    weekday_dist: list[int]  # 7 项，周一=0
    sender_ranking: list[SenderRankItem]
    group_ranking: list[GroupRankItem]   # 仅 group_id=None 时非空
    member: MemberStats | None           # 仅 member_id 非 None 时非空
    generated_at: datetime
```

## 时间范围解析（core/stats/parser.py）

```python
class StatsParseError(Exception): ...   # 附带 usage 文案

def parse_stats_args(message_str: str, at_targets: list[str],
                     now: datetime | None = None) -> tuple[str | None, StatsTimeRange]: ...
USAGE_TEXT: str
```

- 关键词：今日/今天(默认)/昨日/昨天/7天/近7天/30天/近30天/全部/所有
- 自定义：单日 `YYYY-MM-DD`；区间 `A到B`/`A至B`/`A-B`/`A B`（B>=A，跨度 >366 天抛错）
- @ 与纯数字 QQ 同时出现 → @ 优先；无法识别的 token → `StatsParseError`

## 聚合仓储（core/stats/repository.py）

```python
class StatsRepository:
    # 每条 SELECT 以 asyncio.wait_for(…, QUERY_TIMEOUT_SECONDS) 兜底；SQL 全参数化
    async def get_overview(...) / get_hourly_dist(...) / get_weekday_dist(...)
    async def get_daily_trend(...) / get_sender_ranking(...) / get_group_ranking(...)
    async def get_member_overview(...) / get_image_window_counts(...)
```

- 统一半开时间窗口 `[start, end)`；日期比较以 datetime 传参

## 快照管理（core/stats/snapshot.py）

```python
ImageSnapshotManager = SnapshotManager   # 兼容别名

class SnapshotManager:
    REFRESH_MIN_INTERVAL_SECONDS = 60.0
    HOURLY_RETENTION_DAYS = 7
    IMAGE_TOP_RETENTION_DAYS = 31

    # 写入侧（异常仅日志不抛；now 可注入）
    async def run_hourly_snapshot(...) / run_daily_snapshot(...) / run_monthly_snapshot(...)
    async def evict_expired(...) / backfill_on_startup(...) / refresh_current_hour(...)

    # 读取侧（不可服务或异常返回 None，调用方回退实时 SQL）
    def is_range_serviceable(self, start, end, now=None) -> bool
    async def msg_total(...) / msg_per_group(...) / msg_daily_trend(...)
    async def image_total(...) / image_per_group(...)
    async def fill_counts(self, data: StatsData) -> StatsData
```

- 三段式（小时/日/月）预计算快照：`horizon = 上月 1 日`；范围不可服务或异常 → None →
  上游回退实时 SQL，对用户透明

## 调度器（core/stats/scheduler.py）

```python
DAILY_MINUTE_THRESHOLD = 15
MONTHLY_MINUTE_THRESHOLD = 25
def check_daily_due(now, last_daily) -> bool
def check_monthly_due(now, last_monthly) -> bool
```

- 单循环顺序：小时判定 → 日判定（成功：`run_daily_snapshot` → `evict_expired` → 盖戳）→
  月判定；任一步抛异常走退避（不盖戳，下轮补跑）

## 编排层（core/stats/service.py）

- `build_stats` 三端同源：Web / `/群统计` / 定时推送共用同一组装入口
- 群级统计读快照（`overview.total` / `trend` / `group_ranking`），超范围或异常自动回退
  实时 SQL；实时维度（活跃成员/24h·星期分布/发言人排行/个人）始终走 SQL
- `overview_daily_stats(days)`：days>31 → None；先 `refresh_current_hour`（60s 限流），
  再取 msg/image 全群日趋势合并；任何异常 → None（Web 回退 `get_daily_stats`）
- `startup_backfill()` 启动回填（幂等）；`terminate()` 取消回填任务
