# 契约 — 总结模块（core/summary）

> 本文件是总结功能各子模块之间的**接口契约**：数据模型字段、公开方法签名、过滤/降级
> 规则一经确定，改动需同步所有下游调用方。代码内注释「契约见模块接口约定」即指向本文。

## 公共数据模型（core/summary/models.py）

```python
@dataclass
class ChatMessage:
    timestamp: datetime          # 消息时间
    group_id: str                # 群号（字符串）
    sender_id: str               # 发送者 QQ（字符串）
    sender_name: str             # 发送者昵称
    content: str                 # 纯文本内容（非文本消息已过滤）
    message_id: str              # 消息 ID（去重主键，可能为空）
    source: str                  # "mysql" | "onebot"

@dataclass
class StatsResult:
    total: int
    participant_count: int
    time_start: datetime | None
    time_end: datetime | None
    top_senders: list[tuple[str, str, int]]   # (sender_id, sender_name, 条数)
    truncated: bool = False                   # 是否因长度预算被截断

@dataclass
class SummaryResult:
    stats: StatsResult
    sections: list[tuple[str, str]]   # [(板块标题, 板块内容)]，按 4 板块顺序
    raw_llm_text: str
    provider_id: str
    messages_used: int
    sources: dict[str, int] = field(default_factory=dict)   # {"mysql": n, "onebot": m}
    scope_desc: str = ""
```

## 数据获取 HistoryFetcher（core/summary/fetcher.py）

```python
class HistoryFetcher:
    def __init__(self, mysql_mgr: MySQLManager, config_mgr: ConfigManager): ...

    async def fetch_by_count(self, *, group_id: str, event: AstrMessageEvent, count: int) -> FetchOutcome
    async def fetch_by_window(self, *, group_id: str, event: AstrMessageEvent,
                              window_start: datetime, window_end: datetime) -> FetchOutcome

@dataclass
class FetchOutcome:
    messages: list[ChatMessage]   # 已去重、按时间升序、已过滤
    sources: dict[str, int]       # {"mysql": n, "onebot": m}
    onebot_attempted: bool
    onebot_error: str | None
```

- 过滤顺序：非文本剔除 → bot 自身 ID 剔除 → 忽略名单（`get_ignore_senders`）剔除
- 不足判定：数量模式 `m < count × summary_min_mysql_ratio`；时间模式
  `m == 0 或 MySQL 最早消息 > window_start + summary_gap_tolerance_minutes`
- OneBot 补齐失败仅记日志，`onebot_error` 带原因，不阻断

## OneBot 封装（core/summary/onebot.py）

```python
async def fetch_group_history(event: AstrMessageEvent, group_id: str, count: int) -> list[ChatMessage]
```

- 经 `client.api.call_action("get_group_msg_history", ...)`；解析 `messages[]`，仅取
  `type=text` 段；失败抛 `OneBotHistoryError`，由 fetcher 降级

## 总结引擎 Summarizer（core/summary/summarizer.py）

```python
class Summarizer:
    def __init__(self, context: Context, config_mgr: ConfigManager): ...
    async def summarize(self, event: AstrMessageEvent, messages: list[ChatMessage],
                        scope_desc: str, output_mode: str) -> SummaryResult
```

- 素材格式化：每行 `[YYYY-MM-DD HH:MM] 昵称: 内容`；长度预算硬上限 60000 字符，超限保留
  **最近**消息并截断，`stats.truncated=True`
- provider 解析：`summary_provider_id` 非空用之；空则取会话当前 provider；皆无抛
  `SummaryProviderError`
- 占位符：`{stats}` `{messages}` `{time_range}` `{group_id}` `{format_constraint}`
- 板块解析：按 4 板块标题 best-effort 切分；失败则 `sections=[("全部", raw)]`

## 输出格式化 SummaryFormatter（core/summary/formatter.py）

```python
class SummaryFormatter:
    async def render(self, result: SummaryResult, mode: str) -> MessageChain
def strip_markdown(text: str) -> str
```

- `forward`：`Comp.Node × 5`（统计节点 + 4 板块节点），文本经 `strip_markdown()`
- `image`：HTML 模板渲染，保留 Markdown，失败回退纯文本节点

## 持久化 SummaryStorage + Scheduler（core/summary/storage.py, scheduler.py）

```python
class SummaryStorage:
    async def save(self, group_id: str, result: SummaryResult) -> Path
    async def list_by_group(self, group_id: str | None = None, page: int = 1, page_size: int = 20) -> dict
    async def read(self, group_id: str, filename: str) -> dict | None
    async def cleanup_expired(self, retention_days: int) -> int

class CleanupScheduler:
    async def start(self)   # 每日循环，启动先跑一次
    async def stop(self)
```

- 文件名 `<Unix 时间戳>_<6 位随机 hex>.json`；读写路径做群号数字校验，防路径穿越

## 编排层 SummaryService（core/summary/service.py）

```python
class SummaryService:
    def __init__(self, context, config_mgr, mysql_mgr, star): ...
    async def start(self) / stop(self)
    async def handle_count_command(self, event, arg) -> None
    async def handle_window_command(self, event, arg) -> None
```

- 流程：总开关 → 群环境 → 白名单 → 双冷却限流 → 参数校验（超限拒绝并提示）→ fetcher →
  summarizer → formatter → storage.save → `event.send(chain)`
- 发送**不 yield**（保证可脱离 handler 调用）；错误兜底见 service.py 文案
