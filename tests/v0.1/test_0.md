# 测试用例 — astrbot_plugin_group_history_save_mysql

## 测试范围分析

| 文件 | 函数/方法 | 是否需要测试 | 原因 |
|------|----------|-------------|------|
| db_mysql.py | MySQLManager.initialize() | ❌ | 依赖真实 MySQL 连接 |
| db_mysql.py | MySQLManager.insert_chat_message() | ❌ | 依赖真实 MySQL 连接 |
| db_mysql.py | MySQLManager.query_messages() | ❌ | 依赖真实 MySQL 连接 |
| db_config.py | ConfigManager.initialize() | ✅ | 可离线测试（aiosqlite） |
| db_config.py | ConfigManager.add_group() | ✅ | 纯数据库操作 |
| db_config.py | ConfigManager.is_group_enabled() | ✅ | 核心业务逻辑 |
| db_config.py | ConfigManager.get_setting() | ✅ | 纯数据库操作 |
| cleaner.py | ImageCleaner._cleanup_loop() | ❌ | 依赖 asyncio 长时间运行 |
| web_api.py | 所有 API handler | ❌ | 依赖 AstrBot Web 框架 |
| main.py | on_group_message() | ❌ | 依赖 AstrBot 事件对象 |
| main.py | 管理指令 | ❌ | 依赖 AstrBot 事件对象 |

## 可自动化测试用例

### TC-001：ConfigManager 初始化和建表
- 测试对象：db_config.py → ConfigManager.initialize()
- 输入：无
- 预期输出：返回 True，创建 config.db 文件
- 测试方法：直接调用，验证返回值和文件存在

### TC-002：添加群到白名单
- 测试对象：db_config.py → ConfigManager.add_group()
- 输入：group_id = 123456
- 预期输出：返回 True，get_groups() 包含该群
- 测试方法：调用 add_group，再调用 get_groups 验证

### TC-003：群白名单开关切换
- 测试对象：db_config.py → ConfigManager.toggle_group()
- 输入：group_id = 123456
- 预期输出：状态从 True 变为 False
- 测试方法：添加群后切换，验证状态变化

### TC-004：is_group_enabled 白名单模式
- 测试对象：db_config.py → ConfigManager.is_group_enabled()
- 输入：group_id = 123456（在白名单中）
- 预期输出：True
- 测试方法：添加群后检查

### TC-005：is_group_enabled ALL 模式
- 测试对象：db_config.py → ConfigManager.is_group_enabled()
- 输入：all_mode = true, group_id = 999999（不在白名单）
- 预期输出：True
- 测试方法：设置 all_mode 后检查任意群

### TC-006：设置读写
- 测试对象：db_config.py → ConfigManager.set_setting() / get_setting()
- 输入：key = "test_key", value = "test_value"
- 预期输出：get_setting 返回 "test_value"
- 测试方法：设置后读取验证

### TC-007：移除群
- 测试对象：db_config.py → ConfigManager.remove_group()
- 输入：group_id = 123456
- 预期输出：返回 True，get_groups() 不再包含该群
- 测试方法：添加后移除，验证列表

## 需用户手动测试（AstrBot API 无法离线调用）

| 功能 | 触发方式 | 验证要点 |
|------|---------|---------|
| 群消息监听保存 | 在配置的群中发送消息 | 检查 MySQL chat_history 表是否有记录 |
| 图片 URL 保存 | 在群中发送图片 | 检查 MySQL image_records 表 |
| /history_start 指令 | 管理员发送指令 | 验证群被添加到白名单 |
| /history_stop 指令 | 管理员发送指令 | 验证群从白名单移除 |
| /history_status 指令 | 管理员发送指令 | 验证状态信息正确显示 |
| /history_clean 指令 | 管理员发送指令 | 验证图片清理执行 |
| Web 管理后台 | 访问插件 Pages | 验证所有面板功能正常 |
| 图片自动清理 | 等待凌晨 3:00 | 验证过期图片被删除 |
