# 测试用例与执行结果 — v0.6.0 重载自动补库 + 全模块独立化重构

## 测试范围分析

| 文件 | 对象 | 是否需要测试 | 原因 |
|------|------|-------------|------|
| core/db_mysql/{base,chat_history,images,stats,maintenance}.py | MySQLManager 方法面 | ✅ | 重构回归：拆分后公开方法集合与旧 db_mysql.py 一致 |
| core/db_config/{base,groups,summary_settings,profile_settings,stats_settings,snapshots}.py | ConfigManager 方法面 + 类常量 | ✅ | 重构回归：类常量经 MRO 可访问（webapi/summarizer 依赖） |
| core/webapi/{base,storage,query,summary,profile,stats}.py | WebAPI 方法面 | ✅ | 重构回归：42 方法 + 39 路由经 MRO 组装完整 |
| core/{summary,profile,stats}/ | 包导入 | ✅ | 重构回归：移入 core/ 后相对导入可解析 |
| core/parsing.py | parse_onebot_raw_message | ✅ | 新功能核心解析：文本/图片/混合/纯图片/无内容/异常六态 |
| core/backfill.py | ReloadBackfill | ✅ | 新功能核心：开关/窗口/去重/失败隔离/生命周期 |
| main.py | 导入面 + on_group_message 薄委托 | ✅ | 重构回归：瘦身版仅框架交互，解析/落库已迁出 |
| core/db_config/base.py | backfill_enabled/backfill_hours 默认值 | ✅ | 新配置播种 |
| core/saver.py | MessageSaver | ✅ | 逻辑自 main.py 迁移（旧测试覆盖），v0.6.0 补 is_initialized 生命周期 |

## 可自动化测试用例（tests/v0.6.0/test_v060.py，31 例）

### A 组：重构回归（a01–a06）

| 用例 | 断言要点 | 结果 |
|------|---------|------|
| a01 | MySQLManager 公开方法集合与预期清单一致；QUERY_TIMEOUT_SECONDS 等常量可导入 | ✅ |
| a02 | ConfigManager 方法集合 + SUMMARY_TYPES/PROFILE_TYPES/STATS_TYPES 类常量经 MRO 可访问 | ✅ |
| a03 | WebAPI 方法面完整（api_status 等 42 方法） | ✅ |
| a04 | core.summary / core.profile / core.stats 包可导入，服务类可实例化路径存在 | ✅ |
| a05 | main 导入面：瘦身后无 extract_image_urls；core.parsing 提供 extract_image_urls/stats_fallback_text/parse_onebot_raw_message | ✅ |
| a06 | api_save_settings 补库配置 Web 入口：全量四键归一写入；仅补库键字符串透传；非法 backfill_enabled / 越界或非整数 backfill_hours → 400 且零写入 | ✅ |

### B 组：重载自动补库（b01–b08）

| 用例 | 断言要点 | 结果 |
|------|---------|------|
| b01 | parse_onebot_raw_message 六态：纯文本 / 纯图片 / 混合 / 仅 at 无内容 / 空消息 / 异常（message 非 dict 段） | ✅ |
| b02 | backfill_enabled 非 "true" 时 start() 跳过，不创建任务 | ✅ |
| b03 | backfill_hours 夹取 [1,168]；window_start = now - hours（await 任务后断言，消除 stop 竞态） | ✅ |
| b04 | 全链路：假平台 client 翻页 → 解析 → 窗口过滤 → message_id 去重（get_existing_message_ids）→ 逐条入库，汇总计数正确 | ✅ |
| b04a | _run_all 群清单 all_mode 感知：全群模式以 chat_history 有数据的群为准（白名单空也补库）；清单查询失败降级跳过；all_mode 关仍走白名单 | ✅ |
| b05 | 图片 URL 去重：已存在 URL 不再重复入库（get_existing_image_urls + 批内 inserted_urls） | ✅ |
| b06 | 多群汇总日志：全部完成（共 2 个群，拉取 1 条，新增文本 1，新增图片 0，跳过 0） | ✅ |
| b07 | 单群失败不中断：第 1 群 client 取不到 → 零计数跳过，第 2 群正常完成 | ✅ |
| b08 | start/stop 生命周期：幂等 start；stop 取消安全（cancel + await + 吞 CancelledError） | ✅ |

## 全量回归（旧版本测试）

### 执行结果

执行时间：2026-08-08

| 范围 | 结果 | 说明 |
|------|------|------|
| v0.6.0 单独运行 | 31/31 通过 | 0.41s |
| v0.2 单独运行 | 19/19 通过 | |
| v0.3 单独运行 | 119/119 通过 | |
| v0.3.1 单独运行 | 24/24 通过 | |
| v0.3.2 单独运行 | 52/52 通过 | |
| v0.4.0 单独运行 | 304/304 通过 | |
| v0.5.0 单独运行 | 23/23 通过 | |
| v0.5.5 单独运行 | 20/20 通过 | 76 个 DeprecationWarning（aiosqlite 日期适配器，非失败） |
| 全目录合跑（含 v0.6.0） | 561/587 通过，26 失败 | 见下方「合跑失败分析」 |
| 全目录合跑（不含 v0.6.0） | 538/561 通过，23 失败 | 对照组：失败同为 23 例 + v0.6.0 的 3 例 |

### 合跑失败分析（定性结论：pre-existing，非 v0.6.0 引入）

**现象**：全目录合跑时 v0.2（TestPurgeFlow 6 例 / TestApiQuery 4 例）、v0.3（TestWebApiSummary 11 例）、v0.3.2（1 例）、v0.4.0（1 例）、v0.5.5（TC12/13 共 2 例）、v0.6.0（a03/a04/a05 共 3 例）失败；**每个版本目录单独运行全部通过**。

**根因**：各测试文件在模块级向 `sys.modules` 注入形状不同的 `astrbot.*` 假模块（如 v0.6.0 的 `json_response` 假函数返回 `{"json": ...}`，旧测试断言 `resp["__json__"]`；v0.6.0 新增 `MessageChain` stub 而旧测试未注入）。合跑时**后运行的文件覆盖先运行文件注入的 stub**，导致先运行文件的被测导入链或断言失败——纯顺序依赖的测试基建问题。

**决定性证据**：不含 v0.6.0 的全目录合跑同样失败 23 例（其中 v0.5.5 TC12/13 通过 importlib 进程内运行 v0.5.0 smoke 脚本，同样命中 stub 冲突）→ 该问题在 v0.6.0 之前已存在，与本次重构/新功能无关。

**验收口径（沿用历史约定）**：各版本目录**单独运行**全绿 = 回归通过。合跑全绿需统一测试基建（共享 conftest / stub 形状归一），列为 backlog 改进项，不在 v0.6.0 范围内。

## 需用户手动测试（AstrBot 运行时）

| 功能 | 触发方式 | 验证要点 |
|------|---------|---------|
| 重载自动补库 | WebUI 重载插件 / 重启 AstrBot，MySQL 初始化成功后 | 后台日志出现「重载自动补库：后台任务已启动（窗口 12 小时）」，随后逐群「群 xxx 完成（拉取 N 条…）」；Web 管理后台查询到补库文本/图片 |
| 补库开关 | Web 管理后台配置页修改 backfill_enabled / backfill_hours | 关闭后重载不再补库；hours 越界被夹取到 [1,168] |
| 补库去重 | 连续重载两次插件 | 第二次重载新增数明显减少（message_id 已存在跳过） |
| 全功能回归（重构无行为变化） | 群内指令 + Web 后台 | /群统计、/历史总结、人物分析、数据分析各走一遍 |
