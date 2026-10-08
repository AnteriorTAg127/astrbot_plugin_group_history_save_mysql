# MySQL 配置指南

> 返回 [README](../README.md) ｜ 相关：[数据库表结构](database.md)

## 1. 安装 MySQL

确保你的服务器上已安装 MySQL 5.7+ 或 MariaDB 10.3+。

## 2. 创建数据库

```sql
CREATE DATABASE astrbot_history CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
```

## 3. 创建用户并授权（推荐）

```sql
CREATE USER 'astrbot'@'localhost' IDENTIFIED BY '你的密码';
GRANT ALL PRIVILEGES ON astrbot_history.* TO 'astrbot'@'localhost';
FLUSH PRIVILEGES;
```

## 4. 配置插件

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

## 5. 验证连接

配置完成后，插件会自动创建表结构。你可以在 MySQL 中检查：

```sql
USE astrbot_history;
SHOW TABLES;
-- 应该看到 chat_history 和 image_records 两张表
```
