# v0.5.5 分段快照体系 测试报告（test_0.md）

- 测试执行：Test-Agent（dev-flow 阶段 6-7）
- 执行日期：**2026-08-05**
- 环境：Windows / Python 3.12.6 / pytest（`@async_test` = asyncio.run 范式，无 conftest.py）；aiosqlite 在库，aiomysql / astrbot.* 全部 sys.modules stub 隔离
- 范围：v0.5.5 分段快照体系全部变更面（db_config.py 6 表 + 7 方法、stats/repository.py 6 新方法、stats/snapshot.py 泛化、stats/scheduler.py 三类任务、stats/service.py 迁移与回退矩阵、web_api.py api_daily_stats、main.py 回填接线 + 版本、前端 ?v 与 label）
- 本轮**无 API 集成**；依赖真实 MySQL / 常驻进程 / 浏览器缓存的运行时行为收敛至 §4 真机验收清单
- 基线处理：任务书预告的 3 项已知基线漂移（smoke_d [3]/[5]、smoke_g/h/m 假对象缺新方法）全部按「最小改动 + 假对象新方法返回 None 走回退路径」原则更新；另修复 2 项 v0.5.2 时代遗留漂移（smoke_b 昵称 SQL 断言、smoke_g/m StatsService star 构造参数）

---

## 1. 覆盖分析（文件 × 函数/方法 × 是否需要测试 × 覆盖情况）

| 文件 | 函数/方法 | 是否需要测试 | 覆盖情况与原因 |
|------|-----------|:---:|----------------|
| db_config.py | _create_tables 6 张新表（msg_stats_hourly/_top/daily/monthly、image_stats_daily/monthly） | 是 | ✅ smoke_a §12 建表核验（v0.5.5 新增节） |
| db_config.py | snapshot_upsert_msg_hour / snapshot_upsert_daily / snapshot_upsert_monthly（UPSERT 覆盖写、空入参、sender_name 同步更新） | 是 | ✅ smoke_a §13（真实 aiosqlite 两轮覆盖写幂等） |
| db_config.py | snapshot_monthly_totals / snapshot_daily_rows / snapshot_hourly_date_totals（闭区间/群过滤/非法 source/存在性语义） | 是 | ✅ smoke_a §14（含 image/msg 表隔离、无行键缺失语义） |
| db_config.py | snapshot_evict（四阈值 DELETE、monthly 不动、返回 True/False） | 是 | ✅ smoke_a §15（阈值字符串参数化）；✅ **test_v055 TC-03 行级核验**（相对日期四阈值 + 边界保留 + monthly 不动） |
| db_config.py | 月游标 get_setting/set_setting 复用（snapshot_monthly_msg/image_covered） | 是 | ✅ test_v055 TC-02 游标推进断言（真实读写） |
| stats/repository.py | get_msg_window_counts（chat_history 版窗口聚合、Top K 截断、群总量截断前全量、IN 昵称参数化） | 是 | ✅ smoke_b MsgWindowCountsTest（FakePool/FakeCursor）；真实 MySQL 聚合正确性 → §4 M6 手动 |
| stats/repository.py | get_hourly_batch（source 白名单、四键 GROUP BY、逐桶 Top K、昵称派生表无 IN、确定性排序、空窗口、非法 source ValueError） | 是 | ✅ smoke_b HourlyBatchTest 4 例 |
| stats/repository.py | get_daily_batch / get_monthly_batch（SQL 形态、"YYYY-MM" 补零、source 映射、非法 source ValueError） | 是 | ✅ smoke_b DailyBatchTest / MonthlyBatchTest |
| stats/repository.py | get_overview_meta（无 COUNT(*)、COUNT DISTINCT/MIN/MAX、群过滤） | 是 | ✅ smoke_b OverviewMetaTest；✅ TC-04 快照路径调用清单断言 |
| stats/repository.py | get_group_active_senders（按群 COUNT DISTINCT） | 是 | ✅ smoke_b GroupActiveSendersTest；✅ TC-04b 群排行合并断言 |
| stats/snapshot.py | SnapshotManager 别名 ImageSnapshotManager / __all__ 双导出 | 是 | ✅ smoke_d 沿用别名导入；✅ **TC-10 惰性导出 identity** |
| stats/snapshot.py | run_hourly_snapshot 双源（窗口口径/跨天/Top K/异常仅日志） | 是 | ✅ smoke_d [1][2][4][5]（假 repo 补齐 get_msg_window_counts，双源调用与落库断言） |
| stats/snapshot.py | refresh_current_hour 双源 + 60s 单调限流（F4.2） | 是 | ✅ smoke_d [3] 基线更新：限流戳注入走覆盖路径 + 限流命中静默跳过断言（双源均不发查询） |
| stats/snapshot.py | run_daily_snapshot / run_monthly_snapshot（窗口口径、游标防重、邻月防御过滤） | 是 | ✅ TC-08 真实循环驱动落库；✅ smoke_f 调度判定侧；游标推进 TC-02 |
| stats/snapshot.py | backfill_on_startup（四步独立、窗口参数、月游标增量、结尾淘汰） | 是 | ✅ **TC-02 端到端**（真实 SQLite 行级 + 窗口参数核验 + 二次调用月层幂等跳过） |
| stats/snapshot.py | evict_expired（F4.3 四阈值） | 是 | ✅ **TC-03 行级核验**（7天/7天/31天/horizon + monthly 不动 + 边界保留） |
| stats/snapshot.py | is_range_serviceable / 三层归并 _layered_by_group（无重叠无空洞、hourly 存在性优先、规则 1/2） | 是 | ✅ **TC-01 端到端**（跨旧月+上月+近7天+今天种子、逐群/过滤口径、两类不可服务范围返 None） |
| stats/snapshot.py | msg_daily_trend / image_daily_trend（start>=horizon 特例、缺失日不含键、hourly 优先） | 是 | ✅ TC-01 趋势全覆盖断言（含昨日 hourly 优先于 daily 共存行） |
| stats/snapshot.py | msg_total / msg_per_group / image_total / image_per_group | 是 | ✅ TC-01（含群过滤）；✅ TC-04 经 build_stats 串联 |
| stats/snapshot.py | fill_counts 升级（total_images/group_ranking 走图片三层归并，None 回落 snapshot_query；sender 口径不变） | 是 | ✅ smoke_d [6]~[9]（回落与注入口径）；✅ TC-04 total_images=19 三层归并口径 |
| stats/scheduler.py | check_daily_due / check_monthly_due 纯函数（阈值 15/25、被聚合日/月去重、跨年） | 是 | ✅ smoke_f TestCheckDailyDue/TestCheckMonthlyDue 12 例 |
| stats/scheduler.py | _snapshot_loop 三类任务顺序判定 + 两个新去重戳 + 日成功→evict→盖章 | 是 | ✅ smoke_f TestSnapshotLoopThreeTasks 3 例（fake snapshot）；✅ **TC-08 真实 SnapshotManager × 真实 SQLite 组合链**（同一轮三层落库 + 日失败不淘汰不盖章且月被阻 + 恢复补齐） |
| stats/service.py | _build 三处快照迁移（overview.total/trend/group_ranking） | 是 | ✅ **TC-04/04b 快照路径**（repo 调用清单 + 数值断言）；✅ smoke_g 回退路径原断言保持 |
| stats/service.py | 回退矩阵（快照 None → get_overview/get_daily_trend/get_group_ranking） | 是 | ✅ **TC-05 调用清单断言**（含 overview_meta/group_active 不被调用）；✅ smoke_g 全量用例经 FakeSnapshot 返 None 走回退 |
| stats/service.py | overview_daily_stats（days 越界 None、补零、升序、窗口 days+1 天） | 是 | ✅ **TC-06 真实链路**（种子数据、缺失日补 0、今天=hourly、days=32/0 → None） |
| stats/service.py | startup_backfill（幂等、stop 取消安全） | 是 | ✅ **TC-11**（句柄幂等不重建 + 自然结束可再发起 + stop cancel 置 None）；✅ smoke_m 接线断言 |
| web_api.py | api_daily_stats（快照优先、None 回落、未注入 MySQL 路径、days 夹取 90） | 是 | ✅ **TC-07 四态**（list/None/未注入/120→90 夹取，响应结构不变） |
| main.py | initialize → startup_backfill 接线（失败仅日志不阻断） | 是 | ✅ smoke_m test_initialize_starts_stats_service（backfill_calls==1）+ 新增失败不阻断用例 |
| main.py | @register 0.5.5 + desc「分段快照统计」 | 是 | ✅ smoke_m test_register_metadata_v050（版本断言随版本演进至 0.5.5） |
| metadata.yaml | version v0.5.5 + desc | 是 | ✅ TC-09 文本断言 |
| stats/__init__.py | SnapshotManager/ImageSnapshotManager 惰性导出 | 是 | ✅ TC-10（identity/别名同对象/__all__） |
| pages/dashboard/index.html | ?v=0.5.5 × 3、无旧版本残留 | 部分 | ✅ TC-09 文本级断言；真机缓存 → §4 M4 |
| pages/dashboard/data-analysis.js | label「快照 Top K（图片/消息）」 | 部分 | ✅ TC-09 文本断言 + node --check 通过；真机交互 → §4 |

---

## 2. 可自动化测试用例清单

### 2.1 既有冒烟（7 份更新 + 2 份未动，pytest 可收集 286 例 + 2 份独立脚本经 TC-12/13 并入）

| 编号 | 文件 | 用例数 | v0.5.5 更新内容 |
|------|------|:---:|----------------|
| SM-A | smoke_a_config.py | 独立脚本（**153 断言**，经 TC-12 同进程执行） | 追加 §12 六张新表建表、§13 三个新 UPSERT 簇（空入参/覆盖/幂等）、§14 三个查询原语（闭区间/群过滤/非法 source/存在性/表隔离）、§15 snapshot_evict 四阈值 + monthly 不动 |
| SM-B | smoke_b_repository.py | **36**（20→36） | 修复 v0.5.2 昵称派生表 SQL 基线；新增 MsgWindowCountsTest / HourlyBatchTest / DailyBatchTest / MonthlyBatchTest / OverviewMetaTest / GroupActiveSendersTest 六类 16 例 |
| SM-C | smoke_c_parser.py | 56 | 未动（parser 本版零改动） |
| SM-D | smoke_d_snapshot.py | 独立脚本（**60 断言**，经 TC-13 同进程执行） | 基线更新（任务书漂移 #1/#2）：FakeImageRepo 补 get_msg_window_counts 双源记录；[1] 双源调用与 msg 落库断言；[3] 二次刷新前重置 `_last_refresh_mono` 走覆盖路径 + 追加限流命中跳过断言；[5] 异常前重置限流戳、error 计数基线 +2→+4（双源） |
| SM-E | smoke_e_render.py | 56 | 未动（渲染器本版零改动） |
| SM-F | smoke_f_scheduler.py | **45**（30→45） | FakeSnapshot 补 run_daily_snapshot/evict_expired/run_monthly_snapshot（含失败注入与顺序记录）；新增 TestCheckDailyDue 6 例、TestCheckMonthlyDue 6 例（含跨年 1 月→上年 12 月）、TestSnapshotLoopThreeTasks 3 例（同一轮序列 hourly→daily→evict→monthly、分钟段隔离、日失败不淘汰不盖章且月被阻→恢复补齐） |
| SM-G | smoke_g_service.py | **37**（30→37，含 v0.5.1 时代既有增量） | 基线更新（任务书漂移 #3 + v0.5.2 遗留）：构造补 star 参数；FakeSnapshotManager 新增 msg_total/msg_daily_trend/msg_per_group/image_daily_trend 恒返 None（既有用例走回退路径保持原断言）+ snap_call_log；FakeRepo 补 get_overview_meta/get_group_active_senders |
| SM-H | smoke_h_webapi.py | 40 | 基线更新（任务书漂移 #3）：FakeStatsService 补 overview_daily_stats（默认返 None 无副作用，既有用例行为不变）；快照优先/回落新用例在 TC-07 |
| SM-M | smoke_m_main.py | **16**（15→16） | 基线更新（任务书漂移 #3 + v0.5.2 遗留）：FakeStatsService 构造补 star 参数、新增 startup_backfill 记录；版本断言 0.5.5 + desc「分段快照统计」；initialize 用例追加 backfill_calls==1；新增「回填发起失败不阻断初始化」用例 |

### 2.2 新增集成测试 test_v055.py（20 例，跨模块组合链）

| 编号 | 用例 | 验证点 |
|------|------|--------|
| TC-01 | test_tc01_three_layer_merge_end_to_end | 真实 aiosqlite + 假 repo：跨「旧月(月层)+上月前月(月层)+horizon 起(日层)+近 7 天含今天(时层)」种子，msg_total/msg_per_group 无重叠无空洞求和、群过滤口径、msg_daily_trend 逐日全覆盖（昨日 hourly 优先于共存 daily 行、今天=hourly 求和）、image 侧同算法、三类不可服务范围返 None（规则 1/2/趋势特例） |
| TC-02 | test_tc02_backfill_rows_cursor_and_idempotent | backfill_on_startup 真实落库行级核验（6 表值与行数）+ 窗口参数（小时层 [今日-7天, 当前整点)、日层 [horizon, 今日)、月层全历史起点 2000-01-01）+ 游标推进至上月 + 二次调用月层幂等跳过（窗口层重跑） |
| TC-03 | test_tc03_evict_row_level_thresholds | evict_expired 真实 DELETE 行级：hourly/msg_top < today-7d、image_top < today-31d（8 天前图片 top 保留）、daily < horizon（边界日保留）、monthly 不动 |
| TC-04 | test_tc04_group_view_snapshot_path | 真实 SnapshotManager × 真实 StatsService.build_stats 快照路径：total=快照（get_overview 不调用、仅 overview_meta）、trend=快照（get_daily_trend 不调用）、total_images=图片三层归并 |
| TC-04b | test_tc04b_all_groups_ranking_snapshot_merge | 全部群视图快照路径：群排行 count=快照 msg_per_group + 活跃数=get_group_active_senders，合并排序（count DESC）、get_group_ranking 不调用 |
| TC-05 | test_tc05_fallback_repo_call_list | 回退路径调用清单：快照读取侧全 None → get_overview/get_daily_trend/get_group_ranking(limit) 被调用，get_overview_meta/get_group_active_senders 不被调用，输出字段与 v0.5.2 一致 |
| TC-06 | test_tc06_items_structure_zero_fill_and_bounds | overview_daily_stats 真实链路：days=7 → 8 条（days+1 窗口与 MySQL 路径一致）、升序、缺失日补 0、今天=hourly、结构 {date,messages,images}；days=32/0 → None |
| TC-07 | test_tc07_* 四例 | api_daily_stats：快照可供数不打 MySQL；None 回落 MySQL（days 一致）；未注入 stats_service 直接 MySQL；days=120→90 夹取后先问快照再回落 |
| TC-08 | test_tc08_same_round_three_tasks_real_writes | 调度循环 × 真实 SnapshotManager × 真实 SQLite：分钟 26 同一轮触发 小时(窗口聚合落上一整点行)+日(+evict)+月，三层行级落库 + 日/月去重戳 |
| TC-08b | test_tc08b_daily_failure_no_evict_no_stamp_monthly_blocked | 日任务抛异常：不盖章、无日/月落库、月被阻（同循环体顺序语义）；恢复后日成功 → 同轮淘汰 + 月补齐 |
| TC-09 | test_tc09_* 三例 | 前端/元数据文本断言：?v=0.5.5 × 3 且无 0.5.2 残留、label「快照 Top K（图片/消息）」、metadata v0.5.5 |
| TC-10 | test_tc10_lazy_export_identity_and_alias | stats 包惰性导出 SnapshotManager/ImageSnapshotManager identity + 别名同一类对象 + __all__ |
| TC-11 | test_tc11_idempotent_and_stop_cancels | startup_backfill：句柄未结束幂等不重建、stop cancel 置 None、自然结束后可再发起 |
| TC-12 | test_tc12_smoke_a_config_main_green | smoke_a 独立脚本并入同一 pytest 进程执行（153 项内置断言） |
| TC-13 | test_tc13_smoke_d_snapshot_main_green | smoke_d 独立脚本并入同一 pytest 进程执行（60 项内置断言） |

---

## 3. 执行结果（2026-08-05）

### 3.1 单独跑（逐文件，全部绿）

| 文件 | 结果 | 备注 |
|------|:---:|------|
| smoke_a_config.py | PASS（153 断言） | 独立脚本 `python tests/v0.5.0/smoke_a_config.py` |
| smoke_b_repository.py | PASS（36 passed） | pytest |
| smoke_c_parser.py | PASS（56 passed） | 未动，合跑复验 |
| smoke_d_snapshot.py | PASS（60 断言） | 独立脚本 |
| smoke_e_render.py | PASS（56 passed） | 未动，合跑复验 |
| smoke_f_scheduler.py | PASS（45 passed） | pytest |
| smoke_g_service.py | PASS（37 passed） | pytest |
| smoke_h_webapi.py | PASS（40 passed） | pytest |
| smoke_m_main.py | PASS（16 passed） | pytest |
| test_v055.py | PASS（20 passed） | pytest，重复执行 2 次均绿 |

### 3.2 合跑（9 份冒烟 + test_v055.py 同一 pytest 进程）

```
python -m pytest tests/v0.5.0/smoke_a_config.py ... smoke_m_main.py tests/v0.5.5/test_v055.py -q
```

| 文件 | 收集用例 | 通过 | 失败 | 状态 |
|------|:---:|:---:|:---:|:---:|
| smoke_a_config.py | 0（独立脚本，经 TC-12 并入） | — | 0 | PASS |
| smoke_b_repository.py | 36 | 36 | 0 | PASS |
| smoke_c_parser.py | 56 | 56 | 0 | PASS |
| smoke_d_snapshot.py | 0（独立脚本，经 TC-13 并入） | — | 0 | PASS |
| smoke_e_render.py | 56 | 56 | 0 | PASS |
| smoke_f_scheduler.py | 45 | 45 | 0 | PASS |
| smoke_g_service.py | 37 | 37 | 0 | PASS |
| smoke_h_webapi.py | 40 | 40 | 0 | PASS |
| smoke_m_main.py | 16 | 16 | 0 | PASS |
| test_v055.py | 20 | 20 | 0 | PASS |
| **合计** | **306** | **306** | **0** | **PASS** |

`306 passed in 11.4s`（连续执行 2 次结果一致）。**已知 TC-19/TC-20 类 stub 驱逐干扰未复现**；唯一 warning 为 aiosqlite 在 Python 3.12 的 date adapter DeprecationWarning（70 条，库级提示，非生产问题）。

**通过率：单跑 10/10 全绿；合跑 306/306 = 100%**（test_v055.py：20/20 = 100%）

---

## 4. 用户手动验收清单（真机）

> 离线测试无法覆盖的部分：真实 MySQL 聚合结果、常驻调度准点触发、浏览器缓存、image_records 真实清理时序。请逐项真机验收。

| 编号 | 场景 | 操作步骤 | 预期 |
|------|------|----------|------|
| M1 | 日快照定时任务准点触发 | 插件常驻跨任意小时第 15 分钟（如 00:15） | 日志「日快照任务已触发」；config.db 的 msg_stats_daily / image_stats_daily 出现昨日行；同一被聚合日不重复执行（重启当日可能补跑一次，属预期）；第 15 分钟前不触发 |
| M2 | 月快照定时任务准点触发（含跨年） | 插件常驻跨任意小时第 25 分钟；重点月初验证 | 日志「月快照任务已触发」；msg_stats_monthly / image_stats_monthly 出现上一自然月行（month="YYYY-MM"）；plugin_settings 的 snapshot_monthly_msg_covered / image_covered = 上月；1 月验证目标月为上一年 12 月 |
| M3 | 启动回填日志与落库 | 重启插件（MySQL 可用） | 日志「快照启动回填开始/结束（含各层行数）」；近 7 天小时层、horizon 起日层、全历史月层行补齐；二次重启行数不变（幂等）、月层不再重复聚合（游标） |
| M4 | 概览趋势图片清理后不抖动 | Web 概览观察「最近 7 天趋势」→ 等待 image_records 按保留期清理（默认 3 天）后刷新 | 清理前后各日柱高完全一致（快照行已固化）；响应结构 {days, items} 不变，前端图表无兼容性报错 |
| M5 | ?v=0.5.5 缓存破除 | 强刷/无痕打开管理后台，DevTools Network 查看静态资源 | style.css / app.js / data-analysis.js 均带 ?v=0.5.5 且加载新版本；无 0.5.2 残留引用 |
| M6 | 强制刷新限流生效 | 60s 内连续多次 Web 刷新数据分析或 /群统计（多群） | MySQL 侧 get_msg/image_window_counts 聚合至多一次（慢日志/连接数观察）；统计结果不受影响 |
| M7 | 群级快照口径与实时 SQL 一致 | 对已知群分别执行 `/群统计 近7天` 与自定义跨旧月区间（月初起点，如 2026-06-01 到今天） | 总消息数/每日趋势/群排行与 MySQL 人工 COUNT 一致；「全部」预设正常出图（快照月层+日层+时层归并） |
| M8 | 不可服务区间回退无感 | `/群统计` 自定义区间切断旧月中间（如 2026-06-10 到 2026-06-20） | 正常出图（回退实时 SQL），无报错；结果与 MySQL 一致 |
| M9 | 快照淘汰真机核验 | 常驻 ≥ 8 天后检查 config.db | msg_stats_hourly / msg_stats_hourly_top 仅存近 7 天；image_stats_hourly_top 存近 31 天；daily 表存上月+本月；monthly 行数只增 |
| M10 | 个人图片数 31 天口径变化 | `/群统计` 指定 40 天前成员的個人视图 | 该成员图片数为 0（PRD 边界 §7.1 明示口径变化）；群级图片总数不受影响（日/月快照兜底） |

---

## 5. 发现的问题

**本轮测试未发现生产代码 bug**（7 份更新冒烟 + test_v055.py 共 20 例集成用例单跑全绿，合跑 306/306，行为均与源码契约一致）。构建方预告的 3 项基线漂移均确认为 v0.5.5 设计内行为变更，已按任务书方案完成基线更新。

测试基建观察项（非生产 bug）：

1. **test_v050.py（v0.5.0 集成测试）存量漂移**：其 `_build_service` 仍按 3 参构造 `StatsService(ctx, mysql, cfg)`，v0.5.2 已加 `star` 参数（commit aba0463），该文件单跑当前失败——属上一版本遗留、非本版引入，不在本轮任务范围，建议下轮测试同步修复。
2. smoke_a / smoke_d 为独立脚本（pytest 收集 0 用例），经 TC-12/13 同进程包装并入合跑（范式延续 test_v050 TC-20/21）。
3. aiosqlite 在 Python 3.12 产生 date adapter DeprecationWarning（70 条/合跑），为库级提示，不影响正确性。

---

## 6. 结论

v0.5.5 分段快照体系可自动化测试范围**全部通过**：7 份更新冒烟（smoke_a 153 断言 / smoke_b 36 例 / smoke_d 60 断言 / smoke_f 45 例 / smoke_g 37 例 / smoke_h 40 例 / smoke_m 16 例）单跑全绿；新增 test_v055.py 20 例（TC-01~TC-13）覆盖任务书点名的全部跨模块组合链——三层归并端到端（无重叠无空洞/存在性优先/不可服务 None）、启动回填行级核验与游标、淘汰四阈值行级、build_stats 快照路径与回退矩阵调用清单、overview_daily_stats 真实链路、api_daily_stats 快照优先与回落、调度循环真实快照落库与日失败语义、前端版本文本——单跑 20/20；9 份冒烟 + test_v055 同一 pytest 进程合跑 **306/306（100%）**，重复执行稳定，stub 驱逐干扰未复现。**未发现生产 bug**。剩余真实环境行为以 §4 十项真机验收清单交付确认。
