# 代码结构

> 返回 [README](../README.md) ｜ 相关：[总结](summary.md) ｜ [人物分析](profile.md) ｜ [数据分析](stats.md) ｜ [接口契约](contracts/)

新功能代码全部位于 `core/` 子包，指令在 `main.py` 注册并薄封装异步委托子包处理函数；无新增 pip 依赖。

**v0.6.0 模块化结构**：`main.py` 只保留与框架的交互（指令注册、事件委托、初始化/退出），
数据库访问、配置管理、Web API 与保存逻辑全部按子功能拆分到 `core/` 下独立文件：

```
main.py                          # 入口：仅框架交互（@register、指令 handler、事件委托）
core/
├── bootstrap.py                 # 实例装配 + 后台 MySQL 初始化 + 生命周期停机（v0.8.1）
├── commands.py                  # 指令体执行器：history_*/补库（v0.8.1 自 main.py 迁出）
├── db_mysql/                    # MySQL 操作包（主文件只负责连接池管理与最终访问）
│   ├── pool.py                  #   DynamicPool 连接池管理
│   ├── base.py                  #   MySQLManagerBase 核心初始化/执行/迁移
│   ├── chat_history.py          #   消息表读写 + message_id 批量查重
│   ├── images.py                #   图片表读写 + 图片 URL 批量查重
│   ├── query_log.py             #   查询日志读写（v0.7.0）
│   ├── stats.py                 #   统计聚合查询
│   └── maintenance.py           #   清空数据维护
├── public_api.py                #   对外查询接口（v0.7.0，其他插件导入调用）
├── db_config/                   # 本地配置包（SQLite config.db）
│   ├── base.py                  #   ConfigManagerBase 建表/默认值/读写
│   ├── groups.py                #   群白名单管理
│   ├── summary_settings.py      #   总结配置
│   ├── profile_settings.py      #   人物分析配置
│   ├── stats_settings.py        #   数据分析配置
│   └── snapshots.py             #   快照表读写
├── webapi/                      # Web API 包（每个子功能一个文件）
│   ├── base.py                  #   WebAPIBase 路由表注册 + 公共 helper
│   ├── storage.py               #   状态/群管理/设置/清空
│   ├── query.py                 #   消息查询
│   ├── query_log.py             #   查询日志列表（v0.7.0）
│   ├── summary.py               #   总结设置/忽略/历史
│   ├── profile.py               #   人物分析设置/发起/历史
│   └── stats.py                 #   数据分析数据/设置/推送
├── summary/                     # 总结功能（service/fetcher/onebot/summarizer/formatter/t2i_render/storage/scheduler/templates）
├── profile/                     # 人物分析（service/fetcher/stats/analyzer/formatter/t2i_render/storage/scheduler/templates）
├── stats/                       # 数据分析（repository/models/parser/service/snapshot/scheduler/t2i_render/templates）
├── parsing.py                   # 消息解析纯函数（extract_image_urls / parse_onebot_raw_message / stats_fallback_text / resolve_group_id）
├── saver.py                     # MessageSaver 消息缓冲/落库逻辑（原 main.py 内逻辑迁出）
├── cleaner.py                   # ImageCleaner 图片过期清理
└── backfill.py                  # ReloadBackfill 重载自动补库（v0.6.0 新增）
```

> 组装范式：各包以「主文件 base + 子功能 Mixin」多继承组装出单一公开类名
> （`MySQLManager` / `ConfigManager` / `WebAPI`），对外调用零改动；类常量（如
> `ConfigManager.SUMMARY_TYPES`）经 MRO 解析，业务模块可直接引用。

## 总结模块

| 模块 | 职责 |
|------|------|
| core/summary/service.py | 编排层：指令入口、白名单/限流校验、流程串联 |
| core/summary/fetcher.py | 数据获取：混合策略（MySQL 优先 + OneBot 补齐 + 去重合并） |
| core/summary/onebot.py | 协议端 `get_group_msg_history` 封装与消息段解析（补库复用其翻页范式） |
| core/summary/summarizer.py | 总结引擎：统计计算 + LLM 调用 + 提示词占位符渲染 |
| core/summary/formatter.py | 输出格式化：合并转发节点（剥离 Markdown）/ 文转图（含 GFM 表格兜底转换） |
| core/summary/t2i_render.py | 自研图片渲染核心：模板加载、日夜主题判定、多路 CDN/超时配置解析、两轮渲染 + 魔数校验 |
| core/summary/templates/ | 自研 T2I 报告模板（HTML + 内联 CSS/JS，双主题 + CDN 容灾加载器） |
| core/summary/storage.py | 总结 JSON 持久化（按群分目录、列表、读取、过期清理） |
| core/summary/scheduler.py | 定时清理任务（每天清理过期 JSON） |

## 人物分析模块（v0.4.0 新增）

| 模块 | 职责 |
|------|------|
| profile/service.py | 编排层：指令与 Web 共用入口、权限/限流校验、流程串联、目标解析 |
| profile/fetcher.py | 数据获取：单群 MySQL 分页 + OneBot 补齐 / 跨群全局拉取 + 关系上下文双向识别 |
| profile/stats.py | 确定性统计引擎：24h/星期分布、发言长度、emoji 率、互动排行等 |
| profile/analyzer.py | AI 分析：独立 provider 降级链、长度预算截断、五维度开关 prompt、四板块宽松切分 |
| profile/formatter.py | 输出格式化：合并转发 / 图片 / 纯文本三模式 |
| profile/t2i_render.py | 人物报告图片渲染核心（复用 summary_t2i_* 渲染配置） |
| profile/templates/profile_report.html | 自研人物报告模板（双主题 + 活动分布图表 + CDN 容灾加载器 + 免责声明页脚） |
| profile/storage.py | 分析结果 JSON 持久化（按范围分目录、确定性文件名、列表/读取/删除/过期清理） |
| profile/scheduler.py | 定时清理任务（每天清理过期分析文件） |

## 数据分析模块（v0.5.0 新增）

| 模块 | 职责 |
|------|------|
| stats/repository.py | MySQL 聚合仓储：全参数化统计查询（实时维度 8 查询 + v0.5.5 新增快照聚合 6 查询：小时/日/月批量聚合与窗口 Top K），统一半开时间窗口，30s 查询超时兜底 |
| stats/models.py | 数据模型（6 个 dataclass，Web/指令/推送共用契约） |
| stats/parser.py | `/群统计` 参数解析纯函数：时间关键词/单日/区间（4 种分隔符、跨度 ≤366 天校验）/ @ 与 QQ 号混排识别 |
| stats/service.py | 编排层：`build_stats` 三端同源组装（群级读快照、超范围/异常自动回退实时 SQL）、概览趋势快照供数（days ≤ 31）、启动回填任务、每群冷却、群 → 推送目标（umo）内存缓存、日报/周报推送（逐群串行、单群失败不阻断） |
| stats/snapshot.py | 分段快照管理（v0.5.5 起消息 + 图片双源）：小时/日/月三段任务聚合、淘汰、启动窗口重聚合回填、当前小时限流强制刷新、群级统计三层归并供数与图片数注入 |
| stats/scheduler.py | 后台调度：推送时间检查循环（日报/周报到点触发）+ 小时/日/月三类快照循环（各自独立去重戳，日快照成功后顺带淘汰），异常指数退避 |
| stats/t2i_render.py | 报告卡渲染核心：复用 `summary_t2i_*` 配置、两轮渲染 + 魔数校验、绝不抛异常（失败由上层降级纯文本） |
| stats/templates/stats_report.html | 自研统计报告卡模板（860px 双主题，与总结/人物报告同款设计令牌；ECharts 版本锁定 CDN + 纯 CSS 兜底图表） |
