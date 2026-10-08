# v0.3.1 离线测试清单（test_0）

配套离线套件：`tests/v0.3.1/test_v031.py`（24 项已全绿）。
覆盖 v0.3.1 两个新功能：**F1 备用模型降级链**（summarizer）与 **F2 触发反馈**（service）。
单文件自包含，fake 基建自带（不从 test_v03.py import），范式沿用 v0.3：
无 conftest，异步用例经内置 `@async_test`（asyncio.run）驱动；astrbot.* 全量
sys.modules stub 且先于被测包导入。

> 运行方式（插件根目录）：
> `python -m pytest "tests/v0.3.1/test_v031.py" -q`
> 回归基线：`python -m pytest "tests/v0.3/test_v03.py" -q`（119/119 不回归）

---

## 1. 测试范围分析

| 功能 | 被测实现 | 可离线验证 | 离线测不到（转真机） |
|------|----------|-----------|---------------------|
| F1 备用模型降级链 | `summary/summarizer.py`：`_build_configured_chain` / `_resolve_session_provider` / `_call_llm_chain`（经公开接口 `summarize()` 驱动） | 三段调用序与降级条件（异常/空文本）、会话 provider 惰性解析与单次解析、链构建过滤去重、三段皆空异常、整链耗尽抛错类型、provider_id 记录、日志分级 | LLM 供应商真实限流/欠费的触发（用 fake 剧本化模拟） |
| F2 触发反馈 | `summary/service.py`：`_send_feedback` / `_feedback_text` / `_react_to_trigger`（私有方法直测 + handler 端到端） | 三模式分支、reaction 四种失败路径降级文字、未知值静默、反馈失败不冒泡不阻断主流程、`call_action` 参数（action/message_id int 化/code）、校验未通过不发反馈 | 协议端真实贴出 👍 表情的客户端呈现（set_msg_emoji_like 为 NapCat/Lagrange 扩展 action） |

fake 基建一览：

| fake | 职责 |
|------|------|
| `FakeSummaryConfig` | ConfigManager 替身：真实 SUMMARY_DEFAULTS 打底（19 项），typed 读取复用生产侧 `_convert_summary_value`；`typed_overrides` 可直注异常形状（typed 返回非 list） |
| `ChainLLMContext` | Context 替身：`llm_generate` 按 provider id 剧本化（text / empty / exc），记录调用序列；`get_current_chat_provider_id` 可配返回值/异常并记录查询次数 |
| `SimpleLLMContext` | Context 替身（单一文本版），service 端到端用 |
| `FakeEvent` | 事件桩：message_obj.message_id 可配（None/非数字串/数字/数字串）、bot 三态（自动 FakeBotAPI / None / 无 api 属性）、send_fail_on_plain 仅文字发送失败 |
| `FakeBotAPI` | `call_action` 记录调用参数、可配抛异常 |
| `FakeMySQLMgr` / `FakeOneBotFetch` / `FakeStar` | 数据源与渲染桩（端到端组装用） |

---

## 2. 用例明细 — TestProviderFallbackChain（11 例）

驱动方式：构造最小 messages 列表（3 条），经 `Summarizer.summarize()` 公开接口调用，
断言 `context.called_providers()` 调用序列、`context.provider_queries` 会话查询次数、
`result.provider_id` 与异常类型。

| 编号 | 用例 | 输入（配置/剧本） | 预期 | 方法 |
|------|------|------------------|------|------|
| FC-01 | `test_primary_success_no_session_query` | 主选 p-main 成功 | 调用序仅 [p-main]；provider_id==p-main；会话零查询（惰性）；raw 文本透传 | 断言序列/queries/结果 |
| FC-02 | `test_primary_exception_fallback_success` | 主选抛 ValueError → 备用 p-back 成功 | 调用序 [p-main, p-back]；provider_id==p-back；会话零查询；warning 含 provider id + 异常类型原因 | 断言序列 + 日志切片 |
| FC-03 | `test_primary_empty_text_fallback_success` | 主选返回空白 completion_text → 备用成功 | 空文本也触发降级；调用序 [p-main, p-back]；warning 含「返回文本为空」 | 断言序列 + 日志切片 |
| FC-04 | `test_all_configured_fail_session_success` | 主选异常 + 备用空文本 → 会话 chat-prov 成功 | 调用序 [p1, p2, chat-prov]；provider_id==chat-prov；会话仅解析一次；warning 含「降级会话模型」 | 断言序列 + queries==[umo] 一次 |
| FC-05 | `test_empty_chain_raises_provider_error` | 主选空 + 列表 [] + 会话抛异常 | 抛 SummaryProviderError；无任何 LLM 调用；会话解析过一次 | pytest.raises + 调用记录断言 |
| FC-06 | `test_chain_exhausted_raises_last_exception` | 主选 ValueError + 备用自定义 _ProviderDownError + 会话取不到 | 原样抛最后遇到的异常（同一对象 `is` 断言）；error 日志含「降级链耗尽」+ 末次 provider | pytest.raises + excinfo identity + 日志 |
| FC-07 | `test_chain_exhausted_empty_text_raises_runtime_error` | 主选空文本 + 会话取不到 | 抛 RuntimeError（match 空总结文本）；调用序 [p1] | pytest.raises(match) |
| FC-08 | `test_chain_build_filter_dedup` | 主选 "a"；列表 `["", "  ", 42, null, "a", " b ", "b", "a"]`；a/b 均失败；会话取不到 | 实际调用序 == [a, b]：空串/纯空白/非 str 过滤，" b " strip 后与 "b" 保序去重，与主选 "a" 去重 | 以调用序列反推链构建结果 |
| FC-09 | `test_typed_non_list_treated_as_empty` | typed 直返字符串 "not-a-list"（typed_overrides 绕过转换）；会话可用 | 视为空列表不崩；会话立即解析为唯一节点；调用序 [chat-prov] | 断言序列 + queries |
| FC-10 | `test_session_dup_tried_no_recall` | 主选 id == 会话 provider id（chat-prov），主选抛自定义异常 | 不重复调用（调用序仅 [chat-prov]）；会话仅解析一次；链耗尽抛末次异常（原样） | 断言序列 + queries + excinfo |
| FC-11 | `test_session_dup_in_fallback_list_empty_text_exhausted` | 主选异常；备用列表含会话 provider（末次空文本失败） | 备用段只调一次 chat-prov；会话兜底因重复直接放弃 → RuntimeError | 断言序列 [p1, chat-prov] + raises |

---

## 3. 用例明细 — TestFeedback（13 例）

驱动方式：前 9 例直接实例化 `SummaryService` 调用私有 `_send_feedback()`；
后 4 例经 `handle_count_command` 端到端（真实 fetcher/summarizer/formatter/storage +
假 mysql/onebot/star，数据目录重定向 tmp_path）。

| 编号 | 用例 | 输入（配置/事件桩） | 预期 | 方法 |
|------|------|--------------------|------|------|
| FB-01 | `test_mode_none_silent` | mode=none | 无任何发送、无 call_action | 断言 sent==[] 且 api_calls==[] |
| FB-02 | `test_mode_text_sends_configured_text` | mode=text，文案「自定义反馈文案」 | plain_texts == [自定义文案]；不触达协议端 | 断言发送文本 + api_calls==[] |
| FB-03 | `test_mode_text_blank_falls_back_default` | mode=text，文案 "   " | 回退内置默认「📝 收到！正在总结中，请稍候…」 | 断言发送文本 == 默认文案 |
| FB-04 | `test_reaction_success_calls_emoji_like` | mode=reaction，message_id=12345 / "9876" | call_action("set_msg_emoji_like", message_id=int, emoji_id="128077")；无文字发送；数字串 int 化透传 | 断言 api_calls 精确参数 |
| FB-05 | `test_reaction_no_message_id_degrades_text` | message_id=None / message_obj 整体为 None | 降级默认文字；不尝试 call_action | 断言 plain_texts + api_calls==[] |
| FB-06 | `test_reaction_no_bot_degrades_text` | event.bot=None / bot 无 api 属性 | 降级默认文字 | 断言 plain_texts + api_calls==[] |
| FB-07 | `test_reaction_non_numeric_message_id_degrades_text` | message_id="abc" | int 转换失败 → 降级文字；call_action 未被尝试 | 断言 plain_texts + api_calls==[] |
| FB-08 | `test_reaction_call_action_failure_degrades_text` | call_action 抛 RuntimeError（模拟协议端不支持），配置降级文案「降级文案」 | 降级发送配置文案；call_action 已尝试过一次 | 断言 plain_texts + api_calls 长度 |
| FB-09 | `test_unknown_mode_no_send_with_warning` | mode="garbage" | 无发送、不崩；warning 含「未知的 summary_feedback_mode」 | 断言 sent==[] + 日志切片 |
| FB-10 | `test_e2e_count_command_feedback_text_first` | 端到端 mode=text 文案「开工啦」，MySQL 3 行 | plain_texts 首条为反馈文案；总结消息链照常 1 条；LLM 调用 1 次 | handler 全流程断言 |
| FB-11 | `test_e2e_reaction_degrades_then_summary` | 端到端 默认 reaction + message_id=None | 首条为默认反馈文字；总结照常产出 | handler 全流程断言 |
| FB-12 | `test_feedback_send_failure_does_not_block_main` | mode=text，send 对文字发送抛异常（消息链正常） | handler 不冒泡；反馈文字未发出但总结消息链成功；LLM 照常；warning 含「发送提示消息失败」 | send_fail_on_plain 桩 + 全流程断言 |
| FB-13 | `test_invalid_command_no_feedback` | mode=text，指令参数非法 "abc" | 仅用法提示，不发反馈文案；LLM 零调用 | 断言 plain_texts == [用法] |

---

## 4. 执行结果

环境：Python 3.12.6 / pytest 9.0.3 / ruff 0.15.13（check + format 双干净）。

| 用例编号 | 状态 | 备注 |
|----------|------|------|
| FC-01 ~ FC-11 | ✅ 全绿 | 降级链 11 例 |
| FB-01 ~ FB-13 | ✅ 全绿 | 触发反馈 13 例（含 4 例端到端） |
| 合计 | **24/24 passed** | `python -m pytest "tests/v0.3.1/test_v031.py" -q` |
| v0.3 回归基线 | **119/119 passed** | `python -m pytest "tests/v0.3/test_v03.py" -q` 无回归 |

未发现实现缺陷：降级链调用序/惰性解析/去重过滤/抛错类型均与 prd 2.1 一致；
反馈三模式、四种 reaction 降级路径、失败隔离均与 prd 2.2 一致。

---

## 5. 需用户手动测试（离线无法覆盖）

reaction 贴表情走 OneBot v11 扩展 action `set_msg_emoji_like`，仅
NapCat / Lagrange 系协议端支持（go-cqhttp 不支持 → 会自动降级文字，该降级
路径已由 FB-08 离线覆盖），**真实贴表情的客户端呈现**必须真机验收：

- **M-1 reaction 真机贴表情**
  - 【前置】协议端为 NapCat/Lagrange；`summary_feedback_mode=reaction`（默认）；测试群放行。
  - 【操作】群内发送 `/消息总结 50`。
  - 【预期】bot 在触发消息上贴出 👍 表情回应（QQ 客户端可见表情贴），**不**发文字提示；
    随后照常收到合并转发总结。
- **M-2 不支持的协议端自动降级**
  - 【前置】协议端为 go-cqhttp（或任何不支持 set_msg_emoji_like 的实现）。
  - 【操作】群内发送 `/消息总结 50`。
  - 【预期】收到文字反馈（默认「📝 收到！正在总结中，请稍候…」或配置文案）+ 正常总结；
    日志仅 warning（「协议端可能不支持 set_msg_emoji_like」），无 ERROR/Traceback。
- **M-3 text/none 模式体感**
  - 【操作】面板切换 `summary_feedback_mode=text` / `none` 各触发一次。
  - 【预期】text 发配置文案；none 无任何前置反馈，仅最终总结。
- **M-4 降级链真机体感（可选）**
  - 【前置】`summary_provider_id` 指向一个不可用供应商（如停用 key），
    `summary_fallback_providers` 勾选一个可用供应商。
  - 【操作】`/消息总结 50`。
  - 【预期】总结正常产出；历史总结详情中 `provider_id` 为备用供应商 id；
    日志可见一条降级 warning + 无 ERROR（整链未耗尽）。
