# astrbot_plugin_group_history_save_mysql

将 QQ 群聊天记录自动保存到 MySQL 数据库，支持按时间、群号、QQ 号索引过滤，提供 Web 管理后台；v0.3 新增群聊历史自动总结功能（MySQL 优先 + 协议端补齐）。

## 功能

- 📝 自动监听群消息，保存文本到 MySQL `chat_history` 表
- 🖼️ 图片 URL 单独存储到 `image_records` 表（从 OneBot 原始事件提取 QQ 下发的原始链接，兼容新版 AstrBot）
- 🏷️ 文本与图片记录均保存发送时的昵称
- 🔍 建立联合索引，支持按时间/群号/QQ号高效过滤
- 🗂️ 群白名单管理（支持 ALL 模式）
- 🧹 图片表每天自动清理过期数据（默认保留 3 天）
- 🌐 Web 管理后台：状态监控、群管理、统计、查询面板；新增总结设置 / 忽略管理 / 历史总结三个 tab（v0.3）
- 🗑️ 一键清空所有数据（带随机加减法验证，防误触）
- ⚙️ 管理指令：/history_start、/history_stop、/history_status、/history_clean
- 🤖 群聊历史自动总结（v0.3 新增）：`/消息总结 <数量>`、`/消息总结时间 <时长>` 两条群指令，白名单群内任何成员可用
- 🔀 总结混合数据源：MySQL 优先 + OneBot 协议端在线补齐，按 message_id 去重合并，协议端数据不回填数据库
- 📊 规则统计块 + LLM 四板块摘要，合并转发 / 文转图两种输出形式（Web 可配）

## 安装

在 AstrBot 插件市场搜索 `astrbot_plugin_group_history_save_mysql` 安装，或手动克隆到 `data/plugins/`。

## MySQL 配置指南

### 1. 安装 MySQL

确保你的服务器上已安装 MySQL 5.7+ 或 MariaDB 10.3+。

### 2. 创建数据库

```sql
CREATE DATABASE astrbot_history CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
```

### 3. 创建用户并授权（推荐）

```sql
CREATE USER 'astrbot'@'localhost' IDENTIFIED BY '你的密码';
GRANT ALL PRIVILEGES ON astrbot_history.* TO 'astrbot'@'localhost';
FLUSH PRIVILEGES;
```

### 4. 配置插件

在 AstrBot WebUI 的插件配置中填写：

| 配置项 | 说明 | 示例 |
|--------|------|------|
| mysql_host | MySQL 主机地址 | 127.0.0.1 |
| mysql_port | MySQL 端口 | 3306 |
| mysql_user | 用户名 | astrbot |
| mysql_password | 密码 | 你的密码 |
| mysql_database | 数据库名 | astrbot_history |
| pool_size | 连接池大小 | 5 |
| pool_timeout | 连接超时（秒） | 30 |

### 5. 验证连接

配置完成后，插件会自动创建表结构。你可以在 MySQL 中检查：

```sql
USE astrbot_history;
SHOW TABLES;
-- 应该看到 chat_history 和 image_records 两张表
```

## 指令

| 指令 | 参数 | 说明 |
|------|------|------|
| /history_start | [群号] | 开启指定群的记录（默认当前群） |
| /history_stop | [群号] | 关闭指定群的记录（默认当前群） |
| /history_status | - | 查询记录状态 |
| /history_clean | [天数] | 手动清理过期图片 |

> 所有指令仅限 AstrBot 管理员使用。

### 总结指令（v0.3 新增）

| 指令 | 别名 | 参数 | 说明 |
|------|------|------|------|
| /消息总结 | /总结 | `<数量>` 正整数，如 `512` | 总结最近 N 条群消息（规则统计 + LLM 摘要） |
| /消息总结时间 | /总结时间 | `<时长>` 如 `24h`、`1d`（`h`=小时，`d`=天） | 总结最近 X 小时/天的群消息（仅支持单点「最近 X」，不支持区间语法） |

- 权限：白名单群内任何成员可用；仅当前群生效，不支持跨群参数；私聊中使用回复「请在群内使用」
- 缺参或参数非法（非正整数、不符合 `^\d+[hd]$`、≤0）：回复用法提示，不查库、不消耗 LLM
- 超过 Web 配置上限（`summary_max_count` 默认 1000 条 / `summary_max_hours` 默认 168 小时）：拒绝执行并提示上限
- 群不在白名单（whitelist 模式）：回复「本群未开启总结功能」
- 功能总开关关闭（`summary_enabled=false`）：回复「功能未启用」
- 用户/群冷却中再次触发：回复剩余等待秒数

## Web 管理后台

安装插件后，在 AstrBot WebUI 的插件详情页可以打开管理面板：

- **状态监控**：数据库连接状态、今日/总计消息量
- **群管理**：添加/删除群、开关控制、ALL 模式
- **设置**：图片保留天数配置
- **统计**：最近 7 天每日存储量
- **查询**：按关键词（内容/昵称）、群号、QQ号、时间组合查询聊天记录
- **数据维护**：手动清理过期图片；一键清空所有数据（需通过随机加减法验证，题目由后端生成、一次性有效）
- **总结设置**（v0.3 新增）：总结功能全部 15 项配置的分组表单（基础与白名单 / 参数上限 / 总结行为 / 存储）、总结专用 LLM 提供商下拉、提示词模板多行编辑与一键恢复默认
- **忽略管理**（v0.3 新增）：按群增删查忽略发送者，被忽略者的消息不参与总结
- **历史总结**（v0.3 新增）：按群浏览已保存的总结 JSON，弹层查看总结详情（统计块 + LLM 摘要 + 元数据）

## 总结功能配置说明（v0.3 新增）

> 总结功能全部 15 项配置存入 config.db 新增的 `summary_settings` 表（key/value 形式，列表值 JSON 序列化），
> **不进入 `_conf_schema.json`**（插件原有配置保持不变）。配置仅在 dashboard「总结设置」tab 修改，
> 改后即时生效，无需重启插件；默认值由 `ConfigManager` 初始化时自动播种。

| 配置键 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| summary_enabled | bool | true | 功能总开关（关闭后指令回复「功能未启用」） |
| summary_whitelist_mode | string | whitelist | 群白名单模式：`whitelist`=仅白名单群可用；`all`=所有群可用 |
| summary_group_whitelist | list（JSON 存储） | [] | 白名单群号列表（字符串形式），mode=all 时忽略 |
| summary_user_cooldown | int | 60 | 同一用户两次触发的最小间隔（秒） |
| summary_group_cooldown | int | 120 | 同一群两次总结的最小间隔（秒） |
| summary_max_count | int | 1000 | `/消息总结` 数量参数上限，超出拒绝 |
| summary_max_hours | int | 168 | `/消息总结时间` 时间跨度上限（小时，即 7 天），超出拒绝 |
| summary_min_mysql_ratio | float | 0.8 | 数量模式补齐阈值：MySQL 实得/请求 < 此值才拉协议端 |
| summary_gap_tolerance_minutes | int | 30 | 时间模式缺口容忍：MySQL 最早消息晚于窗口起点 + 此值才拉协议端 |
| summary_onebot_max_fetch | int | 200 | 单次从协议端最多拉取条数（协议端自身上限内） |
| summary_provider_id | string（页内下拉） | "" | 总结专用 LLM 提供商；空=回退该群会话当前 provider |
| summary_prompt | text | （内置 4 板块默认模板） | LLM 总结提示词，支持占位符 `{stats}` `{messages}` `{time_range}` `{group_id}` `{format_constraint}` |
| summary_output_mode | string | forward | 输出形式：`forward`=合并转发（剥离 Markdown）；`image`=文转图（保留 Markdown） |
| summary_rank_top_n | int | 5 | 活跃排行展示条数 |
| summary_retention_days | int | 30 | 总结 JSON 保留天数，定时任务每日清理过期文件 |

占位符说明：`{stats}` 统计块、`{messages}` 格式化消息列表（每行 `[时间] 昵称: 内容`）、`{time_range}` 时间范围描述、`{group_id}` 群号、`{format_constraint}` 按输出模式注入的格式约束（合并转发=禁用 Markdown / 文转图=可用 Markdown）。

## 总结工作原理（v0.3 新增）

### 混合数据源

1. **MySQL 优先**：按群号 + 范围查询本插件 MySQL `chat_history` 表
2. **协议端补齐**：仅在 MySQL 数据不足时，经 OneBot v11 `get_group_msg_history` 在线补齐——数量模式按 `summary_min_mysql_ratio` 判定；时间模式按窗口内条数为 0 或 MySQL 最早消息晚于窗口起点 + `summary_gap_tolerance_minutes` 判定
3. **去重合并**：两源按 `message_id` 去重（无 message_id 时退化为「秒级时间戳 + 发送者 + 内容前 32 字符」），按时间升序合并
4. **不回填**：OneBot 拉到的消息仅本次总结使用，不写入 `chat_history`
5. **降级容错**：任一数据源失败不阻断流程，以可用数据继续；两源皆空回复「没有可总结的消息」提示

> 协议端只能拉取其缓存范围内的近期消息，更久的历史只能依赖 MySQL。

### 总结内容与输出

- **规则统计块**：消息总数、参与者人数、时间跨度、发言条数排行 Top N（`summary_rank_top_n`）
- **LLM 四板块摘要**：📢 重要通知与结论 / 💬 讨论要点·争议 / 🎉 有趣片段 / ✅ TODO·待跟进，每个条目含参与者与大致时间
- **输出形式**（`summary_output_mode`）：
  - `forward` 合并转发：1 个统计节点 + 4 个板块节点，剥离 Markdown 标记（提示词约束 + 发送前程序化剥离双重保障）
  - `image` 文转图：保留 Markdown，经渲染器渲染成图片发送

### 权限、限流与素材过滤

- 独立群白名单（`summary_whitelist_mode` + `summary_group_whitelist`），与录制白名单（group_config 表）互不干扰；白名单群内任何成员可用
- 用户/群双冷却（`summary_user_cooldown` / `summary_group_cooldown`），超限回复剩余等待秒数；冷却状态存内存，重启清零
- 非文本消息（图片/语音/视频等）完全忽略：不占位、不统计、不送 LLM
- 默认过滤 bot 自身消息（硬编码）；每群可配置忽略发送者（dashboard「忽略管理」，存 config.db `group_ignore_senders` 表）

### 结果持久化

- JSON 存储：`data/plugin_data/astrbot_plugin_group_history_save_mysql/summaries/<群号>/<时间戳>.json`，内容含统计块、LLM 摘要原文、元数据（群号、时间/条数范围、数据源构成、生成时间）
- 保留天数可配（`summary_retention_days`，默认 30 天），定时任务每天清理一次过期文件，随插件生命周期启停
- 可在 dashboard「历史总结」tab 按群浏览与查看详情

### 代码结构

新功能代码全部位于 `summary/` 子包，指令在 `main.py` 注册并薄封装异步委托子包处理函数；无新增 pip 依赖。

| 模块 | 职责 |
|------|------|
| summary/service.py | 编排层：指令入口、白名单/限流校验、流程串联 |
| summary/fetcher.py | 数据获取：混合策略（MySQL 优先 + OneBot 补齐 + 去重合并） |
| summary/onebot.py | 协议端 `get_group_msg_history` 封装与消息段解析 |
| summary/summarizer.py | 总结引擎：统计计算 + LLM 调用 + 提示词占位符渲染 |
| summary/formatter.py | 输出格式化：合并转发节点（剥离 Markdown）/ 文转图 |
| summary/storage.py | 总结 JSON 持久化（按群分目录、列表、读取、过期清理） |
| summary/scheduler.py | 定时清理任务（每天清理过期 JSON） |

## 数据库表结构

> 群号与 QQ 号均以文本（`VARCHAR(32)`）存储。从 v0.1 升级时插件会自动检测旧表并执行
> `ALTER TABLE` 迁移（数字自动转字符串、`image_records` 自动补 `sender_name` 列），无需手动操作。

### chat_history（聊天记录）

| 字段 | 类型 | 说明 |
|------|------|------|
| id | BIGINT | 主键 |
| timestamp | DATETIME | 消息时间 |
| group_id | VARCHAR(32) | 群号 |
| sender_id | VARCHAR(32) | QQ 号 |
| sender_name | VARCHAR(128) | 昵称 |
| message_type | VARCHAR(16) | 类型（text/mixed） |
| content | TEXT | 文本内容 |
| message_id | VARCHAR(64) | 消息 ID |

**索引**：
- `idx_group_time` (group_id, timestamp)
- `idx_sender_time` (sender_id, timestamp)
- `idx_group_sender_time` (group_id, sender_id, timestamp)

### image_records（图片记录）

| 字段 | 类型 | 说明 |
|------|------|------|
| id | BIGINT | 主键 |
| timestamp | DATETIME | 记录时间 |
| group_id | VARCHAR(32) | 群号 |
| sender_id | VARCHAR(32) | QQ 号 |
| sender_name | VARCHAR(128) | 发送时的昵称 |
| image_url | VARCHAR(1024) | 图片原始 URL（QQ 下发链接） |

**索引**：
- `idx_img_time` (timestamp)
- `idx_img_group` (group_id)

### config.db 新增表（v0.3 新增）

| 表名 | 说明 |
|------|------|
| summary_settings | 总结功能配置（key TEXT PRIMARY KEY, value TEXT），15 项总结配置均存于此，仅 dashboard「总结设置」tab 修改 |
| group_ignore_senders | 每群忽略发送者（group_id, sender_id, created_at，group_id + sender_id 联合唯一约束） |

> 总结结果不入库，以 JSON 文件持久化，路径与清理策略见上文「总结工作原理 → 结果持久化」。

## 支持平台

- aiocqhttp (OneBot v11)


## License

MIT
