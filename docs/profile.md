# 人物分析

> 返回 [README](../README.md) ｜ 相关：[指令](commands.md) ｜ [代码结构](architecture.md)

## 配置说明（v0.4.0 新增）

> 人物分析全部 19 项配置存入 config.db 新增的 `profile_settings` 表（key/value 形式，列表值 JSON 序列化），
> **不进入 `_conf_schema.json`**（插件原有配置保持不变）。配置仅在 dashboard「人物分析 → 分析设置」tab 修改，
> 改后即时生效，无需重启插件；默认值由 `ConfigManager` 初始化时自动播种。

| 配置键 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| profile_enabled | bool | true | 功能总开关 |
| profile_permission | string | admin | 指令权限：`admin`=仅管理员 / `all`=所有人 |
| profile_output_mode | string | forward | 输出形式：`forward`=合并转发 / `image`=图片（自研模板渲染）/ `text`=纯文本 |
| profile_provider | string（页内下拉） | "" | 分析专用 LLM 提供商（主选）；未配置时使用当前会话模型 |
| profile_fallback_providers | list（JSON 存储） | [] | 备用分析模型列表，主选失败后按序尝试，全部失败回退会话模型 |
| profile_max_count | int | 2000 | 单次分析最大消息条数 |
| profile_max_prompt_chars | int | 60000 | 素材长度预算（送入 LLM 的完整提示词字符上限），超出从最旧消息开始截断，统计仍基于全量 |
| profile_relation_context | bool | true | 关系上下文开关：开启后双向识别目标↔他人的 @/回复互动对象 |
| profile_relation_max_partners | int | 10 | 互动对象上下文最大人数（Top N） |
| profile_dim_habits | bool | true | 分析维度开关：发言习惯 |
| profile_dim_activity | bool | true | 分析维度开关：活动时间（24h / 星期分布图表） |
| profile_dim_personality | bool | true | 分析维度开关：性格（AI 推测） |
| profile_dim_hobbies | bool | true | 分析维度开关：兴趣爱好（AI 推测） |
| profile_dim_relations | bool | true | 分析维度开关：人物关系（AI 推测） |
| profile_user_cooldown | int | 60 | 同一用户两次触发的最小间隔（秒） |
| profile_group_cooldown | int | 30 | 同一群两次分析的最小间隔（秒） |
| profile_feedback_mode | string | reaction | 触发反馈模式：reaction 贴表情 / text 文字提示 / none 关闭 |
| profile_feedback_text | string | 正在生成人物画像，请稍候… | 文字反馈文案（reaction 降级时同用） |
| profile_keep_days | int | 30 | 分析结果 JSON 保留天数，定时任务每日清理过期文件 |

> 图片渲染复用总结功能的 `summary_t2i_*` 共享配置（主题 auto/light/dark、时段、超时、CDN 节点顺序），不新增 profile 专用渲染键。
> 关闭的分析维度不进提示词、不渲染；关闭「人物关系」后不再拉取关系上下文，只基于目标自身消息分析。
