# astrbot_plugin_group_history_save_mysql

将 QQ 群聊天记录自动保存到 MySQL 数据库，支持按时间、群号、QQ 号索引过滤，提供 Web 管理后台；v0.3 新增群聊历史自动总结功能（MySQL 优先 + 协议端补齐）；v0.4 新增人物分析功能（发言习惯/活动时间/性格/爱好/人物关系）；v0.5 新增数据分析功能（Web 实时统计面板 / `/群统计` 指令报告卡 / 定时日报周报推送 / 分段快照统计，纯 SQL 聚合、不依赖 LLM）；v0.6 新增插件重载后自动从 OneBot 拉取历史消息补库，并将全部代码重构为 `core/` 模块化结构。

## 功能

- 📝 自动监听群消息，保存文本到 MySQL `chat_history` 表
- 🖼️ 图片 URL 单独存储到 `image_records` 表（从 OneBot 原始事件提取 QQ 下发的原始链接，兼容新版 AstrBot）
- 🏷️ 文本与图片记录均保存发送时的昵称
- 🔍 建立联合索引，支持按时间/群号/QQ号高效过滤
- 🗂️ 群白名单管理（支持 ALL 模式）
- 🧹 图片表每天自动清理过期数据（默认保留 3 天）
- 🌐 Web 管理后台：状态监控、群管理、统计、查询面板；新增总结设置 / 忽略管理 / 历史总结三个 tab（v0.3）；存储库分区新增「数据分析」子 tab（v0.5.0）
- 🗑️ 一键清空所有数据（带随机加减法验证，防误触）
- ⚙️ 管理指令：/history_start、/history_stop、/history_status、/history_clean
- 🤖 群聊历史自动总结（v0.3 新增）：`/消息总结 <数量>`、`/消息总结时间 <时长>` 两条群指令，白名单群内任何成员可用
- 🔀 总结混合数据源：MySQL 优先 + OneBot 协议端在线补齐，按 message_id 去重合并，协议端数据不回填数据库
- 📊 规则统计块 + LLM 四板块摘要，合并转发 / 文转图两种输出形式（Web 可配）
- 🛡️ 备用模型降级链（v0.3.1 新增）：总结 LLM 按「主选提供商 → 备用列表按序 → 会话模型兜底」调用，任一节点失败自动降级，历史总结记录实际使用的模型
- 👍 总结触发反馈（v0.3.1 新增）：指令生效后即时确认（默认贴表情回应，协议端不支持时自动降级文字提示，可配文字模式或关闭）
- 🎨 自研图片报告模板（v0.3.2 新增）：文转图改用与管理面板同款风格的自研模板，支持多路 JS CDN 容灾（国内镜像优先，自动切换）、Markdown 表格渲染、发言人排行柱状图、白天/夜间自动主题（时间可调）、渲染超时可配；移动端字号友好
- 🔀 备用模型列表弹窗（v0.3.2 新增）：原长复选框改为点击弹窗，降级顺序可拖拽排序、可选模型滚动勾选，点遮罩不关闭
- 👤 人物分析（v0.4.0 新增）：聊天指令 `/人物分析 [@成员 或 QQ号]`（别名 `/人物画像`、`/分析TA`），分析群成员发言习惯、活动时间、性格、爱好与人物关系
- 🌐 跨群分析（v0.4.0 新增）：仅 Web 后台「人物分析」分区提供（按全体已保存群分析），聊天指令仅分析当前群
- 🧩 分析触发（v0.4.0 新增）：支持 @ 成员或直接输入 QQ 号触发；默认仅管理员可用（可配置）；分析报告含「本报告基于公开群聊记录由 AI 生成，仅为推测，仅供参考」免责声明
- 💾 存储扩展（v0.4.0 新增）：`chat_history` 表新增 `at_list` / `reply_id` 列（自动迁移），记录 @ 对象与回复目标，供人物关系分析使用
- 📈 数据分析（v0.5.0 新增）：Web 后台存储库分区新增「数据分析」tab，实时 SQL 聚合（不调 LLM、不缓存、不落盘）——统计卡片、每日趋势、发言人/群排行、24h·星期发言规律、个人 × 群交叉查询，支持「全部群」汇总与自定义日期区间
- 🖼️ `/群统计` 指令（v0.5.0 新增）：群/个人统计图片报告卡（别名 群数据、统计），支持时间关键词（今日/昨日/7天/30天/全部）与自定义日期/区间，@ 某人或输入 QQ 号查看个人卡片；所有人可用，每群 30s 冷却（冷却期内静默忽略）
- 📬 定时推送（v0.5.0 新增）：日报（默认每天 21:00）与可选周报（默认关，默认周一 09:00），全局开关 + 群级开关 + 推送时间全部在 Web「数据分析」tab 管理，T2I 图片报告卡主动推送到群
- 🕐 图片小时级快照（v0.5.0 新增）：每小时增量聚合图片数（群级全量 + 每小时每群个人 Top K），解决图片表滚动清理后无法回溯统计的问题，「今日」数据在每次统计前强制刷新
- 🧱 分段快照统计（v0.5.5 新增）：图片小时级快照泛化为「消息 + 图片 × 小时/日/月」三段式预计算快照体系，数据分析群级统计（总消息数/每日趋势/群排行）直接读快照，快照不可服务或异常自动整体回退实时 SQL；启动自动回填历史快照（兼顾宕机缺档补偿），强制刷新 60 秒限流
- 🔄 重载自动补库（v0.6.0 新增）：插件加载/重载、MySQL 初始化成功后，后台自动从 OneBot 协议端拉取白名单启用群近 `backfill_hours` 小时的历史消息（文本 + 图片 URL）补库，按 `message_id` 与图片 URL 双重去重，弥补重载窗口期间的消息缺口
- 🧩 模块化重构（v0.6.0）：全部代码迁入 `core/` 子包——`core/db_mysql`（连接池 + 数据库访问拆分）、`core/db_config`（本地配置管理拆分）、`core/webapi`（Web API 按子功能拆分）、`core/{summary,profile,stats}`（三大功能包）、`core/{parsing,saver,cleaner}`（保存逻辑）；`main.py` 仅保留框架交互（指令注册与事件委托）

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

### 人物分析指令（v0.4.0 新增）

| 指令 | 别名 | 参数 | 说明 |
|------|------|------|------|
| /人物分析 | /人物画像、/分析TA | `[@成员 或 QQ号]` | 分析群成员发言习惯与人物画像（AI 推测，仅供参考） |

- 触发方式：@ 群成员（自动剔除 `all` 与 bot 自身）或直接输入纯数字 QQ 号；两者皆无时回复用法提示
- 权限：默认仅管理员可用（`profile_permission=admin`，可配为 `all` 全员可用）
- 范围：仅分析当前群；跨群（全部已保存群）分析请在 Web 后台「人物分析 → 发起分析」tab 发起
- 输出形式：`profile_output_mode` 支持合并转发 / 图片 / 纯文本三种模式（默认合并转发）
- 报告含免责声明「本报告基于公开群聊记录由 AI 生成，仅为推测，仅供参考」

### 群统计指令（v0.5.0 新增）

| 指令 | 别名 | 参数 | 说明 |
|------|------|------|------|
| /群统计 | /群数据、/统计 | `[@某人 或 QQ号] [时间范围]`，均可省略、顺序自由 | 群/个人统计图片报告卡（实时 SQL 聚合，不调 LLM） |

- 时间范围：关键词 `今日`（默认）/ `昨日` / `7天` / `30天` / `全部`（也兼容 今天/昨天/近7天/近30天/所有）；单日 `YYYY-MM-DD`；区间 `YYYY-MM-DD到YYYY-MM-DD`（分隔符支持 到/至/-/空格，最长 366 天）
- 指定成员：@ 某人或纯数字 QQ 号（7–20 位）；@ 与 QQ 号同时出现时 @ 优先
- 可自由组合：`/群统计 7天`、`/群统计 @某人 30天`、`/群统计 123456789 2026-08-01到2026-08-04`；不带任何参数即当前群今日数据
- 权限：所有人可用；仅群环境有效，私聊触发回复用法提示
- 限流：每群 30s 冷却（`stats_cooldown`，Web 可配，0 = 不限流），冷却期内重复触发**静默忽略**（不回复、不渲染）
- 输出：T2I 图片报告卡（渲染失败自动降级纯文本摘要）——
  - 群数据卡片：总消息数、活跃成员数、Top N 发言人（附图片数）、活跃时段峰值、图片总数
  - 个人卡片：消息数、占群比例、活跃天数、日均条数 + 24h/星期分布图 + 图片数
- 无法解析的参数：回复用法提示，不查库

## Web 管理后台

安装插件后，在 AstrBot WebUI 的插件详情页可以打开管理面板：

- **状态监控**：数据库连接状态、今日/总计消息量
- **群管理**：添加/删除群、开关控制、ALL 模式
- **设置**：图片保留天数配置；重载自动补库开关与时长（v0.6.0 新增）
- **统计**：最近 7 天每日存储量（v0.5.5 起改由快照供数，图片表滚动清理不再影响趋势）
- **查询**：按关键词（内容/昵称）、群号、QQ号、时间组合查询聊天记录
- **数据维护**：手动清理过期图片；一键清空所有数据（需通过随机加减法验证，题目由后端生成、一次性有效）
- **总结设置**（v0.3 新增）：总结功能全部 24 项配置的分组表单（基础与白名单 / 参数上限 / 总结行为 / 存储 / 图片渲染）、总结专用 LLM 提供商下拉、提示词模板多行编辑与一键恢复默认
- **忽略管理**（v0.3 新增）：按群增删查忽略发送者，被忽略者的消息不参与总结
- **历史总结**（v0.3 新增）：按群浏览已保存的总结 JSON，弹层查看总结详情（统计块 + LLM 摘要 + 元数据）
- **人物分析**（v0.4.0 新增）：后台第三分区，三个 tab——
  - **分析设置**：人物分析全部 19 项配置的分组表单（总开关/权限/输出模式/LLM/参数上限/维度开关/冷却/反馈/保留天数）、分析专用 LLM 提供商下拉、备用模型列表
  - **发起分析**：选择分析范围（单群或全部群）与目标成员，跨群分析仅此处提供，可浏览群列表；@/QQ 目标选择
  - **历史分析**：按范围浏览已保存的分析结果 JSON，查看详情与删除
- **数据分析**（v0.5.0 新增）：存储库分区第 5 个子 tab，群级统计由预计算快照供数、个人/发言人维度实时 SQL 聚合（不缓存、不调 LLM、不落盘）——
  - **过滤器**：群选择器（全部白名单群 + 「全部群」汇总）+ 时间范围（今日 / 昨日 / 近7天 / 近30天 / 全部 + 自定义日期区间，最长 366 天）
  - **统计卡片**：总消息数、活跃成员数、图片数（来自快照表）、活跃时段峰值
  - **图表与排行**：每日趋势、发言人排行（Top N 附图片数，点击成员进入个人 × 群交叉视图）、群排行（仅「全部群」视图展示）、24h·星期发言规律图表（群 / 选定个人双维度）
  - **推送设置区**：各白名单群的群级推送开关 + 日报/周报全局配置（开关 / 推送时间 / 周报星期）

## 总结功能配置说明（v0.3 新增）

> 总结功能全部 24 项配置存入 config.db 新增的 `summary_settings` 表（key/value 形式，列表值 JSON 序列化），
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
| summary_provider_id | string（页内下拉） | "" | 总结专用 LLM 提供商（主选）；失败后按序尝试 `summary_fallback_providers` 备用列表，全部失败兜底该群会话当前 provider |
| summary_prompt | text | （内置 4 板块默认模板） | LLM 总结提示词，支持占位符 `{stats}` `{messages}` `{time_range}` `{group_id}` `{format_constraint}` |
| summary_output_mode | string | forward | 输出形式：`forward`=合并转发（剥离 Markdown）；`image`=文转图（保留 Markdown） |
| summary_rank_top_n | int | 5 | 活跃排行展示条数 |
| summary_max_prompt_chars | int | 60000 | 素材长度预算（送入 LLM 的完整提示词字符上限），超出从最旧消息开始截断，统计仍基于全量 |
| summary_retention_days | int | 30 | 总结 JSON 保留天数，定时任务每日清理过期文件 |
| summary_fallback_providers | list（JSON 存储） | [] | 备用总结模型列表，主选失败后按序尝试，全部失败回退会话模型 |
| summary_feedback_mode | string | reaction | 触发反馈模式：reaction 贴表情 / text 文字提示 / none 关闭 |
| summary_feedback_text | string | 📝 收到！正在总结中，请稍候… | 文字反馈文案（reaction 降级时同用） |
| summary_t2i_theme_mode | string | auto | 图片报告主题：`auto`=按时段自动 / `light`=强制浅色 / `dark`=强制深色 |
| summary_t2i_dark_start | string | 22:00 | 深色时段起点（HH:MM，服务器本地时间） |
| summary_t2i_light_start | string | 08:00 | 浅色时段起点（HH:MM）；默认 08:00–22:00 浅色、22:00–08:00 深色 |
| summary_t2i_timeout | int | 30 | 图片单轮渲染超时（秒，5–300）；失败自动以双倍超时重试第二轮 |
| summary_t2i_cdn_providers | list（JSON 存储） | ["bootcdn","npmmirror","staticfile","jsdelivr","unpkg"] | 图片模板加载 Markdown/图表脚本的 CDN 尝试顺序，国内镜像优先，单节点失败自动切换 |

占位符说明：`{stats}` 统计块、`{messages}` 格式化消息列表（每行 `[时间] 昵称: 内容`）、`{time_range}` 时间范围描述、`{group_id}` 群号、`{format_constraint}` 按输出模式注入的格式约束（合并转发=禁用 Markdown / 文转图=可用 Markdown）。

## 人物分析配置说明（v0.4.0 新增）

> 人物分析全部 19 项配置存入 config.db 新增的 `profile_settings` 表（key/value 形式，列表值 JSON 序列化），
> **不进入 `_conf_schema.json`**（插件原有配置保持不变）。配置仅在 dashboard「人物分析 → 分析设置」tab 修改，
> 改后即时生效，无需重启插件；默认值由 `ConfigManager` 初始化时自动播种。

| 配置键 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| profile_enabled | bool | true | 功能总开关 |
| profile_permission | string | admin | 指令权限：`admin`=仅管理员 / `all`=所有人 |
| profile_output_mode | string | forward | 输出形式：`forward`=合并转发 / `image`=图片（自研模板渲染）/ `text`=纯文本 |
| profile_provider | string（页内下拉） | "" | 分析专用 LLM 提供商（主选）；未配置时使用当前会话模型 |
| profile_fallback_providers | list（JSON 存储） | [] | 备用分析模型列表，主选失败后按序尝试，全部失败回退会话模型 |
| profile_max_count | int | 2000 | 单次分析最大消息条数 |
| profile_max_prompt_chars | int | 60000 | 素材长度预算（送入 LLM 的完整提示词字符上限），超出从最旧消息开始截断，统计仍基于全量 |
| profile_relation_context | bool | true | 关系上下文开关：开启后双向识别目标↔他人的 @/回复互动对象 |
| profile_relation_max_partners | int | 10 | 互动对象上下文最大人数（Top N） |
| profile_dim_habits | bool | true | 分析维度开关：发言习惯 |
| profile_dim_activity | bool | true | 分析维度开关：活动时间（24h / 星期分布图表） |
| profile_dim_personality | bool | true | 分析维度开关：性格（AI 推测） |
| profile_dim_hobbies | bool | true | 分析维度开关：兴趣爱好（AI 推测） |
| profile_dim_relations | bool | true | 分析维度开关：人物关系（AI 推测） |
| profile_user_cooldown | int | 60 | 同一用户两次触发的最小间隔（秒） |
| profile_group_cooldown | int | 30 | 同一群两次分析的最小间隔（秒） |
| profile_feedback_mode | string | reaction | 触发反馈模式：reaction 贴表情 / text 文字提示 / none 关闭 |
| profile_feedback_text | string | 正在生成人物画像，请稍候… | 文字反馈文案（reaction 降级时同用） |
| profile_keep_days | int | 30 | 分析结果 JSON 保留天数，定时任务每日清理过期文件 |

> 图片渲染复用总结功能的 `summary_t2i_*` 共享配置（主题 auto/light/dark、时段、超时、CDN 节点顺序），不新增 profile 专用渲染键。
> 关闭的分析维度不进提示词、不渲染；关闭「人物关系」后不再拉取关系上下文，只基于目标自身消息分析。

## 数据分析配置说明（v0.5.0 新增）

> 数据分析全部 8 项配置存入 config.db 新增的 `stats_settings` 表（key/value 形式），
> **不进入 `_conf_schema.json`**（插件原有配置保持不变）。配置在 Web 后台「数据分析」tab 的推送设置区管理，
> 改后即时生效，无需重启插件；默认值由 `ConfigManager` 初始化时自动播种，非法值拒绝写入。

| 配置键 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| stats_top_n | int | 10 | 排行展示条数（1–50） |
| stats_cooldown | int | 30 | `/群统计` 每群冷却秒数（0–600，0 = 不限流） |
| stats_image_top_k | int | 20 | 快照 Top K（图片/消息共用）：图片与消息快照的每小时每群个人 Top K（1–100） |
| push_daily_enabled | bool | true | 日报全局总开关（仍需群级开关同时开启才推送） |
| push_daily_time | string | 21:00 | 日报推送时间（HH:MM，24 小时制） |
| push_weekly_enabled | bool | false | 周报开关（可选功能，默认关） |
| push_weekly_weekday | int | 1 | 周报推送星期（1=周一 … 7=周日） |
| push_weekly_time | string | 09:00 | 周报推送时间（HH:MM，24 小时制） |

> 群级推送开关独立存于 `push_group` 表（每个白名单群一个开关，默认关），在「数据分析」tab 推送设置区逐群切换；
> 全局开关与群级开关同时开启的群才会收到日报/周报。
> 报告卡图片渲染复用总结功能的 `summary_t2i_*` 共享配置（主题 auto/light/dark、时段、超时、CDN 节点顺序），不新增 stats 专用渲染键。

## 重载自动补库配置说明（v0.6.0 新增，v0.6.1 完善）

> 两项配置存于 config.db `plugin_settings` 表（key/value 形式），随插件初始化自动播种默认值；
> 在 Web 管理后台「设置」页（🔄 重载自动补库分组）可修改；补库任务在启动/重载时读取，
> 修改后**重启插件**生效。

| 配置键 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| backfill_enabled | bool（字符串） | true | 重载自动补库总开关：`true` 开启，其他值跳过 |
| backfill_hours | int | 12 | 补库时间窗口上限（小时），夹取 [1,168]（1 小时 ~ 7 天），非法值回退 12 |
| backfill_round_cap | int | 200 | 单轮请求条数上限，夹取 [1,5000]（协议端常见单次硬限约 200 条） |
| backfill_max_rounds | int | 5 | 最大翻页轮数，夹取 [1,50]（拉到已记录边界即提前停止） |

> v0.6.1 起，补库窗口起点还参考内部键 `last_terminate_time`（插件卸载时自动记录，非用户配置）：
> 窗口取「上次卸载时间与 `now - backfill_hours`」的较新者，把拉取量收窄到停机缺口。

### 补库工作原理（v0.6.0 新增，v0.6.1 消息触发式）

1. **触发时机**：插件重启后，某群**第一条新消息到达时**触发该群补库（每群本重启周期内只补库
   一次，后续该群消息不再补库）；不再在加载时对所有群批量补库
2. **起点**：用这条触发消息自带的真实 `message_seq` 作 `get_group_msg_history` 起点，从它往前
   拉停机窗口缺口（该消息本身由实时路径入库）——NapCat 走 `getMsgHistory` 正式历史，不依赖
   aio 最新视图，规避群缓存空导致的「消息不存在」误报
3. **门控缓冲**：触发后该群进入门控，补库期间该群新消息先缓冲不落库，补库完成后**带去重**写入
   （跳过补库已写入的 message_id / 图片 URL）；其余群不受影响，正常实时落库
4. **窗口**：起点取「上次插件卸载时间（`last_terminate_time`，停机缺口）与 `now - backfill_hours`」
   的较新者，作为窗口上限；精确边界由「重叠边界停止」决定
5. **拉取**：从触发消息 seq 往前按 `message_seq` 多轮翻页（单轮上限 × 最大轮数，默认 200 条 × 5 轮，
   两者可经配置自定义；轮间 0.3s、超时 15s；本轮最旧消息已出窗口，或本轮与已记录消息**多数相同**
   （到达已记录边界，v0.6.1）即提前停止拉取；单轮失败降级跳过该群）
6. **窗口过滤**：仅保留 `timestamp >= 窗口起点` 的消息；空 `message_id` 消息补库侧跳过
   （由实时缓冲单源覆盖，v0.6.1）
7. **去重入库**：按 `message_id` 批量比对 `chat_history` 已存在记录（跳过）；图片 URL 按
   `(group_id, image_url)` 比对 `image_records` 已存在记录（跳过），单条消息内重复 URL 只入一次；
   **按最旧→最新顺序写入**，保持自增 id 与时间单调（v0.6.1）
8. **入库**：文本消息写入 `chat_history`（message_type 按有无图片 text/mixed，at_list/reply_id/真实
   时间戳透传），图片 URL 逐条写入 `image_records`；单条失败记 error 不中断该群
9. **收尾（v0.6.1）**：补库完成后，把该群门控期间缓冲的实时消息带去重写入，再触发快照启动回填
   （节流），使回填历史纳入快照层
10. **汇总日志**：每群完成输出 `[HistorySave] 重载自动补库` 汇总（拉取/新增文本/新增图片/跳过计数），
    可据日志核对补库情况

> 协议端只能拉取其缓存范围内的近期消息，补库窗口建议与协议端缓存一致（默认 12 小时）；
> 连续重载时重复消息由 `message_id` 去重自动跳过，不会重复入库。

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
  - `image` 文转图：保留 Markdown（含表格），经自研报告模板渲染成图片发送（多 CDN 容灾、日夜双主题、排行柱状图，详见「图片渲染」配置组）

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

新功能代码全部位于 `core/` 子包，指令在 `main.py` 注册并薄封装异步委托子包处理函数；无新增 pip 依赖。

**v0.6.0 模块化结构**：`main.py` 只保留与框架的交互（指令注册、事件委托、初始化/退出），
数据库访问、配置管理、Web API 与保存逻辑全部按子功能拆分到 `core/` 下独立文件：

```
main.py                          # 入口：仅框架交互（@register、指令 handler、事件委托）
core/
├── db_mysql/                    # MySQL 操作包（主文件只负责连接池管理与最终访问）
│   ├── pool.py                  #   DynamicPool 连接池管理
│   ├── base.py                  #   MySQLManagerBase 核心初始化/执行/迁移
│   ├── chat_history.py          #   消息表读写 + message_id 批量查重
│   ├── images.py                #   图片表读写 + 图片 URL 批量查重
│   ├── stats.py                 #   统计聚合查询
│   └── maintenance.py           #   清空数据维护
├── db_config/                   # 本地配置包（SQLite config.db）
│   ├── base.py                  #   ConfigManagerBase 建表/默认值/读写
│   ├── groups.py                #   群白名单管理
│   ├── summary_settings.py      #   总结配置
│   ├── profile_settings.py      #   人物分析配置
│   ├── stats_settings.py        #   数据分析配置
│   └── snapshots.py             #   快照表读写
├── webapi/                      # Web API 包（每个子功能一个文件）
│   ├── base.py                  #   WebAPIBase 路由表注册 + 公共 helper
│   ├── storage.py               #   状态/群管理/设置/清空
│   ├── query.py                 #   消息查询
│   ├── summary.py               #   总结设置/忽略/历史
│   ├── profile.py               #   人物分析设置/发起/历史
│   └── stats.py                 #   数据分析数据/设置/推送
├── summary/                     # 总结功能（service/fetcher/onebot/summarizer/formatter/t2i_render/storage/scheduler/templates）
├── profile/                     # 人物分析（service/fetcher/stats/analyzer/formatter/t2i_render/storage/scheduler/templates）
├── stats/                       # 数据分析（repository/models/parser/service/snapshot/scheduler/t2i_render/templates）
├── parsing.py                   # 消息解析纯函数（extract_image_urls / parse_onebot_raw_message / stats_fallback_text）
├── saver.py                     # MessageSaver 消息缓冲/落库逻辑（原 main.py 内逻辑迁出）
├── cleaner.py                   # ImageCleaner 图片过期清理
└── backfill.py                  # ReloadBackfill 重载自动补库（v0.6.0 新增）
```

> 组装范式：各包以「主文件 base + 子功能 Mixin」多继承组装出单一公开类名
> （`MySQLManager` / `ConfigManager` / `WebAPI`），对外调用零改动；类常量（如
> `ConfigManager.SUMMARY_TYPES`）经 MRO 解析，业务模块可直接引用。

| 模块 | 职责 |
|------|------|
| core/summary/service.py | 编排层：指令入口、白名单/限流校验、流程串联 |
| core/summary/fetcher.py | 数据获取：混合策略（MySQL 优先 + OneBot 补齐 + 去重合并） |
| core/summary/onebot.py | 协议端 `get_group_msg_history` 封装与消息段解析（补库复用其翻页范式） |
| core/summary/summarizer.py | 总结引擎：统计计算 + LLM 调用 + 提示词占位符渲染 |
| core/summary/formatter.py | 输出格式化：合并转发节点（剥离 Markdown）/ 文转图（含 GFM 表格兜底转换） |
| core/summary/t2i_render.py | 自研图片渲染核心：模板加载、日夜主题判定、多路 CDN/超时配置解析、两轮渲染 + 魔数校验 |
| core/summary/templates/ | 自研 T2I 报告模板（HTML + 内联 CSS/JS，双主题 + CDN 容灾加载器） |
| core/summary/storage.py | 总结 JSON 持久化（按群分目录、列表、读取、过期清理） |
| core/summary/scheduler.py | 定时清理任务（每天清理过期 JSON） |

### 人物分析模块（v0.4.0 新增）

| 模块 | 职责 |
|------|------|
| profile/service.py | 编排层：指令与 Web 共用入口、权限/限流校验、流程串联、目标解析 |
| profile/fetcher.py | 数据获取：单群 MySQL 分页 + OneBot 补齐 / 跨群全局拉取 + 关系上下文双向识别 |
| profile/stats.py | 确定性统计引擎：24h/星期分布、发言长度、emoji 率、互动排行等 |
| profile/analyzer.py | AI 分析：独立 provider 降级链、长度预算截断、五维度开关 prompt、四板块宽松切分 |
| profile/formatter.py | 输出格式化：合并转发 / 图片 / 纯文本三模式 |
| profile/t2i_render.py | 人物报告图片渲染核心（复用 summary_t2i_* 渲染配置） |
| profile/templates/profile_report.html | 自研人物报告模板（双主题 + 活动分布图表 + CDN 容灾加载器 + 免责声明页脚） |
| profile/storage.py | 分析结果 JSON 持久化（按范围分目录、确定性文件名、列表/读取/删除/过期清理） |
| profile/scheduler.py | 定时清理任务（每天清理过期分析文件） |

### 数据分析模块（v0.5.0 新增）

数据分析为纯 SQL 聚合 + 确定性计算，**不调用 LLM**、无新增 pip 依赖；只统计启用了存储的群（消息入库才有数据），「全部群」汇总即 `chat_history` 中实际有数据的群。

**三端同源**：Web「数据分析」tab、`/群统计` 指令与定时推送（日报/周报）共用同一数据组装入口 `build_stats`——先限流强制刷新当前小时快照（60 秒内至多一次），再按分段快照读取群级统计（总消息数/每日趋势/群排行），同时并发执行实时维度的 MySQL 聚合查询（活跃成员数/24h 与星期分布/发言人排行/个人概览），最后注入快照表图片数，保证三端展示的口径完全一致。

**分段快照供数（v0.5.5 新增）**：群级统计改由预计算快照读数——总消息数、每日趋势、群排行消息数走「消息 + 图片 × 小时/日/月」快照的三层归并（月层取完整月份，剩余日期存在性优先：该日有小时快照则用小时快照求和、否则用日快照兜底，无重叠无空洞），口径精确、无 Top K 截断误差；活跃成员数、发言人排行与个人维度全部保持实时 SQL。查询范围快照不可服务（如自定义区间切断了旧月中间）或快照层异常时，自动整体回退实时 SQL 路径，不报错、行为对用户透明。注意：`image_stats_hourly_top` 仅保留 31 天，超 31 天范围的发言人/个人图片数归 0（群级图片总数不受影响，由日/月快照兜底）；上线前已被清理的图片记录无法追溯回填。

**图片快照为何小时级增量**：`image_records` 表滚动清理（`image_retention_days`，默认保留 3 天），过期图片删除后无法再回溯统计，且个人级图片统计直接扫 MySQL 行数大。因此调度器每小时（整点后 5 分）从 MySQL 聚合**上一个完整小时**的图片数写入 SQLite 快照表（群级全量 + 每小时每群个人 Top K）；v0.5.5 起该循环双源化，同时聚合消息写入消息快照表（见上条「分段快照供数」）；任何手动统计（`/群统计` 指令、Web 数据分析接口）执行前，再把**当前未完整小时**的聚合结果 UPSERT 覆盖写一次（60 秒限流，期间至多执行一次），保证「今日」数据实时。

**个人图片数 Top K 口径**：快照表每小时每群仅记录图片数前 K（`stats_image_top_k`，默认 20）名活跃发送者的数据，因此排行/卡片中的个人图片数仅在 Top K 内准确（页面与报告卡注明口径）；消息数口径不受影响，来自 `chat_history`（text/mixed）。

| 模块 | 职责 |
|------|------|
| stats/repository.py | MySQL 聚合仓储：全参数化统计查询（实时维度 8 查询 + v0.5.5 新增快照聚合 6 查询：小时/日/月批量聚合与窗口 Top K），统一半开时间窗口，30s 查询超时兜底 |
| stats/models.py | 数据模型（6 个 dataclass，Web/指令/推送共用契约） |
| stats/parser.py | `/群统计` 参数解析纯函数：时间关键词/单日/区间（4 种分隔符、跨度 ≤366 天校验）/ @ 与 QQ 号混排识别 |
| stats/service.py | 编排层：`build_stats` 三端同源组装（群级读快照、超范围/异常自动回退实时 SQL）、概览趋势快照供数（days ≤ 31）、启动回填任务、每群冷却、群 → 推送目标（umo）内存缓存、日报/周报推送（逐群串行、单群失败不阻断） |
| stats/snapshot.py | 分段快照管理（v0.5.5 起消息 + 图片双源）：小时/日/月三段任务聚合、淘汰、启动窗口重聚合回填、当前小时限流强制刷新、群级统计三层归并供数与图片数注入 |
| stats/scheduler.py | 后台调度：推送时间检查循环（日报/周报到点触发）+ 小时/日/月三类快照循环（各自独立去重戳，日快照成功后顺带淘汰），异常指数退避 |
| stats/t2i_render.py | 报告卡渲染核心：复用 `summary_t2i_*` 配置、两轮渲染 + 魔数校验、绝不抛异常（失败由上层降级纯文本） |
| stats/templates/stats_report.html | 自研统计报告卡模板（860px 双主题，与总结/人物报告同款设计令牌；ECharts 版本锁定 CDN + 纯 CSS 兜底图表） |

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
| at_list | TEXT（JSON） | @ 对象列表（v0.4.0 新增，旧表自动 ALTER 迁移） |
| reply_id | VARCHAR(64) | 回复目标消息 ID（v0.4.0 新增，旧表自动 ALTER 迁移，可空） |

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
| summary_settings | 总结功能配置（key TEXT PRIMARY KEY, value TEXT），24 项总结配置均存于此，仅 dashboard「总结设置」tab 修改 |
| group_ignore_senders | 每群忽略发送者（group_id, sender_id, created_at，group_id + sender_id 联合唯一约束） |

### config.db 新增表（v0.4.0 新增）

| 表名 | 说明 |
|------|------|
| profile_settings | 人物分析配置（key TEXT PRIMARY KEY, value TEXT），19 项分析配置均存于此，仅 dashboard「人物分析 → 分析设置」tab 修改 |

> 分析结果不入库，以 JSON 文件持久化：`data/plugin_data/astrbot_plugin_group_history_save_mysql/profiles/<scope目录>/<文件名>.json`，
> 按范围分子目录（`group_<群号>` / `all`），文件名含生成时间与目标 QQ 号，保留天数 `profile_keep_days`（默认 30 天），
> 定时任务每日清理过期文件。

### config.db 新增表（v0.5.0 新增）

| 表名 | 说明 |
|------|------|
| stats_settings | 数据分析配置（key TEXT PRIMARY KEY, value TEXT NOT NULL），8 项配置均存于此，仅 Web 后台「数据分析」tab 修改 |
| push_group | 群级推送开关（group_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0）；以 group_config 白名单为准，不在白名单的行忽略 |
| image_stats_hourly | 图片统计小时级快照（群级全量）：date TEXT, hour INTEGER, group_id TEXT, image_count INTEGER，主键 (date, hour, group_id) |
| image_stats_hourly_top | 图片统计小时级快照（每小时每群个人 Top K）：date TEXT, hour INTEGER, group_id TEXT, sender_id TEXT, sender_name TEXT, image_count INTEGER，主键 (date, hour, group_id, sender_id) |

> v0.5.0 **MySQL 无 schema 变更**：统计全部基于 `chat_history`（`idx_group_time` / `idx_group_sender_time`）
> 与 `image_records`（`idx_img_time` / `idx_img_group`）既有索引的聚合查询。
> 群 → 推送目标（unified_msg_origin）映射仅存内存缓存，由群消息监听实时登记，重启后由该群首条消息重建；
> 统计结果不落盘，每次实时查询。

### config.db 新增表（v0.5.5 新增）

| 表名 | 说明 |
|------|------|
| msg_stats_hourly | 消息统计小时级快照（群级全量）：date TEXT, hour INTEGER, group_id TEXT, msg_count INTEGER，主键 (date, hour, group_id)；保留 7 天 |
| msg_stats_hourly_top | 消息统计小时级快照（每小时每群发言人 Top K，含昵称）：date TEXT, hour INTEGER, group_id TEXT, sender_id TEXT, sender_name TEXT, msg_count INTEGER，主键 (date, hour, group_id, sender_id)；保留 7 天 |
| msg_stats_daily | 消息统计日级快照（每群每日消息总数，全量不受 Top K 截断，只存完整日）：date TEXT, group_id TEXT, msg_count INTEGER，主键 (date, group_id)；保留上月+本月 |
| msg_stats_monthly | 消息统计月级快照（只存完整月，month="YYYY-MM"）：month TEXT, group_id TEXT, msg_count INTEGER，主键 (month, group_id)；永不淘汰 |
| image_stats_daily | 图片统计日级快照（每群每日图片总数，只存完整日）：date TEXT, group_id TEXT, image_count INTEGER，主键 (date, group_id)；保留上月+本月 |
| image_stats_monthly | 图片统计月级快照（只存完整月）：month TEXT, group_id TEXT, image_count INTEGER，主键 (month, group_id)；永不淘汰 |

> **三段式保留策略**：小时层保留 7 天（其中图片个人 Top K 层 `image_stats_hourly_top` 保留 31 天，
> 保护近 30 天报告的个人图片口径），日层保留上月+本月，月层永久；淘汰在每日日快照任务成功后执行，
> 与三层归并的地平线（上月 1 日）严格对齐。全部快照表 UPSERT 覆盖写，行只增盖不删。
> **启动回填**：插件每次启动（MySQL 初始化成功后）后台自动回填一次——窗口批量重聚合（每层一条
> GROUP BY SQL，幂等）补齐近 7 天小时层（同时补偿宕机错过的整点缺档）、上月 1 日起的日层，
> 月层按持久游标（plugin_settings 内部键，非用户配置）增量补齐；上线前 `image_records` 已被清理的
> 天数无法追溯回填图片数（旧月图片月快照自然为 0 行）。异常仅记日志不阻断启动。
> **强制刷新限流**：手动统计执行前的当前未完整小时强制刷新（消息 + 图片双源）60 秒内至多执行一次，
> 高频统计请求不再反复触发 MySQL 聚合。
> v0.5.5 **MySQL 仍无 schema 变更**：快照全部为 `chat_history` / `image_records` 的只读聚合；
> v0.5.2 已有的 `image_stats_hourly(_top)` 数据原样保留、直接复用。

## 支持平台

- aiocqhttp (OneBot v11)


## License

AGPL-3.0
