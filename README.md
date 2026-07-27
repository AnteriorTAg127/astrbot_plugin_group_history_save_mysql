# astrbot_plugin_group_history_save_mysql

将 QQ 群聊天记录自动保存到 MySQL 数据库，支持按时间、群号、QQ 号索引过滤，提供 Web 管理后台。

## 功能

- 📝 自动监听群消息，保存文本到 MySQL `chat_history` 表
- 🖼️ 图片 URL 单独存储到 `image_records` 表（从 OneBot 原始事件提取 QQ 下发的原始链接，兼容新版 AstrBot）
- 🏷️ 文本与图片记录均保存发送时的昵称
- 🔍 建立联合索引，支持按时间/群号/QQ号高效过滤
- 🗂️ 群白名单管理（支持 ALL 模式）
- 🧹 图片表每天自动清理过期数据（默认保留 3 天）
- 🌐 Web 管理后台：状态监控、群管理、统计、查询面板
- 🗑️ 一键清空所有数据（带随机加减法验证，防误触）
- ⚙️ 管理指令：/history_start、/history_stop、/history_status、/history_clean

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

## Web 管理后台

安装插件后，在 AstrBot WebUI 的插件详情页可以打开管理面板：

- **状态监控**：数据库连接状态、今日/总计消息量
- **群管理**：添加/删除群、开关控制、ALL 模式
- **设置**：图片保留天数配置
- **统计**：最近 7 天每日存储量
- **查询**：按关键词（内容/昵称）、群号、QQ号、时间组合查询聊天记录
- **数据维护**：手动清理过期图片；一键清空所有数据（需通过随机加减法验证，题目由后端生成、一次性有效）

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

## 支持平台

- aiocqhttp (OneBot v11)


## License

MIT
