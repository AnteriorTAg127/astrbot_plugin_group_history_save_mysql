# 契约 — 人物分析模块（core/profile）

> 本文件是人物分析功能各子模块之间的**接口契约**：数据模型字段、公开方法签名、过滤/
> 降级规则一经确定，改动需同步所有下游调用方。代码内注释「契约见模块接口约定」即指向本文。

## 数据模型（core/profile/models.py）

```python
@dataclass
class ProfileTarget:
    sender_id: str; sender_name: str
    scope: str            # "group" | "all"
    group_id: str         # scope="all" 时为空

@dataclass
class ProfileMessage:
    timestamp: datetime; group_id: str; sender_id: str; sender_name: str
    content: str; message_id: str; source: str   # "mysql" | "onebot"
    at_list: list[str] = field(default_factory=list)
    reply_id: str = ""

@dataclass
class ProfileStats:
    total: int; group_count: int; group_breakdown: list[tuple[str, int]]
    time_start: datetime | None; time_end: datetime | None; active_days: int
    hour_dist: list[int]      # len 24
    weekday_dist: list[int]   # len 7（Mon=0..Sun=6）
    peak_hour: int; peak_weekday: int
    avg_length: float; total_chars: int; emoji_ratio: float; question_ratio: float
    top_partners: list[tuple[str, str, int]]   # (sender_id, name, count)
    truncated: bool = False

@dataclass
class ProfileResult:
    target: ProfileTarget; stats: ProfileStats
    sections: list[tuple[str, str]]; raw_llm_text: str; provider_id: str
    messages_used: int; sources: dict[str, int] = field(default_factory=dict)
    relation_context_complete: bool = True; scope_desc: str = ""; created_at: str = ""

@dataclass
class ProfileFetchOutcome:
    target_messages: list[ProfileMessage]
    context_messages: list[ProfileMessage]   # 互动对象消息（关系开关开时，否则 []）
    partners: list[tuple[str, str, int]]
    sources: dict[str, int] = field(default_factory=dict)
    onebot_attempted: bool = False; onebot_error: str | None = None
    relation_context_complete: bool = True

# 归一化纯函数
def mysql_row_to_profile_message(row: dict) -> ProfileMessage
```

## 捕获函数（core/profile/capture.py）

```python
def extract_at_targets(message_chain) -> list[str]   # 提取 Comp.At 的 qq，去重保序
def extract_reply_id(event) -> str                    # 回复目标 message_id，取不到返回 ""
```

## 数据获取（core/profile/fetcher.py）

```python
class ProfileFetcher:
    def __init__(self, mysql_mgr, config_mgr): ...
    # 公开接口：单群分页拉取 + OneBot 补齐 / 跨群全局拉取 + 关系上下文双向识别
```

- 契约方法签名一经确定不得私改；关系上下文双向识别目标 ↔ 他人的 @/回复互动对象

## 确定性统计（core/profile/stats.py）

- 输入 `ProfileStats` 既有字段契约（24h/星期分布、发言长度、emoji 率、互动排行等），
  纯确定性计算，不调 LLM；`ProfileStats` 字段定义与 `models.py` 一致

## 存储与调度（core/profile/storage.py, scheduler.py）

- 分析结果不入库，以 JSON 文件持久化，按范围分子目录（`group_<群号>` / `all`），
  文件名含生成时间与目标 QQ 号；保留天数 `profile_keep_days`，定时任务每日清理

## 编排层（core/profile/service.py）

```python
class ProfileService:
    def __init__(self, context, config_mgr, mysql_mgr, star): ...
    # 指令与 Web 共用入口；权限/限流校验、目标解析、流程串联
```

- 契约方法签名不得私改；改动需同步 `webapi/profile.py` 与指令 handler
