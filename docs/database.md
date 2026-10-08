# 数据库表结构

> 返回 [README](../README.md) ｜ 相关：[MySQL 配置](mysql-setup.md)

> 群号与 QQ 号均以文本（`VARCHAR(32)`）存储。从 v0.1 升级时插件会自动检测旧表并执行
> `ALTER TABLE` 迁移（数字自动转字符串、`image_records` 自动补 `sender_name` 列），无需手动操作。

## config.db 新增表（v0.7.0 新增）：query_log（查询日志，内置 SQLite）

> 查询日志存于本地 SQLite（`data/plugin_data/astrbot_plugin_group_history_save_mysql/config.db`），
> 与 MySQL 聊天记录存储解耦——MySQL 不可用时审计仍可用。

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER | 主键（自增） |
| caller | TEXT | 调用方标识（显式传入或自动推断） |
| method | TEXT | 调用的方法名（如 query_records） |
| params | TEXT | 查询参数 JSON（不含消息内容全文与密钥） |
| result_count | INTEGER | 返回记录条数 |
| success | INTEGER | 1=成功 0=失败 |
| error_msg | TEXT | 失败原因（成功为 NULL，无堆栈） |
| cost_ms | INTEGER | 查询耗时（毫秒） |
| created_at | TEXT | 调用时间（ISO 文本 YYYY-MM-DD HH:MM:SS） |

> 仅记录**经对外接口（`core/public_api.py`）**发起的查询；Web 后台自身与内部模块（总结/统计/补库）的查询不记录。
> 保留天数经 `query_log_settings` 表配置（Web 可调，默认 30 天、范围 1–3650），写入时按 5% 概率顺带清理过期行。
> 早期 v0.7.0 曾建于 MySQL 的 `query_log` 表已废弃，旧库残留表无害，可手动 DROP。

## chat_history（聊天记录）

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

## image_records（图片记录）

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

## config.db 新增表（v0.3 新增）

| 表名 | 说明 |
|------|------|
| summary_settings | 总结功能配置（key TEXT PRIMARY KEY, value TEXT），24 项总结配置均存于此，仅 dashboard「总结设置」tab 修改 |
| group_ignore_senders | 每群忽略发送者（group_id, sender_id, created_at，group_id + sender_id 联合唯一约束） |

## config.db 新增表（v0.4.0 新增）

| 表名 | 说明 |
|------|------|
| profile_settings | 人物分析配置（key TEXT PRIMARY KEY, value TEXT），19 项分析配置均存于此，仅 dashboard「人物分析 → 分析设置」tab 修改 |

> 分析结果不入库，以 JSON 文件持久化：`data/plugin_data/astrbot_plugin_group_history_save_mysql/profiles/<scope目录>/<文件名>.json`，
> 按范围分子目录（`group_<群号>` / `all`），文件名含生成时间与目标 QQ 号，保留天数 `profile_keep_days`（默认 30 天），
> 定时任务每日清理过期文件。

## config.db 新增表（v0.5.0 新增）

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

## config.db 新增表（v0.5.5 新增）

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
