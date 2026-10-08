# v0.2 测试文档 — astrbot_plugin_group_history_save_mysql

> 测试日期：2026-07-28　|　测试执行：Agent-T　|　测试脚本：`test_v02.py`（同目录）
> 环境：Windows / Python 3.12.6 / pytest 9.0.3；aiomysql 未安装（已 stub），无真实 MySQL。

## 一、测试范围分析

| 文件 | 函数/对象 | v0.2 变更点 | 是否可离线测试 | 原因 |
|------|-----------|-------------|----------------|------|
| main.py | `extract_image_urls(message_obj, message_chain)` | F5 图片原始链接提取 | ✅ 可离线 | 模块级纯函数，仅依赖 `astrbot.api.message_components.Image` 与 `logger`，均可 stub |
| main.py | `GroupHistoryPlugin.on_group_message` | F5/F6/F7 集成 | ❌ 不可离线 | 依赖完整事件对象、MySQL 与本地配置链路，属端到端集成，列入手动测试 |
| web_api.py | `make_challenge()` | F8 随机出题 | ✅ 可离线 | 纯函数，仅用 random/uuid 标准库 |
| web_api.py | `WebAPI.api_purge_challenge` / `api_purge` | F8 清空验证流程 | ✅ 可离线 | 直接实例化 WebAPI（MockContext/MockMySQL），monkeypatch 模块内 `request` 即可驱动 handler |
| web_api.py | `WebAPI.api_query` | F6 查询参数字符串化 | ✅ 可离线 | mock `request.query` 与 `mysql_mgr.query_messages` 即可断言透传参数 |
| web_api.py | 其余 v0.1 接口 | 无变更 | — | 不在 v0.2 测试范围 |
| db_mysql.py | `_create_tables` DDL | F6/F7 建表语句 | ⚠️ 仅静态核对 | 需要真实 MySQL 执行 DDL；离线无法验证 ALTER/索引实际效果 |
| db_mysql.py | `_migrate_schema` | F6/F7 存量迁移 | ⚠️ 仅静态核对 | 依赖 `INFORMATION_SCHEMA` 查询与 ALTER TABLE，需真实 MySQL + v0.1 旧库 |
| db_mysql.py | `insert_image_record` 签名 | F7 昵称参数 | ⚠️ 仅静态核对 | 签名与 INSERT 语句可静态确认；实际写入需真实 MySQL |
| db_mysql.py | `purge_all` | F8 清空 | ⚠️ 仅静态核对 | 签名/返回结构/DELETE 语义已静态确认；行为经 web_api 层 Mock 验证 |
| pages/dashboard（前端） | 清空按钮/弹窗 | F8 前端交互 | ❌ 不可离线 | 需真实 Web 面板 + 后端联调，列入手动测试 |

### db_mysql.py 静态核对结论（逐行审阅，不做离线执行）

| 核对项 | 位置 | 结论 |
|--------|------|------|
| chat_history DDL：group_id/sender_id VARCHAR(32) NOT NULL | db_mysql.py L372-386 | ✅ 符合 PRD F6 |
| image_records DDL：group_id/sender_id VARCHAR(32) NOT NULL + sender_name VARCHAR(128) NOT NULL DEFAULT '' | db_mysql.py L388-399 | ✅ 符合 PRD F6/F7 |
| 迁移：逐表逐列查 INFORMATION_SCHEMA.COLUMNS(DATA_TYPE)，非 varchar/char/text 时 MODIFY VARCHAR(32) NOT NULL | db_mysql.py L417-441 | ✅ 4 列全覆盖，每列独立 try/except |
| 迁移：image_records 缺 sender_name 时 ADD COLUMN ... DEFAULT '' | db_mysql.py L444-461 | ✅ 符合 PRD F7 |
| 迁移整体失败不阻断启动（外层 try/except 仅记 error） | db_mysql.py L462-463 | ✅ |
| `insert_image_record(group_id, sender_id, image_url, sender_name="")`，INSERT 含 sender_name | db_mysql.py L509-535 | ✅ 形参均为 str，与 main.py 调用一致 |
| `purge_all()`：DELETE 取删除条数 + 每表 `ALTER TABLE ... AUTO_INCREMENT=1` 复位自增 ID（独立 try/except） | db_mysql.py `purge_all` / `_reset_auto_increment` | ✅ 修复现场 F-1：清空后 ID 从头开始 |
| main.py 字段字符串化：`str(event.get_group_id())`/`str(event.get_sender_id())`；白名单仍用 int 比较 | main.py L135-145 | ✅ 符合 PRD F6（本地白名单保持 INTEGER） |
| main.py 图片入库传 `sender_name=event.get_sender_name()` | main.py L180-186 | ✅ 符合 PRD F7 |
| web_api.py api_query：group_id/sender_id 无 type=int，空串 `or None`；keyword strip 后透传/空白转 None | web_api.py `api_query` | ✅ 已由 TC-Q1/TC-Q1b/TC-Q2 离线验证 |
| db_mysql.py query_messages：keyword → `(content LIKE %s OR sender_name LIKE %s)`，转义 `%` `_` `\` | db_mysql.py `query_messages` | ⚠️ 静态核对（LIKE 语义需真实 MySQL 验证，见 MT-6） |

## 二、可自动化用例清单

| 编号 | 测试函数 | 覆盖 | 说明 |
|------|----------|------|------|
| TC-E1 | `test_tc_e1_raw_message_url` | F5 | 标准 OneBot 事件，image 段 data.url → 返回该链接 |
| TC-E2 | `test_tc_e2_file_fallback_lagrange` | F5 | 无 url、data.file 为 http（Lagrange 兼容）→ 返回 file |
| TC-E3 | `test_tc_e3_local_paths_rejected` | F5 | raw 段与消息链均为本地路径 → 空列表 |
| TC-E4 | `test_tc_e4_fallback_to_chain` | F5 | raw 为 None/非 dict/无 message/空数组/无 image 段 → 回退消息链 http 链接（本地路径不收集） |
| TC-E5 | `test_tc_e5_malformed_segments` | F5 | 畸形段（None/42/str/缺 data/data 非 dict/url 非 str）→ 不抛异常，正常段仍提取 |
| TC-E6 | `test_tc_e6_multiple_images_in_order` | F5 | 多图片段按序返回 |
| TC-E7 | `test_tc_e7_no_raw_message_attr` | F5 | message_obj 无 raw_message 属性（object()）→ 不抛异常 |
| TC-C1 | `test_tc_c1_format` | F8 | 抽样 200 次：question 匹配 `^\d{1,2} [+\-] \d{1,2} = \?$`；challenge_id 为 32 位 hex |
| TC-C2 | `test_tc_c2_answer_consistency` | F8 | 解析 a/op/b 验证 answer 一致且 >= 0（200 次） |
| TC-P1 | `test_tc_p1_get_challenge` | F8 | GET challenge → 返回 challenge_id/question，存入 _purge_challenges（TTL≈300s），两条 purge 路由已注册 |
| TC-P2 | `test_tc_p2_correct_answer` | F8 | 正确答案 → success=True、deleted_messages=3、deleted_images=2，purge_all 调用 1 次，challenge 被删除 |
| TC-P3 | `test_tc_p3_replay_same_challenge` | F8 | 复用 challenge_id → 400「验证已失效」，purge_all 不再调用 |
| TC-P4 | `test_tc_p4_wrong_answer_consumes` | F8 | 错误答案 → 400「答案不正确」，purge_all 未调用；challenge 已消费（再提交=失效） |
| TC-P5 | `test_tc_p5_non_numeric_answer` | F8 | answer="abc" → 400，不崩溃 |
| TC-P6 | `test_tc_p6_expired_challenge` | F8 | 手工置过期 → 正确答案也 400「验证已过期」 |
| TC-P7 | `test_tc_p7_cleanup_on_generation` | F8 | 生成新 challenge 时清理过期项，未过期项保留 |
| TC-Q1 | `test_tc_q1_string_params` | F6 | group_id 透传 "123"；sender_id 空串→None；page/page_size type=int 转换 |
| TC-Q1b | `test_tc_q1b_both_empty` | F6 | group_id/sender_id 均空串 → 均 None；分页默认值 1/50（补充用例） |
| TC-Q2 | `test_tc_q2_keyword` | 搜索 | keyword 非空 strip 后透传；纯空白 → None |

## 三、需用户手动测试清单（无法离线覆盖）

| 编号 | 场景 | 验收标准 |
|------|------|----------|
| MT-1 | 真实环境群内发图片 | `image_records.image_url` 为 `https://gchat.qpic.cn/` 开头的原始链接（非本地临时路径） |
| MT-2 | v0.1 旧库升级 | 启动后 chat_history/image_records 的 group_id/sender_id 自动变为 varchar(32)；image_records 自动出现 sender_name 列；旧数据完整（数字转字符串不丢） |
| MT-3 | 全新安装建库 | DDL 直接为 VARCHAR(32) + image_records.sender_name（已静态核对，建议实机确认） |
| MT-4 | Web 面板「清空所有数据」按钮 | 弹窗出题 → 答错提示并换题 → 答对后两表清空、概览刷新、toast 显示删除条数；**清空后再发一条消息，新记录 id 从 1 开始**（不再延续旧最大值） |
| MT-5 | 清空后执行 `/history_status` | 统计（今日/总消息、今日/总图片）全部归零 |
| MT-6 | 查询面板「关键词」搜索 | 输入消息内容片段或昵称能命中对应记录；含 `%` `_` 等字符不报错、按字面匹配；群号/QQ 号框输入完整号码可精确过滤 |

## 四、执行结果

- 执行命令：`cd F:/astrbot/AstrBot/data/plugins/astrbot_plugin_group_history_save_mysql && python -m pytest "tests/v0.2/test_v02.py" -v -p no:cacheprovider`
- 通过率：**19/19（100%）**
- 主 agent 复验：2026-07-28，移除 conftest.py 后以纯 `@async_test` 装饰器驱动；本轮新增关键词/复位修复后再跑 19 passed（ruff check/format 均通过）

| 编号 | 测试函数 | 状态 |
|------|----------|------|
| TC-E1 | TestExtractImageUrls::test_tc_e1_raw_message_url | ✅ PASSED |
| TC-E2 | TestExtractImageUrls::test_tc_e2_file_fallback_lagrange | ✅ PASSED |
| TC-E3 | TestExtractImageUrls::test_tc_e3_local_paths_rejected | ✅ PASSED |
| TC-E4 | TestExtractImageUrls::test_tc_e4_fallback_to_chain | ✅ PASSED |
| TC-E5 | TestExtractImageUrls::test_tc_e5_malformed_segments | ✅ PASSED |
| TC-E6 | TestExtractImageUrls::test_tc_e6_multiple_images_in_order | ✅ PASSED |
| TC-E7 | TestExtractImageUrls::test_tc_e7_no_raw_message_attr | ✅ PASSED |
| TC-C1 | TestMakeChallenge::test_tc_c1_format | ✅ PASSED |
| TC-C2 | TestMakeChallenge::test_tc_c2_answer_consistency | ✅ PASSED |
| TC-P1 | TestPurgeFlow::test_tc_p1_get_challenge | ✅ PASSED |
| TC-P2 | TestPurgeFlow::test_tc_p2_correct_answer | ✅ PASSED |
| TC-P3 | TestPurgeFlow::test_tc_p3_replay_same_challenge | ✅ PASSED |
| TC-P4 | TestPurgeFlow::test_tc_p4_wrong_answer_consumes | ✅ PASSED |
| TC-P5 | TestPurgeFlow::test_tc_p5_non_numeric_answer | ✅ PASSED |
| TC-P6 | TestPurgeFlow::test_tc_p6_expired_challenge | ✅ PASSED |
| TC-P7 | TestPurgeFlow::test_tc_p7_cleanup_on_generation | ✅ PASSED |
| TC-Q1 | TestApiQuery::test_tc_q1_string_params | ✅ PASSED |
| TC-Q1b | TestApiQuery::test_tc_q1b_both_empty | ✅ PASSED |
| TC-Q2 | TestApiQuery::test_tc_q2_keyword | ✅ PASSED |

### 测试基础设施说明

- stub 注入（astrbot.*、aiomysql）在 `test_v02.py` 头部、导入被测包之前完成；aiomysql stub 提供 `Connection`/`DictCursor` 属性。
- 异步用例驱动：本环境装有 pytest-asyncio 1.4.0 且为 strict 模式（未加 `@pytest.mark.asyncio` 的协程会被拒绝），
  为不依赖其标记/配置，`test_v02.py` 内置 `@async_test` 装饰器，用 `asyncio.run` 将异步测试包成同步函数执行。
  初版曾以 `conftest.py` 的 `pytest_pyfunc_call` hook 驱动，与装饰器重复且依赖 pluggy 私有 API，已删除，最终方案仅保留装饰器。
- 未发现插件代码缺陷；全部失败排查均为测试基础设施问题，与被测代码无关。
