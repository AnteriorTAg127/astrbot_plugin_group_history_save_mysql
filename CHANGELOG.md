# Changelog

## [0.5.2] - 2026-08-04

修复数据分析/查询页打开即吃满 CPU 的两个独立问题：

1. **旧表复合索引缺失**：复合索引（`idx_group_time` 等）只存在于
   `CREATE TABLE IF NOT EXISTS` 的 DDL 中，早期版本已建出的表该 DDL 为 no-op，
   而旧迁移逻辑只补 `idx_message_id`，导致按群过滤（`WHERE group_id = … AND
   timestamp …`）与全表 `GROUP BY` 退化为全表扫描。现 `_migrate_schema` 启动时
   幂等补建全部必需索引。
2. **相关子查询放大扫描**：发言人排行取最新昵称的 `_latest_sender_names` /
   `_latest_image_sender_names` 使用相关子查询（外层每行重跑一次内层，
   `ORDER BY timestamp DESC, id DESC LIMIT 1`），慢日志实测扫描行数放大到
   434 万、单条 27 秒。改写为派生表按窗口聚合 `MAX(id)` 再按主键等值回表，
   一条 SQL 批量取回全部昵称，5.7/8.0 兼容。
3. **/群统计 图片卡渲染必败**：`StatsService` 构造时把 AstrBot ``Context`` 当
   渲染器注入了 `StatsT2IRenderer`（Context 无 ``html_render`` 属性），指令
   报告卡与定时推送 T2I 首轮即报 ``'Context' object has no attribute
   'html_render'`` 双双失败。`StatsService` 补 `star` 构造参数（范式同
   summary/profile 服务），渲染器改注入 Star 实例，`_context` 仍专用于推送。

### Changed

- `_migrate_schema` 索引补建改为数据驱动循环：先查 `INFORMATION_SCHEMA.STATISTICS`
  判定缺失再 `ALTER TABLE ... ADD INDEX`（在线 DDL，不阻塞写入），覆盖
  `idx_group_time` / `idx_sender_time` / `idx_group_sender_time` /
  `idx_message_id` / 新增 `idx_timestamp`
- 新建表 DDL 同步增加 `idx_timestamp (timestamp)`：无群条件的时间窗聚合
  （概览每日趋势、群排行、快照任务）走 timestamp 前缀范围扫描
- `stats/repository` 最新昵称查询改派生表批量取回，消除相关子查询
- `stats/service.StatsService` 新增 `star` 构造参数，T2I 渲染器改注入 Star 实例
  （此前误传 Context 导致渲染必败）；`main.py` 构造处同步传入插件实例
- 版本升至 0.5.2，静态引用 `?v=0.5.2`

## [0.5.1] - 2026-08-04

修复 `all_mode`（全局记录模式）下数据分析模块群列表为空的问题：此前群下拉与
推送开关列表仅以白名单表为基准，而全局模式下白名单为空，导致下拉框只剩
「全部群」、推送设置区显示「暂无白名单群」。现列表源改为「有数据的群
（chat_history 去重）∪ 白名单」，两种记录模式下均可逐群查看统计、逐群开关推送。

### Added

- **GET `/stats/groups` 端点**：返回群下拉列表（白名单 ∪ 有数据的群，附历史消息数）
  与 all_mode 标志
- `stats/repository.get_all_groups_summary()`：全量有数据的群（消息数降序 + 最近活跃时刻）
- `db_config.get_push_flags()`：任意群号批量读取推送开关（无行默认关）

### Changed

- `GET /stats/settings` 的 `push_groups` 改为模式感知：all_mode 下以有数据的群为基准
  （附消息数），并新增 `all_mode` 键；定时推送 `push_report` 目标解析同口径
- 前端群下拉改列全部有数据的群（显示历史消息数）；推送设置区空态文案按模式区分
- 版本升至 0.5.1，静态引用 `?v=0.5.1`

### Fixed

- all_mode 下数据分析群下拉仅「全部群」、推送开关列表为空（列表源未考虑全局模式）

## [0.5.0] - 2026-08-04

新增「数据分析」能力版本：纯 SQL 聚合 + 确定性计算，不调用 LLM、无新增 pip 依赖。
Web 实时统计面板、群内 `/群统计` 指令图片报告卡、定时日报/周报推送与图片小时级快照统计。
离线集成测试 267/267 全绿。

### Added

- **Web「数据分析」tab（新功能）**：存储库分区第 5 个子 tab，全部数据实时 SQL 聚合（不缓存结果、
  不落盘分析产物）——过滤器栏（群选择器支持「全部群」汇总 + 时间范围今日/昨日/近7天/近30天/全部 +
  自定义日期区间，最长 366 天）、统计卡片（总消息数/活跃成员数/图片数/活跃时段峰值）、每日趋势、
  发言人排行（Top N 附图片数，点击成员进入个人 × 群交叉查询）、群排行（仅「全部群」视图）、
  24h·星期发言规律图表（群/选定个人双维度）与推送设置区；前端沿用插件页桥接封装与纯 textContent
  防 XSS 基线，图表经版本锁定 CDN 引入 ECharts，并以纯 CSS 兜底图表互为降级
- **`/群统计` 指令**：别名 `群数据` / `统计`；`[@某人 或 QQ号] [时间范围]` 均可省略、顺序自由——
  时间支持关键词 今日（默认）/ 昨日 / 7天 / 30天 / 全部、单日 `YYYY-MM-DD` 与区间
  `YYYY-MM-DD到YYYY-MM-DD`（分隔符支持 到/至/-/空格，最长 366 天）；@ 与 QQ 号同时出现时 @ 优先；
  所有人可用，仅群环境有效（私聊回用法提示）；每群 30s 冷却（`stats_cooldown` Web 可配，0 = 不限流），
  冷却期内重复触发静默忽略（不回复、不渲染）
- **统计报告卡**：T2I 图片输出，新增自研模板 `stats/templates/stats_report.html`（860px 双主题，
  与总结/人物报告同款设计令牌）——群数据卡片（总消息数/活跃成员数/Top N 发言人附图片数/活跃时段峰值/
  图片总数）与个人卡片（消息数/占群比例/活跃天数/日均 + 24h·星期分布图 + 图片数）；
  渲染复用 `summary_t2i_*` 共享配置，渲染失败自动降级纯文本摘要；无法解析的参数回用法提示不查库
- **定时推送（日报/周报）**：日报全局开关（默认开）+ 推送时间（默认 21:00）；周报可选（默认关，
  推送星期默认周一、时间默认 09:00）；群级开关每群独立（默认关），全局与群级同时开启才推送；
  推送内容为 T2I 图片报告卡（日报 = 当日群数据卡，周报 = 上一整周群数据卡）；经
  `context.send_message(umo)` 主动发送，群 → umo 映射内存缓存在群消息监听与指令路径实时更新，
  无缓存的群跳过并记 warning；逐群串行发送、群间间隔防限频，单群失败仅记日志不阻断整批
- **图片小时级快照统计**：解决 `image_records` 滚动清理（默认保留 3 天）后图片数无法回溯统计的问题——
  每小时整点后定时从 MySQL 聚合上一个完整小时写入 SQLite 快照表（群级全量 + 每小时每群个人 Top K，
  `stats_image_top_k` 默认 20）；任何手动统计（指令 / Web 接口）执行前强制刷新当前未完整小时，
  保证「今日」数据实时；排行与卡片图片数按日期范围对快照求和，个人图片数仅 Top K 内准确（页面注明口径）
- **stats/ 子包（新架构）**：新功能代码全部位于 `stats/` 子包（repository 聚合查询 / models + parser
  参数解析 / service 编排 / snapshot 图片快照 / scheduler 推送与快照调度 / t2i_render + templates
  报告卡渲染），指令在 `main.py` 注册并薄封装异步委托；日志统一 `[Stats]` 前缀
- **配置管理**：新增 8 项配置（stats_top_n / stats_cooldown / stats_image_top_k / push_daily_enabled /
  push_daily_time / push_weekly_enabled / push_weekly_weekday / push_weekly_time）存入 config.db
  **新增 `stats_settings` 表**，群级推送开关存入**新增 `push_group` 表**；由 `ConfigManager` 统一建表、
  播种默认值与类型化读写（范围与 HH:MM 格式校验，非法值拒绝写入）；**不进入 `_conf_schema.json`**，
  仅在 Web 后台「数据分析」tab 管理，改后即时生效无需重启插件
- **Web API**：新增 5 个数据分析端点——`stats/data`（实时统计数据）、`stats/settings`（配置读取）、
  `stats/settings/save`（先全量校验后写入）、`stats/settings/reset`（恢复全部默认）、
  `stats/push/toggle`（群级推送开关切换）

### Changed

- metadata.yaml 版本升至 v0.5.0，desc 更新为「…数据分析支持 Web 实时统计面板与 /群统计 指令报告卡
  （定时日报/周报推送 + 图片小时级快照统计）」
- Dashboard 静态资源缓存参数升级 `?v=0.5.0`
- **MySQL 无 schema 变更**：统计全部基于 `chat_history` / `image_records` 既有索引的聚合查询；
  存量数据与配置完全向后兼容，新配置由 `ConfigManager` 初始化自动播种默认值

### Fixed

- **「全部」时间预设跨度校验豁免**：Web 端「全部」预设（起点 2000-01-01）此前被无条件的 366 天
  跨度校验误杀，与指令侧解析器的「全部」口径不一致；现起点恰为哨兵值时豁免跨度校验并归一显示
  「全部」（自定义区间仍正常校验，超限照常报错）

## [0.4.6] - 2026-08-04

### Changed

- **Web 后台面板放宽**：整面板由 860px 放宽至 1200px（顶部标题/功能分区/子 tab
  与下方内容区同步拉伸），查询表格等内容获得更多横向空间
- 版本号同步：`@register` 与 `metadata.yaml` 统一为 0.4.6
- Dashboard 静态资源缓存参数升级 `?v=0.4.6`

## [0.4.5] - 2026-08-04

基于十角度系统性代码审查（V0.3/V0.4 全量变更）的修复版本：14 项确认问题修复，
不涉及新功能；效率优化与架构重构延后。测试基线 486/486 全绿。

### Fixed

**安全**

- 修复 Web 后台查询表格存储型 XSS：`sender_name`（QQ 昵称）现经 HTML 转义渲染
- 修复历史总结详情在 DOMPurify CDN 加载失败时的 XSS 降级分支：缺库时改为纯文本渲染
  （对齐人物分析页既有正确实现）
- Dashboard `marked` CDN 引用锁定 `@15` 主版本：marked v16+ 调整了包内文件路径，
  原未锁定地址解析到新版本时实际 404（部分环境总结详情 Markdown 渲染已静默失效）

**可靠性（消息不丢失）**

- MySQL 瞬时抖动不再静默丢消息：连接获取在超时窗口内退避重试；实时写入失败自动退回
  启动缓冲区等待补录（仅重放失败部分，不重复入库）
- 初始化重试放弃时明确记录被丢弃的缓冲消息条数（此前无任何日志）
- 初始化超时放宽至 120 秒：存量大数据表的索引迁移（DDL）不再被 10 秒超时中途砍断
  导致存储功能被永久停用
- 数据库迁移（新增列）失败时初始化显式失败并给出授权提示，不再「初始化成功但
  全部读写静默报错」
- 查询执行新增超时兜底（常规 30 秒 / 建表迁移 300 秒双档）；被取消或超时的连接
  直接销毁重建，杜绝协议脏连接复用造成数据错乱
- 连接池取消安全修复：插件卸载/停用不再卡死 30~60 秒；取消路径不再泄漏连接；
  池统计计数不变量逐路径修复

**数据正确性**

- 启动窗口补录的消息现使用真实到达时间（此前取补录时刻，导致故障窗口内的消息
  时间戳晚于后续消息，影响时间窗总结与人物分析统计）
- 素材去重不再误杀：同一用户 1 秒内连发相同短消息（复读）时两条均保留
  （退化去重键仅对缺失消息 ID 的数据生效）

**Web 后台**

- 设置保存失败时正确返回错误提示（此前数据库写入异常仍显示「保存成功」）
- 「恢复全部默认」按钮恢复可用（原生确认框在插件页沙箱中被静默禁用，
  改用自定义确认弹窗）
- 导出图片的下载文件名净化：不再包含路径分隔符与 `.json.png` 双后缀

### Changed

- 版本号同步：`@register` 与 `metadata.yaml` 统一为 0.4.5（v0.4.2 升版时装饰器漏改）
- Dashboard 静态资源缓存参数升级 `?v=0.4.5`

## [0.4.2] - 2026-08-03

### Changed

- **导出图片改为后端 T2I 流水线渲染**：v0.4.1 的前端截图方案（SVG foreignObject 逐元素内联样式）
  在复杂排版下错位失真，本版废弃并删除 `pages/dashboard/capture.js`，改为复用 AstrBot 原生
  `html_render` 文转图流水线——与聊天端发送的图片总结/人物报告**同模板、同主题、同超时配置**，
  排版 100% 一致
- **新增两个导出 Web API 端点**：`GET summary/history/export?group_id=&filename=` 与
  `GET profile/history/export?filename=`——读取已保存的存储 JSON，经 T2I 渲染器按存储数据结构
  直接重建报告（`render_from_dict`：`_to_attr_obj` 递归把存储 dict 转为属性对象适配既有模板数据组装，
  时间戳字符串与 `_fmt_time` 兼容解析），渲染产物字节经 `save_temp_img` 落盘后由 `file_response`
  返回下载；渲染失败（模板缺失 / 两轮渲染全败 / T2I 服务不可用）统一返回 502，前端 toast 明确报错，
  不降级
- **导出交互改版**：前端「🖼️ 导出图片」按钮改为 `bridge.download` 触发浏览器下载，渲染期间按钮
  禁用并提示「正在渲染图片，请稍候…（约 10~60 秒）」，完成/失败均有 toast 反馈

### Added

- 渲染器新增字典数据源入口：`summary/t2i_render.py` 与 `profile/t2i_render.py` 各新增
  `render_from_dict(data) -> bytes`（失败抛 ValueError 由端点转 502），与 `_render_two_rounds_bytes`
  图片字节输出、`_ret_to_bytes` 产物归一化；渲染器实例经 `SummaryService.renderer` /
  `ProfileService.renderer` 公开并注入 `WebAPI`
- 存储 JSON 适配：`_to_attr_obj` 递归 dict→属性对象（嵌套 `target`/`stats` 字段经 `getattr`
  命中），`sources` 键值字典经 `vars()` 还原取回，`_fmt_time` 兼容 `YYYY-MM-DD HH:MM:SS`
  字符串时间戳（导出图不再出现「未知用户 / 活动时间无发现 / 群数 0」）

### Removed

- 删除 `pages/dashboard/capture.js`（v0.4.1 前端截图方案，排版错位已废弃）

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
