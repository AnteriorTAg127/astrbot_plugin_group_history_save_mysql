# Changelog

## [0.4.0] - 2026-08-03

### Added

- **人物分析（新功能）**：新增群指令 `/人物分析 [@成员 或 QQ号]`（别名 `/人物画像`、`/分析TA`），
  基于历史发言分析成员人物画像——发言习惯、活动时间（24 小时 / 星期分布图表）、性格、兴趣爱好、
  人物关系五大维度；聊天指令仅分析当前群
- **跨群分析**：按全部已保存群分析仅 Web 后台提供（「人物分析」分区「发起分析」tab），
  聊天指令不提供 all 范围；跨群走 MySQL 全量拉取、不做 OneBot 补齐
- **目标触发**：支持 @ 群成员（自动剔除 `all` 与 bot 自身）或直接输入纯数字 QQ 号触发；
  两者皆无时回复用法提示
- **权限与限流**：`profile_permission` 配置（默认 `admin` 仅管理员，可配 `all` 全员可用）+
  用户/群双冷却（`profile_user_cooldown` 默认 60s / `profile_group_cooldown` 默认 30s）；
  触发即时反馈（`profile_feedback_mode`：reaction 贴 👍 / text / none，reaction 失败自动降级文字）
- **数据获取**：单群分析 MySQL 分页拉取（DESC→升序），不足时经 OneBot 原始消息筛目标补齐；
  关系上下文双向识别（`profile_relation_context` 默认开，`profile_relation_max_partners` Top N）——
  聚合目标↔他人的 @ 互动、经 `reply_id` 反查回复对象、同群扫描池与 OneBot 实时补强；
  全程容错降级，数据源/是否完整写入结果元数据
- **确定性统计引擎**：`profile/stats.py` 无 AI 计算发言统计（24h/星期分布、峰值时段、发言长度、
  emoji 率、问号率、活跃天数、群分布、互动排行），统计不随 LLM 波动
- **AI 分析**：独立 provider 降级链（`profile_provider` → `profile_fallback_providers` 按序 →
  会话模型兜底，全失败返回「分析失败」兜底绝不抛异常）+ 长度预算截断
  （`profile_max_prompt_chars` 默认 60000）+ 五维度开关（关闭维度不进 prompt、不渲染）+
  四板块宽松切分；输出含免责声明「本报告基于公开群聊记录由 AI 生成，仅为推测，仅供参考」
- **输出形式**（`profile_output_mode`）：合并转发 `forward` / 图片 `image`（自研人物报告模板渲染）/
  纯文本 `text` 三模式，互斥可选
- **自研人物报告模板**：`profile/templates/profile_report.html`（860px 双主题，与总结/管理面板同款
  设计令牌）——报告头 + 四统计卡 + 小时/星期活动分布图表（ECharts 峰值高亮 + 纯 CSS 竖柱兜底）+ 
  互动排行横向条形图（ECharts + CSS 兜底）+ 板块 Markdown（marked GFM 客户端渲染 + Python 预转换
  兜底 HTML 含表格）+ 五节点 CDN 容灾加载器 + 免责声明页脚；渲染复用总结的 `summary_t2i_*` 共享配置
  （主题/时段/超时/CDN 节点顺序，不新增 profile 专用渲染键）
- **存储层增强**：`chat_history` 表新增 `at_list` / `reply_id` 两列（幂等 ALTER 自动迁移旧表），
  入库记录 @ 对象与回复目标；`db_mysql` 新增 `get_messages_by_ids`（关系上下文反查用）
- **配置管理**：全部 19 项新配置存入 config.db **新增 `profile_settings` 表**（key/value，列表值
  JSON 序列化），由 `ConfigManager` 统一建表、播种默认值与 CRUD；**不进入 `_conf_schema.json`**，
  仅在 dashboard「人物分析 → 分析设置」tab 修改，改后即时生效无需重启插件
- **Web 管理后台第三分区**：「人物分析」分区三个 tab——分析设置（19 项配置分组表单 + provider 下拉 +
  备用模型列表）/ 发起分析（范围选择 + 目标成员 + 群列表，跨群分析入口）/ 历史分析（按范围浏览
  已保存分析 JSON，详情与删除）；新增 9 个 profile Web API 端点（settings/providers/groups/analyze/history）
- **结果持久化**：分析结果以 JSON 保存到 `data/plugin_data/astrbot_plugin_group_history_save_mysql/profiles/<scope目录>/<文件名>.json`
  （scope 目录 `group_<群号>` 或 `all`，文件名含生成时间与目标 QQ 号），保留天数可配
  （`profile_keep_days` 默认 30 天），定时任务每日清理过期文件
- **架构**：新功能代码全部位于 `profile/` 子包（service 编排 / fetcher 获取 / stats 统计 / analyzer
  AI 分析 / formatter 输出 / t2i_render 渲染 / templates 模板 / storage 持久化 / scheduler 清理），
  指令在 `main.py` 注册并薄封装异步委托子包；日志统一 `[Profile]` 前缀；无新增 pip 依赖

### Changed

- metadata.yaml 版本升至 v0.4.0，desc 更新为「…人物分析支持群成员发言习惯与画像分析（@ 或 QQ 触发，Web 可跨群）」
- 完全向后兼容 v0.3.2 数据与配置：`chat_history` 新增列自动幂等迁移、存量数据不受影响；
  新配置由 `ConfigManager` 初始化自动播种默认值

## [0.3.2] - 2026-07-31

### Added

- **自研 T2I 报告模板**：图片总结改用插件自带模板 `summary/templates/summary_report.html`（860px 画布、
  与管理面板同款双主题设计令牌、移动端友好字号），含报告头、四统计卡、发言人排行、分板块 Markdown 内容与页脚，
  替代原最小化渲染模板
- **多路 JS CDN 容灾**：模板内联加载器按 `summary_t2i_cdn_providers` 顺序尝试
  （默认国内镜像优先 bootcdn/npmmirror/staticfile → jsdelivr/unpkg），单节点 8 秒超时自动切换；
  加载 `marked`（Markdown + GFM 表格）与 `echarts`（柱状图）；全部失败自动降级为服务端预转换 HTML 与纯 CSS 柱状图，
  图片仍可产出
- **发言人排行柱状图**：把活跃排行 Top N 渲染为横向柱状图（ECharts 主题色板 + 纯 CSS 渐变横条双形态互为兜底）
- **白天/夜间自动主题**：`summary_t2i_theme_mode`（auto/light/dark）+ `summary_t2i_dark_start`/
  `summary_t2i_light_start`（默认 22:00/08:00，HH:MM 可配），按服务器本地时间判定，22 点后深色、8 点后浅色
- **渲染超时可配**：`summary_t2i_timeout`（默认 30 秒，5–300），两轮渲染 R1 PNG(T) / R2 JPEG 质量 80(2T)，
  并对截图结果做文件头魔数校验，防止把渲染服务返回的错误页面当成图片
- **图片渲染配置组**：dashboard「总结设置」新增「图片渲染」分组，含主题模式 / 深色起点 / 浅色起点 / 渲染超时 /
  CDN 节点顺序 5 项
- **备用模型列表交互改版**：原内联长复选框列表改为触发按钮 + 弹窗——弹窗内「降级顺序」区可拖拽排序、
  「可选模型」区滚动勾选，点击遮罩不关闭（防误触丢失排序），失效模型保留可拖可删

### Changed

- 图片模式格式约束放开并**推荐 Markdown 表格**（GFM 管道符语法），表格在图片中正常渲染
- 图片渲染兜底链路改为「自研模板 → `text_to_image` → 纯文本」
- 总结配置项 19 → 24（新增 5 项图片渲染配置）
- metadata.yaml 版本升至 v0.3.2
- 完全向后兼容 v0.3.1：新配置由 `ConfigManager` 初始化自动播种默认值，无需迁移，已存配置不受影响

## [0.3.1] - 2026-07-31

### Added

- **备用模型降级链**：新增 `summary_fallback_providers` 配置（Web 后台多选），LLM 总结按
  「主选提供商 → 备用列表按序 → 会话模型兜底」调用，任一节点失败（异常/空文本）自动降级，
  历史总结记录实际使用的模型
- **总结触发反馈**：新增 `summary_feedback_mode`（`reaction`/`text`/`none`，默认 `reaction`）与
  `summary_feedback_text` 配置，指令生效后即时确认（贴 👍 表情或文字提示）；协议端不支持
  贴表情时自动降级文字

### Changed

- 面板改为两级导航：「存储库 / 消息总结」分区（总结三 tab 归入消息总结分区，惰性加载）
- OneBot 历史拉取改为 `message_seq` 多轮翻页（最多 5 轮、轮间短延迟防限频、每轮 1.3x 超量请求
  补偿过滤损耗），兼容单次条数受限（~200 条硬限）的协议端；第 2 轮起失败返回已拉到的部分消息
- 素材长度预算 `summary_max_prompt_chars` 升级为 Web 可配（默认 60000，非法值回退常量默认）
- 总结设置数字输入框加宽，长数字不再被旋钮挤占
- 总结配置项 15 → 19（长度预算升级为配置 + 本版新增备用模型列表与反馈 3 项）

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
- **配置管理**：全部 15 项新配置存入 config.db **新增 `summary_settings` 表**（key/value，列表值 JSON 序列化），
  由 `ConfigManager` 统一建表、播种默认值与 CRUD；**不进入 `_conf_schema.json`**（原有插件配置文件保持不变），
  仅在 dashboard「总结设置」tab 修改，改后即时生效无需重启插件
- **Web 管理后台新增 3 个 tab**：总结设置（15 项配置分组表单：基础与白名单 / 参数上限 / 总结行为 / 存储，
  provider 下拉，提示词编辑与恢复默认）/ 忽略管理（每群忽略发送者增删查）/ 历史总结（按群浏览已存总结 + 详情弹层）
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
