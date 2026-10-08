# 测试用例 — astrbot_plugin_group_history_save_mysql（v0.7.1）

## 测试范围分析

本版改动：数据层 `ChatHistoryMixin._build_chat_history_where`（抽取自 query_messages）+ `export_messages`（新增）；Web 端点 `QueryMixin.api_query_export`（新增）+ `_records_to_csv` / `_save_export_temp`（新增）；前端导出按钮。

| 文件 | 函数/方法 | 是否需要测试 | 原因 |
|------|----------|-------------|------|
| core/db_mysql/chat_history.py | `_build_chat_history_where` | ✅ | WHERE 构建核心逻辑，全条件组合 + LIKE 转义 |
| core/db_mysql/chat_history.py | `export_messages` | ✅ | LIMIT 上限、timestamp 字符串化、异常兜底 |
| core/db_mysql/chat_history.py | `get_messages_by_ids` | ✅ | IN 分块（≤500）、结果合并、空入参短路 |
| core/db_mysql/chat_history.py | `query_messages` | ✅ | WHERE 重构后行为回归（COUNT + LIMIT/OFFSET） |
| core/webapi/query.py | `_records_to_csv` | ✅ | BOM/列序/None/reply 扁平/CSV 转义 |
| core/webapi/query.py | `api_query_export` | ✅ | format/limit 校验、CSV/JSON 生成、file_response |
| core/webapi/base.py | 路由注册 | ✅ | 静态断言 query/export 路由 + WebAPI 组装 |
| metadata.yaml / index.html | 版本与接线 | ✅ | 静态断言 version + 导出按钮 + ?v |
| pages/dashboard/storage.js | `collectQueryParams`/`doExport` | ✅（语法） | `node --check` 语法校验（逻辑随 doQuery 同口径） |

## 可自动化测试用例

| 编号 | 对象 | 输入 | 预期 | 方法 |
|------|------|------|------|------|
| A-1 | `_build_chat_history_where` | 全条件 + keyword=`a%b_c\d` | WHERE 五段 AND、LIKE 参数转义为 `%a\%b\_c\\d%` | 真实静态方法 |
| A-2 | `_build_chat_history_where` | 全 None | `("1=1", [])` | 真实静态方法 |
| A-3 | `export_messages` | group_id + keyword + limit=10 | 单次 SQL 含 LIMIT 无 OFFSET、参数序正确、timestamp→str | fake pool/cursor |
| A-4 | `export_messages` | limit=0 | 返回 []，不触碰 pool | fake pool |
| A-5 | `export_messages` | cursor.execute 抛异常 | 返回 []，记 `_log_op_error` | fake pool |
| A-6 | `query_messages` | page=2/page_size=25 | 两次 SQL（COUNT + LIMIT/OFFSET），参数不变 | fake pool |
| A-7 | `get_messages_by_ids` | 1200 个 id | 3 次 execute（≤500/块）、结果合并、timestamp 字符串化 | fake pool + 顺序 cursor |
| A-8 | `get_messages_by_ids` | [] / ["", None] | 返回 []，不触碰 pool | fake pool |
| B-1 | `_records_to_csv` | 含逗号/引号/换行 + None + reply | BOM 前缀、12 列序、转义还原、None 写 "None"、reply 扁平 | 纯函数 + csv.reader 回读 |
| B-1b | `_records_to_csv` | reply_message=None、空串字段 | reply 三列写 "None"、空串仍写空 | 纯函数 |
| B-2 | `api_query_export` | format=xlsx | 400 且 error 含 csv | mock request |
| B-3 | `api_query_export` | format=csv + limit=10 | file_response 文件名 `.csv`、content_type、落盘文本含 BOM | mock `_save_export_temp` |
| B-4 | `api_query_export` | format=json + limit=99999999 | content_type json、limit 夹取到 500000、JSON 含 reply_message:None | mock `_save_export_temp` |
| B-5 | `api_query_export` | 空串 group/sender/keyword | 归 None、默认 limit=100000 | mock `_save_export_temp` |
| B-6 | `api_query_export_download` | token 有效 | 消费令牌返回文件流（filename/content_type 正确） | mock handle_file |
| B-6b | `api_query_export_download` | JSON 文件 | content_type application/json | mock handle_file |
| B-6c | `api_query_export_download` | 缺 token | 400 | - |
| B-6d | `api_query_export_download` | 令牌无效/过期 | 404 | mock handle_file 抛 KeyError |
| C-1 | base.py / WebAPI | 静态读源码 | 含 query/export 路由与 api_query_export 方法 | 文本断言 |
| C-2 | metadata.yaml | 静态读 | `version: v0.7.1` | 文本断言 |
| C-3 | index.html | 静态读 | 含 exportCsvBtn/exportJsonBtn + `?v=0.7.1` 且无 `?v=0.7.0` | 文本断言 |
| C-4 | 常量 | - | EXPORT_LIMIT_DEFAULT=100000、MAX=500000 | 直接断言 |

## 需用户手动测试

离线测试已覆盖后端数据层与端点逻辑、前端语法与静态接线；以下需在运行中的 AstrBot 内真机验证：

| 功能 | 触发方式 | 验证要点 |
|------|---------|---------|
| 导出下载链路 | Web 后台「查询」页填筛选 → 点「导出 CSV / 导出 JSON」 | 浏览器触发下载、文件名含时间戳、CSV 中文/Excel 打开正常、JSON 含 reply_message |
| 大数据量 | 筛选出 >1 页的数据再导出 | 导出条数 = 全部匹配（≤10 万上限），不受当前分页影响 |
| 回复关联 | 导出含回复消息的记录 | CSV 后三列 / JSON reply_message 正确回填 |

## 执行结果

执行时间：2026-08-13

命令：`PYTHONIOENCODING=utf-8 python -m pytest "tests/v0.7.1/test_v071.py" -v`

| 用例编号 | 状态 | 备注 |
|---------|------|------|
| A-1 | ✅ | 全条件 + LIKE 转义 |
| A-2 | ✅ | 无条件 1=1 |
| A-3 | ✅ | WHERE+LIMIT、timestamp 字符串化 |
| A-4 | ✅ | limit<=0 短路 |
| A-5 | ✅ | 异常兜底 |
| A-6 | ✅ | query_messages 回归 |
| A-7 | ✅ | get_messages_by_ids 分块 |
| A-8 | ✅ | 空入参短路 |
| B-1 | ✅ | CSV 形状与转义 |
| B-1b | ✅ | reply=None → 三列 "None"、空串仍空 |
| B-2 | ✅ | format 非法 400 |
| B-3 | ✅ | CSV 成功路径 |
| B-4 | ✅ | JSON + limit 夹取 |
| B-5 | ✅ | 默认值与空串归 None |
| B-6 | ✅ | 令牌消费返回文件流 |
| B-6b | ✅ | JSON content_type |
| B-6c | ✅ | 缺 token 400 |
| B-6d | ✅ | 无效令牌 404 |
| C-1 | ✅ | 路由与组装 |
| C-2 | ✅ | metadata v0.7.1 |
| C-3 | ✅ | 前端接线 + ?v |
| C-4 | ✅ | 常量 |

通过率：22/22（100%）
