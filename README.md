# astrbot_plugin_group_history_save_mysql

将 QQ 群聊天记录自动保存到 MySQL 数据库，支持按时间、群号、QQ 号索引过滤，提供 Web 管理后台；
并在此基础上提供**群聊历史自动总结**、**人物分析**、**数据分析**、**重载自动补库**与**对外查询接口**等能力。
完整版本演进见 [CHANGELOG.md](CHANGELOG.md)。

## 功能

- 📝 自动监听群消息，保存文本到 MySQL `chat_history` 表
- 🖼️ 图片 URL 单独存储到 `image_records` 表（从 OneBot 原始事件提取 QQ 下发的原始链接，兼容新版 AstrBot）
- 🏷️ 文本与图片记录均保存发送时的昵称
- 🔍 建立联合索引，支持按时间/群号/QQ号高效过滤
- 🗂️ 群白名单管理（支持 ALL 模式）
- 🧹 图片表每天自动清理过期数据（默认保留 3 天）
- 🌐 Web 管理后台：状态监控、群管理、统计、查询面板；总结设置 / 忽略管理 / 历史总结 / 人物分析 / 数据分析 / 查询日志等 tab
- 🗑️ 一键清空所有数据（带随机加减法验证，防误触）
- ⚙️ 管理指令：/history_start、/history_stop、/history_status、/history_clean、/补库
- 🤖 群聊历史自动总结（v0.3）：`/消息总结 <数量>`、`/消息总结时间 <时长>`，白名单群内任何成员可用
- 🔀 总结混合数据源：MySQL 优先 + OneBot 协议端在线补齐，按 message_id 去重合并，协议端数据不回填数据库
- 📊 规则统计块 + LLM 四板块摘要，合并转发 / 文转图两种输出形式（Web 可配）
- 🛡️ 备用模型降级链（v0.3.1）：按「主选提供商 → 备用列表按序 → 会话模型兜底」调用
- 👍 触发反馈（v0.3.1）：指令生效后即时确认（贴表情 / 文字，可配）
- 🎨 自研图片报告模板（v0.3.2）：多路 JS CDN 容灾、Markdown 表格渲染、发言人排行柱状图、日夜双主题
- 👤 人物分析（v0.4.0）：`/人物分析 [@成员 或 QQ号]` 分析发言习惯、活动时间、性格、爱好与人物关系
- 🌐 跨群分析（v0.4.0）：仅 Web 后台「人物分析」分区提供（按全体已保存群分析）
- 🔌 对外查询接口（v0.7.0）：其他插件经 `core.public_api` 调用 `query_records()` / `count_messages()`；导入无副作用、不暴露内部对象
- 📋 查询日志（v0.7.0）：每次对外查询写入内置 SQLite，保留天数 Web 可调，Web 可审计
- 💾 存储扩展（v0.4.0）：`chat_history` 表新增 `at_list` / `reply_id` 列（自动迁移）
- 📈 数据分析（v0.5.0）：Web 实时 SQL 聚合面板（统计卡片、趋势、排行、24h·星期规律、个人 × 群交叉查询）
- 🖼️ `/群统计` 指令（v0.5.0）：群/个人统计图片报告卡，支持时间关键词与自定义日期，每群 30s 冷却
- 📬 定时推送（v0.5.0）：日报（默认每天 21:00）与可选周报，全局 + 群级开关在 Web 管理
- 🧱 分段快照统计（v0.5.5）：消息 + 图片 × 小时/日/月预计算快照，快照不可服务自动回退实时 SQL
- 🔄 重载自动补库（v0.6.0）：插件加载/重载后从 OneBot 拉取近 `backfill_hours` 小时历史消息补库，双重去重
- 🧩 模块化重构（v0.6.0）：全部代码迁入 `core/` 子包，`main.py` 仅保留框架交互
- 🪶 结构下沉（v0.8.1）：实例装配/生命周期迁至 `core/bootstrap.py`、指令体迁至 `core/commands.py`

## 安装

在 AstrBot 插件市场搜索 `astrbot_plugin_group_history_save_mysql` 安装，或手动克隆到 `data/plugins/`。
安装后按 [MySQL 配置指南](docs/mysql-setup.md) 填写数据库连接信息，插件会自动建表。

## 文档

| 文档 | 内容 |
|------|------|
| [MySQL 配置指南](docs/mysql-setup.md) | 安装 MySQL、建库建用户、插件连接配置与验证 |
| [指令](docs/commands.md) | 管理指令、总结 / 人物分析 / 群统计指令与参数、权限与限流 |
| [Web 管理后台](docs/web-ui.md) | 各分区与 tab 的功能说明 |
| [总结功能](docs/summary.md) | 24 项配置说明 + 混合数据源与输出工作原理 |
| [人物分析](docs/profile.md) | 19 项配置说明 |
| [数据分析](docs/stats.md) | 8 项配置说明 + 快照/回退工作原理 |
| [重载自动补库](docs/backfill.md) | 配置说明 + 触发式补库工作原理 |
| [代码结构](docs/architecture.md) | `core/` 模块划分与各功能模块职责表 |
| [接口契约](docs/contracts/) | 总结 / 人物分析 / 数据分析各子模块的接口契约 |
| [数据库表结构](docs/database.md) | MySQL 与内置 SQLite（config.db）各表字段与索引 |
| [对外查询接口 (PUBLIC_API.md)](PUBLIC_API.md) | 其他插件编程调用的完整文档 |
| [更新日志 (CHANGELOG.md)](CHANGELOG.md) | 各版本变更记录 |

## 支持平台

- aiocqhttp (OneBot v11)

## License

AGPL-3.0
