# Changelog

## [0.3.0] - 2026-07-30

### Added

- **群聊历史自动总结（新功能）**：新增群指令 `/消息总结 <数量>`（别名 `/总结`）与 `/消息总结时间 <时长>`
  （别名 `/总结时间`，`h`=小时 / `d`=天，仅支持单点「最近 X」，不支持区间语法），
  白名单群内任何成员可用，仅当前群生效；私聊中使用回复「请在群内使用」
- **指令参数校验**：缺参/非法参数（非正整数、不符合 `^\d+[hd]$`、≤0）回复用法提示（不查库、不消耗 LLM）；
  超出 Web 配置上限（`summary_max_count` 默认 1000 条 / `summary_max_hours` 默认 168 小时）拒绝执行并提示上限；
  功能关闭回复「功能未启用」、群不在白名单回复「本群未开启总结功能」、冷却中回复剩余等待秒数
- **混合数据源**：总结优先查本插件 MySQL `chat_history`，不足时经 OneBot v11 `get_group_msg_history`
  从协议端在线补齐（数量模式按 `summary_min_mysql_ratio` 判定，时间模式按窗口条数与
  `summary_gap_tolerance_minutes` 判定），两源按 `message_id` 去重（无 message_id 时退化为
  「秒级时间戳 + 发送者 + 内容前 32 字符」），按时间升序合并
- **OneBot 数据不回填**：协议端拉到的消息仅本次总结使用，不写入 `chat_history`；
  任一数据源失败自动降级为可用数据继续，两源皆空回复无可总结消息提示
- **总结内容**：规则统计块（消息总数/参与者人数/时间跨度/发言条数排行 Top N）
  + LLM 四板块摘要（📢 重要通知与结论 / 💬 讨论要点·争议 / 🎉 有趣片段 / ✅ TODO·待跟进，
  每个条目含参与者与大致时间）
- **输出形式**（Web 可配 `summary_output_mode`）：合并转发 `forward`（1 个统计节点 + 4 个板块节点，
  提示词约束 + 发送前程序化剥离双重保障去除 Markdown 标记）/ 文转图 `image`（保留 Markdown，渲染成图片发送）
- **LLM 提供商**：Web 可指定总结专用 provider（`summary_provider_id`，dashboard 下拉选择），
  未配置时回退该群会话当前 provider，两者皆不可用回复错误提示
- **提示词模板**（`summary_prompt`）：内置占位符 `{stats}` `{messages}` `{time_range}` `{group_id}`
  `{format_constraint}`，dashboard 多行文本框可自由编辑调试，支持一键恢复默认模板
- **权限与限流**：独立群白名单（`summary_group_whitelist` + `whitelist`/`all` 模式，
  与现有录制白名单 group_config 表互不干扰）+ 用户/群双冷却（`summary_user_cooldown` 默认 60s /
  `summary_group_cooldown` 默认 120s），冷却表存内存、重启清零
- **素材过滤**：非文本消息完全忽略（不占位、不统计、不送 LLM）；默认过滤 bot 自身消息（硬编码）；
  每群可配置忽略发送者（dashboard「忽略管理」tab 增删查，存 config.db `group_ignore_senders` 表）
- **结果持久化**：总结以 JSON 保存到 `data/plugin_data/astrbot_plugin_group_history_save_mysql/summaries/<群号>/<时间戳>.json`
  （含统计块、摘要原文、元数据），保留天数可配（`summary_retention_days` 默认 30 天），
  定时任务每天清理一次过期文件，随插件生命周期启停
- **配置管理**：全部 16 项新配置存入 config.db **新增 `summary_settings` 表**（key/value，列表值 JSON 序列化），
  由 `ConfigManager` 统一建表、播种默认值与 CRUD；**不进入 `_conf_schema.json`**（原有插件配置文件保持不变），
  仅在 dashboard「总结设置」tab 修改，改后即时生效无需重启插件
- **Web 管理后台新增 3 个 tab**：总结设置（16 项配置分组表单：基础与白名单 / 参数上限 / 总结行为 / 存储，
  provider 下拉，提示词编辑与恢复默认）/ 忽略管理（每群忽略发送者增删查）/ 历史总结（按群浏览已存总结 + 详情弹层）
- **OneBot 多轮翻页**：`fetch_group_history` 按 `message_seq` 从新到旧翻页累计（最多 5 轮、轮间短延迟防限频、
  每轮 1.3x 超量请求补偿过滤损耗），兼容单次 count 硬限 ~200 条的协议端实现；支持大 count 的协议端
  第一轮即因短页终止（行为等价单次调用）；首轮失败仍抛错降级，第 2 轮起失败返回已拉到的部分消息
- **dashboard 两级导航**：顶部分区控件「存储库 / 消息总结」，总结三 tab 归入「消息总结」独立分区；
  存储区启动不再预触总结接口，进入总结分区才惰性加载
- **素材长度预算 Web 可配**：`summary_max_prompt_chars`（默认 60000 字符）可在 dashboard「总结设置 →
  参数上限」调整，非法值回退常量默认；超限从最旧消息截断，统计块仍基于全量
- **架构**：新功能代码全部位于 `summary/` 子包（service 编排层 / fetcher 混合获取 / onebot 协议端封装 /
  summarizer 总结引擎 / formatter 输出格式化 / storage JSON 持久化 / scheduler 定时清理），
  指令在 `main.py` 注册并薄封装异步委托子包；日志统一 `[HistorySummary]` 前缀；无新增 pip 依赖

### Changed

- metadata.yaml 版本升至 v0.3.0，desc 更新为「将 QQ 群聊天记录保存到 MySQL，支持 Web 管理后台与群聊历史自动总结（MySQL 优先 + 协议端补齐）」
- 完全向后兼容 v0.2.1 数据与配置：`chat_history` / `image_records` 表结构不变，新配置由 `ConfigManager` 初始化自动播种默认值

## [0.2.1] - 2026-07-28

### Fixed

- **连接池性能（严重）**：重构 `DynamicPool._get_connection` 和 `_health_check`，
  将 `aiomysql.connect()` / `conn.ping()` 等网络 I/O 移出 `Condition` 锁外，
  高并发下连接池不再退化为串行，吞吐量显著提升
- **清空性能**：`purge_all` 优先使用 `TRUNCATE TABLE`（DDL，自动复位自增 ID），
  大表清空从秒级降到毫秒级；无 DROP 权限时自动回退到 DELETE + ALTER 方式
- **数据目录规范**：`db_config.py` 改用 `StarTools.get_data_dir()` 获取数据目录，
  符合 AstrBot 插件规范（路径不变，存量 config.db 继续使用）
- **cursor 资源管理**：aiosqlite 所有 cursor 统一改用 `async with` 管理，
  避免长期运行资源泄漏
- **add_group 副作用**：改用 SQLite UPSERT（`ON CONFLICT DO UPDATE`），
  重复添加群时 `enabled` 强制为 1 但 `created_at` 不被重置
- **toggle_group 竞态**：改用单条 `UPDATE ... SET enabled = 1 - enabled` 配合
  `rowcount` 判断，消除 SELECT-then-UPDATE 竞态
- **_resolve_group_id**：改为同步函数（内部无 await），调用处去掉 await
- **异常日志**：`on_group_message` 异常日志增加 `exc_info=True`，保留完整堆栈
- **清理退避**：`cleaner._cleanup_loop` 异常重试改为指数退避（60→120→300→600s），
  连续失败 5 次告警，避免数据库不可用时无限刷日志
- **challenge 上限**：`_purge_challenges` 增加 1000 条上限，超限返回 429
- **参数转换**：`request.query.get` 的 `type=int` 改为显式 `try/except int()`，
  转换失败回退默认值
- **docstring 修正**：`insert_chat_message` 的 `message_type` 注释修正为 `text/mixed`

## [0.2.0] - 2026-07-28

### Fixed

- 修复新版 AstrBot 下图片链接失效的问题：预处理阶段会将消息链中 `Image` 组件的 url/file 改写为本地临时路径，
  现改为从 OneBot 原始事件（`raw_message`）的图片消息段提取 QQ 下发的原始链接（`https://gchat.qpic.cn/...`），
  提取不到 http 链接时回退消息链；本地路径一律不入库
- 修复「清空所有数据」后自增 ID 不复位的问题：清空后对两张表执行 `ALTER TABLE ... AUTO_INCREMENT = 1`，
  新数据从 1 重新开始（MySQL 自增主键最小为 1，无法从 0 开始）
- 修复查询面板群号/QQ号被 `parseInt` 导致非数字输入匹配不到的问题，改为字符串原样透传

### Changed

- `chat_history` 与 `image_records` 两张表的 `group_id`、`sender_id` 列由 `BIGINT` 改为 `VARCHAR(32)`（文本格式）。
  升级时插件自动检测旧表结构并执行 `ALTER TABLE` 迁移，已有数字数据自动转为字符串，无需手动操作
- Web 查询接口的群号/QQ 号参数改为字符串传递

### Added

- `image_records` 表新增 `sender_name` 列，记录图片发送时的昵称（旧表升级时自动 `ADD COLUMN`）
- Web 管理后台「设置 → 数据维护」新增「清空所有数据」按钮：点击后弹出随机加减法验证题，
  答案正确才会清空 `chat_history` 与 `image_records` 全部数据；验证题由后端生成，一次性使用、5 分钟过期，
  防止误操作与穷举
- 查询面板新增「关键词」过滤，对消息内容与发送昵称做模糊匹配

## [0.1.0] - 2026-07-27

### Added

- 初始版本发布
- 自动监听 QQ 群消息并保存到 MySQL
- 文本消息存储到 `chat_history` 表
- 图片 URL 存储到 `image_records` 表
- 群白名单管理（aiosqlite 本地存储）
- ALL 模式支持（记录所有群）
- 图片表每天自动清理过期数据
- Web 管理后台（状态监控、群管理、统计、查询）
- 管理指令：/history_start、/history_stop、/history_status、/history_clean
- 数据库联合索引（时间/群号/QQ号）
