# 测试用例 — astrbot_plugin_group_history_save_mysql（v0.7.0）

## 0. 测试环境与总体策略

- 无真实 MySQL、无运行中的 AstrBot：自动化测试全部用 stub/mock 完成（astrbot.* / aiomysql 经 sys.modules 注入；数据层 _FakeCursor/_FakeConn/_FakePool；public_api 用 _FakeMgr 替身；Web 端点 patch 模块级 request；随机清理概率 mock.patch random）。
- 实现文件：`tests/v0.7.0/test_v070.py`（A/B/C/D 四组 24 例，unittest + @async_test 装饰器，无 conftest）。
- 运行方式（插件根目录）：`PYTHONIOENCODING=utf-8 python "tests/v0.7.0/test_v070.py"` 或 `python -m pytest "tests/v0.7.0/test_v070.py" -v`。

## 1. 测试范围分析

| 文件 | 函数/方法 | 是否需要测试 | 原因 |
|------|----------|-------------|------|
| core/public_api.py（新增） | query_records | ✅ | v0.7.0 核心对外接口：未初始化异常/参数夹取/raise_on_error 透传/reply 反查/成功与失败日志/错误不扩散 |
| core/public_api.py | _infer_caller | ✅ | 调用方标识推断（跳过本插件包帧、data.plugins.x.y.z 截取到包根、失败回退 "unknown"） |
| core/public_api.py | _enrich_query_reply | ✅ | reply_id 批量反查回填 reply_message（空 records 短路、取不到为 None） |
| core/public_api.py | register_mysql_manager / PublicAPIError | ✅ | 注册机制与受控异常类型 |
| core/db_config/query_log.py（新增，SQLite 迁移） | insert_query_log | ✅ | 真实 SQLite 落库（JSON 中文保留、success→1/0、ISO 时间文本、5% 概率清理、失败返回 False） |
| core/db_config/query_log.py | query_query_logs | ✅ | page 归 1/page_size 夹取/caller LIKE 转义/method 精确/时间闭区间/排序/布尔化/异常空结果 |
| core/db_config/query_log.py | cleanup_query_logs | ✅ | 过期删除返回 rowcount、异常 warning+0 |
| core/db_config/query_log.py | 保留天数配置 | ✅ | 默认 30、set 生效、越界夹取 [1,3650]、QUERY_LOG_DEFAULTS 类常量 |
| core/db_mysql/chat_history.py | query_messages(raise_on_error) | ✅ | 新增参数：默认 False 行为不变、True 时 raise |
| core/db_mysql/base.py | _create_tables（query_log 建表） | ✅ | 幂等建表/两索引/utf8mb4（源码断言 + 手动项） |
| core/db_mysql/__init__.py | MySQLManager 组装 | ✅ | QueryLogMixin 三方法出现在门面（MRO 无冲突） |
| core/webapi/query_log.py（新增） | api_query_log_list | ✅ | 参数解析（非法 int 回退/page<1 归 1/page_size 夹取/空串归 None）、委托 query_query_logs |
| core/webapi/base.py + __init__.py | 路由注册 / WebAPI 组装 | ✅ | query_log/list 条目存在、绑定 api_query_log_list |
| main.py | register_mysql_manager 接线 + 版本 | ✅ | __init__ 构造 MySQLManager 后立即注册；@register 版本 0.7.0 |
| metadata.yaml | version | ✅ | v0.7.0 |
| pages/dashboard/index.html / storage.js / app.js / style.css | query-log tab 全套 | ✅（静态）/ ⚠️（浏览器） | 元素存在性/惰性加载接线/textContent 防 XSS/参数摘要截断；视觉效果与交互需浏览器人工验证 |

## 2. 可自动化测试用例（24 例全绿）

### A 组：对外公共 API（core/public_api.py）— 5 例
- A-1 未初始化（未 register）抛 PublicAPIError，且不写日志
- A-2 成功路径：参数夹取（page<1→1、page_size>200→200）、空串归 None、raise_on_error=True 透传、reply 反查回填 reply_message、日志写入（caller/method/params/result_count/success/cost_ms 均正确）
- A-3 显式 caller 优先；缺省自动推断（__main__ 场景非 unknown）
- A-4 查询异常路径：返回空结果不抛底层异常、日志 success=False + error_msg 截断 ≤500
- A-5 日志写失败不影响查询结果（仅 warning）

### B 组：查询日志数据层（core/db_mysql/query_log.py + chat_history.py）— 6 例
- B-1 insert_query_log：SQL 参数绑定、params JSON 序列化（中文保留）、success 布尔→1/0、created_at 缺省取当前时间
- B-2 概率清理：random.random 打桩 <0.05 触发 cleanup（older_than=now-30 天）、≥0.05 不触发
- B-3 query_query_logs：page_size 夹取 [1,200]、caller LIKE 通配符转义、method 精确、时间闭区间、created_at 字符串化/success 布尔化、ORDER BY created_at DESC, id DESC
- B-4 cleanup_query_logs：rowcount 返回、异常返回 0（warning）
- B-5 保留天数常量 QUERY_LOG_RETENTION_DAYS == 30
- B-6 查询异常返回空结果并记 _log_op_error

### C 组：Web API 端点（core/webapi/query_log.py）— 5 例
- C-1 默认参数：page=1、page_size=100
- C-2 非法 int 回退默认（ValueError/TypeError）
- C-2b page<1 归 1、page_size 上限 200
- C-3 空串筛选归 None、时间透传
- C-4 路由注册存在（base.py 含 query_log/list 条目绑定 api_query_log_list）

### D 组：接线 / 版本 / 前端静态 — 8 例
- D-1 MySQLManager 组装含 QueryLogMixin 三方法；WebAPI 组装含 api_query_log_list
- D-2 版本号：main.py @register 与 metadata.yaml 均为 0.7.0
- D-3 前端：index.html 含 data-tab="query-log" 与全部控件 id、静态引用 ?v=0.7.0 且无 ?v=0.6.1；storage.js 含 query_log/list 与 loadQueryLog；app.js TAB_LAZY_LOAD 含 "query-log"
- D-4 query_messages 新增 raise_on_error 参数存在（默认 False）

## 3. 需用户手动测试项（真实环境）

1. **真实 MySQL 建表/查询**：插件启动后 query_log 表自动创建（重复启动幂等）；SHOW CREATE TABLE 核对列定义/索引/utf8mb4；中文 params 无乱码。
2. **真实查询链路**：调用一次 query_records → query_log 表出现记录（caller/params/result_count/success/cost_ms/created_at 正确）；停 MySQL 后调用 → success=0 + error_msg 简短无堆栈。
3. **30 天清理**：手动改某行 created_at 过期，多次对外查询观察 5% 概率清理生效。
4. **AstrBot 环境插件加载**：插件管理重启无报错；register_mysql_manager 执行无异常；MySQL 未连接时对外查询记失败日志不崩溃。
5. **其他插件真实导入调用**：按 PRD 示例 `from data.plugins.astrbot_plugin_group_history_save_mysql.core.public_api import query_records` 调用；验证纯数据返回、reply_message、显式/推断 caller、未初始化 PublicAPIError 可捕获。
6. **Web 面板实际操作**：tab 切换/惰性加载/默认 100 条/筛选（caller 模糊/method 精确/时间补秒 :00/:59）/分页边界/空态/错误提示/快速切页竞态/XSS（构造含 `<img onerror>` 的 caller/params 以纯文本显示）。
7. **回归验证**：Web「查询」tab、总结/统计/人物分析不受影响（query_messages 默认行为未变；Web 自身与内部模块查询不写日志）。

## 4. 风险与待确认项

- **page_size<1 分层规则**：webapi 端点 `<1 → 100`，db 层 `<1 → 1`（max(1, min(...))），均合法，测试按层级区分断言。
- **time_start/time_end 空串未归一**：query_records 与端点对 time 字段只透传不归 None（底层空串不生效，行为等价），params_log 会记录空串——可接受，与 PRD 对 group_id/sender_id/keyword 的归一口径区分。
- **前端无自动化测试框架**：交互/竞态/XSS 需浏览器人工验证（§3.6）。

## 5. 执行结果

执行时间：2026-08-11 17:1x

命令：`PYTHONIOENCODING=utf-8 python "tests/v0.7.0/test_v070.py"`

| 组 | 用例数 | 状态 | 备注 |
|----|-------|------|------|
| A 组 public_api | 7 | ✅ 7/7 | 未初始化/成功/失败（_error 键）/成功无 _error/时间格式非法/日志降级/注销 |
| E 组 count_messages | 10 | ✅ 10/10 | 统计接口全分支（含批量 GROUP BY 单条 SQL 断言） |
| B 组 query_log 数据层 | 6 | ✅ 6/6 | insert/清理概率/查询/cleanup/常量/异常 |
| C 组 webapi 端点 | 5 | ✅ 5/5 | 默认/非法/夹取/筛选/路由 |
| D 组 接线/版本/前端 | 8 | ✅ 8/8 | 组装/版本/前端静态/raise_on_error 契约 |

通过率：40/40（100%）。全部通过，无需 debug 轮次。

> PRD-2 增量（count_messages 统计接口）：新增 E 组 10 用例——对外接口 7 例
> （未初始化/参数缺失/sender_ids 超限/时间非法/成功批量/单人 str/失败 _error/日志降级）
> + 数据层 2 例（三组计数并行 GROUP BY 单条 SQL 断言、仅群维度单条 COUNT）；
> 用例 30→40。

> 查询日志迁移内置 SQLite 后测试重写：B 组改真实 aiosqlite 内存库（含 LIKE 转义语义验证、保留天数夹取、概率清理），A/C 组适配双注册与 settings 端点，D 组改 ConfigManager 组装断言；用例 26→30。

> 外部 review 采纳后追加 A-4b（成功无 _error 键）与 A-4c（时间格式非法抛 PublicAPIError 不写日志），用例 24→26。

补充验证（主 agent 执行）：
- 冒烟测试 7 组全过（临时脚本，已删除）：未初始化抛异常 / 成功路径（夹取+reply+日志）/ 显式 caller / 失败路径 / 日志写失败降级 / _infer_caller / raise_on_error 分支。
- `py_compile` 8 个 Python 文件全过；`ruff check` 全过；`node --check storage.js + app.js` 全过。
