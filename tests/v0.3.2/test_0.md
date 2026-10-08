# v0.3.2 离线测试清单（test_0）

配套离线套件：`tests/v0.3.2/test_v032.py`（52 项已全绿）。
覆盖 v0.3.2「自研 T2I 报告模板」渲染增强的全部可离线验证行为：
**_markdown_to_html 的 GFM 表格扩展**（formatter）、**T2IRenderer 渲染核心**
（主题判定 / CDN 节点序 / 两轮渲染 + 魔数校验 / 模板数据契约，t2i_render）、
**image 新兜底链路**（自研模板 → text_to_image → 纯文本，formatter）、
**LLM 表格约束放开**（summarizer）、**配置 19 → 24 项**（db_config）。
单文件自包含，fake 基建自带（不从 test_v03.py / test_v031.py import），
范式沿用 v0.3.1：无 conftest，异步用例经内置 `@async_test`（asyncio.run）驱动；
astrbot.* 全量 sys.modules stub 且先于被测包导入。

> 运行方式（插件根目录）：
> `python -m pytest "tests/v0.3.2/test_v032.py" -q`
> 回归基线（三文件合跑）：
> `python -m pytest "tests/v0.3/test_v03.py" "tests/v0.3.1/test_v031.py" "tests/v0.3.2/test_v032.py" -q`
> （195/195 全绿；v0.3 基线按 v0.3.2 新行为修正 4 个断言后 119/119 不回归，
> v0.3.1 基线 24/24 零改动不回归）

---

## 1. 测试范围分析

| 功能 | 被测实现 | 可离线验证 | 离线测不到（转真机） |
|------|----------|-----------|---------------------|
| GFM 表格扩展 | `summary/formatter.py`：`_markdown_to_html`（表头/分隔行/表体消费、单元格行内转换、html.escape 防注入、冒号对齐、非表格输入回归） | 标准表格结构（table/thead/tbody/th/td + 列数）、`**粗体**`/`` `代码` ``/`*斜体*` 单元格转换、`<script>` 转义、`:---`/`---:` 分隔行识别、无分隔行不误判表格、标题/列表/引用/代码块等非表格路径行为不变 | — |
| 主题判定（auto/light/dark） | `summary/t2i_render.py`：`_theme_for` / `_hhmm_to_minutes` 纯函数 + `T2IRenderer._resolve_theme`（配置解析 + 服务器本地时间，本地时间经模块 `datetime` 打桩固定） | light/dark 强制模式直通（且不读时段配置）、auto 默认窗口 `[08:00, 22:00)` 边界判定、跨午夜配置自洽、light==dark 恒深色、非法 HH:MM 逐项回退默认 + warning、非法 mode 回退 auto | 宿主机真实时区对 `datetime.now()` 的影响（用打桩模拟） |
| CDN 节点序解析 | `summary/t2i_render.py`：`_resolve_cdn_providers` | 默认 5 节点有序、未知 key 过滤（strip/lower 归一 + 空项忽略）且合法序保留、空列表/非 list/读取异常回退默认、保序去重、全未知回退默认 | 模板内联加载器对真实 CDN 的 8s 超时切换（浏览器端逻辑，见手动 M-2） |
| 渲染超时解析 | `summary/t2i_render.py`：`_resolve_timeout` | 合法值（5/30/300）直通、越界（4/301/999）回退 30 + warning、非法值/读取异常回退 30 | — |
| 魔数校验 | `summary/t2i_render.py`：`_validate_image`（静态方法） | PNG/JPEG 头 bytes（含 bytearray）、错误 HTML 页字节判假、http(s) URL 放行、已存在文件按内容验头、不存在文件/None/空值/未知类型判假 | — |
| 模板数据契约 | `summary/t2i_render.py`：`_build_template_data` | 9 契约键齐全、stats/sources/bars/sections/cdn 各字段按约定组装（sources 展示名映射、bars percent 与 bars_max、sections raw+fallback_html 含表格转换、cdn libs marked/echarts）、stats=None 不抛、top_senders 脏条目（缺字段/None/条数非数字）防御式跳过与昵称回落 | — |
| 两轮渲染 + render 契约 | `summary/t2i_render.py`：`render` / `_render_two_rounds` / `_to_chain`（假 star 剧本化 `html_render` 逐轮返回值/异常并记录 options） | R1 抛异常 + R2 合法 PNG → 图片链（bytes 落临时文件 fromFileSystem）、R2 options 超时 = R1 两倍且 jpeg q80、return_url=False、R1 HTML 错误页 + R2 JPEG 成功（.jpg 后缀）、两轮皆错误页 → None + 错误页 `<title>` 入日志、全抛异常 → None（两轮均尝试）、模板缺失 → None 且不触达渲染、自定义超时（7s）→ options.timeout=7000 且 R1 成功即止 | Playwright 真实截图耗时/超时、T2I 服务真实错误页（用字节/异常剧本模拟） |
| image 新兜底链路 | `summary/formatter.py`：`SummaryFormatter.__init__` / `_render_image` / `_image_chain` | config_mgr 注入开关（注入 → t2i 为 T2IRenderer；缺省 → None，签名兼容 v0.3.1）、自研模板两轮全失败 → 降级 text_to_image（本地路径 → fromFileSystem，完整 Markdown 送入）、未注入 → 跳过自研模板级直走 text_to_image（URL → fromURL）、两级全失败 → 剥 Markdown 纯文本兜底链（render 绝不抛）、forward 模式零改动（单 Nodes 包裹 + 统计/板块节点）、`_IMAGE_TMPL` 常量已删除 | — |
| LLM 表格约束 | `summary/summarizer.py`：`_FORMAT_CONSTRAINT_IMAGE` / `_FORMAT_CONSTRAINT_FORWARD` | image 约束含「表格」且保留 Markdown 许可；forward 约束不含「表格」且仍禁用全部 Markdown | LLM 是否真的更爱输出表格（模型行为，真机体感） |
| 配置 19 → 24 项 | `db_config.py`：`SUMMARY_DEFAULTS` / `SUMMARY_TYPES` + 真实 aiosqlite 冒烟 | 键集合一致且各 24 项、5 新键默认值（auto/22:00/08:00/30/5 节点 JSON）与类型声明（str×3/int/list）、initialize 后 typed 读取 5 新键默认值正确 | — |

fake 基建一览：

| fake | 职责 |
|------|------|
| `FakeT2IConfig` | ConfigManager 替身（T2I 渲染配置读取路径）：真实 SUMMARY_DEFAULTS（24 项，含 5 新键）打底，typed 读取复用生产侧 `_convert_summary_value`；`typed_overrides` 可直注 typed 返回值（Exception 实例则抛出，模拟单键读取失败）；`exc` 使全部读取抛异常；记录 `calls` 供配置读取序断言。签名为 db_config 现行单参 `(key)`，T2IRenderer 签名探测应命中单参分支 |
| `FakeRenderStar` | Star 替身：`html_render` 按剧本逐轮返回值（Exception 实例则抛出）并记录每次 tmpl/data/return_url/options；`text_to_image` 可配成功返回值或异常（formatter 降级链路用） |
| `_StubLogger` | 带记录能力的 logger 替身（info/warning/error/debug 分桶），供配置回退 warning / 魔数校验 warning / 错误页 `<title>` 日志断言 |
| `_StubImage` | Image 替身：含 `fromURL` / `fromFileSystem` 类方法（v0.3.2 两轮渲染产物落盘/URL 两路） |
| `_FixedDateTime`（`_patch_now`） | 以真实 datetime 子类替换 t2i_render 模块内 `datetime`，仅固定 `now()` 返回值（strptime 继承真实实现，HH:MM 解析行为不受影响） |
| 魔数夹具字节 | `PNG_BYTES` / `JPEG_BYTES`（合法头）与 `HTML_PAGE_BYTES`（T2I 错误页风格，含 `<title>502 Bad Gateway</title>`） |

---

## 2. 用例明细 — TestMarkdownTable（7 例）

驱动方式：直接调用模块级纯函数 `_markdown_to_html`，断言输出 HTML 片段。

| 编号 | 用例 | 输入 | 预期 | 方法 |
|------|------|------|------|------|
| MT-01 | `test_standard_table_structure` | 表头 + `| --- | --- |` + 2 行表体 | `<table>/<thead>/<tbody>` 齐全；`<th>` ×2、`<td>` ×4；表头与表体行逐字匹配 | 结构计数 + 片段断言 |
| MT-02 | `test_cell_inline_bold_and_code` | 单元格含 `**粗体**` / `` `代码` `` / `*斜体*` | `<th><strong>粗体</strong></th>` / `<th><code>代码</code></th>` / `<td><em>斜体</em></td>` | 片段断言 |
| MT-03 | `test_script_cell_escaped` | 单元格含 `<script>alert(1)</script>` | 输出无真实 `<script>`，含 `&lt;script&gt;`（防注入） | 正反断言 |
| MT-04 | `test_colon_alignment_separator` | 分隔行 `|:---|---:|` | 正常解析为表格（对齐冒号不做渲染） | 结构断言 |
| MT-05 | `test_single_pipe_row_not_table` | 仅一行 `| a | b |`（无分隔行） | 不当作表格 → `<p>| a | b |</p>`（防误判回归） | 反断言 `<table>` + 段落断言 |
| MT-06 | `test_pipe_rows_without_separator_not_table` | 连续两管道行，第二行非分隔符 | 仍不作表格，逐行段落输出 | 反断言 + 双段落断言 |
| MT-07 | `test_non_table_regression` | 标题/无序列表/有序列表/引用/围栏代码块/行内代码与粗斜体 | 行为与 v0.3 完全一致；代码块内容同样转义（`<b>` → `&lt;b&gt;`） | 逐形态断言 |

---

## 3. 用例明细 — TestThemeResolve（10 例）

驱动方式：前 5 例直测纯函数 `_theme_for` / `_hhmm_to_minutes`；后 5 例经
`T2IRenderer._resolve_theme`（`_patch_now` 把模块内 `datetime.now()` 固定为
指定本地时刻，配置经 `FakeT2IConfig.typed_overrides` 注入）。

| 编号 | 用例 | 输入 | 预期 | 方法 |
|------|------|------|------|------|
| TH-01 | `test_hhmm_to_minutes_valid` | "08:00"/"22:00"/"00:00"/"23:59"/" 08:00 "/"8:0" | 480/1320/0/1439/480/480（strip 生效；"8:0" 为 strptime 合法宽松形式，按 08:00 接受——实现行为固化） | 纯函数直断 |
| TH-02 | `test_hhmm_to_minutes_invalid` | "abc"/"25:00"/"12:60"/""/None | 均返回 None | 纯函数直断 |
| TH-03 | `test_theme_for_default_window` | 窗口 08:00–22:00，时刻 12:00/23:00/07:00/08:00/22:00 | light/dark/dark/light/dark（左闭右开边界） | 纯函数直断 |
| TH-04 | `test_theme_for_cross_midnight` | light=22:00 dark=08:00，时刻 23:00/09:00/07:00/12:00 | light/dark/light/dark（区间环绕午夜） | 纯函数直断 |
| TH-05 | `test_theme_for_equal_starts_always_dark` | light==dark==08:00，任意时刻 | 恒 dark（区间为空） | 纯函数直断 |
| TH-06 | `test_mode_light_dark_passthrough` | mode=light/dark，本地时间打桩 23:00 | 强制模式直通；light 分支仅读 `summary_t2i_theme_mode` 一键（短路，calls 断言） | 实例级 + 读取序断言 |
| TH-07 | `test_mode_auto_default_window_by_local_time` | mode=auto 默认窗口，打桩 12/23/7/8/22 点 | light/dark/dark/light/dark | 打桩 now + 实例级 |
| TH-08 | `test_mode_auto_cross_midnight_config` | dark_start=08:00 light_start=22:00，打桩 23/9 点 | light/dark | 打桩 now + 实例级 |
| TH-09 | `test_invalid_hhmm_falls_back_defaults` | dark_start="25:00" light_start="abc"，打桩 12:00 | 逐项回退默认 22:00/08:00 → light；两条「非法」warning 均含对应时段名 | warning 切片断言 |
| TH-10 | `test_invalid_mode_falls_back_auto` | mode="xxx"，打桩 23:00 | 回退 auto → dark；warning 含「主题模式配置」+「回退 auto」 | warning 切片断言 |

---

## 4. 用例明细 — TestCdnProviders（6 例）

| 编号 | 用例 | 输入 | 预期 | 方法 |
|------|------|------|------|------|
| CDN-01 | `test_default_order` | 默认配置 | `["bootcdn","npmmirror","staticfile","jsdelivr","unpkg"]` 有序 | 全等断言 |
| CDN-02 | `test_unknown_filtered_order_kept` | `["jsdelivr","fakecdn"," BootCDN ",""]` | `["jsdelivr","bootcdn"]`（未知过滤 + strip/lower 归一 + 空项忽略 + 保序） | 全等断言 |
| CDN-03 | `test_empty_or_non_list_falls_back` | `[]` / `"not-a-list"` | 均回退默认序 | 两子例全等断言 |
| CDN-04 | `test_read_exception_falls_back` | typed 读取抛 RuntimeError | 回退默认序 + warning 含「读取 CDN 节点配置失败」 | warning 切片断言 |
| CDN-05 | `test_dedup` | `["unpkg","bootcdn","unpkg"]` | `["unpkg","bootcdn"]`（保序去重） | 全等断言 |
| CDN-06 | `test_all_unknown_falls_back` | `["x","y"]`（过滤后为空） | 回退默认序 | 全等断言 |

---

## 5. 用例明细 — TestTimeout（3 例）

| 编号 | 用例 | 输入 | 预期 | 方法 |
|------|------|------|------|------|
| TO-01 | `test_valid_values_passthrough` | 5 / 30 / 300（秒） | 原样返回 | 逐值直断 |
| TO-02 | `test_out_of_range_falls_back_30` | 4 / 301 / 999 | 均回退 30；warning 含「越界」 | 逐值直断 + warning 切片 |
| TO-03 | `test_invalid_or_exception_falls_back_30` | "abc" / 读取抛 RuntimeError | 均回退 30；warning 含「读取 T2I 渲染超时配置失败」 | 直断 + warning 切片 |

---

## 6. 用例明细 — TestValidateImage（6 例）

驱动方式：静态方法 `T2IRenderer._validate_image` 直测；文件型用例借 `tmp_path`
写入真实文件。

| 编号 | 用例 | 输入 | 预期 | 方法 |
|------|------|------|------|------|
| VI-01 | `test_png_bytes_valid` | PNG 头 bytes / bytearray | True | 直断 |
| VI-02 | `test_jpeg_bytes_valid` | JPEG 头 bytes | True | 直断 |
| VI-03 | `test_html_bytes_invalid` | T2I 错误页 HTML 字节 | False（防止错误页当图片） | 直断 |
| VI-04 | `test_http_urls_valid` | `https://` / `http://` URL | True（兼容直接返 URL 部署形态） | 直断 |
| VI-05 | `test_file_paths` | 临时 PNG 文件 / 临时 HTML 文件 / 不存在路径 | True / False / False（按内容验头） | tmp_path 写文件 + 直断 |
| VI-06 | `test_empty_and_unrecognized_invalid` | None / b"" / "" / 纯空白 / int | 均 False | 直断 |

---

## 7. 用例明细 — TestBuildTemplateData（3 例）

驱动方式：构造 `SummaryResult` 后直调 `_build_template_data(result, theme, providers)`。

| 编号 | 用例 | 输入 | 预期 | 方法 |
|------|------|------|------|------|
| TD-01 | `test_contract_keys_complete` | 完整 stats（2 数据源、2 发言人 3/1）+ 含粗体与 GFM 表格的 2 板块 | 9 契约键齐全；meta 4 键；stats.sources 展示名映射（MySQL/OneBot）；bars percent 100.0/33.3 + bars_max=3；sections 各项 title/raw/fallback_html（含 `<strong>` 与 `<table>`）；cdn.providers 透传 + libs 含 marked/echarts | 键集合 + 逐字段断言 |
| TD-02 | `test_stats_none_safe` | stats=None、sources=None | 不抛；total/participants=0、sources=[]、bars=[]、bars_max=0、time_start="未知"、标题照常 | 防御式取值断言 |
| TD-03 | `test_dirty_top_senders_skipped` | top_senders 混入长度不足元组 / None / 条数非数字；昵称为空项 | 脏条目跳过；昵称为空回落 sender_id；bars==["Alice","222"]；percent 66.7 | 结果反推断言 |

---

## 8. 用例明细 — TestRenderChain（6 例）

驱动方式：`FakeRenderStar.html_script` 剧本化 `html_render` 逐轮返回值
（Exception 实例则抛出）并记录 options；`T2IRenderer.render` 全流程。

| 编号 | 用例 | 剧本 | 预期 | 方法 |
|------|------|------|------|------|
| RC-01 | `test_r1_exception_r2_png_success_with_doubled_timeout` | R1 抛异常，R2 返合法 PNG 字节 | 图片链（bytes 落临时文件 fromFileSystem，.png 且存在）；两轮调用均 return_url=False；data 契约 9 键；R1 options `{timeout:30000,type:png,full_page:True}`，R2 `{timeout:60000,type:jpeg,quality:80,full_page:True}`（R2=2×R1） | options 逐键断言 |
| RC-02 | `test_r1_html_page_r2_jpeg_success` | R1 返 HTML 错误页字节，R2 返合法 JPEG | 成功；产物 .jpg 后缀（按魔数取后缀）；两轮均尝试 | 后缀 + 调用次数断言 |
| RC-03 | `test_both_rounds_bad_content_returns_none` | 两轮均返错误页字节 | None（不抛）；warning 记出错误页 `<title>`（502 Bad Gateway）+「两轮渲染均失败」 | warning 切片断言 |
| RC-04 | `test_html_render_all_exceptions_returns_none` | 两轮均抛异常 | None（绝不向上抛）；两轮均尝试 | 调用次数断言 |
| RC-05 | `test_template_missing_returns_none` | `_load_template` 桩返回 None | None 且不触达 html_render（html_calls 为空） | 调用记录断言 |
| RC-06 | `test_custom_timeout_propagates_to_options` | timeout=7，R1 返合法 PNG | R1 options.timeout=7000；R1 成功即止（仅一轮） | options 断言 + 调用次数 |

---

## 9. 用例明细 — TestFormatterImageChain（6 例）

驱动方式：`SummaryFormatter(star[, config_mgr])` + `render(result, mode)` 全链路。

| 编号 | 用例 | 输入（剧本） | 预期 | 方法 |
|------|------|-------------|------|------|
| FI-01 | `test_t2i_created_only_with_config_mgr` | 注入 / 不注入 config_mgr | 注入 → `t2i` 为 T2IRenderer；不注入 → None（签名向后兼容 v0.3.1） | 类型断言 |
| FI-02 | `test_t2i_fail_degrades_to_text_to_image_local_path` | 自研模板两轮全抛异常；text_to_image 返本地路径 `/local/fallback.png` | Image 链 file==该路径（fromFileSystem）；html_calls==2、t2i_calls==1；完整 Markdown（`# 群聊总结`）送入 text_to_image | 全链路断言 |
| FI-03 | `test_no_config_mgr_direct_text_to_image` | 不注入；text_to_image 返 URL | 跳过自研模板级（html_calls 为空）；Image.fromURL | 全链路断言 |
| FI-04 | `test_all_render_fail_plain_fallback` | 两轮全抛 + text_to_image 抛异常 | 纯文本兜底链（Plain 非空、含「群聊总结」、Markdown 已剥）；render 绝不抛 | 全链路断言 |
| FI-05 | `test_forward_mode_unchanged` | mode=forward | 单 Nodes 包裹 3 节点（1 统计 + 2 板块）；统计文本含总数/数据源构成；板块节点含内容（forward 零改动） | 结构断言 |
| FI-06 | `test_image_tmpl_constant_removed` | — | formatter 模块无 `_IMAGE_TMPL`（已被自研模板取代删除） | getattr 断言 |

---

## 10. 用例明细 — TestFormatConstraint（2 例）

| 编号 | 用例 | 预期 | 方法 |
|------|------|------|------|
| FC-01 | `test_image_constraint_allows_table` | `_FORMAT_CONSTRAINT_IMAGE` 含「表格」/「Markdown 表格」且保留 v0.3 的「可以使用 Markdown 格式」 | 常量断言 |
| FC-02 | `test_forward_constraint_no_table` | `_FORMAT_CONSTRAINT_FORWARD` 不含「表格」，仍含「不要使用任何 Markdown 格式」（forward 约束不变） | 常量断言 |

---

## 11. 用例明细 — TestDbConfigV032（3 例）

| 编号 | 用例 | 预期 | 方法 |
|------|------|------|------|
| DB-01 | `test_keys_consistent_and_24` | SUMMARY_DEFAULTS 与 SUMMARY_TYPES 键集合一致且各 24 项 | 集合/长度断言 |
| DB-02 | `test_new_keys_defaults_and_types` | 5 新键默认值：`summary_t2i_theme_mode=="auto"`、`dark_start=="22:00"`、`light_start=="08:00"`、`timeout=="30"`、`cdn_providers`（JSON）==5 节点默认序；类型声明 str×3 / int / list | 逐键断言 |
| DB-03 | `test_real_sqlite_new_keys_typed`（真实 aiosqlite 冒烟，aiosqlite 缺失时 skipif） | initialize 后 `get_summary_setting_typed` 读 5 新键：auto / 22:00 / 08:00 / 30（int）/ 5 节点 list | tmp_path 真实库 + typed 读取断言 |

---

## 12. v0.3 基线断言修正（4 项，仅改断言不改被测代码）

v0.3.2 配置扩至 24 项 + image 渲染链路重构后，`tests/v0.3/test_v03.py`
有 4 个基线用例因断言旧行为而失败，按新行为修正如下：

| 用例 | 原断言 | 修正为 | 原因 |
|------|--------|--------|------|
| `TestDbConfigSummary::test_defaults_seeded_15` | `len(settings) == 19` | `== 24`（方法名保留历史命名，docstring 注明演进） | 新增 5 项 T2I 渲染配置 |
| `TestDbConfigSummary::test_reset_all` | `len(result) == 19` | `== 24` | 同上 |
| `TestWebApiSummary::test_settings_get_three_keys` | settings/defaults/types 各 `== 19` | 各 `== 24` | web_api 遍历 SUMMARY_DEFAULTS，后端零改动随配置层扩容 |
| `TestFormatterRender::test_image_mode_t2i_fail_html_fallback_local_path` | t2i 失败 → 单轮 html_render 兜底（`html_calls==1`，本地路径 fromFileSystem） | 重写为 v0.3.2 链路：注入 config_mgr 启用自研模板级 → 两轮 html_render 全失败（`html_calls==2`）→ `T2IRenderer.render` 返回 None → 降级 `text_to_image`（`t2i_calls==1`）→ 本地路径 Image.fromFileSystem；「text_to_image 也失败 → 纯文本兜底」由本类既有 `test_image_mode_all_fail_plain_fallback` 与本套件 FI-04 覆盖 | `_IMAGE_TMPL` 单轮级已删除，html_render 移入 T2IRenderer 两轮渲染 |

同批顺手修复 test_v03.py 3 个**先前遗留**的 ruff check 错误（I001 import 排序、
F401 未用导入 `summarizer_mod`、C408 `dict()` 字面量），与本次断言修正无关，
仅为使「经手的测试文件 ruff 双干净」达标。

---

## 13. 执行结果

环境：Python 3.12.6 / pytest 9.0.3 / ruff 0.15.13。
ruff：test_v032.py / test_v03.py / test_v031.py 三文件 check + format **双干净**。

| 套件 | 状态 | 备注 |
|------|------|------|
| MT-01 ~ MT-07 | ✅ 全绿 | GFM 表格扩展 7 例 |
| TH-01 ~ TH-10 | ✅ 全绿 | 主题判定 10 例（纯函数 5 + 实例级打桩 5） |
| CDN-01 ~ CDN-06 | ✅ 全绿 | CDN 节点序 6 例 |
| TO-01 ~ TO-03 | ✅ 全绿 | 渲染超时 3 例 |
| VI-01 ~ VI-06 | ✅ 全绿 | 魔数校验 6 例 |
| TD-01 ~ TD-03 | ✅ 全绿 | 模板数据契约 3 例 |
| RC-01 ~ RC-06 | ✅ 全绿 | 两轮渲染 + render 契约 6 例 |
| FI-01 ~ FI-06 | ✅ 全绿 | image 新兜底链路 6 例 |
| FC-01 ~ FC-02 | ✅ 全绿 | LLM 表格约束 2 例 |
| DB-01 ~ DB-03 | ✅ 全绿 | 配置 24 项 3 例（含真实 aiosqlite 冒烟） |
| test_v032.py 合计 | **52/52 passed** | `python -m pytest "tests/v0.3.2/test_v032.py" -q` |
| v0.3 回归基线 | **119/119 passed** | 修正 4 个断言后无其他回归 |
| v0.3.1 回归基线 | **24/24 passed** | 零改动不回归 |
| 三文件合跑 | **195/195 passed** | `python -m pytest "tests/v0.3/test_v03.py" "tests/v0.3.1/test_v031.py" "tests/v0.3.2/test_v032.py" -q` |

未发现实现缺陷：主题判定边界/跨午夜语义、CDN 过滤回退、两轮 options 联动
（T 与 2T、jpeg q80）、魔数校验、模板数据契约、新兜底链路三级降级均与
prd 3.1–3.6 及分工模块 B 契约一致。

---

## 14. 需用户手动测试（离线无法覆盖）

自研模板的真实截图渲染、CDN 加载器（浏览器端 JS 逻辑）、ECharts 初始化、
移动端排版均依赖真实 T2I 服务 + 真实 CDN + 真实 QQ 客户端，离线仅能剧本化
模拟 Python 侧行为，以下必须真机验收：

- **M-1 自研模板真机渲染（浅色 / 深色）**
  - 【前置】T2I 渲染服务可用；`summary_output_mode=image`；`summary_t2i_theme_mode=auto`（默认）。
  - 【操作】白天时段（08:00–22:00）群内 `/消息总结 50` 触发一次；改 `summary_t2i_theme_mode=dark` 再触发一次（或等到 22:00 后）。
  - 【预期】两次均收到**图片**总结（非合并转发、非纯文本）；浅色版白底深色字、深色版深底浅字，风格与 Web 管理面板一致（主色蓝、卡片圆角阴影）；图片内板块 Markdown（粗体/列表/表格）渲染正常。
- **M-2 CDN 全挂降级**
  - 【前置】渲染宿主机断外网（或防火墙屏蔽 5 个 CDN 域名）；image 模式。
  - 【操作】`/消息总结 50`。
  - 【预期】图片**仍能产出**（截图超时需覆盖加载失败耗时，默认 30s 通常足够）；板块内容为服务端预转换 HTML（含表格）；发言人排行为纯 CSS 横条而非 ECharts 交互图；日志可见 CDN 加载失败相关告警，无 ERROR/Traceback。
- **M-3 ECharts 柱状图 / 纯 CSS 柱图**
  - 【前置】外网可达（CDN 正常）；测试群发言人数 ≥2。
  - 【操作】`/消息总结 100`。
  - 【预期】发言人排行区块为横向条形图（y 轴人名 / x 轴条数 / 柱顶数值），配色随主题；断网对照（M-2）时为「昵称 + 渐变横条 + 数值」纯 CSS 形态，百分比 = count/最大值。
- **M-4 移动端字号与可读性**
  - 【前置】M-1 产出的图片。
  - 【操作】手机 QQ 中点开图片查看并双指放大。
  - 【预期】860px 画布放大后清晰；正文/表格/图表标签字号可辨（基础 16px、表格 ≥15px、图表标签 ≥14px）；表格长内容自动换行不溢出屏幕。
- **M-5 渲染超时两轮策略**
  - 【前置】`summary_t2i_timeout` 先设为较小值（如 5）；构造一份内容极多的总结（`/消息总结 500`，消息量大的群）。
  - 【操作】触发总结，观察日志 `[HistorySummary] T2I 第 1/2 轮渲染`。
  - 【预期】R1 png（超时 5000ms）超时/失败后自动进 R2 jpeg（超时 10000ms）；最终产出图片或（两轮皆败时）降级 text_to_image 图片/纯文本；`timeout` 改回 30 后 R1 通常一轮成功。
- **M-6 Web「图片渲染」分组 5 项保存与回显**
  - 【前置】Dashboard 打开「消息总结 → 总结设置」。
  - 【操作】在「图片渲染」分组修改 5 项（主题模式下拉 / 深色起点 / 浅色起点 / 渲染超时 / CDN 顺序文本框如 `unpkg, bootcdn`）→ 保存 → 刷新页面；再点「恢复默认」。
  - 【预期】保存成功且刷新后 5 项**回显一致**（CDN 文本框逗号拼接回读）；恢复默认后回到 auto/22:00/08:00/30/默认 5 节点；非法输入（超时 999、起点 25:00）保存不崩，渲染侧按越界/非法回退默认并有 warning（已由 TO-02 / TH-09 离线覆盖解析逻辑，此处验前端闭环）。
