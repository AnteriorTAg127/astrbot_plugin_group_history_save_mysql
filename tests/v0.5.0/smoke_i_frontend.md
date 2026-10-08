# 模块 I 前端冒烟自查 — 数据分析 tab（v0.5.0）

> 前端无离线单测条件（无 npm/构建链、依赖 AstrBot WebUI 桥接注入），按任务约定以
> **语法检查（node --check）+ 结构交叉核对 + 静态服务 mock 桥接运行时清单** 为验收标准。
> 自查人：Agent-I　日期：2026-08-04　涉及文件：`pages/dashboard/{index.html, data-analysis.js, app.js, style.css}`

## 0. 交付物

| 文件 | 变更 | 说明 |
|------|------|------|
| `pages/dashboard/index.html` | 修改 | 新增「数据分析」子 tab 按钮（查询与设置之间）+ `#page-data-analysis` 完整骨架；引入 `data-analysis.js`（type=module，与既有脚本同序 defer）；全部本地静态引用升 `?v=0.5.0` |
| `pages/dashboard/data-analysis.js` | 新增 | tab 全部逻辑（约 900 行，8 区块中文注释）：过滤栏/卡片/趋势/分布/双排行/个人交叉视图/推送设置区 |
| `pages/dashboard/app.js` | 修改 | import 接线；`TAB_LAZY_LOAD` 首次进入惰性加载；新增 `TAB_ENTER_HOOKS`（每次进入 resize 图表，切 tab 不销毁实例）；init 绑定事件 |
| `pages/dashboard/style.css` | 修改 | 仅追加 `da-*` 前缀样式块（过滤栏/图表容器/排行徽标/推送区/加载与空态），未改动既有规则 |

## 1. node --check（ESM + 顶层 await 语法）

```
node --check pages/dashboard/data-analysis.js   # OK
node --check pages/dashboard/app.js             # OK
```

## 2. index.html 结构交叉核对（脚本自动比对）

| 检查项 | 结果 |
|--------|------|
| data-analysis.js 中 22 处 `getElementById` 引用全部存在于 index.html | ✅ 0 缺失 |
| `querySelector("#...")` 选择器 id 全部存在 | ✅ 0 缺失 |
| 本地静态引用（style.css / app.js / data-analysis.js）均 `?v=0.5.0` | ✅ 3/3 |
| tab 按钮存在且顺序为 查询 < 数据分析 < 设置 | ✅ |
| `section#page-data-analysis` 与 app.js `page-${name}` 激活规则匹配 | ✅ |
| app.js 接线：import / TAB_LAZY_LOAD / TAB_ENTER_HOOKS / bindDataAnalysisEvents | ✅ 4/4 |
| XSS 测试夹具 `<img src=x onerror=alert(1)>` 作为昵称：渲染后 textContent 为字面量，innerHTML 无 `<img` 标签 | ✅ |

## 3. 与模块 H（web_api.py / stats/service.py）契约对齐核对

运行时核对直接对照了后端实现源码（`api_stats_data` / `api_stats_settings*` / `api_stats_push_toggle` / `StatsService._build`），前端参数与展示规则逐项对齐：

| 契约点 | 后端口径（已实现） | 前端行为 | 结果 |
|--------|--------------------|----------|------|
| `start`/`end` 语义 | **均含当日**，内部转左闭右开 `[start 00:00, end 次日 00:00)` | 预设与自定义均传含当日的末日（早期实现误传「不含次日」，已修正） | ✅ |
| 缺省窗口 | 均省略默认近7天 | 前端始终显式传 start/end（预设 7d：`今日-6 ~ 今日`） | ✅ |
| 跨度校验 | 含首尾 >366 天 → 400 | 前端同口径预拦截（`(end-start)+1 > 366` toast），366 天放行 | ✅ |
| 响应顶层键 | `{"stats": StatsData}`（避桥接解包撞名） | `resp.stats` 读取，缺失视为错误 | ✅ |
| `sender_ranking` | 仅选定群维度非空（service.py:321-329；全部群视图恒空） | 全部群视图空态文案区分提示「排行仅选定群视图提供，请查看群排行」 | ✅ |
| `group_ranking` | 仅 `group_id` 与 `member_id` 均空时非空（service.py:331-335） | 选群或进入个人视图即隐藏群排行卡 | ✅ |
| `member.rank` | 全部群个人视图恒 None | None → 显示「—」 | ✅ |
| `stats/settings` 响应 | `{settings: 8 项 typed, push_groups: [{group_id, enabled}]}` | 直接回填表单 + 渲染开关列表；bool 经 `String(raw).toLowerCase()==="true"` 归一 | ✅ |
| `stats/settings/save` body | **扁平 `{key: value}`**，逐键校验，任一非法整体 400（早期实现误包 `{settings}`，已修正） | 扁平提交；400 错误文案 toast 透出；成功用返回的 `data.settings` 回填归一化值 | ✅ |
| `stats/settings/reset` | 无 body，返回 `{reset, settings}` | confirmDialog 二次确认（iframe sandbox 无 allow-modals）；成功回填 | ✅ |
| `stats/push/toggle` | `{group_id, enabled}`；enabled 接受 bool | 直接 POST；失败回滚开关勾选并 toast | ✅ |
| 时间标签 | 后端返回 `query.time_range.label`（显式传参时为 `start ~ end` 形式） | meta 行直接展示后端 label | ✅ |

## 4. XSS 安全审计

- `data-analysis.js` 中全部 16 处 `innerHTML` 均为 `innerHTML = ""` 清空操作，**零注入式拼接**；动态文本一律 `textContent` / `createElement`（common.js `el` 助手）。
- 昵称/群号/错误信息/成员条文案全部 textContent 落地；运行时以恶意昵称夹具实测无 DOM 注入。
- 未使用 `insertAdjacentHTML` / `document.write` / `eval`。

## 5. 运行时清单（静态 HTTP 服务 + mock 桥接 harness）

方法：临时 harness 页在模块脚本求值**前**注入 mock `window.AstrBotPluginPage`（还原 AstrBot WebUI 的注入时序；common.js 于模块顶层捕获桥接，事后注入无效），mock 数据按模块 H 实现口径裁剪；验证完成后 harness 文件已删除。

| # | 场景 | 结果 |
|---|------|------|
| 1 | 首次点击 tab 触发惰性加载：`groups` + `stats/settings` + `stats/data` 三请求，此前零请求 | ✅ |
| 2 | 统计卡片：千分位（8,780）、peak_hour → 「21 时」 | ✅ |
| 3 | 群下拉：首项「全部群」value=""，未启用群标注「（未启用记录）」 | ✅ |
| 4 | ECharts 5.5.1 CDN 加载成功，趋势/24h/星期三图均出 canvas；分布先 CSS 柱图后升级 | ✅ |
| 5 | 全部群视图：发言人排行空态提示文案正确、群排行 3 行 | ✅ |
| 6 | 选群 → 发言人排行 5 行；恶意昵称 textContent 字面渲染 | ✅ |
| 7 | 点击排行行 → 个人视图：保留群+时间追加 `sender_id`；成员条/交叉卡 6 指标（消息数/占比%/名次/活跃天数/日均/图片数）；分布标题带成员名；群排行隐藏；选中行高亮 | ✅ |
| 8 | 「返回群视图」清除 sender_id 重查，成员条/卡隐藏，选中态清除 | ✅ |
| 9 | 预设参数：今日 `start=end=今天`；昨日单日；近30天 `今天-29 ~ 今天`；全部 `2000-01-01 ~ 今天`；end 均含当日 | ✅ |
| 10 | 自定义区间：只填一端拦截 toast；367 天拦截（toast 含当前天数）；366 天放行；结束早于开始拦截；预设点击清空自定义输入 | ✅ |
| 11 | 刷新期间过滤控件（下拉/预设/日期/刷新/返回）全部禁用，完成后恢复 | ✅ |
| 12 | 群级推送开关：change → POST `{group_id, enabled}`；模拟失败回滚勾选并 toast | ✅ |
| 13 | 保存设置：扁平 8 键 body、number/boolean 类型正确；范围越界（TopN=0）与非整数（abc）前端拦截不 POST；模拟后端 400 错误文案透出 | ✅ |
| 14 | 恢复默认：confirmDialog 取消不 POST、确认后 POST reset 并按返回 settings 回填 | ✅ |
| 15 | 切 tab 图表实例不销毁（隐藏后 `isDisposed()===false`），返回后同一实例保留、无重复查询（TAB_ENTER_HOOKS 仅 resize） | ✅ |
| 16 | 查询失败路径：toast + 趋势区错误空态 + 排行表收起（harness 早期桥接缺失时实测到该降级形态） | ✅ |

## 6. 降级路径

- **ECharts CDN 全挂**：`ensureECharts` 绝不 reject（双 CDN 顺次尝试）；趋势降级为数据表格，分布保持 CSS 柱图（复用既有 pc-* 组件）。
- **`stats/settings` 加载失败**：仅 toast + 占位文案，不阻断统计主体。
- **群列表加载失败**：保留静态「全部群」选项，toast 提示。
- **过期异步渲染**：`renderSeq` 序号比对，慢响应不覆盖新查询结果（含 ensureECharts 回调）。

## 7. 跨模块契约问题（已解决）

**「全部」预设与 366 天跨度校验冲突**：自查发现 `web_api.api_stats_data` 对显式 start/end 无条件执行「跨度 >366 天 → 400」，无 all-time 豁免；前端按契约发送 `start=2000-01-01`（与 `stats/parser.py:52 _ALL_TIME_START` 同值）会被 400 拒绝，而指令侧 parser.py:151 「全部」关键词不经跨度校验，两端不一致。已上报编排。

**处置（2026-08-04 当日闭环）**：主 agent 按建议修复——`web_api.py:50` 新增 `STATS_ALL_TIME_START = date(2000, 1, 1)` 哨兵常量，`api_stats_data` 跨度校验前对 `start_date == 哨兵值` 豁免并将 label 归一为「全部」（已核实现网代码 web_api.py:1276-1284，豁免面严格限定哨兵值，2000-01-02 起超限仍 400）；`smoke_h_webapi.py` 新增 2 例回归 37/37 通过。前端实现本已按契约就位（「全部」→ `start=2000-01-01`、`end=今日`），**零改动**可用，后端返回的 `label=「全部」` 由 meta 行直接展示。

## 8. 结论

`node --check` 通过；结构与选择器交叉核对 0 缺失；与模块 H 已实现契约逐项对齐（含两处早期偏差——end 含当日语义、save 扁平 body——已在自查中发现并修正）；XSS 基线审计通过；16 项运行时清单全绿；降级路径完备；§7 跨模块契约问题已由主 agent 修复并经现网代码核实。模块 I 前端自查**通过**，无遗留。
