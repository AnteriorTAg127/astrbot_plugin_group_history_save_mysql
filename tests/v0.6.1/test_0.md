# 测试用例 — astrbot_plugin_group_history_save_mysql（v0.6.1）

## 测试范围分析

| 文件 | 函数/方法 | 是否需要测试 | 原因 |
|------|----------|-------------|------|
| core/backfill.py | ReloadBackfill.maybe_trigger | ✅ | 消息触发式补库入口（起点 seq/幂等/未就绪/无 seq） |
| core/backfill.py | ReloadBackfill._backfill_group | ✅ | 翻页（起点 seq）/排序/窗口/去重/降级/解析失败区分 |
| core/backfill.py | ReloadBackfill._compute_window_start | ✅ | 窗口起点（last_terminate_time 收窄/非法回退） |
| core/backfill.py | ReloadBackfill._maybe_snapshot_backfill | ✅ | 快照回填节流 |
| core/backfill.py | ReloadBackfill.stop | ✅ | 取消所有任务，finally 仍 flush |
| core/saver.py | MessageSaver.begin_backfill/end_backfill | ✅ | 按群门控缓冲 + 带去重 flush |
| core/saver.py | MessageSaver.flush_pending | ✅ | dedup 按群/全量、空缓冲 None/0 |
| core/saver.py | MessageSaver.handle_group_message | ✅ | 门控条件按群 |
| core/parsing.py | parse_onebot_raw_message | ✅ | \n 拼接 / time 防御 |
| core/webapi/storage.py | api_save_settings | ✅ | backfill_hours 校验（F9） |
| core/db_mysql/pool.py | 循环导入 | ✅ | 直连导入 + 常量再导出（R4） |
| main.py | @register 版本 / ReloadBackfill 接线 | ✅ | 版本 0.6.1 / 注入 saver/stats_service |
| core/backfill.py | __init__ 框架初始化 | ❌ | 框架交互，无需单独测试 |

## 可自动化测试用例（tests/v0.6.1/test_v061.py，32 例）

### A 组：补库翻页（起点 seq）/ 排序 / 窗口 / 去重（12 例）

| 用例 | 测试对象 | 输入 | 预期 | 结果 |
|------|---------|------|------|------|
| a01-1 | 翻页参数默认值（F1） | 无 | `DEFAULT_ROUND_CAP == 200`、`DEFAULT_MAX_ROUNDS == 5`、`BACKFILL_OVERLAP_THRESHOLD == 0.5` | ✅ |
| a01-2 | 多轮 message_seq 翻页（起点=触发消息 seq，v0.6.1） | 3000 条 seq 1..3000、起点 seq=3000、page_size=1000 | 3 轮翻页（seq 3000→2000→1000），count 均 1000，pulled=2999（起点之前全部） | ✅ |
| a02-1 | 页长 == request_count 继续翻页（F1） | _FakeClientAPI 起点 6000：第 1 轮 4500（seq 5999..1500）、第 2 轮 500（seq 1499..1000） | 第 2 轮 count=500（min 分层）、页长恰 500 不判短页；两轮累计 pulled=5000 | ✅ |
| a02-2 | 协议端硬限（页长 < request_count）正确终止 | _BulkClient 1000 条、page_size=200、起点 seq=1000 | 仅 1 轮协议调用，pulled=200 | ✅ |
| a01-3 | 重叠边界停止（v0.6.1） | 起点 seq=600：第 1 轮 200 条新、第 2 轮 150 条已记录+50 条新 | 第 2 轮重叠 150/200 ≥50% 即停（不再拉第 3 轮）；pulled=400、inserted_text=250、skipped=150 | ✅ |
| a02-3 | 首轮失败降级（生产修复） | _FailFirstClient 首轮抛「消息0不存在」（NapCat 缓存空） | 不 raise 整群作废：返回零计数、保留拉取失败 warning | ✅ |
| a03 | 插入顺序最旧→最新（F2） | 起点 seq=3，单页新→旧乱序 3 条 | `insert_chat_message` timestamp 单调递增（12:30→13:00→14:00） | ✅ |
| a04 | 窗口提前终止（F7） | 起点 seq=1200、1200 条 time=(seq-150)×10s；第 2 轮最旧 seq 出窗口 | 仅 2 次协议调用；pulled=1050；边界 m-150 保留、m-149 被权威过滤 | ✅ |
| a05-1 | 空 message_id 跳过（F4） | 空 id 文本 + 正常文本 + 空 id 图片 | 空 id 两条不写库、skipped=2、群级 warning；仅 m_ok 入库 | ✅ |
| a06 | 批内 URL 去重收窄（F10） | 消息 A 同 URL 两段 + 消息 B 同 URL | A 内重复 URL 仅入 1 行；A/B 共用 URL 各写 1 行 → img_calls=2 | ✅ |
| a05-2 | 无内容消息不计解析失败（生产修复） | video 段 + 正常文本 + time 缺失文本 | video 不计失败；仅 time 缺失 1 条算真失败（warning「有 1 条含文本/图片」）；pulled=1 | ✅ |
| a05-3 | 本地路径图片不计解析失败（生产修复） | image url/file 为本地路径 + 正常文本 | 本地图片无可提取 http 链接、正常跳过；文本入库；无「解析失败」warning | ✅ |

### B 组：按群门控缓冲与去重 flush（6 例）

| 用例 | 测试对象 | 输入 | 预期 | 结果 |
|------|---------|------|------|------|
| b01-1 | 按群门控（v0.6.1） | begin_backfill("111") 后群 111 消息 + 群 222 消息 | 仅群 111 缓冲（insert 零调用）、群 222 正常实时落库；end_backfill("111") flush 群 111；门控解除后群 111 恢复实时 | ✅ |
| b01-2 | _backfill_group 收尾链（F3/F6） | _FakeClientAPI 空页快速完成 | finally 调用 saver.end_backfill(该群) 一次 | ✅ |
| b01-3 | terminate 取消仍 flush（R1） | 任务进入协议端 sleep 后 stop() 取消 | 取消后 finally 仍执行 end_backfill 一次 | ✅ |
| b02-1 | 去重 flush 按群（F3） | 缓冲含已存在 message_id 记录（群 111）+ 群 222 缓冲 | end_backfill("111") 跳过 m_dup、落库 m_new；群 222 缓冲保留不 flush；再 flush 群 222 | ✅ |
| b02-2 | dedup=False 旧版行为 | 单条缓冲 + 空缓冲 | 逐条落库、无查重、返回 None；空缓冲 None/0 均不报错 | ✅ |
| b03 | 缓冲容量（R3） | 新 MessageSaver | `_pending_records.maxlen == 5000` | ✅ |

### C 组：解析 + 消息触发式补库（12 例）

| 用例 | 测试对象 | 输入 | 预期 | 结果 |
|------|---------|------|------|------|
| c01 | 文本以 `\n` 拼接（F5） | 两段文本 | text == `第一段\n第二段`（与实时口径一致） | ✅ |
| c02 | time 防御（F8） | 缺 time / time="abc" / time 正常 | 缺与非法均返回 None 且记 warning；正常解析 | ✅ |
| c03-1 | maybe_trigger 每群一次（v0.6.1） | 同群两条消息（起点 seq=3000） | 只触发一次（begun 一次）；起点 seq=3000 传协议端；完成后 end_backfill；_backfilled_groups 记录 1 群 | ✅ |
| c03-2 | MySQL 未就绪不触发 | _RecordingSaver(initialized=False) | 不触发、不标记（后续消息再试） | ✅ |
| c03-3 | 无 message_seq 跳过并标记 | 事件 raw_message 无 seq | 不触发、标记已补库（避免每次消息都试）、warning | ✅ |
| c03-4 | 快照回填节流（F6） | 连续两次 _maybe_snapshot_backfill | 60s 节流内只触发一次 startup_backfill | ✅ |
| c03-5 | 窗口起点：最后记录 − 5min（v0.8.0） | last_message_time = now-6h | window_start == now-6h-5min（BACHFILL_OVERLAP_MINUTES=5） | ✅ |
| c03-6 | 窗口起点：群无记录 | last_message_time = None | 回退 now-12h（backfill_hours） | ✅ |
| c03-7 | 窗口起点：force 显式 hours（v0.8.0） | `_compute_window_start(None, hours=3)` | window_start ≈ now-3h（不再依赖 last_terminate_time） | ✅ |
| c04-1 | 强制补库绕过「每群一次」（v0.8.0） | 已标记 _backfilled_groups 后 force_backfill | 照常触发（begun/ended 一次）；_active_groups 清空 | ✅ |
| c04-2 | 强制补库并发去重（v0.8.0） | 任务运行中再 force | 第二个 force 返回 False、begin 仅一次；收尾链仍 end_backfill | ✅ |

### D 组：接线与校验（4 例）

| 用例 | 测试对象 | 输入 | 预期 | 结果 |
|------|---------|------|------|------|
| d01-1 | backfill_hours 校验放行（F9） | int 12 / str "12" | `{"backfill_hours": "12"}` 写入 | ✅ |
| d01-2 | backfill_hours 校验拒绝（F9） | float 12.9 / bool True / "abc" | 400 且零写入（`int(str(...))` 统一拒绝） | ✅ |
| d02 | 循环导入消除（R4） | `from core.db_mysql.pool import DynamicPool` 直连 + 包级常量 | pool 直连导入通过；base 从 pool 再导出同一对象；`db_mysql.__all__` 仍含 4 名 | ✅ |
| d03 | 版本号与接线 | main.py / metadata.yaml / 签名 | @register 与 metadata 均 0.8.0；ReloadBackfill 构造含 saver/stats_service；force_backfill 方法面 + get_last_message_time 接线；MessageSaver 门控/去重方法面 | ✅ |

## 执行结果

执行时间：2026-08-08

| 范围 | 结果 | 说明 |
|------|------|------|
| v0.6.1 单独运行（pytest） | **32/32 通过** | ~2.1s，pytest 9.0.3 / Python 3.12.6 |
| v0.6.1 直接脚本运行 | **32/32 通过** | `PYTHONIOENCODING=utf-8 python tests/v0.6.1/test_v061.py`，OK |

### 失败记录（开发期，已修复）

- 重构初期旧接口断言失败 18 例（A/B/C 组引用已删除的 `_run_all`/`start`/旧签名）——由 v0.6.1 消息触发式重构（`maybe_trigger`/`_backfill_group(group_id, start_seq, …)`/按群门控）一次性重写修复。
- `test_a02_page_len` 测试数据算错（_BulkClient 固定 page_size 导致第 2 轮超量交付）——改用 `_FakeClientAPI` 精确控制页，pulled=5000 通过。
- `test_c01` 断言在 await 任务前读取 calls——调整为先 await 再断言。
- 被测产品代码在 32/32 全绿中未发现缺陷。

## 需用户手动测试（AstrBot 真机）

| 功能 | 触发方式 | 验证要点 |
|------|---------|---------|
| 消息触发式补库 | 重启插件后，往某群发一条新消息 | 日志出现「群 X 首条消息触发（起点 seq=…）」；该群补库完成日志；补库期间该群新消息缓冲、完成后 flush |
| 每群只补一次 | 同一群继续发消息 | 不再出现触发日志（_backfilled_groups 已标记） |
| NapCat 正式历史 | 有停机窗口的群 | 能拉到停机期间文本消息入库（走 getMsgHistory，非 aio 视图） |
| 补库与实时共存 | 补库进行中其他群发消息 | 其他群正常实时落库，不受门控影响 |

## 验收口径（沿用历史约定）

各版本目录单独运行全绿 = 回归通过；本版以 v0.6.1 单跑 32/32 全绿为准。

**通过率：32/32 = 100%**（v0.6.1 单跑全绿）
