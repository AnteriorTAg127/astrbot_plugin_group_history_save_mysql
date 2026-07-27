# Changelog

## [0.2.0] - 2026-07-28

### Fixed

- 修复新版 AstrBot 下图片链接失效的问题：预处理阶段会将消息链中 `Image` 组件的 url/file 改写为本地临时路径，
  现改为从 OneBot 原始事件（`raw_message`）的图片消息段提取 QQ 下发的原始链接（`https://gchat.qpic.cn/...`），
  提取不到 http 链接时回退消息链；本地路径一律不入库

### Changed

- `chat_history` 与 `image_records` 两张表的 `group_id`、`sender_id` 列由 `BIGINT` 改为 `VARCHAR(32)`（文本格式）。
  升级时插件自动检测旧表结构并执行 `ALTER TABLE` 迁移，已有数字数据自动转为字符串，无需手动操作
- Web 查询接口的群号/QQ 号参数改为字符串传递

### Added

- `image_records` 表新增 `sender_name` 列，记录图片发送时的昵称（旧表升级时自动 `ADD COLUMN`）
- Web 管理后台「设置 → 数据维护」新增「清空所有数据」按钮：点击后弹出随机加减法验证题，
  答案正确才会清空 `chat_history` 与 `image_records` 全部数据；验证题由后端生成，一次性使用、5 分钟过期，
  防止误操作与穷举

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
