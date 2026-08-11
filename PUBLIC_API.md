# 对外查询接口使用文档（Public API）

> 适用于 **astrbot_plugin_group_history_save_mysql v0.7.0+**。
> 本插件向同实例内的其他 AstrBot 插件提供聊天记录查询接口，供编程调用
> （统计、检索、自动化、二次开发等场景）。所有调用都会记录到查询日志。

---

## 1. 快速开始

> ⚠️ **安全警告（v0.7.0 决策：接口全开放、无鉴权）**
>
> - 本接口**不做任何权限校验**，同进程内任何插件都可调用，且可**分页遍历全部群的聊天记录**
>   ——聊天记录属敏感隐私数据，请仅在可信插件中调用，并在插件代码中妥善保管查询结果
> - 接口**无限流**：`keyword` 模糊匹配走全表扫描、`page` 无上限，高频或超大参数调用可能
>   拖垮 MySQL 连接池（`pool_max_size` 默认 10）；请勿高频无意义调用，keyword 控制在
>   500 字符以内、page 控制在 1000 以内
> - `caller` 标识为调用方**自报**，可被伪造，查询日志中的调用方仅作参考，不构成安全审计依据

```python
from data.plugins.astrbot_plugin_group_history_save_mysql.core.public_api import query_records

result = await query_records(
    caller="my_plugin",      # 可选：调用方标识；缺省自动从调用栈推断
    group_id="123456789",    # 可选：群号过滤
    sender_id=None,          # 可选：QQ 号过滤
    time_start=None,         # 可选：开始时间 YYYY-MM-DD HH:MM:SS
    time_end=None,           # 可选：结束时间
    keyword=None,            # 可选：关键词（模糊匹配内容与昵称）
    page=1,
    page_size=50,            # 夹取 [1, 200]
)
```

## 2. 函数签名

接口一览：`query_records`（聊天记录查询，见 §2.1）、`count_messages`（消息统计，见 §9）。

### 2.1 query_records

```python
async def query_records(
    caller: str | None = None,      # 调用方标识（见 §5）
    group_id: str | None = None,    # 群号过滤，空串视同未提供
    sender_id: str | None = None,   # QQ 号过滤，空串视同未提供
    time_start: str | None = None,  # 开始时间 "YYYY-MM-DD HH:MM:SS"
    time_end: str | None = None,    # 结束时间 "YYYY-MM-DD HH:MM:SS"
    keyword: str | None = None,     # 关键词，模糊匹配 content 与 sender_name
    page: int = 1,                  # 页码，<1 归 1
    page_size: int = 50,            # 每页条数，夹取 [1, 200]
) -> dict
```

### 参数说明

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `caller` | str | 否 | 调用方标识，写入查询日志；缺省自动从调用栈推断 |
| `group_id` | str | 否 | 群号过滤（字符串形式，与库内存储一致） |
| `sender_id` | str | 否 | QQ 号过滤（字符串形式） |
| `time_start` / `time_end` | str | 否 | 时间范围过滤，格式 `YYYY-MM-DD HH:MM:SS`，闭区间 |
| `keyword` | str | 否 | 关键词，模糊匹配消息内容与发送者昵称；`%` `_` 通配符按字面匹配 |
| `page` | int | 否 | 页码，从 1 开始；`<1` 自动归 1 |
| `page_size` | int | 否 | 每页条数，自动夹取到 [1, 200] |

## 3. 返回值

返回**纯数据** `dict`（无 datetime 原生类型、无数据库行对象/连接对象）：

```python
{
    "total": 123,            # int：满足条件的总条数（不分页）
    "records": [             # list[dict]：当前页记录
        {
            "id": 1,                          # int：自增主键
            "timestamp": "2026-08-11 10:00:00",  # str：消息时间
            "group_id": "123456789",          # str
            "sender_id": "10001",             # str：发送者 QQ
            "sender_name": "张三",            # str：发送时昵称
            "message_type": "text",           # str：text / mixed
            "content": "消息内容",            # str：文本内容
            "message_id": "m123",             # str：消息 ID
            "at_list": "10002,10003",         # str：@ 的 QQ 列表，英文逗号分隔
            "reply_id": "m120",               # str：回复目标消息 ID，无则空串
            "reply_message": {                # dict | None：回复目标消息摘要
                "timestamp": "2026-08-11 09:58:00",
                "sender_id": "10002",
                "sender_name": "李四",
                "content": "被回复的内容",
                # ... 同 records 内字段
            },
        },
        # ...
    ],
}
```

- 记录按**时间倒序**（新 → 旧）返回
- `reply_message`：本条消息回复的目标消息摘要；无回复或取不到时为 `None`

> **失败与无结果的区别**：查询**执行失败**时返回值含 `"_error"` 键（如
> `{"total": 0, "records": [], "_error": "查询执行失败，详情见查询日志"}`），
> 成功时**没有**该键——调用方用 `"_error" in result` 即可区分「确实没有数据」
> （`total=0` 且无 `_error`）与「查询失败」（含 `_error`）。

## 4. 完整示例

### 4.1 基本查询 + 异常捕获

```python
from astrbot.api import logger
from data.plugins.astrbot_plugin_group_history_save_mysql.core.public_api import (
    query_records,
    PublicAPIError,
)

async def fetch_history():
    try:
        result = await query_records(
            caller="my_plugin",
            group_id="123456789",
            keyword="重要通知",
            page=1,
            page_size=20,
        )
    except PublicAPIError as e:
        # 本插件 MySQL 尚未初始化完成（插件刚启动 / 数据库不可用）
        logger.error(f"查询聊天记录失败: {e}")
        return []
    return result.get("records", [])
```

### 4.2 分页遍历全部结果

```python
async def fetch_all(group_id: str, max_pages: int = 10):
    all_records = []
    for page in range(1, max_pages + 1):
        result = await query_records(caller="my_plugin", group_id=group_id, page=page, page_size=200)
        all_records.extend(result["records"])
        if page * 200 >= result["total"]:
            break
    return all_records
```

## 5. 调用方标识（caller）

- 显式传入 `caller` 参数优先；
- 缺省时自动从**调用栈**推断第一个不属于本插件包的模块名
  （如 `data.plugins.my_plugin.core.handler` → 记为 `data.plugins.my_plugin`），推断失败记为 `"unknown"`；
- **推断为尽力而为**：调用方使用装饰器/代理时帧信息可能被混淆，返回的模块名可能不准确，**建议显式传入 `caller`** 以获得准确审计标识；
- 标识仅用于**查询日志记录与审计**，不参与任何权限判断（接口全开放）。

## 6. 查询日志

- **每次调用都会写入**内置 SQLite 数据库（`data/plugin_data/astrbot_plugin_group_history_save_mysql/config.db` 的 `query_log` 表，成功与失败都记录），字段：
  调用方 / 方法名 / 查询参数 JSON / 结果条数 / 成败 / 失败原因 / 耗时(ms) / 调用时间；
- **与 MySQL 解耦**：查询日志属插件自身审计数据，不依赖聊天记录存储——MySQL 不可用时审计仍可用；
- 可在 **Web 管理后台 → 存储库 → 查询日志** tab 查看（默认最近 100 条，支持筛选分页）；
- **保留天数可在 Web 后台调整**（默认 30 天，范围 1–3650 天），写入时按 5% 概率自动清理过期行；
- 仅记录**经本接口**发起的查询；Web 后台自身的查询与插件内部模块（总结/统计/补库）的查询不记录；
- 查询日志表写入失败不影响查询结果（仅记 warning）。

## 7. 异常与错误处理

| 场景 | 行为 |
|------|------|
| 本插件 MySQL 尚未初始化完成 | 抛 `PublicAPIError("本插件 MySQL 尚未初始化完成，请稍后重试")`，**不写查询日志**；调用方应 try/except 捕获 |
| 时间参数格式非法 | 抛 `PublicAPIError`（提示正确格式 `YYYY-MM-DD HH:MM:SS`），**不写查询日志**；调用方参数错误，应修复后重试 |
| 查询本身失败（数据库异常等） | **不抛异常**，返回 `{"total": 0, "records": [], "_error": "查询执行失败，详情见查询日志"}`；失败细节（`success=0` + 原因）写入查询日志 |
| 查询日志写入失败 | 仅记 warning，不影响查询结果 |

## 8. 安全性与边界

- **导入无副作用**：`import` 本模块不会加载整个插件、不触发插件注册、不做任何 I/O；
- **不暴露内部对象**：接口不返回/不提供连接池、配置管理器等内部对象；
- **参数化查询**：全部 SQL 参数化绑定，无注入面；`keyword` 的 `%` `_` 通配符已转义；
- **错误不扩散**：底层异常不会抛给调用方，只以 `PublicAPIError`（未初始化）或空结果（查询失败）呈现；
- **日志不泄露敏感信息**：日志参数不含消息内容全文、不含任何密钥；`keyword`/`caller` 超长自动截断；
- 调用方需与本插件**同进程**（AstrBot 单进程运行，天然满足）；经 PEP 420 namespace package 导入，无需安装额外依赖；
- 接口范围**仅聊天记录查询**（多条件分页 + 回复关联）；统计、人物分析等能力不在此接口内。

### 已知风险（v0.7.0 决策：保持现状、不加防护）

| 风险 | 等级 | 说明 |
|------|------|------|
| 数据读取便利化 | 🟠 中 | 全开放无鉴权，任意同进程插件可分页遍历全部群聊天记录（敏感隐私数据）；恶意插件本可做更糟的事，此接口未显著扩大攻击面，但降低了数据获取门槛 |
| 资源耗尽 | 🟠 中 | 无限流；`keyword` LIKE 模糊匹配全表扫描（无索引可用）+ `page` 无上限（大 OFFSET 扫表），高频/超大参数调用可拖垮 MySQL 连接池（有 30s 查询超时兜底） |
| caller 可伪造 | 🟡 低 | 调用方标识自报，可伪装成其他插件名；查询日志审计仅能追溯"声称"的来源 |

> 上述风险经评估后**有意保留**（v0.7.0 决策：接口全开放、不加限流与参数上限），
> 调用方应自觉遵守 §1 安全警告中的使用约束；后续版本如引入 llm_tool 形式，
> 需额外防范 prompt injection（工具描述约束 LLM 只返回统计结论、不透传原始聊天内容）。

## 9. 消息统计（count_messages，v0.7.0 新增）

统计文本消息数（按人 / 按群 / 跨群总计），实时聚合 MySQL `chat_history` 表；
支持**批量查询**（一次传多个 QQ 号，GROUP BY 单条 SQL 聚合，无 N+1 循环），适合高并发。

### 9.1 函数签名

```python
async def count_messages(
    caller: str | None = None,           # 调用方标识（同 query_records）
    group_id: str | None = None,         # 限定单群（可选）
    sender_ids: str | list[str] | None = None,   # 单人（str）或批量（list，≤500）
    time_start: str | None = None,       # 起 YYYY-MM-DD HH:MM:SS（可选）
    time_end: str | None = None,         # 止（可选）
) -> dict
```

> **`group_id` 与 `sender_ids` 至少提供其一**，否则抛 `PublicAPIError`
> （避免全库无过滤 COUNT 的全表扫描）。

### 9.2 返回值

```python
{
    "group_total": 500,        # int：群文本消息总数（给了 group_id 时含；时间过滤）
    "senders": {               # 给了 sender_ids 时含
        "10001": {"in_group": 12, "total": 30},   # 群内发言数（给了 group_id 时含）+ 跨群总数
        "10002": {"in_group": 5, "total": 40},
    },
}
```

| group_id | sender_ids | 返回键 |
|----------|-----------|--------|
| ✅ | ✅ | `group_total` + `senders[].{in_group, total}` |
| ✅ | ❌ | `group_total` |
| ❌ | ✅ | `senders[].{total}`（无 in_group 键） |

- 所有计数均为**文本消息数**（纯图片消息不入库，天然排除）；不含图片统计
- 时间区间（闭区间）同时过滤所有计数；查询失败返回含 `"_error"` 键的空结果
- 批量上限：`sender_ids` 单次 ≤ **500**（超限抛 `PublicAPIError` 提示分批）

### 9.3 示例

```python
from data.plugins.astrbot_plugin_group_history_save_mysql.core.public_api import count_messages

# 批量：统计某群整份成员名单的发言量（典型场景）
result = await count_messages(
    caller="my_plugin",
    group_id="123456789",
    sender_ids=["10001", "10002", "10003"],
    time_start="2026-08-01 00:00:00",
    time_end="2026-08-31 23:59:59",
)
for sid, c in result["senders"].items():
    print(f"{sid}: 群内 {c['in_group']} 条，跨群共 {c['total']} 条")
print("群总计:", result["group_total"])
```

### 9.4 注意事项

- 统计口径为**实时聚合**，与 `/群统计` 指令（快照口径）可能不同
- 高并发优化：批量 GROUP BY 单条 SQL + 多计数并行；COUNT 走复合索引
- 每次调用写查询日志（method=`count_messages`，Web「查询日志」tab 可见）

## 10. 常见问题



**Q：调用时报 `ModuleNotFoundError`？**
A：确认本插件已安装且已加载（`data/plugins/astrbot_plugin_group_history_save_mysql/` 存在）；导入路径需从 `data.plugins.` 开始。

**Q：报 `PublicAPIError` 怎么办？**
A：本插件刚启动或 MySQL 未连接。稍后重试（MySQL 初始化在后台进行，最多重试 5 次、每次间隔 60 秒）；或检查数据库配置。

**Q：查询结果为空？**
A：先确认群号/QQ 号用**字符串**形式传入（与库内存储一致）；再确认时间格式为 `YYYY-MM-DD HH:MM:SS`；最后到 Web 后台「查询」tab 用相同条件验证是否存在数据。

**Q：本插件被禁用或重载时，调用接口会发生什么？**
A：不会让调用方崩溃，但表现取决于持有引用的方式：
- **每次调用时才 import**（推荐）：禁用后重新 import 得到新模块对象（未注册），调用抛 `PublicAPIError`，可捕获；插件重新启用后恢复正常。
- **模块顶层 `from ... import query_records` 持有旧引用**：插件 `terminate()` 会注销内部引用，此时调用同样抛 `PublicAPIError`（不再访问已关闭的连接池）；但**插件重新启用后，旧引用仍指向旧模块对象，继续抛 `PublicAPIError`**——需要重新 import（或重启调用方插件）才能恢复。
> 建议：调用方在**每次调用前** import（函数内 import），或监听插件重载后重新获取引用。

**Q：为什么 Web 后台「查询日志」看不到我通过 Web 面板发起的查询？**
A：查询日志只记录**经本对外接口**发起的查询，Web 面板自身查询不记录（设计如此，避免日志被高频操作淹没）。

## 11. 版本记录

| 版本 | 说明 |
|------|------|
| v0.7.0 | 新增 `query_records` / `PublicAPIError` / `register_mysql_manager`；查询日志存内置 SQLite（config.db 的 query_log 表）+ Web「查询日志」tab + 保留天数可配置 |
| v0.7.0 | 新增 `count_messages` 消息统计接口（群总计 + 批量人统计，GROUP BY 聚合，支持高并发） |
