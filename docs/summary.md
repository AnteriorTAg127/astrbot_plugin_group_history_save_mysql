# 总结功能

> 返回 [README](../README.md) ｜ 相关：[指令](commands.md) ｜ [代码结构](architecture.md)

## 配置说明（v0.3 新增）

> 总结功能全部 24 项配置存入 config.db 新增的 `summary_settings` 表（key/value 形式，列表值 JSON 序列化），
> **不进入 `_conf_schema.json`**（插件原有配置保持不变）。配置仅在 dashboard「总结设置」tab 修改，
> 改后即时生效，无需重启插件；默认值由 `ConfigManager` 初始化时自动播种。

| 配置键 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| summary_enabled | bool | true | 功能总开关（关闭后指令回复「功能未启用」） |
| summary_whitelist_mode | string | whitelist | 群白名单模式：`whitelist`=仅白名单群可用；`all`=所有群可用 |
| summary_group_whitelist | list（JSON 存储） | [] | 白名单群号列表（字符串形式），mode=all 时忽略 |
| summary_user_cooldown | int | 60 | 同一用户两次触发的最小间隔（秒） |
| summary_group_cooldown | int | 120 | 同一群两次总结的最小间隔（秒） |
| summary_max_count | int | 1000 | `/消息总结` 数量参数上限，超出拒绝 |
| summary_max_hours | int | 168 | `/消息总结时间` 时间跨度上限（小时，即 7 天），超出拒绝 |
| summary_min_mysql_ratio | float | 0.8 | 数量模式补齐阈值：MySQL 实得/请求 < 此值才拉协议端 |
| summary_gap_tolerance_minutes | int | 30 | 时间模式缺口容忍：MySQL 最早消息晚于窗口起点 + 此值才拉协议端 |
| summary_onebot_max_fetch | int | 200 | 单次从协议端最多拉取条数（协议端自身上限内） |
| summary_provider_id | string（页内下拉） | "" | 总结专用 LLM 提供商（主选）；失败后按序尝试 `summary_fallback_providers` 备用列表，全部失败兜底该群会话当前 provider |
| summary_prompt | text | （内置 4 板块默认模板） | LLM 总结提示词，支持占位符 `{stats}` `{messages}` `{time_range}` `{group_id}` `{format_constraint}` |
| summary_output_mode | string | forward | 输出形式：`forward`=合并转发（剥离 Markdown）；`image`=文转图（保留 Markdown） |
| summary_rank_top_n | int | 5 | 活跃排行展示条数 |
| summary_max_prompt_chars | int | 60000 | 素材长度预算（送入 LLM 的完整提示词字符上限），超出从最旧消息开始截断，统计仍基于全量 |
| summary_retention_days | int | 30 | 总结 JSON 保留天数，定时任务每日清理过期文件 |
| summary_fallback_providers | list（JSON 存储） | [] | 备用总结模型列表，主选失败后按序尝试，全部失败回退会话模型 |
| summary_feedback_mode | string | reaction | 触发反馈模式：reaction 贴表情 / text 文字提示 / none 关闭 |
| summary_feedback_text | string | 📝 收到！正在总结中，请稍候… | 文字反馈文案（reaction 降级时同用） |
| summary_t2i_theme_mode | string | auto | 图片报告主题：`auto`=按时段自动 / `light`=强制浅色 / `dark`=强制深色 |
| summary_t2i_dark_start | string | 22:00 | 深色时段起点（HH:MM，服务器本地时间） |
| summary_t2i_light_start | string | 08:00 | 浅色时段起点（HH:MM）；默认 08:00–22:00 浅色、22:00–08:00 深色 |
| summary_t2i_timeout | int | 30 | 图片单轮渲染超时（秒，5–300）；失败自动以双倍超时重试第二轮 |
| summary_t2i_cdn_providers | list（JSON 存储） | ["bootcdn","npmmirror","staticfile","jsdelivr","unpkg"] | 图片模板加载 Markdown/图表脚本的 CDN 尝试顺序，国内镜像优先，单节点失败自动切换 |

占位符说明：`{stats}` 统计块、`{messages}` 格式化消息列表（每行 `[时间] 昵称: 内容`）、`{time_range}` 时间范围描述、`{group_id}` 群号、`{format_constraint}` 按输出模式注入的格式约束（合并转发=禁用 Markdown / 文转图=可用 Markdown）。

## 工作原理

### 混合数据源

1. **MySQL 优先**：按群号 + 范围查询本插件 MySQL `chat_history` 表
2. **协议端补齐**：仅在 MySQL 数据不足时，经 OneBot v11 `get_group_msg_history` 在线补齐——数量模式按 `summary_min_mysql_ratio` 判定；时间模式按窗口内条数为 0 或 MySQL 最早消息晚于窗口起点 + `summary_gap_tolerance_minutes` 判定
3. **去重合并**：两源按 `message_id` 去重（无 message_id 时退化为「秒级时间戳 + 发送者 + 内容前 32 字符」），按时间升序合并
4. **不回填**：OneBot 拉到的消息仅本次总结使用，不写入 `chat_history`
5. **降级容错**：任一数据源失败不阻断流程，以可用数据继续；两源皆空回复「没有可总结的消息」提示

> 协议端只能拉取其缓存范围内的近期消息，更久的历史只能依赖 MySQL。

### 总结内容与输出

- **规则统计块**：消息总数、参与者人数、时间跨度、发言条数排行 Top N（`summary_rank_top_n`）
- **LLM 四板块摘要**：📢 重要通知与结论 / 💬 讨论要点·争议 / 🎉 有趣片段 / ✅ TODO·待跟进，每个条目含参与者与大致时间
- **输出形式**（`summary_output_mode`）：
  - `forward` 合并转发：1 个统计节点 + 4 个板块节点，剥离 Markdown 标记（提示词约束 + 发送前程序化剥离双重保障）
  - `image` 文转图：保留 Markdown（含表格），经自研报告模板渲染成图片发送（多 CDN 容灾、日夜双主题、排行柱状图，详见「图片渲染」配置组）

### 权限、限流与素材过滤

- 独立群白名单（`summary_whitelist_mode` + `summary_group_whitelist`），与录制白名单（group_config 表）互不干扰；白名单群内任何成员可用
- 用户/群双冷却（`summary_user_cooldown` / `summary_group_cooldown`），超限回复剩余等待秒数；冷却状态存内存，重启清零
- 非文本消息（图片/语音/视频等）完全忽略：不占位、不统计、不送 LLM
- 默认过滤 bot 自身消息（硬编码）；每群可配置忽略发送者（dashboard「忽略管理」，存 config.db `group_ignore_senders` 表）

### 结果持久化

- JSON 存储：`data/plugin_data/astrbot_plugin_group_history_save_mysql/summaries/<群号>/<时间戳>.json`，内容含统计块、LLM 摘要原文、元数据（群号、时间/条数范围、数据源构成、生成时间）
- 保留天数可配（`summary_retention_days`，默认 30 天），定时任务每天清理一次过期文件，随插件生命周期启停
- 可在 dashboard「历史总结」tab 按群浏览与查看详情
