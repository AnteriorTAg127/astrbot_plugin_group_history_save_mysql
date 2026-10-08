# v0.4.0 离线测试清单（test_0）

配套离线套件：`tests/v0.4.0/` 下 10 个文件（pytest 合计 **284 项全绿**）。

覆盖 v0.4.0「人物分析」全部 13 个模块（A–M）的可离线验证行为：

| 套件文件 | 模块 | 用例数 |
|----------|------|--------|
| `test_profile_config_smoke.py` | A 配置层（db_config.py） | 37 断言（asyncio.run 冒烟脚本，0 pytest） |
| `test_db_mysql_v040.py` | B 存储增强（db_mysql.py） | 9 |
| `test_models_capture.py` | C 模型 + 捕获（profile/models.py, capture.py） | 34 |
| `test_profile_fetcher.py` | D 数据获取（profile/fetcher.py） | 24 |
| `test_profile_stats.py` | E 确定性统计（profile/stats.py） | 16 |
| `test_profile_analyzer.py` | F AI 分析（profile/analyzer.py） | 36 |
| `test_profile_formatter.py` | H 输出格式化（profile/formatter.py） | 37 |
| `test_profile_storage.py` | I 持久化 + 调度（profile/storage.py, scheduler.py） | 24 |
| `test_profile_service.py` | J 编排层（profile/service.py） | 43 |
| `test_profile_webapi.py` | K Web API（web_api.py） | 39 |
| `test_profile_main.py` | M 主入口集成（main.py） | 22 |
| `test_profile_config_smoke.py` | G 渲染测试依赖真实 T2I 服务，离线仅脚本模拟 | — |

> 运行方式（插件根目录）：
> `python -m pytest "tests/v0.4.0" -q` → 284 passed
> 冒烟脚本：`python "tests/v0.4.0/test_profile_config_smoke.py"` → 37 项断言 PASS
> 范式：无 conftest；astrbot.* / aiomysql 全量 sys.modules stub 且先于被测包导入；
> 多文件合跑前剔除插件包缓存（v0.3.1 教训），故 10 文件可同进程合跑。

---

## 1. 测试范围分析

| 文件/函数 | 是否需要测试 | 原因 |
|-----------|------------|------|
| main.py 全部（M 主入口） | ✅ 必须 | v0.4.0 核心集成：/人物分析 指令委托、capture 透传、生命周期接线、WebAPI 注入（test_profile_main.py 22 例） |
| web_api.py profile 9 端点（K） | ✅ 必须 | 新增端点契约 + 删除端点 DELETE+POST 双方法修复回归（test_profile_webapi.py 39 例） |
| db_config.py PROFILE_DEFAULTS/TYPES + profile_settings（A） | ✅ 必须 | 19 项配置声明与播种一致性（冒烟 37 断言） |
| db_mysql.py 迁移 + 新列 + get_messages_by_ids（B） | ✅ 必须 | v0.4.0 存储增强：幂等 ALTER / at_list/reply_id 透传 / IN 全参数化（test_db_mysql_v040.py 9 例） |
| profile/models.py（C） | ✅ 必须 | 5 个 dataclass 默认值契约 + mysql_row_to_profile_message 归一化（34 例） |
| profile/capture.py（C） | ✅ 必须 | extract_at_targets / extract_reply_id 防御式解析（34 例中 19 例） |
| profile/fetcher.py（D） | ✅ 必须 | 单群/全局拉取 + 关系上下文识别 + 降级（24 例） |
| profile/stats.py（E） | ✅ 必须 | 确定性统计：分布/峰值/比率/活跃天数（16 例） |
| profile/analyzer.py（F） | ✅ 必须 | provider 降级链 + 截断 + 维度开关 + 绝不抛异常（36 例） |
| profile/t2i_render.py + 模板（G） | ⚠️ 部分离线 | 离线仅能 jinja2 试渲染/剧本化模拟；真实截图渲染需真机（见 M-1~M-6） |
| profile/formatter.py（H） | ✅ 必须 | forward/image/text 三模式 + image 三级降级 + 绝不抛异常（37 例） |
| profile/storage.py + scheduler.py（I） | ✅ 必须 | save/list/read/delete/cleanup + 路径穿越拦截 + 清理调度（24 例） |
| profile/service.py（J） | ✅ 必须 | 校验链/限流/目标解析/两入口编排/反馈/生命周期（43 例） |
| profile/__init__.py（J） | ✅ 覆盖 | PEP 562 惰性导出（test_profile_service.py 合盖） |
| pages/dashboard/*（L 前端） | ⚠️ 需手动 | node --check 由 Agent-L 执行；浏览器端交互需真机（见 M-7~M-10） |

## 2. 需用户手动测试（离线无法覆盖）

| 编号 | 场景 | 操作 | 预期 |
|------|------|------|------|
| M-1 | /人物分析 指令端到端（forward） | 群内 `/人物分析 @某人` 或 `/人物分析 QQ号`（别名 `/人物画像`、`/分析TA`） | 收到转发消息或文本画像，含统计（时段分布/发言排行）+ 性格/爱好/关系板块；管理员限制生效 |
| M-2 | /人物分析 图片输出 | 设置 `profile_output_mode=image` 后触发 | 收到 860px 双主题人物报告图片（小时/星期图表、互动排行、免责声明页脚） |
| M-3 | Web「发起分析」跨群 | Dashboard 人物分析 tab → QQ+范围（当前群/全部群）→ 触发 | 结果渲染含活动图表；跨群分析仅 Web 可触发（指令只做当前群） |
| M-4 | 关系上下文 | 群内有人 @ 目标或回复目标后触发分析 | 关系板块反映互动对象与互动次数（at_list/reply_id 入库生效） |
| M-5 | 历史删除（DELETE+POST 双方法） | Web 历史 tab 删除一条；用 curl 分别以 `-X DELETE` 与 `-X POST` 调 `/profile/history?filename=...` | 两种方法均删除成功返回 `{"deleted": true}`；404/400/503 分支正确 |
| M-6 | 迁移幂等（真实 MySQL） | 存量 v0.3 库启动插件（或手工连库执行 `_migrate_schema`） | chat_history 自动新增 at_list/reply_id 列；重复执行不再报错；旧行查询正常 |
| M-7 | 配置保存闭环 | Dashboard 人物分析设置 tab 改 19 项保存 → 刷新 | 保存成功且回显一致；恢复默认回到默认值 |
| M-8 | 生命周期 | 插件重载/停止 | 日志无 Traceback；清理调度器停止；MySQL 连接池关闭 |
| M-9 | 前端分区切换 | 顶部分区控件切换「存储库 / 消息总结 / 人物分析」 | 高亮联动、惰性加载、控制台无报错 |
| M-10 | 触发反馈 | 指令触发分析时默认 reaction 贴 👍 | 贴表情成功或自动降级文字提示 |

## 3. 用例明细 — Module M 主入口（test_profile_main.py，22 例）

| 编号 | 用例 | 输入 | 预期 | 方法 |
|------|------|------|------|------|
| M1-01 | `RegisterAndInitTest::test_register_metadata_v040` | import main 模块 | `@register` 版本 == "0.4.0" | 装饰器捕获断言 |
| M1-02 | `RegisterAndInitTest::test_constructor_wires_injections` | 构造 GroupHistoryPlugin | WebAPI 收到 profile_service/profile_storage/summary_storage 注入且与属性同引用 | 引用断言 |
| M1-03 | `RegisterAndInitTest::test_mysql_mgr_constructed_with_config_defaults` | 构造插件 | mysql_mgr 以配置默认 host/port/database 构造 | kwargs 断言 |
| M1-04 | `ExtractImageUrlsTest::test_prefers_raw_onebot_segments` | raw.message 含 image 段 url/file | 优先取 OneBot 原始段 URL（非本地） | 直断 |
| M1-05 | `ExtractImageUrlsTest::test_skips_non_http_url_in_raw` | raw 段 url 为本地路径 | 不收集 | 直断 |
| M1-06 | `ExtractImageUrlsTest::test_falls_back_to_chain_components` | 无 raw，链上 Image 组件 | 回退链组件；本地 file 不收集 | 直断 |
| M1-07 | `ExtractImageUrlsTest::test_empty_inputs` | None/空链/空 raw | 均返回 [] | 直断 |
| M1-08 | `ExtractImageUrlsTest::test_ignore_other_segment_types` | 仅 text/face 段 | [] | 直断 |
| M1-09 | `OnGroupMessageTest::test_text_message_persists_with_at_and_reply` | 文本+At+Reply 链 | insert_chat_message 收到 at_list="777"、reply_id="r1"（capture 透传） | 调用记录断言 |
| M1-10 | `OnGroupMessageTest::test_mixed_message_writes_both_tables` | mixed 消息（文本+图） | chat_history 与 image_records 双写；message_type="mixed" | 双表调用断言 |
| M1-11 | `OnGroupMessageTest::test_skips_when_no_text_no_image` | 仅 At/Reply 无内容 | 不入库 | 空调用断言 |
| M1-12 | `OnGroupMessageTest::test_group_not_in_whitelist_skipped` | 群未在白名单 | 跳过 | 空调用断言 |
| M1-13 | `OnGroupMessageTest::test_buffers_during_init_window_with_at_reply` | 初始化窗口内消息 | 进入 _pending_records 且含 at_list/reply_id | 缓冲断言 |
| M1-14 | `OnGroupMessageTest::test_drops_when_db_gave_up` | 初始化窗口超时 | 消息丢弃不崩溃 | 缓冲为空断言 |
| M1-15 | `PersistAndFlushTest::test_persist_message_passes_through_at_reply` | _persist_message 直调 | at_list/reply_id 原样透传 | 调用记录断言 |
| M1-16 | `PersistAndFlushTest::test_flush_pending_records` | 缓冲 2 条含 at/reply | 全部入库透传且清空缓冲 | 记录+清空断言 |
| M1-17 | `CommandAndLifecycleTest::test_profile_command_registered_with_aliases` | main 模块加载 | profile_analyze 为 async handler（别名由 filter.command 注册） | iscoroutinefunction 断言 |
| M1-18 | `CommandAndLifecycleTest::test_profile_command_delegates` | 调 plugin.profile_analyze(event, "12345") | 原样委托 profile_service.handle_command | 委托记录断言 |
| M1-19 | `CommandAndLifecycleTest::test_initialize_starts_background_init` | initialize() | _init_task 创建；后台任务完成后 profile/summary service 均 started（M 生命周期） | wait_for 等待 + started 断言 |
| M1-20 | `CommandAndLifecycleTest::test_terminate_lifo_stops_services` | terminate() | summary_service 先 stop → profile_service stop → mysql_mgr.close（LIFO） | 顺序断言 |
| M1-21 | `CaptureIntegrationTest::test_capture_functions_used_by_main` | 真实 capture 模块 + At/AtAll/Reply 链 | extract_at_targets 剔除 AtAll；extract_reply_id 取回复目标（capture 与 main 契约一致） | 直断 |
| M1-22 | `ProfileDeleteRouteTest::test_delete_route_methods_are_delete_and_post` | 读真实 web_api.py 源码 | 删除端点注册 methods 含 DELETE+POST；成功分支含 `json_response({"deleted": True})`（本次修复回归守卫） | 源码级正则断言 |

## 4. 用例明细 — Module B 存储增强（test_db_mysql_v040.py，9 例）

| 编号 | 用例 | 输入 | 预期 | 方法 |
|------|------|------|------|------|
| B1-01 | `MigrationIdempotentTest::test_adds_missing_columns` | 存量库无 at_list/reply_id（fake INFORMATION_SCHEMA） | chat_history 恰好 ADD 两列 | ALTER 捕获断言 |
| B1-02 | `MigrationIdempotentTest::test_skips_when_columns_exist` | 已迁移库 | 不再执行任何 ADD COLUMN（幂等可重入） | ALTER 捕获断言 |
| B1-03 | `MigrationIdempotentTest::test_partial_migration_one_missing` | 只缺 reply_id | 仅 ADD reply_id | ALTER 捕获断言 |
| B2-01 | `InsertChatMessageTest::test_insert_with_at_reply` | 带 at_list="777,888"/reply_id="r1" | SQL 列与参数均含两新值（尾部两参数） | SQL+参数断言 |
| B2-02 | `InsertChatMessageTest::test_insert_without_at_reply_defaults` | 旧调用不带新参 | 两列默认空串，SQL 仍含列（向后兼容） | SQL+参数断言 |
| B3-01 | `QueryMessagesColumnsTest::test_select_contains_new_columns` | query_messages() | SELECT 列表含 at_list/reply_id；records 返回注入行；LIMIT/OFFSET 占位符 | SQL 断言 |
| B4-01 | `GetMessagesByIdsTest::test_empty_input_returns_empty_no_sql` | [] / ["", None, ""] | 返回 [] 且不触达 DB | 直断 + SQL 空断言 |
| B4-02 | `GetMessagesByIdsTest::test_in_placeholders_parameterized` | ["r1","r2",""] | 空串过滤后 IN (%s,%s) 全参数化；timestamp 字符串化 | SQL+参数断言 |
| B4-03 | `GetMessagesByIdsTest::test_exception_degrades_empty` | acquire 抛异常 | 返回 [] 不抛（降级契约） | 直断 |

## 5. 用例明细 — Module K Web API（test_profile_webapi.py，39 例）

| 编号 | 用例 | 输入 | 预期 | 方法 |
|------|------|------|------|------|
| K-01 | `RouteRegistrationTest::test_all_nine_profile_routes_registered` | FakeContext 注册表 | 9 端点方法集合齐全；**history 删除 = (DELETE, POST)**（本次修复后断言已同步） | 注册集合断言 |
| K-02~10 | SettingsEndpointsTest 9 例 | GET/POST/reset 各类输入 | settings 读返回 3 键（settings/defaults/types）；保存全量校验（未知键/非法 bool/int/非 dict 拒绝）；写失败 500 | 响应断言 |
| K-11~12 | providers 2 例 | 正常/空白 id | providers 列表；失败降级 [] | 响应断言 |
| K-13~14 | groups 2 例 | 正常/异常 | 返回配置群列表；失败 500 | 响应断言 |
| K-15~22 | analyze 8 例 | 单群/全局（""/"all"）/缺参/非数字/未注入/超时/服务异常 | 序列化 ProfileResult 或 400/503/504/500；_profile_result_to_dict 契约 | 响应断言 |
| K-23~26 | history 列表 4 例 | 正常/分页归一/未注入/失败 | 顶层 `profiles` 键 + total；503/500 分支 | 响应断言 |
| K-27~30 | history 详情 4 例 | 命中/缺参/404/未注入 | 详情或 400/404/503 | 响应断言 |
| K-31~35 | history 删除 5 例 | query 传参 / body 传参 / 缺参 / 404 / 未注入 | **query 与 body 双路均删除成功**；400/404/503 分支 | 响应断言 |
| K-36~38 | 序列化 3 例 | _profile_result_to_dict / _to_jsonable | dataclass→dict、datetime→ISO、tuple→list、None 安全、嵌套 | 直断 |

## 6. 用例明细 — 其余模块（pytest 合跑统计）

| 编号区间 | 套件 | 用例数 | 覆盖要点 |
|----------|------|--------|---------|
| C-01~34 | test_models_capture.py | 34 | 5 dataclass 默认值；mysql_row_to_profile_message（时间戳三级回退/at_list "1,2"→[1,2]/NULL 兜底）；extract_at_targets（去重保序/剔除 AtAll/防御）；extract_reply_id（组件优先/raw_message 回退/None 安全） |
| D-01~24 | test_profile_fetcher.py | 24 | _onebot_raw_to_profile_message；单群 MySQL+OneBot 补/全局；去重升序；关系上下文开/关（at 聚合/reply 反查/他人→目标）；全程降级 |
| E-01~16 | test_profile_stats.py | 16 | 24h/7weekday 分布（Mon=0 锚定）；peak/avg_length/emoji/问号比率；活跃天数；group_breakdown 排序；空集兜底 |
| F-01~36 | test_profile_analyzer.py | 36 | provider 降级链（异常/空文本/非字符串，provider_id 记实际成功者）；event=None Web 全局跳过会话兜底；长度预算截断保最近；维度开关；4 板块宽松切分；绝不抛异常 |
| H-01~37 | test_profile_formatter.py | 37 | strip_markdown；forward 单 Nodes 聚合（头+sections+页脚）；text 剥 Markdown；image 三级降级（自研模板→text_to_image→纯文本）；mode 分发；NeverRaises |
| I-01~24 | test_profile_storage.py | 24 | save/list/read/delete/cleanup；路径穿越双重白名单（..）/非法文件名 ValueError；碰撞后缀；调度器 24h 循环 + 退避 + keep_days 回退 |
| J-01~43 | test_profile_service.py | 43 | 校验链（总开关→权限→群环境→限流，admin/all）；目标 @/QQ 解析；run_analysis 指令/Web 双入口 scope 推导；结果注入；storage 容错；端到端 mock；全流程异常兜底（降级 ProfileResult 保留 stats）；反馈（reaction 失败降级文字/text/none）；生命周期 start/stop |
| A-01~37 | test_profile_config_smoke.py | 37 断言 | 真实 aiosqlite 临时库：建表播种 19 项/typed 读取/保存全量校验/reset 恢复默认/缺失键自愈播种/close |
| G | — | — | 依赖真实 T2I 服务；Agent-G 已离线完成 jinja2 双环境试渲染 136 断言 + node --check，真机见 M-2 |

## 7. 执行结果

环境：Python 3.12.6 / pytest 9.0.3 / Windows 11 / 插件根目录执行。

| 执行时间 | 套件/用例 | 状态 | 实际输出 | 备注 |
|----------|-----------|------|---------|------|
| 2026-08-03 | test_profile_config_smoke.py | ✅ A-01~37 全 PASS | `冒烟通过：37 项断言全部 PASS` | asyncio.run 脚本，0 pytest |
| 2026-08-03 | test_db_mysql_v040.py | ✅ B1-01~B4-03 全绿 | `Ran 9 tests ... OK` | 本批次新增套件 |
| 2026-08-03 | test_models_capture.py | ✅ C-01~34 全绿 | `Ran 34 tests ... OK` | |
| 2026-08-03 | test_profile_stats.py | ✅ E-01~16 全绿 | `Ran 16 tests ... OK` | |
| 2026-08-03 | test_profile_storage.py | ✅ I-01~24 全绿 | `Ran 24 tests ... OK` | |
| 2026-08-03 | test_profile_fetcher.py | ✅ D-01~24 全绿 | `Ran 24 tests ... OK` | |
| 2026-08-03 | test_profile_analyzer.py | ✅ F-01~36 全绿 | `Ran 36 tests ... OK` | |
| 2026-08-03 | test_profile_formatter.py | ✅ H-01~37 全绿 | `Ran 37 tests ... OK` | |
| 2026-08-03 | test_profile_service.py | ✅ J-01~43 全绿 | `Ran 43 tests ... OK` | |
| 2026-08-03 | test_profile_main.py | ✅ M1-01~M1-22 全绿 | `Ran 22 tests ... OK` | M 主入口 + 删除端点回归 |
| 2026-08-03 | test_profile_webapi.py | ✅ K-01~38 全绿 | `Ran 39 tests ... OK` | **修正 1 个过期断言**（删除端点期望 (DELETE,) → (DELETE, POST)，见 debug_0.md） |
| 2026-08-03 | 全量合跑 | ✅ **284/284** | `284 passed in 1.63s` | `python -m pytest tests/v0.4.0 -q`，10 文件同进程 stub 隔离正常 |

### 通过率统计

- pytest 用例：10 个文件共 **284/284 通过（100%）**（含本批次修复的 1 个过期断言后全绿）
- 冒烟断言：37/37（额外，未计入 pytest 数）
- 失败用例：0（修复过期测试断言后归零；修复前 1 个，记录于 debug_0.md）
- 需用户手动测试：M-1 ~ M-10（见第 2 节）

### 发现与处理

1. **test_profile_webapi.py 过期断言（K-01）**：Agent-K 时期的测试期望 history 删除端点
   methods 为 `("DELETE",)`，而 L2 验收后主 agent 已将其修复为 `["DELETE", "POST"]`
   （web_api.py:267-271，浏览器无法发出 DELETE 的兼容修复）。测试未随修复更新 →
   修复测试断言为 `("DELETE", "POST")` 并加注释说明，**被测代码零改动**。详细记录见
   `tests/v0.4.0/debug/debug_0.md`。
2. **main.py 删除端点回归守卫**：test_profile_main.py M1-22 以源码级正则断言删除端点
   methods 含 DELETE+POST 且成功分支含 `json_response({"deleted": True})`，防止回退。
3. **db_mysql 离线测试范式**：真实 aiomysql 的 `pool.acquire()` 返回同时实现
   `__await__` 与 `__aenter__/__aexit__` 的对象（`async with coroutine()` 在
   Python 3.12 不支持），fake pool 按此协议形态实现 `_FakeAcquire`，保证被测代码
   `async with self.pool.acquire() as conn` 路径真实走通。
