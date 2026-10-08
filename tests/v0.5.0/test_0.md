# v0.5.0 数据分析 测试报告（test_0.md）

- 测试执行：Test-Agent（dev-flow 阶段 6-7）
- 执行日期：**2026-08-04**
- 环境：Windows / Python 3.12.6 / pytest 9.0.3（`@async_test` = asyncio.run 范式，无 conftest.py）；jinja2、aiosqlite 在库，aiomysql / astrbot.* 全部 sys.modules stub 隔离
- 范围：v0.5.0 数据分析全部新增/修改代码（stats/ 8 模块 + 报告模板 + db_config.py stats 簇 + web_api.py stats 簇 + main.py /群统计 接线 + 前端 data-analysis.js）
- 本轮**无 API 集成**（未提供 api_config.json）；凡依赖真实 MySQL / 浏览器渲染 / QQ 协议的运行时行为，全部收敛至 §4 用户手动验收清单

---

## 1. 覆盖分析（文件 × 函数/方法 × 是否需要测试 × 覆盖情况）

| 文件 | 函数/方法 | 是否需要测试 | 覆盖情况与原因 |
|------|-----------|:---:|----------------|
| stats/models.py | StatsTimeRange / StatsQuery / SenderRankItem / GroupRankItem / MemberStats / StatsData（6 dataclass 字段形态） | 是 | ✅ smoke_c_parser.py TestModels；字段形态被 smoke_g / test_v050 全体组装用例反复消费 |
| stats/parser.py | parse_stats_args / USAGE_TEXT / StatsParseError（关键词/相对日期/绝对区间/非法输入） | 是 | ✅ smoke_c_parser.py 56 例（含今日/昨日/本周/上周/全部/自定义日期/区间校验/topN） |
| stats/repository.py | get_overview / get_hourly_dist / get_weekday_dist / get_daily_trend / get_sender_ranking / get_group_ranking / get_member_overview / get_image_window_counts + _execute 超时 + _window_clause 参数化 | 是 | ✅ smoke_b_repository.py 20 例（FakePool/FakeCursor，含参数安全、30s 超时、空/None 防御）；真实 MySQL 聚合 SQL 正确性 → §4 手动（需真库） |
| stats/snapshot.py | run_hourly_snapshot / refresh_current_hour / _aggregate_and_upsert / _hour_floor | 是 | ✅ smoke_d_snapshot.py 独立断言；✅ **test_v050 TC-01~04 组合链**（真实快照 × 真实 StatsService.build_stats，冒烟从未串联） |
| stats/snapshot.py | fill_counts（total_images / 排行 / 群排行 / 个人 image_count / 跨群求和 / 失败保零） | 是 | ✅ smoke_d 手搓 StatsData 单测；✅ test_v050 TC-01~04 经真实 snapshot_query 回注（含 (gid,sid) 键匹配与跨群求和） |
| stats/t2i_render.py | 纯函数：_theme_for / _hhmm_to_minutes / _extract_html_title / 魔数校验 / CDN 序解析 / _build_context | 是 | ✅ smoke_e_render.py 56 例（主题环判定、超时夹取、CDN 非法键过滤、上下文契约字段） |
| stats/t2i_render.py | render_card 两轮渲染真实链路（html_render → 图片落盘） | 是 | ⚠️ 离线不可测（依赖 Star.html_render/浏览器）→ §4 手动清单 M2/M3 |
| stats/scheduler.py | check_push_due / check_snapshot_due / 推送循环 / 快照循环 / 退避重试 / 去重日期戳 | 是 | ✅ smoke_f_scheduler.py 30 例；✅ smoke_g TestRenderAndLifecycle（start/stop 接线） |
| stats/scheduler.py | 真实小时级任务准点触发 | 是 | ⚠️ 需常驻进程 → §4 手动清单 M7 |
| stats/service.py | build_stats / _build（gather 裁剪、趋势补零、ratio/rank/peak/avg） | 是 | ✅ smoke_g 四视图 + 异常包装；✅ test_v050 TC-01~04 真快照组合 |
| stats/service.py | check_cooldown（0=不限流/期内拒绝不盖章/群隔离） | 是 | ✅ smoke_g TestCooldown（FakeConfig）；✅ test_v050 TC-09/10 **真实 typed 配置链路**（补缺口） |
| stats/service.py | record_group_umo / get_group_umo | 是 | ✅ smoke_g TestUmoCache + smoke_m TestUmoRecording |
| stats/service.py | push_report / _push_range（daily 今日窗口 / weekly 上一整周）/ _push_one_group / _fallback_text / _image_chain | 是 | ✅ smoke_g TestPushReport（FakeConfig）；✅ test_v050 TC-05~08 **真实 get_push_groups 白名单门控**组合（补缺口：非白名单忽略/渲染失败降级文本内容/无 umo 跳过/weekly 窗口口径） |
| stats/service.py | start / stop（调度器惰性创建、可重入、LIFO） | 是 | ✅ smoke_g TestRenderAndLifecycle + smoke_m TestLifecycle |
| stats/__init__.py | PEP 562 惰性 __getattr__ 导出 | 是 | ✅ **test_v050 TC-19/19b**（identity / AttributeError / models 轻量导入不拉重链）——冒烟未覆盖，本次补齐 |
| stats/templates/stats_report.html | 模板结构 / data 契约字段 / CDN 内联加载器 | 部分 | ✅ smoke_e 结构与契约字段核对；真实渲染效果 → §4 手动 M2 |
| db_config.py（stats 簇） | _validate_stats_setting（类型/范围/HH:MM 归一化） | 是 | ✅ smoke_a_config.py 独立断言；✅ test_v050 TC-20 同进程包装执行 + TC-12/13 经 web_api 全量校验串联 |
| db_config.py（stats 簇） | get_stats_setting / _typed / set_stats_setting / get_all_stats_settings / reset_stats_settings | 是 | ✅ smoke_a + test_v050 TC-09/10/11/12/14 真实 DB 往返 |
| db_config.py（stats 簇） | get_push_groups（LEFT JOIN 白名单）/ set_push_group（UPSERT） | 是 | ✅ smoke_a；✅ test_v050 TC-05/15（白名单门控进推送编排，补缺口） |
| db_config.py（stats 簇） | snapshot_upsert（ON CONFLICT 覆盖写）/ snapshot_query（(date,hour) 半开区间） | 是 | ✅ smoke_a + smoke_d；✅ test_v050 TC-01 落库行级核验 |
| web_api.py（stats 簇） | api_stats_data（窗口解析/366 天/哨兵豁免/top_n 夹取/500 透出） | 是 | ✅ smoke_h 37 例内覆盖；✅ test_v050 TC-16 串真实 StatsService + 真实 SQLite 全链路（补缺口） |
| web_api.py（stats 簇） | api_stats_settings / _save（扁平 body 全量校验拒绝部分写入）/ _reset / api_stats_push_toggle | 是 | ✅ smoke_h（FakeConfigMgr）；✅ test_v050 TC-11~15 真实 ConfigManager 落库往返（补缺口） |
| web_api.py（stats 簇） | _stats_value_to_jsonable / _stats_data_to_dict / 5 条路由注册 | 是 | ✅ smoke_h SerializerHelperTest / RouteRegistrationTest；✅ test_v050 TC-11 路由挂载回归 |
| main.py（stats 接线） | group_stats handler（私聊 usage / 冷却静默 / 解析错误 / 图片回包 / 渲染降级 / StatsBuildError / 兜底 / stop_event） | 是 | ✅ smoke_m_main.py TestGroupStatsCommand 8 例 |
| main.py（stats 接线） | @filter.command 别名注册（群数据/统计）+ GreedyStr 注解 + @register 版本元数据 | 是 | ✅ **test_v050 TC-17/18/18b**（smoke_m 恒等装饰器丢弃参数无法断言，本次以记录式装饰器补齐） |
| main.py（stats 接线） | on_group_message umo 登记 / initialize start / terminate LIFO stop / WebAPI 注入 | 是 | ✅ smoke_m TestUmoRecording + TestLifecycle |
| pages/dashboard/data-analysis.js | tab 全部交互逻辑 | 部分 | ⚠️ 前端无离线单测条件：smoke_i_frontend.md 已做 node --check + 22 处选择器交叉核对 + mock 桥接 16 项运行时清单；真机复核 → §4 手动 M6/M8 |

**缺口专项结论**（任务点名的 5 类缺口）：

| 缺口 | 处置 |
|------|------|
| 指令别名（群数据/统计）注册无断言 | ✅ test_v050 TC-17 记录式装饰器捕获并断言 |
| push_report weekly 时间窗口计算 | ✅ TC-06 以 2026-08-05（周三）注入，断言 [2026-07-27, 2026-08-03) 上一整周口径 |
| snapshot fill_counts + build_stats 组合路径 | ✅ TC-01~04 真实 ImageSnapshotManager × 真实 aiosqlite × 真实 service 三模块串联 |
| 渲染失败降级文本内容 | ✅ TC-07 逐段断言（零宽空格包裹/【群聊日报·今日】/Top3/昵称缺失回退 QQ 号）；指令侧降级文案 smoke_m 已覆盖 |
| 冷却配置 0=不限流 | ✅ TC-09 经真实 typed 配置链路断言恒放行且不盖章 |

---

## 2. 可自动化测试用例清单

### 2.1 既有冒烟（9 份，pytest 可收集 244 例 + 2 份独立脚本经 TC-20/21 并入）

| 编号 | 文件 | 用例数 | 覆盖模块 |
|------|------|:---:|----------|
| SM-A | smoke_a_config.py | 独立脚本（约 60 断言）| db_config stats 簇 CRUD/校验/推送开关/快照读写（经 TC-20 同进程执行） |
| SM-B | smoke_b_repository.py | 20 | stats/repository.py 八查询 + 超时 + 参数安全 |
| SM-C | smoke_c_parser.py | 56 | stats/parser.py + models |
| SM-D | smoke_d_snapshot.py | 独立脚本（约 40 断言）| stats/snapshot.py 三入口（经 TC-21 同进程执行） |
| SM-E | smoke_e_render.py | 56 | stats/t2i_render.py 纯函数 + 模板契约 |
| SM-F | smoke_f_scheduler.py | 30 | stats/scheduler.py 到点判定 + 双循环 + 退避 |
| SM-G | smoke_g_service.py | 30 | stats/service.py 四视图/冷却/umo/推送/生命周期 |
| SM-H | smoke_h_webapi.py | 37 | web_api.py stats 五端点 + 序列化 + 路由 |
| SM-M | smoke_m_main.py | 15 | main.py 指令接线 + umo 登记 + 生命周期 |
| SM-I | smoke_i_frontend.md | 文档清单 | 前端（node --check + 结构核对 + mock 桥接 16 项） |

### 2.2 新增集成测试 test_v050.py（23 例，补缺口与跨模块组合）

| 编号 | 用例 | 验证点 |
|------|------|--------|
| TC-01 | test_tc01_group_view_full_snapshot_chain | 单群视图：refresh 窗口参数 [整点, now) → UPSERT 真实 SQLite（行级核验两张快照表）→ fill_counts 回注 total_images=9 / 排行 image_count / Top K 外保 0 |
| TC-02 | test_tc02_all_groups_view_group_ranking_images | 全部群视图：发言人排行恒空、群排行 image_count 按 by_group 注入、total_images=Σ |
| TC-03 | test_tc03_all_groups_member_view_cross_group_sum | 全部群个人视图：member.image_count 跨群按 sender 求和、rank 恒 None、不查发言人排行 |
| TC-04 | test_tc04_group_member_view_rank_lookup_and_limit50 | 单群个人视图：ranking_full limit=50 单次两用、rank 匹配、展示切 top_n、ratio 计算 |
| TC-05 | test_tc05_whitelist_gating_only_enabled_whitelisted_pushed | 推送白名单门控：仅「白名单 ∩ enabled」群发送；非白名单 enabled 行被 LEFT JOIN 排除；图片链 file 类型；1/1 完成日志 |
| TC-06 | test_tc06_weekly_range_is_last_full_week | weekly 窗口 = 上一整周 [上周一, 本周一)、标题「群聊数据周报」、top_n 真实配置透传 |
| TC-07 | test_tc07_render_fail_fallback_text_content | 渲染失败降级：​ 包裹 + 【群聊日报·今日】+ 总消息数 + Top3（昵称缺失回退 QQ 号）+ warning 日志 |
| TC-08 | test_tc08_no_umo_warn_skip_not_block | enabled 群无 umo：warning 跳过、不发送、不抛异常、0/1 计数 |
| TC-09 | test_tc09_zero_cooldown_unlimited | stats_cooldown=0：连续放行且冷却表不盖章（真实 typed 读取） |
| TC-10 | test_tc10_cooldown_reject_isolate_expire | 冷却 600s：期内拒绝不盖章、群隔离、到期放行 |
| TC-11 | test_tc11_settings_get_real_db | stats/settings：8 项 typed 默认值 + push_groups 真实读取 + 5 条 stats 路由挂载 |
| TC-12 | test_tc12_settings_save_normalize_and_persist | save：扁平 body 归一化（"9:00"→"09:00"）写入真实 DB 并回读核验 |
| TC-13 | test_tc13_settings_save_invalid_rejects_all_no_partial_write | save 全量校验：stats_cooldown=999 整体 400，合法项 stats_top_n=5 不部分写入 |
| TC-14 | test_tc14_settings_reset_restores_defaults | reset：已改值恢复默认（真实 DB 核验） |
| TC-15 | test_tc15_push_toggle_persists_and_validates | toggle：真实 push_group 持久化 + 非数字群号 / 非法 enabled 400 |
| TC-16 | test_tc16_stats_data_endpoint_with_real_service | stats/data 端点全链路：顶层 stats 键、单日 label、datetime 序列化格式、快照注入、缺省「近7天」 |
| TC-17 | test_tc17_group_stats_registered_with_aliases | /群统计 主指令名 + 别名 {群数据, 统计}（PRD F2） |
| TC-18 | test_tc18_group_stats_arg_annotation_greedystr | arg 注解 GreedyStr（剩余全文，防日期区间被空格截断） |
| TC-18b | test_tc18b_register_metadata_version_050 | @register 插件名 + 版本 0.5.0 |
| TC-19 | test_tc19_lazy_exports_identity_and_errors | stats 包 PEP 562 惰性导出 identity / 缓存 / 未知属性 AttributeError / __all__ |
| TC-19b | test_tc19b_models_import_keeps_light | stats.models 导入不拉起 repository/service 重链 |
| TC-20 | test_tc20_smoke_a_config_main_green | smoke_a 独立脚本并入同一 pytest 进程执行（全部内置断言） |
| TC-21 | test_tc21_smoke_d_snapshot_main_green | smoke_d 独立脚本并入同一 pytest 进程执行（全部内置断言） |

---

## 3. 执行结果（2026-08-04）

### 3.1 合跑（9 份冒烟 + test_v050.py 同一 pytest 进程，验证无跨文件干扰）

```
python -m pytest tests/v0.5.0/smoke_a_config.py ... smoke_m_main.py test_v050.py -v
```

| 文件 | 收集用例 | 通过 | 失败 | 状态 | 实际输出/备注 |
|------|:---:|:---:|:---:|:---:|----------------|
| smoke_a_config.py | 0（独立脚本） | — | 0 | PASS | 断言经 TC-20 在同进程执行全绿 |
| smoke_b_repository.py | 20 | 20 | 0 | PASS | 20 passed |
| smoke_c_parser.py | 56 | 56 | 0 | PASS | 56 passed |
| smoke_d_snapshot.py | 0（独立脚本） | — | 0 | PASS | 断言经 TC-21 在同进程执行全绿 |
| smoke_e_render.py | 56 | 56 | 0 | PASS | 56 passed |
| smoke_f_scheduler.py | 30 | 30 | 0 | PASS | 30 passed |
| smoke_g_service.py | 30 | 30 | 0 | PASS | 30 passed |
| smoke_h_webapi.py | 37 | 37 | 0 | PASS | 37 passed |
| smoke_m_main.py | 15 | 15 | 0 | PASS | 15 passed |
| test_v050.py | 23 | 23 | 0 | PASS | 23 passed |
| **合计** | **267** | **267** | **0** | **PASS** | `267 passed in 6.69s`，无 warning/error、无跨文件干扰 |

### 3.2 test_v050.py 单跑确认

| 用例 | 状态 | 实际输出 | 备注 |
|------|:---:|----------|------|
| TC-01 ~ TC-04（快照组合链） | PASS | 4 passed | SQLite 行级核验 image_stats_hourly/_top 落库正确 |
| TC-05 ~ TC-08（推送门控/降级） | PASS | 4 passed | 白名单门控、weekly 窗口、降级文本、无 umo 跳过均符合契约 |
| TC-09 / TC-10（冷却真实配置） | PASS | 2 passed | 0=不限流不盖章；期内拒绝不刷新 |
| TC-11 ~ TC-16（web_api × 真实 DB） | PASS | 6 passed | 归一化/全量校验/持久化/序列化全链路 |
| TC-17 / TC-18 / TC-18b（指令接线） | PASS | 3 passed | 别名 {群数据,统计}、GreedyStr、版本 0.5.0 |
| TC-19 / TC-19b（惰性导出） | PASS | 2 passed | identity 一致、轻量导入不拉重链 |
| TC-20 / TC-21（独立脚本并入） | PASS | 2 passed | smoke_a/smoke_d 全部内置断言同进程通过 |
| **合计** | **23/23 PASS** | `23 passed in 3.23s` | 单独执行与合跑结果一致（重复执行 2 次均绿） |

**通过率：267/267 = 100%**（test_v050.py：23/23 = 100%）

---

## 4. 用户手动验收清单（真机）

> 离线测试无法覆盖的部分：真实 MySQL 聚合结果、T2I 浏览器渲染、QQ 协议收发、常驻调度、真实浏览器缓存。请按下表逐项真机验收。

| 编号 | 场景 | 操作步骤 | 预期 |
|------|------|----------|------|
| M1 | /群统计 参数组合出图 | 群内发送：`/群统计`、`/群统计 近30天`、`/群统计 2026-07-01 到 2026-07-31`、`/群统计 @某成员`、`/群统计 前50` | 各组合均回图片报告卡；时间/范围/Top N 与参数一致；私聊发送回 usage 文本 |
| M1b | 别名触发 | 群内发送 `/群数据`、`/统计` | 与 `/群统计` 行为一致（TC-17 已断言注册，此处验真机分发） |
| M2 | 报告卡视觉·双主题 | 白天（≥08:00）与夜间（≥22:00）各触发一次 | 浅色/深色主题自动切换（summary_t2i_theme_mode=auto）；卡片含总览/趋势/24h 分布/星期分布/排行/页脚免责声明 |
| M3 | 图表渲染 + CDN 兜底 | 观察卡片趋势图/排行图；断网一个 CDN 节点（或改 summary_t2i_cdn_providers 首节点为非法键）再触发 | ECharts 正常出图；节点失败自动切换下一节点；双 CDN 全挂时趋势降级数据表格不白屏 |
| M4 | 渲染失败降级 | 临时令 T2I 不可用（如 html_render 依赖缺失环境）触发 `/群统计` | 回纯文本摘要「【群聊统计·…】总消息数：N，Top3 发言人：…（图片卡片渲染失败，本条为纯文本摘要）」 |
| M5 | 定时日报/周报送达 | Web 设置 push_daily_enabled=21:00 附近时刻 + 目标群推送开关开；开周报并设临近时刻 | 到点后群内收到图片日报/周报；周报口径为上一整周；未开关群不收到；无消息群（无 umo）跳过并日志 warning |
| M6 | Web 数据分析 tab | 打开管理后台 → 数据分析：预设/自定义区间/选群/全部群/点排行行进个人视图/返回群视图/推送开关/保存/恢复默认 | 交互与 smoke_i_frontend.md §5 清单一致；367 天拦截、366 天放行；「全部」预设豁免跨度校验 |
| M7 | 图片快照小时任务 | 保持插件常驻跨整点（每小时第 5 分钟） | config.db 的 image_stats_hourly / _top 出现对应 (date,hour) 行；同小时重复聚合覆盖不增行 |
| M8 | 浏览器缓存破坏 | 强刷/无痕打开管理后台，DevTools 查看静态资源 | style.css / app.js / data-analysis.js 均带 ?v=0.5.0 且加载新版本 |
| M9 | 冷却真机行为 | stats_cooldown 分别设 0 与 60，群内连续 `/群统计` 两次 | 0：两次都出图；60：第二次静默无回复（TC-09/10 已离线断言逻辑） |
| M10 | 真实 MySQL 聚合核对 | 对已知小群用 `/群统计 今天`，人工数几条消息比对 | 总消息数/Top 发言人与 chat_history 实际一致（text/mixed 口径，纯图片消息不计入消息数） |

---

## 5. 发现的问题

**本轮测试未发现生产代码 bug**（test_v050.py 23 例与 9 份冒烟共 267 例全部通过，行为均与源码契约一致）。

测试基建观察项（非生产 bug，无需修复）：

1. `smoke_a_config.py` / `smoke_d_snapshot.py` 为 `if __name__ == "__main__"` 独立脚本，pytest 默认收集贡献 0 用例——已由 test_v050.py TC-20/TC-21 以同进程包装调用并入「一个 pytest 进程」要求，断言全部纳入合跑。
2. `smoke_m_main.py` 注入的 fake 模块不清理 sys.modules 残留——test_v050.py 的 `_purge_plugin_modules()` 已按「包内名无条件剔除 + __file__ 路径剔除」双策略兼容，合跑验证无干扰。

---

## 6. 结论

v0.5.0 数据分析模块可自动化测试范围**全部通过**：9 份冒烟 + 新增 test_v050.py 集成测试在同一 pytest 进程合跑 **267/267（100%）**，test_v050.py 单跑 **23/23（100%）**，重复执行结果稳定。任务点名的 5 类缺口（别名注册、weekly 窗口、fill_counts×build_stats 组合、降级文本内容、冷却 0=不限流）与 3 条跨模块组合路径全部补齐。剩余真实环境行为以 §4 十项手动验收清单交付真机确认。
