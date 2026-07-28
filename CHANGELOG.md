# Changelog

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
