# 测试用例与执行报告 — astrbot_plugin_group_history_save_mysql（v0.9.0）

> 阶段 6+7（测试分析与执行）。执行时间：2026-09-05。执行环境：Windows / Python 3.12.6 /
> pytest 9.0.3（无 pytest-asyncio，@async_test 范式）/ 无真实 MySQL、无运行中的 AstrBot。
> **本环境为 DSH 文件沙箱**：禁止在 tempfile 安全区（%TEMP%\dsh-*）与随机后缀目录内建文件，
> 且 pytest 默认 basetemp（`pytest-of-User`）与编号目录 `.lock` 被拦——Agent-A 在
> `smoke_a_sqlite.py` 已记录同一约束。本轮新增测试基建 `tests/v0.9.0/_sandbox_tmp.py`
> （pytest 插件：把 tmp_path / tempfile 重定向到本目录普通子目录，仅路径重定向、零语义改动），
> 所有基线套件经 `-p _sandbox_tmp` 在沙箱内如实执行。

## 0. 套件与运行方式

- 新增离线套件：`tests/v0.9.0/test_v090.py`（A–G 七组 34 例 / 165 处断言调用点）。
- 模块 A 冒烟（既有）：`tests/v0.9.0/smoke_a_sqlite.py`（120 断言）。
- 基线逐文件运行器：`python tests/v0.9.0/_run_baseline.py [文件正则]`
  （每文件独立子进程、cwd=文件目录、输出落 `.baseline_out/<stem>.out.txt`、汇总
  `_baseline_log.txt`；沙箱 piped-stdio 限制故一律文件重定向捕获）。
- 单文件手工运行（插件根目录）：
  `python -m pytest "tests/v0.9.0/test_v090.py" -v -p _sandbox_tmp`
  （`PYTHONPATH` 需含 `tests/v0.9.0/test`；不带 `-p _sandbox_tmp` 亦可在非沙箱环境运行——
  test_v090 自身全部临时文件走本目录 `tmp_v090/` 普通目录，不依赖插件）。
- 验收口径（沿用项目历史约定，见 tests/v0.6.0 与 v0.6.1 的 test_0.md）：
  **各版本目录单跑全绿 = 回归通过**；合跑需统一测试基建，为历史 backlog。

## 1. 测试范围分析

| 文件 | 函数/方法 | 是否测 | 覆盖点 / 不测原因 |
|------|-----------|--------|-------------------|
| core/bootstrap.py | `_resolve_backend` | ✅ | A 组纯函数全分支：未锁定/一致/不一致双向/非法值回退/归一化（真 import，非复制逻辑） |
| core/bootstrap.py | `_parse_bool_cfg` | ✅ | B 组：bool/字符串真假/大小写/垃圾值回退默认/int 1-0 |
| core/bootstrap.py | `PluginBootstrap.__init__` | ⚠️半 | 装配链依赖全部真实服务构造（ConfigManager 建连等 I/O），离线整链构造脆弱；锁判定/警告/三分支逻辑经 A 组纯函数覆盖，`storage_info_provider=self._storage_info` 接线经 G-4 源码断言 + E 组端点行为间接覆盖 |
| core/bootstrap.py | `_storage_info` / `_write_backend_lock` / `_background_mysql_init` | ❌ | 依赖完整装配实例与后台任务运行时（框架/DB I/O），列入手动清单 M2/M4/M7；纯判定部分已归并进 `_resolve_backend` A 组 |
| core/db_sqlite.py | 生命周期 `initialize/close/ping` | ✅ | C-1（真实 aiosqlite）+ smoke_a 全链路 |
| core/db_sqlite.py | `insert_chat_message` / `insert_image_record` | ✅ | C-2 空 ID 共存 + 唯一索引幂等；smoke_a 覆盖超长截断/URL 超长跳过 |
| core/db_sqlite.py | `query_messages` | ✅ | C-3 输出 dict 键与 MySQL 侧逐键同构 + timestamp 字符串化 + NULL 语义；smoke_a 覆盖过滤/转义/闭区间/分页/排序 |
| core/db_sqlite.py | `get_messages_by_ids` / `get_recent_messages` / `get_last_message_time` / `get_all_group_ids` / `count_messages` / `get_all_groups_summary` / `get_stats` / `get_daily_stats` / `clean_old_images` | ✅（smoke_a） | 模块 A 冒烟 120 断言全量覆盖；C 组不重复 |
| core/db_sqlite.py | `get_existing_message_ids` | ✅ | C-4：600 个 ID 分块 >500 正常、group_id 隔离、miss 不误报 |
| core/db_sqlite.py | `purge_all` / `get_meta` / `set_meta` | ✅ | C-5：同构键、DELETE 语义 truncated=False、自增复位、可再插入、meta UPSERT 往返 |
| core/db_sqlite.py | 构造参数归一（wal/busy_timeout） | ✅ | C-6：非法回退 5000、夹取 0–60000、字符串 bool；属性 `is_sqlite_backend`/`db_path` 断言 |
| core/sqlite_migrator.py | `migrate` | ✅ | D 组 6 例：全量+标记+重跑拦截 / hours 校验+窗口透传+不写标记 / 防重入（Event 卡假池）/ 建连失败+批次失败保留已导 / 重复行 / 脏 timestamp 行 |
| core/sqlite_migrator.py | `_create_mysql_pool` | ✅间接 | 全部 D 组用例经子测试类覆写注入假池（该覆写点即本方法）；真实 aiomysql 建连列手动 M6 |
| core/sqlite_migrator.py | `_migrate_chat_history` / `_migrate_image_records` / `_fetch_batch` | ✅ | D 组间接：分页游标推进（id>last_id）、批内预查、跳过计数、进度日志路径 |
| core/db_mysql/stats.py | `get_all_groups_summary`（新增） | ✅ | 下沉的 SQL/归一语义经 test_profile_fetcher.py `TestGroupsSummary`（_StatsHost 挂真实 Mixin + 假池）回归 |
| core/profile/fetcher.py | `get_all_groups_summary`（改委托） | ✅ | 同上文件新增 `test_fetcher_delegates_to_manager`（委托计数+透传） |
| core/commands.py | `group_migrate` | ✅ | F-1 无 migrator 回复 / F-2 hours 转发 / F-3 异常兜底「迁移异常」 |
| core/commands.py | `group_status` sqlite 分支 | ✅ | F-4：is_sqlite_backend 短路、无「连接池:」行（真实 pool 占位键由 C-1 覆盖） |
| core/webapi/storage.py | `api_storage_info` | ✅ | E-1 503 / E-2 透传 / E-3 500 不冒泡（patch 模块属性 json/error_response） |
| core/webapi/base.py | 路由表 + `__init__` 新参 | ✅ | E-4：43 条路由、storage/info 路径+GET；旧位置参数构造不炸（v03/v050/v055 等基线同时回归） |
| main.py | 导出聊天记录注册 / 群统计不可用文案 / 版本 | ✅（静态） | G-4 源码文本断言；真实指令交互列手动 M8/M9 |
| _conf_schema.json | 三项新增 | ✅ | G-3：合法 JSON、options/default/hint 关键词、wal/busy 类型默认 |
| metadata.yaml | version/desc | ✅ | G-4 + v0.7.0 D-2 同步断言 |
| pages/dashboard/index.html | 横幅容器/?v= | ✅（静态） | G-1；浏览器渲染列手动 M10 |
| pages/dashboard/app.js | 横幅渲染/tab 拦截/清空文案 | ✅（静态） | G-2 关键符号；交互列手动 M10 |
| pages/dashboard/style.css | 横幅样式 | ❌ | CSS 无离线断言面，浏览器目视（M10） |
| core/db_sqlite.py 自愈重连/并发串行锁 | 半 | smoke_a（重连）；锁正确性靠单连接串行设计 + D-3 迁移并发用例间接 | 真实高并发列手动 |
| bootstrap 后台重试/停用链、saver/backfill 双后端透传 | ❌ | 逻辑 v0.9.0 未改动（仅日志文案后端名替换），既有套件（v061/v060）继续回归 | - |

## 2. 可自动化测试用例清单（test_v090.py，34 例）

### A 组 `_resolve_backend`（TC-101~105）
- TC-101 未锁定按配置（缺省/显式 mysql/显式 sqlite）
- TC-102 非法配置值回退 mysql（"oracle"/空串/None）
- TC-103 已锁定且一致：正常（mysql/mysql、sqlite/sqlite）
- TC-104 已锁定且不一致：双向按锁旧后端 + mismatch=True + has_lock=True（含缺省配置）
- TC-105 归一化：大小写/混空格；锁值空串/None/垃圾 → 视为未锁定

### B 组 `_parse_bool_cfg`（TC-111~115）
- TC-111 bool 直返（False 不被 default=True 吞）
- TC-112 "true"/"TRUE"/" True "/"1"/"yes"/"on" → True
- TC-113 "false"/"FALSE"/" 0 "/"no"/"off" → False
- TC-114 垃圾/None/空串/"2" → default
- TC-115 int 1/0 归一

### C 组 SQLiteManager 契约（TC-121~126，真实 aiosqlite 临时文件库）
- TC-121 ping()：connected/latency_ms/db + pool 四键恒 1（commands pool_info 取值路径逐字执行）+ get_stats 四键
- TC-122 空 message_id 多条共存；同 id 二次插入幂等 True 且不重复成行
- TC-123 query_messages 顶层 {"total","records"}；记录键与 MySQL 侧 10 键逐一对齐；timestamp 为可 strptime 的字符串；空 ID 读出 None；at_list/reply_id 透传；get_messages_by_ids 不含 id 键
- TC-124 get_existing_message_ids 600+1 ID 分块正常、ghost 不命中、跨群不误报
- TC-125 purge_all 同构键/计数/truncated=False + 清空后自增复位可再插；get_meta None→set→往返→UPSERT 覆盖；is_sqlite_backend=True、db_path 指 .db
- TC-126 busy_timeout 非法回退 5000、合法透传、99999→60000、-5→0、None→5000；wal "false"/"TRUE"/0 归一

### D 组 SQLiteMigrator（TC-131~136，假 MySQL 池 + 真实 SQLite 写入侧）
- TC-131 全量导入 3 消息+2 图片计数正确、message_id/at_list 落库、meta 标记、重跑「已完成过全量迁移」拦截、池关闭
- TC-132 hours "abc"/"0"/"-3" → 用法提示；"24" → 仅窗口内 1 条、窗口参数为 datetime(now-24h±容差)、不写全量标记
- TC-133 防重入：asyncio.Event 卡住假池 fetch，进行中二次调用 → 「迁移正在进行中」；放行后首个完成
- TC-134 建连抛 → 「连接 MySQL 失败」含原因；image 批次抛 → 「迁移失败」含可重跑说明、**chat 已导入 2 条保留**、半途失败不写标记、finally 关池
- TC-135 重复行：dup1 消息 + dup 图片 URL 预插后迁移——数据层绝无重复；图片侧预查 skipped=1 生效；消息侧跨群预查恒不命中（**PROD-BUG-01**，当前实际行为钉死：重复消息计入「已导入」）
- TC-136 MySQL 行 timestamp 为 None（脏值）→ 按当前时间入库不抛、计数正确

### E 组 WebAPI（TC-141~144）
- TC-141 provider None → error_response(503)
- TC-142 async provider 返回 dict → json_response 原样透传（六键含 history_db_path/stats_available）
- TC-143 provider 抛异常 → error_response(500)，不向 Web 层冒泡
- TC-144 路由表：总 43 条（v0.9.0 +storage/info）、路径存在、GET、api_storage_info 已组装进 WebAPI

### F 组 commands.group_migrate（TC-151~154）
- TC-151 migrator None → 「当前为 MySQL 存储模式，无需导入。」
- TC-152 hours 原样转发、结果透传
- TC-153 migrator 抛异常 → 「迁移异常：…」不冒泡
- TC-154 group_status sqlite 分支：「存储后端: SQLite」且无「连接池:」行

### G 组 前端/配置/元数据静态（TC-161~164）
- TC-161 index.html：`id="storageBanner"`、`?v=0.9.0` ×3、无 ?v=0.5./0.6./0.7./0.8. 残留
- TC-162 app.js：`apiGet("storage/info")`、renderStorageBanner、applySqlitePurgeHints、红横幅文案、数据分析拦截 toast
- TC-163 _conf_schema.json：合法 JSON；storage_backend options=["mysql","sqlite"]/default="mysql"/hint 含「危险选项」「锁定」「重启」；sqlite_wal_mode bool true；sqlite_busy_timeout_ms int 5000 + 范围提示
- TC-164 metadata version: v0.9.0；main.py "0.9.0" + `/导出聊天记录` 注册 + SQLite 不可用文案；bootstrap active_backend_lock/警告文案/storage_info_provider 接线；webapi/base.py storage/info

## 3. 需用户真机手动测试清单（AstrBot 运行时，无法离线覆盖）

| 编号 | 场景 | 步骤 | 验证要点 |
|------|------|------|---------|
| M1 | sqlite 端到端落数据 | 配置页 storage_backend=sqlite → 重启插件 → 白名单群发消息 | history.db 出现于 data/plugin_data/<插件>/；/history_status 显示「存储后端: SQLite」与总消息增长；日志「SQLite 聊天记录数据库初始化成功」 |
| M2 | 启动锁生效 | 首次 sqlite 启动成功后查 data/plugin_data/<插件>/backend.lock → **重启框架**再查 | 锁文件应存在且内容 "sqlite"、不被框架剥离（PROD-BUG-02 修复后验证点）；配置页改 mysql 后重启 → 仍走 sqlite + 3 条 ERROR + 面板黄横幅；删除 backend.lock 后重启 → 按新配置切换 |
| M3 | 配置不一致防护 | （在未触发 M2 剥离的前提下）sqlite 锁定后改配置为 mysql → 重启 | 仍按 sqlite 启动 + 连发 3 条 ERROR（文案含解锁步骤与数据独立警告）+ 面板黄横幅 |
| M4 | 改配置回 sqlite→mysql 切换 | 删除配置中 active_backend_lock 字段 → 改 storage_backend=mysql → 重启 | 按 mysql 启动、重新写锁；两侧数据互不可见（预期行为） |
| M5 | /导出聊天记录 实测 | sqlite 模式群内（管理员）发 `/导出聊天记录`，再发 `/导出聊天记录 24` | 第一次：全量导入汇总文案（消息/图片/跳过/耗时）与 MySQL 侧行数一致（PROD-BUG-01 修复后计数准确）；第二次被「已完成过全量迁移」拦截；带 24 正常增量。非管理员 → 权限拦截 |
| M6 | 迁移真实 MySQL 连接 | 迁移参数用真实 mysql_* 配置；故意停 MySQL 再触发 | 「连接 MySQL 失败：…」回复；恢复后可重跑，零重复（有 ID 消息） |
| M7 | mysql 默认零回归 | 全默认配置启动，走 v0.8.1 同款冒烟 | 行为/日志与 v0.8.1 一致；配置实体正常读写；无 sqlite 相关初始化 |
| M8 | /群统计 sqlite 不可用 | sqlite 模式群内 `/群统计` | 回复「数据分析模块在 SQLite 存储模式下不可用…」（PRD F4 文案） |
| M9 | sqlite 下功能可用性 | 总结（/消息总结）、人物分析（/人物分析 + Web 发起，含分析群下拉）、Web「查询」tab、对外 API query_records/count_messages | 全部可用（SQLiteManager 鸭子类型透传）；Web 面板功能分区正常 |
| M10 | Web 面板横幅 | sqlite 模式打开面板；再按 M3 造黄横幅 | 红横幅常驻（无关闭按钮、含 history.db 路径第二行）；黄横幅可关闭；正常 mysql 无横幅；「数据分析」子 tab 点击 → toast 不可用且不发请求；清空弹窗含 SQLite 文案；provider 拉取失败时面板其余功能不受影响 |
| M11 | 图片清理 sqlite | 配置短保留期 → 等定时/手动 /history_clean | clean_old_images 正常删除 image_records 过期行 |
| M12 | 补库 sqlite | 重启后群内首条消息触发 /补库 | ReloadBackfill→get_existing_*/get_recent_messages/get_last_message_time 全链路按 SQLite 语义工作 |
| M13 | 无 ID 消息重复导入披露 | 全量迁移后再按小时补导（MySQL 侧含空 message_id 历史行） | 无 ID 行会被重复导入（NULL 不去重，设计使然，见 PROD-BUG-01 附注）——确认可接受或在 README 披露 |

## 4. 执行结果

执行时间：2026-09-05 13:4x（最终全量重跑；逐文件命令见 §0）。

### 4.1 本版新增

| 文件 | 用例数 | 通过 | 失败 | 备注 |
|------|-------|------|------|------|
| tests/v0.9.0/test_v090.py | 34 | 34 | 0 | 全绿；165 处断言调用点（≥35 目标达成） |
| tests/v0.9.0/smoke_a_sqlite.py | 120 断言 | 120 | 0 | 模块 A 冒烟回归（脚本模式） |

### 4.2 既有基线（v0.1–v0.7.0 全量，逐文件）

| 文件 | 用例数 | 通过 | 失败 | 处置 / 备注 |
|------|-------|------|------|-------------|
| tests/v0.1/test_config.py | 7 | 7 | 0 | ✅（脚本模式；经 _sandbox_tmp 后临时目录可写） |
| tests/v0.2/test_v02.py | 19 | 19 | 0 | ✅ 零改动 |
| tests/v0.3/test_v03.py | 119 | 113 | 6 | 路由计数断言 39→43 **已同步**（+storage/info，v0.7.0 曾 +query_log×3 未同步）；余 6 例 TestOneBotPagination 为 **v0.6.1 翻页锚点重构的既有失败**（首轮不传 message_seq 新语义），与 v0.9.0 无关，不修 |
| tests/v0.3.1/test_v031.py | 24 | 24 | 0 | ✅ 零改动（13 例 setup ERROR 为沙箱临时区限制，经 _sandbox_tmp 全绿） |
| tests/v0.3.2/test_v032.py | 52 | 52 | 0 | ✅ 零改动（同上，2 例沙箱 ERROR 消除） |
| tests/v0.4.0/test_db_mysql_v040.py | 11 | 11 | 0 | ✅ 零改动 |
| tests/v0.4.0/test_models_capture.py | 34 | 34 | 0 | ✅ 零改动 |
| tests/v0.4.0/test_profile_analyzer.py | 36 | 36 | 0 | ✅ 零改动 |
| tests/v0.4.0/test_profile_config_smoke.py | 37 断言 | 37 | 0 | ✅ 脚本模式（pytest 收集 0 例，运行器改判） |
| tests/v0.4.0/test_profile_fetcher.py | 29 | 29 | 0 | **v0.9.0 签名适配**：get_all_groups_summary 下沉 db_mysql/stats 后，TestGroupsSummary 4 例改挂真实 StatsMixin + 新增委托用例 1 例（28→29） |
| tests/v0.4.0/test_profile_formatter.py | 37 | 37 | 0 | ✅ 零改动 |
| tests/v0.4.0/test_profile_main.py | 22 | 22 | 0 | 版本断言 0.6.0→0.9.0 **已同步**（v0.8.1 文档记载重构前即失败） |
| tests/v0.4.0/test_profile_service.py | 50 | 50 | 0 | ✅ 零改动 |
| tests/v0.4.0/test_profile_stats.py | 16 | 16 | 0 | ✅ 零改动 |
| tests/v0.4.0/test_profile_storage.py | 24 | 24 | 0 | ✅ 零改动（39 例失败/错误全为沙箱临时区限制，经 _sandbox_tmp 全绿） |
| tests/v0.4.0/test_profile_webapi.py | 46 | 46 | 0 | ✅ 零改动（路由子集断言天然兼容 43 条） |
| tests/v0.5.0/test_v050.py | 23 | 22 | 1 | tc18b 版本 0.6.0→0.9.0 **已同步**；余 1 例 tc06 为**时钟炸弹既有失败**：注入 now=2026-08-05 但快照 horizon 判定读真实系统时钟（今 2026-09-05），窗口 [07-27,08-03) 不再可服务→走实时回退，overview_meta 0 调用。与 v0.9.0 无关，不修（建议：给 service 注入 now 贯通或改相对日期） |
| tests/v0.5.0/smoke_a_config.py | 153 断言 | 153 | 0 | ✅ 脚本模式（initialize 失败为沙箱 mkdtemp 禁令，经 `_sandbox_tmp` 模块前缀跑通） |
| tests/v0.5.0/smoke_b_repository.py | 36 | 36 | 0 | ✅ 零改动 |
| tests/v0.5.0/smoke_c_parser.py | 56 | 56 | 0 | ✅ 零改动 |
| tests/v0.5.0/smoke_d_snapshot.py | 60 断言 | 60 | 0 | ✅ 脚本模式（同 smoke_a） |
| tests/v0.5.0/smoke_e_render.py | 56 | 54 | 2 | 2 例 TestNodeCheck：subprocess 管道 stdio 被沙箱拦（named pipe EPERM，官方已知边界）。**等价验证已通过**：`_verify_nodecheck.py` 以文件重定向跑同两例 → 2/2 绿（node --check 语义零差异）。与被测代码无关，套件不修 |
| tests/v0.5.0/smoke_f_scheduler.py | 45 | 45 | 0 | ✅ 零改动 |
| tests/v0.5.0/smoke_g_service.py | 37 | 37 | 0 | ✅ 零改动 |
| tests/v0.5.0/smoke_h_webapi.py | 40 | 40 | 0 | ✅ 零改动（stats 6 路由子集断言天然兼容） |
| tests/v0.5.0/smoke_m_main.py | 16 | 16 | 0 | **v0.9.0 适配**：补 StarTools stub（db_sqlite 导入链）、FakeBackfill 吸收 saver=/stats_service= kwargs + maybe_trigger、版本断言 0.9.0（v0.8.1 文档记载重构前即失败） |
| tests/v0.5.5/test_v055.py | 20 | 20 | 0 | **已同步**：?v=0.5.6→0.9.0（×3 + 残留断言反转）、metadata v0.6.0→v0.9.0；9 例真实 ConfigManager 失败为沙箱临时区限制，经 _sandbox_tmp 全绿 |
| tests/v0.6.0/test_v060.py | 31 | 20 | 11 | 11 例为**既有失败**：v0.6.1 起 ReloadBackfill 删除 start/_run_all 旧接口（消息触发式重构），该套件从未随迁；与 v0.9.0 无关，不修（v0.6.1 test_0.md 记载当时口径「本版单跑全绿为准」） |
| tests/v0.6.1/test_v061.py | 38 | 38 | 0 | **已同步**：d03 版本断言 0.8.0→0.9.0（main + metadata 两处） |
| tests/v0.7.0/test_v070.py | 40 | 40 | 0 | **已同步**：D-2 "0.7.0"→"0.9.0"、D-3 ?v=0.7.0→0.9.0（残留断言同步反转）；**测试质量修复 2 处**：B-4 打桩关闭 5% 概率顺带清理（消除确定性假失败源，B-2 已专测概率路径）、E-9 补漏掉的 @async_test（原协程从未 await、断言空转；修复后本套件 0 警告） |

> 注：§4.2 为最终全量重跑结果（2026-09-05 14:0x，含 §6 全部改动后）；
> 所有套件经 `-p _sandbox_tmp` 沙箱垫片执行，脚本模式经 `python -m _sandbox_tmp`。
> `tests/v0.7.1/test_v071.py` 不在基线内：v0.7.1 已被用户放弃、
> 代码还原（changelog「版本放弃」条目），其断言指向已删除的导出端点，必然失败。

### 4.3 汇总与通过率

- pytest 组织套件 27 个文件：**991 例收集 / 971 通过 / 20 失败**，
  原始通过率 **97.98%**；另脚本自检套件 5 个文件（v0.1 test_config 7 项 /
  smoke_a_config 153 / smoke_d_snapshot 60 / test_profile_config_smoke 37 /
  smoke_a_sqlite 120，共 377 项断言）**全绿**。
- 20 例失败归因全部核验完毕，均**非 v0.9.0 引入**：
  v0.3 OneBotPagination 6（v0.6.1 重构既有）+ test_v060 11（v0.6.1 接口删除既有）
  + tc06 1（时钟炸弹既有）+ TestNodeCheck 2（沙箱管道限制，已等价验证 2/2 绿）。
- **剔除既有失败与环境限制后：v0.9.0 相关口径通过率 100%**——971 个通过 + 20 个
  非本版失败（6+11+1+2）全部归因核验完毕，无一由 v0.9.0 生产代码回归或本版漂移同步
  遗漏造成；v0.7.0 套件随后又修复 2 处测试自身缺陷（B-4 随机假失败源、E-9 空转断言），
  复跑 40/40 无警告。
- 本版新增用例 34/34、冒烟 120/120 全绿；合跑兼容性：test_v090+test_v070 同进程 74/74，
  test_v090+test_v061+test_v070 同进程仅 v061 既有 3 例失败（v061↔v070 互扰，不含本版文件时同样失败，非本版引入）。
- ruff：test_v090.py 与 6 份测试基建脚本 + 8 份被触碰基线文件 check/format 全干净。

## 5. 发现的生产问题（测试不改生产代码，移交开发决策）

> **修复状态（2026-08-20 调试轮，见 tests/v0.9.0/debug/debug_0.md）**：
> PROD-BUG-02 ✅ 已修复——锁改落插件自有文件 `backend.lock`（方案①），`_resolve_backend`
> 签名改为 `(cfg, locked_raw)` 双参，测试 A 组同步 + 新增 A-6 回归钉；
> PROD-BUG-01 ✅ 已修复——消息侧按群分组预查（同图片侧范式），D-5 按修复后口径更正
> （消息/图片重复各计 skipped，汇总「跳过重复 2 条」）；空 message_id 行重导边界（附注）
> 属设计使然，README 迁移章节「幂等可重跑」表述已隐含（无 ID 行本就无重复判定依据）。
> DOC-DEV-01 ✅ PRD F3 已订正为 dict 契约。

### PROD-BUG-02【严重】启动锁 `active_backend_lock` 会被 AstrBot 框架在插件加载时剥离

- **现象**：sqlite 后端启动、锁标记成功写入 `data/config/<插件>_config.json` 后，
  **下次插件重载/框架重启时锁被框架删除**（内存与磁盘双剥离）。
- **根因**：AstrBot v26.6 `astrbot/core/config/astrbot_config.py::check_config_integrity`
  以 schema 派生的 default_config 为参照重建配置 dict：凡不在 schema 中的键一律
  **丢弃**（`conf.clear(); conf.update(new_conf)`）并触发 `save_config()` 落盘——
  官方文档同步强调「Schema 更新时移除多余项」。PRD F2 选择「锁不进 schema」
  与框架行为直接冲突。
- **复现**：`python tests/v0.9.0/_repro_prod_bug_02.py`（用 AstrBot 自带 venv +
  真实 _conf_schema.json 走框架同款构造路径），输出 `Config key removed: active_backend_lock`、
  内存/磁盘双 False。
- **影响**：PRD F2「一经启用即锁定」的三重防护②③实际不可依赖——用户在配置页把
  sqlite 改成 mysql 并重启后，**会真的切换后端**（无 3 条 ERROR、无挡回），随即
  「看不到另一侧历史」，恰是该危险选项要防的事故；反之 mysql→sqlite 同。
  仅在同一次运行内（不重启）锁语义成立。
- **修复方向建议**（择一，均属生产改动）：
  ① 锁改存插件自有文件（如 data/plugin_data/<插件>/storage_lock.json）或 config.db
  （sqlite 侧已有 meta 表；mysql 侧可用 config.db settings 键），彻底脱离框架托管的配置实体；
  ② 在 schema 中增加 `active_backend_lock`（type string，default ""，`invisible: true`）
  ——键随 schema 保留、WebUI 不显示，改动最小但违背 PRD §5「不进 schema」原文，需产品确认。

### PROD-BUG-01【中】SQLiteMigrator 消息侧跨群查重恒不命中，迁移汇总计数失真

- **位置**：`core/sqlite_migrator.py` `_migrate_chat_history`：
  `get_existing_message_ids("", batch_ids)`（注释意图「跨群整批一次查」）。
- **根因**：`get_existing_message_ids`（MySQL/SQLite 双侧同名契约）SQL 恒带
  `WHERE group_id = ?`，传 `""` 匹配不到任何群 → 预查**永远返回空集**，重复检测形同虚设。
- **影响**：重复消息撞 UNIQUE 索引时 `insert_chat_message` 按幂等返回 True，被计入
  「消息 N 条」而非「跳过重复」——完成回复**虚高导入数、虚低跳过数**（数据本体不重复，
  无正确性事故；图片侧按真实群号预查，不受影响）。半途失败重跑/带 hours 增量补导时
  用户无法从回复判断实际跳过量。
- **修复方向建议**：按行内 group_id 分组预查（同图片侧范式）；或 SQLiteManager 增补
  跨群查询内部方法（注意与 MySQL 契约面隔离）。
- **附注（设计边界，建议 README 披露）**：空 message_id 历史行存 NULL、UNIQUE 不去重，
  「全量后再增量补导」会把 MySQL 无 ID 消息重复导入一份（同 M13 手动项）。

### DOC-DEV-01【文档偏差】PRD F3「query_messages 返回三元组」与双后端实际契约不符

- PRD F3 写 `query_messages(...) -> tuple[list[dict], int, bool]`；实际 MySQL/SQLite
  两侧均为 `dict {"total", "records"}`（v0.7.0 起含 raise_on_error）。测试以代码事实
  钉死（TC-123），建议 PRD 下版勘误。

## 6. 漂移同步清单（本次对既有测试文件的改动，全部仅测试侧）

| 文件 | 同步点 |
|------|--------|
| tests/v0.3/test_v03.py | 路由计数 39→43（v0.9.0 storage/info +1；v0.7.0 query_log ×3 属历史漏同步，一并计入）；ruff 遗留 I001/F401 清理（先例：v0.3.2 文档） |
| tests/v0.4.0/test_profile_fetcher.py | TestGroupsSummary 适配 get_all_groups_summary 下沉委托（改挂真实 StatsMixin + 新增委托用例） |
| tests/v0.4.0/test_profile_main.py | 版本断言 0.6.0→0.9.0 |
| tests/v0.5.0/test_v050.py | tc18b 版本断言 0.6.0→0.9.0 |
| tests/v0.5.0/smoke_m_main.py | StarTools stub / FakeBackfill kwargs+maybe_trigger 适配 bootstrap 新接线 / 版本断言 0.9.0 |
| tests/v0.5.5/test_v055.py | ?v=0.5.6→0.9.0 ×3 + 残留断言反转；metadata v0.6.0→v0.9.0 |
| tests/v0.6.1/test_v061.py | d03 版本断言 0.8.0→0.9.0（main + metadata） |
| tests/v0.7.0/test_v070.py | D-2/D-3 版本与 ?v= 断言→0.9.0；B-4 打桩去 5% 随机假失败；E-9 补 @async_test（原断言空转） |

## 7. 测试基建产物（tests/ 目录，不提交）

- `_run_baseline.py`：基线逐文件运行器（脚本模式/pytest 模式分流、UTF-8、文件重定向捕获）。
- `_sandbox_tmp.py`：DSH 沙箱临时区兼容插件（tmp_path/tempfile 路径重定向；
  `python -m _sandbox_tmp <script>` 兼作脚本模式入口）。
- `_verify_nodecheck.py`：smoke_e TestNodeCheck 的沙箱等价验证（subprocess 文件重定向）。
- `_repro_prod_bug_02.py`：PROD-BUG-02 复现脚本（需 AstrBot venv）。
- `_count_asserts.py` / `_cleanup.py`：断言统计与临时目录清理。
