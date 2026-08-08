"""人物分析引擎（Module F）：LLM 叙述生成 + 独立 provider 降级链。

接收上游 fetcher（Module D）的目标/上下文消息与 stats（Module E）的确定性统计，
经 :class:`ProfileAnalyzer` 产出 :class:`ProfileResult`（板块叙述 + 元数据）：

1. **长度预算**：目标消息样本按 ``profile_max_prompt_chars``（默认 60000）字符
   预算截断，**保最近**（从最旧逐条丢弃）；互动上下文消息同样计入预算，但
   优先级低于目标消息（仅使用目标消息分配后的剩余预算）；传入的
   :class:`ProfileStats` 始终为全量统计，截断不修改它（截断仅记日志）；
2. **维度开关**：仅启用的维度（``profile_dim_*``）写入 prompt 产出要求，关闭
   的维度不要求 LLM 产出；活动时间规律的解读并入「发言习惯」板块，不单独成块；
3. **Prompt 组装**：角色设定 + 目标基本信息（昵称/QQ/范围/时间跨度）+ 关键
   统计（总数/活跃天数/peak_hour/peak_weekday/avg_length/emoji_ratio/各群分布）
   + 互动对象排行（Top N 及条数）+ 消息样本（``[群号][时间] 内容``，时间升序）
   + 互动上下文（关系维度启用时）+ 产出要求（措辞约束：结论基于所给记录、
   避免绝对化断言、性格/爱好/关系均为推测、文末无需重复免责声明）；
   image 输出模式追加「可用 Markdown 表格呈现结构化信息」；
4. **独立 provider 降级链**（读 profile_* 配置，**不复用 summary 配置**）：
   主选 ``profile_provider``（空串跳过）→ 备用列表 ``profile_fallback_providers``
   （按序、过滤非法项、保序去重）→ 会话模型兜底（event 非 None 时经
   ``context.get_current_chat_provider_id(event.unified_msg_origin)`` 惰性解析；
   event 为 None 的 Web 全局场景无会话，兜底跳过）；任一节点调用异常或
   ``completion_text`` 为空即降级下一个；``ProfileResult.provider_id`` 记录
   **实际成功**的节点；
5. **绝不抛异常**：整链耗尽返回 sections 为「分析失败」兜底单段、provider_id
   为空的 :class:`ProfileResult`（由 service 层兜底文案）；任何意外异常同样
   吞掉降级为失败结果；
6. **LLM 调用**：经 ``context.llm_generate(chat_provider_id=..., prompt=...)``
   （AstrBot v4.5.7+ SDK，见 docs/zh/dev/star/guides/ai.md），返回
   :class:`LLMResponse`，取 ``completion_text`` 属性提取纯文本（非字符串
   视为空文本降级）；非末尾节点失败记 warning，整链耗尽记 error（均不打
   prompt 全文、warning 不挂 exc_info 降噪）；
7. **板块解析**（best-effort）：按 ``## 标题`` 标题行宽松切分 LLM 输出，
   标题经关键词归一为 4 个规范板块（发言习惯/性格分析/兴趣爱好/人物关系），
   同名板块去重保留首个，按规范顺序输出；切分失败（无 ## 标题或解析异常）
   回退单段 ``[("人物画像", raw)]``。

契约见 开发/v0.4.0/分工.md「共享接口契约 → Module F」，不得私改。
"""

from __future__ import annotations

import re

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context

from ..db_config import ConfigManager
from .models import ProfileMessage, ProfileResult, ProfileStats, ProfileTarget

# 素材长度预算（完整 prompt 字符数）的默认值与兜底：防 LLM 上下文爆掉（超窗口/高成本）。
# 正常以 Web 配置 profile_max_prompt_chars 为准（见 _get_max_prompt_chars）；
# 超限时按时间从最旧开始逐条丢弃、保留最近的消息，直至不超限。
MAX_PROMPT_CHARS = 60000

# 4 个规范板块标题（规范顺序）；活动时间规律解读并入「发言习惯」，不单独成块
SECTION_TITLES: tuple[str, ...] = (
    "发言习惯",
    "性格分析",
    "兴趣爱好",
    "人物关系",
)

# 板块标题宽松归一规则：(关键词, 归一后的规范标题)。
# LLM 可能微调标题文案（如「发言习惯与活跃时间」「性格」），行内含关键词即归一；
# 顺序敏感——更具体的关键词在前（「习惯」先于「关系」等，互不冲突即可）。
_SECTION_KEYWORD_RULES: tuple[tuple[str, str], ...] = (
    ("习惯", SECTION_TITLES[0]),
    ("性格", SECTION_TITLES[1]),
    ("兴趣", SECTION_TITLES[2]),
    ("爱好", SECTION_TITLES[2]),
    ("关系", SECTION_TITLES[3]),
)

# ## 标题行（容忍 ### 及行首空白）；分组 1 为标题文本
_HEADING_RE = re.compile(r"^\s*#{2,}\s*(\S.*?)\s*$")

# 星期索引（ProfileStats.weekday_dist，Mon=0..Sun=6）→ 中文名
_WEEKDAY_NAMES: tuple[str, ...] = (
    "周一",
    "周二",
    "周三",
    "周四",
    "周五",
    "周六",
    "周日",
)


class ProfileAnalyzer:
    """人物分析引擎：prompt 组装、长度预算、独立 provider 降级链与板块解析。"""

    def __init__(self, context: Context, config_mgr: ConfigManager) -> None:
        self.context = context
        self.config_mgr = config_mgr

    async def analyze(
        self,
        event: AstrMessageEvent | None,
        target: ProfileTarget,
        stats: ProfileStats,
        target_messages: list[ProfileMessage],
        context_messages: list[ProfileMessage],
        output_mode: str,
    ) -> ProfileResult:
        """对给定目标消息执行 LLM 人物画像分析，产出 :class:`ProfileResult`。

        Args:
            event: 当前消息事件（指令场景）；用于降级链的会话模型兜底。
                **Web 全局分析场景传 None**——无会话可解析，会话兜底跳过。
            target: 分析目标（昵称/QQ/范围，透传进结果）。
            stats: 确定性统计（全量，透传进结果；本方法不修改它）。
            target_messages: 目标用户消息（fetcher 已去重；内部仍按时间排序
                兜底，保证截断策略始终保留最近的消息）。
            context_messages: 互动对象消息（关系上下文；关系维度关闭或未采集
                时为 []，不计入 prompt）。
            output_mode: 输出形态 ``"forward"`` / ``"image"`` / ``"text"``；
                image 模式在产出要求中追加「可用 Markdown 表格」。

        Returns:
            ProfileResult: sections 为板块叙述（解析失败则单段），provider_id
            记录降级链中**实际成功**的 provider；``messages_used`` 为实际送入
            的**目标消息**条数（上下文消息不计）；``sources`` / ``scope_desc`` /
            ``created_at`` / ``relation_context_complete`` 保持默认，由 service
            层填充。**绝不抛异常**：整链耗尽或意外异常均返回「分析失败」兜底
            结果（provider_id 为空串）。
        """
        try:
            return await self._analyze_impl(
                event, target, stats, target_messages, context_messages, output_mode
            )
        except Exception as e:  # 绝不向上抛：任何意外异常降级为失败结果
            logger.error(
                f"[Profile] 人物分析引擎意外异常，降级失败结果："
                f"{type(e).__name__}: {e}",
                exc_info=e,
            )
            return self._failure_result(
                target, stats, 0, f"分析引擎内部异常 {type(e).__name__}"
            )

    async def _analyze_impl(
        self,
        event: AstrMessageEvent | None,
        target: ProfileTarget,
        stats: ProfileStats,
        target_messages: list[ProfileMessage],
        context_messages: list[ProfileMessage],
        output_mode: str,
    ) -> ProfileResult:
        """analyze 的实际实现（异常由外层 analyze 兜底）。"""
        dims = await self._load_dims()
        max_prompt_chars = await self._get_max_prompt_chars()

        # 兜底按时间升序：截断「保最近」依赖该顺序
        ordered_target = sorted(target_messages, key=lambda m: m.timestamp)
        ordered_context = sorted(context_messages, key=lambda m: m.timestamp)
        target_lines = [self._format_target_line(m) for m in ordered_target]
        context_lines = [self._format_context_line(m) for m in ordered_context]

        # 互动上下文块仅在关系维度启用时进入 prompt（无样本时给出降级说明）
        scaffold = self._build_scaffold(
            target, stats, dims, output_mode, include_context=dims["relations"]
        )
        prompt, messages_used, truncated = self._truncate_material(
            scaffold, target_lines, context_lines, max_prompt_chars
        )
        if truncated:
            logger.info(
                f"[Profile] 素材超出长度预算已截断：目标全量 {len(target_lines)} 条，"
                f"实际送入 {messages_used} 条（预算 {max_prompt_chars} 字符，统计仍全量）"
            )

        chain = await self._build_chain()
        raw, used_provider = await self._call_llm_chain(chain, prompt, event)

        if not raw:
            # 整链耗尽：失败兜底（不抛异常，service 层据此出兜底文案）
            return self._failure_result(
                target, stats, messages_used, "所有 LLM 模型均调用失败或返回空文本"
            )

        logger.info(
            f"[Profile] 人物分析 LLM 调用成功：provider={used_provider}，"
            f"送入目标消息 {messages_used} 条"
        )
        return ProfileResult(
            target=target,
            stats=stats,
            sections=self._parse_sections(raw),
            raw_llm_text=raw,
            provider_id=used_provider,
            messages_used=messages_used,
        )

    # ========== 配置读取 ==========

    async def _load_dims(self) -> dict[str, bool]:
        """读取 5 个维度开关（profile_dim_*）。

        配置层保证 bool 类型；非 bool 防御性回退为 True（与 PROFILE_DEFAULTS
        全 true 的默认语义一致，避免因配置异常静默关闭维度）。
        """
        dims: dict[str, bool] = {}
        for name in ("habits", "activity", "personality", "hobbies", "relations"):
            value = await self.config_mgr.get_profile_setting_typed(
                f"profile_dim_{name}"
            )
            dims[name] = value if isinstance(value, bool) else True
        return dims

    async def _get_max_prompt_chars(self) -> int:
        """读取素材长度预算配置（int，非法/非正数回退常量默认 MAX_PROMPT_CHARS）。"""
        value = await self.config_mgr.get_profile_setting_typed(
            "profile_max_prompt_chars"
        )
        # bool 是 int 子类需显式排除；配置层已兜底，此处仅防御
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            return MAX_PROMPT_CHARS
        return value

    # ========== Prompt 组装 ==========

    @staticmethod
    def _format_target_line(msg: ProfileMessage) -> str:
        """目标消息样本行：``[群号][YYYY-MM-DD HH:MM] 内容``。"""
        return f"[{msg.group_id}][{msg.timestamp:%Y-%m-%d %H:%M}] {msg.content}"

    @staticmethod
    def _format_context_line(msg: ProfileMessage) -> str:
        """互动上下文样本行：``[群号][YYYY-MM-DD HH:MM] 昵称: 内容``（需区分发言人）。"""
        return (
            f"[{msg.group_id}][{msg.timestamp:%Y-%m-%d %H:%M}] "
            f"{msg.sender_name or msg.sender_id}: {msg.content}"
        )

    @staticmethod
    def _weekday_name(index: int) -> str:
        """星期索引（Mon=0..Sun=6）→ 中文名；越界防御返回「星期N」。"""
        if 0 <= index < len(_WEEKDAY_NAMES):
            return _WEEKDAY_NAMES[index]
        return f"星期{index}"

    def _build_scaffold(
        self,
        target: ProfileTarget,
        stats: ProfileStats,
        dims: dict[str, bool],
        output_mode: str,
        include_context: bool,
    ) -> str:
        """组装 prompt 骨架（含 ``{target_messages}`` / ``{context_messages}`` 占位符）。

        素材占位符留待 :meth:`_truncate_material` 按长度预算填充；骨架其余部分
        （角色/统计/排行/产出要求）不受预算削减。
        """
        blocks: list[str] = [
            "你是一名社群发言画像分析助手。请基于下方提供的 QQ 群聊发言记录与统计数据，"
            "对指定用户进行客观、克制的人物画像分析。",
            self._build_target_block(target, stats),
            self._build_stats_block(stats),
            self._build_partners_block(stats),
            "【目标发言样本】（按时间升序，格式 [群号][时间] 内容；受长度预算限制可能仅保留最近部分）\n"
            "{target_messages}",
        ]
        if include_context:
            blocks.append(
                "【互动上下文】（互动对象的发言样本，仅用于辅助人物关系分析）\n"
                "{context_messages}"
            )
        blocks.append(self._build_output_requirements(dims, output_mode))
        return "\n\n".join(blocks)

    @staticmethod
    def _build_target_block(target: ProfileTarget, stats: ProfileStats) -> str:
        """目标基本信息块：昵称/QQ/分析范围/时间跨度。"""
        if target.scope == "group":
            scope = f"指定群 {target.group_id}" if target.group_id else "指定群"
        else:
            scope = f"全部已保存群（共 {stats.group_count} 个群）"
        lines = [
            "【目标用户】",
            f"昵称: {target.sender_name or target.sender_id}",
            f"QQ: {target.sender_id}",
            f"分析范围: {scope}",
        ]
        if stats.time_start is not None and stats.time_end is not None:
            lines.append(
                f"时间跨度: {stats.time_start:%Y-%m-%d %H:%M} ~ "
                f"{stats.time_end:%Y-%m-%d %H:%M}"
            )
        return "\n".join(lines)

    def _build_stats_block(self, stats: ProfileStats) -> str:
        """关键统计块（全量统计，明示样本截断不影响统计）。"""
        lines = [
            "【关键统计】（基于全量消息计算，下方样本可能因长度预算被截断，但统计不受影响）",
            f"消息总数: {stats.total}",
            f"活跃天数: {stats.active_days}",
            f"最活跃小时: {stats.peak_hour} 点",
            f"最活跃星期: {self._weekday_name(stats.peak_weekday)}",
            f"平均消息长度: {stats.avg_length:.1f} 字符",
            f"含表情消息占比: {stats.emoji_ratio * 100:.1f}%",
        ]
        if stats.group_breakdown:
            dist = "、".join(
                f"群 {group_id}({count} 条)"
                for group_id, count in stats.group_breakdown
            )
            lines.append(f"各群分布: {dist}")
        else:
            lines.append("各群分布: 无数据")
        return "\n".join(lines)

    @staticmethod
    def _build_partners_block(stats: ProfileStats) -> str:
        """互动对象排行块（Top N 及条数；无数据显式标注）。"""
        lines = ["【互动对象排行】"]
        if stats.top_partners:
            for rank, (sid, name, count) in enumerate(stats.top_partners, 1):
                lines.append(f"{rank}. {name}(QQ {sid}): {count} 条")
        else:
            lines.append("无数据")
        return "\n".join(lines)

    @staticmethod
    def _build_output_requirements(dims: dict[str, bool], output_mode: str) -> str:
        """产出要求块：仅列出启用维度的板块指示 + 措辞约束 + image 表格提示。

        活动时间规律的解读并入「发言习惯」板块：habits/activity 任一启用即
        产出该板块，板块描述按各自开关组合；两者均关闭才不产出。全部维度
        关闭时给出「## 人物画像」简要总评指示兜底（避免 LLM 无所适从）。
        """
        bullets: list[str] = []
        if dims["habits"] or dims["activity"]:
            parts: list[str] = []
            if dims["habits"]:
                parts.append("发言频率、平均长度、口头禅与高频表达、语气与表情使用")
            if dims["activity"]:
                parts.append(
                    "解读活动时间规律（最活跃小时/星期，结合关键统计的小时与星期分布）"
                )
            bullets.append(f"## 发言习惯：{'，'.join(parts)}")
        if dims["personality"]:
            bullets.append(
                "## 性格分析：基于发言内容推断性格特征（如外向/理性/幽默/温和等），"
                "并给出具体发言依据"
            )
        if dims["hobbies"]:
            bullets.append("## 兴趣爱好：从发言主题归纳兴趣领域")
        if dims["relations"]:
            bullets.append(
                "## 人物关系：刻画与 Top 互动对象的关系（谁互动最多、关系性质推测、互动模式）"
            )
        if not bullets:
            bullets.append("## 人物画像：用一段简要总评概括该用户的发言形象")

        lines = [
            "【输出要求】",
            "1. 仅输出以下启用维度的 Markdown 板块，板块标题须原样使用，勿自行新增其他二级标题：",
        ]
        lines.extend(f"   - {bullet}" for bullet in bullets)
        lines.append(
            "2. 措辞约束：结论须基于所给发言记录，避免绝对化断言；"
            "性格/爱好/关系均为推测；文末无需重复免责声明（模板会加）。"
        )
        if output_mode == "image":
            lines.append("3. 可使用 Markdown 表格（GFM 管道符语法）呈现结构化信息。")
        return "\n".join(lines)

    # ========== 长度预算截断 ==========

    @staticmethod
    def _keep_recent(lines: list[str], available: int) -> tuple[list[str], int]:
        """从尾部（最近的消息）向前累积，返回 (保留行[原序], 已用字符数)。

        每条另计 1 个换行符（首条不计）；单条即超预算时仍保留该条作为最后
        手段（至少送入 1 条）。镜像 summary/summarizer.py 的截断内核。
        """
        kept_rev: list[str] = []
        used = 0
        for line in reversed(lines):
            add = len(line) + (1 if kept_rev else 0)
            if used + add > available and kept_rev:
                break
            kept_rev.append(line)
            used += add
            if used > available:
                break
        return list(reversed(kept_rev)), used

    def _truncate_material(
        self,
        scaffold: str,
        target_lines: list[str],
        context_lines: list[str],
        max_prompt_chars: int,
    ) -> tuple[str, int, bool]:
        """对素材应用长度预算并组装最终 prompt。

        预算先分配给目标消息（保最近），剩余预算再分配给互动上下文消息
        （同样保最近，优先级低于目标消息）；统计始终全量，截断不触及 stats。

        Returns:
            (最终 prompt, 实际送入的目标消息条数, 是否发生任何削减)
        """
        base_len = len(
            scaffold.replace("{target_messages}", "").replace("{context_messages}", "")
        )
        available = max(0, max_prompt_chars - base_len)

        kept_target, used = self._keep_recent(target_lines, available)
        kept_context, _ = self._keep_recent(context_lines, max(0, available - used))

        truncated = len(kept_target) < len(target_lines) or len(kept_context) < len(
            context_lines
        )

        prompt = scaffold.replace(
            "{target_messages}", "\n".join(kept_target) or "（无目标发言样本）"
        )
        if "{context_messages}" in prompt:
            if context_lines:
                ctx_text = (
                    "\n".join(kept_context) or "（互动上下文消息已全部被长度预算裁减）"
                )
            else:
                ctx_text = (
                    "（未提供互动上下文样本，人物关系仅可基于目标提及他人作浅层推断）"
                )
            prompt = prompt.replace("{context_messages}", ctx_text)
        return prompt, len(kept_target), truncated

    # ========== Provider 降级链 ==========

    async def _build_chain(self) -> list[str]:
        """构建已配置段降级链：主选 → 备用列表（不含会话 provider，惰性兜底）。

        - 主选：``profile_provider`` strip 后非空入链（空串跳过）；
        - 备用列表：``profile_fallback_providers`` 经 typed 读取（非 list 视为
          空列表），逐项过滤——仅保留 strip 后非空的 str（非字符串/空串静默
          丢弃），保持配置顺序去重（与主选或前序项重复的只保留首份）；
        - 两段皆空时返回空列表：会话 provider 兜底在 :meth:`_call_llm_chain`
          内惰性解析。**读 profile_* 配置，不复用 summary 配置**。
        """
        chain: list[str] = []
        primary = (
            await self.config_mgr.get_profile_setting("profile_provider")
        ).strip()
        if primary:
            chain.append(primary)

        fallbacks = await self.config_mgr.get_profile_setting_typed(
            "profile_fallback_providers"
        )
        if not isinstance(fallbacks, list):
            fallbacks = []
        for item in fallbacks:
            if not isinstance(item, str):
                continue
            pid = item.strip()
            if pid and pid not in chain:
                chain.append(pid)
        return chain

    async def _resolve_session_provider(self, event: AstrMessageEvent | None) -> str:
        """惰性解析会话 provider（最终兜底）；取不到一律返回空串。

        - event 为 None（Web 全局分析）→ 无会话可解析，直接返回空串；
        - ``context.get_current_chat_provider_id(umo)`` 在未找到时**抛出
          ProviderNotFoundError**（而非返回空值），此处统一捕获视为取不到。
        """
        if event is None:
            return ""
        try:
            provider_id = await self.context.get_current_chat_provider_id(
                event.unified_msg_origin
            )
        except Exception as e:
            logger.warning(
                f"[Profile] 获取会话 provider 失败（将视为取不到）: "
                f"{type(e).__name__}: {e}"
            )
            return ""
        return (provider_id or "").strip()

    async def _call_llm_chain(
        self, chain: list[str], prompt: str, event: AstrMessageEvent | None
    ) -> tuple[str, str]:
        """按「主选 → 备用列表 → 会话兜底」逐节点尝试，返回 (纯文本, 实际成功的 provider id)。

        惰性设计：入参 ``chain`` 仅为已配置段（见 :meth:`_build_chain`）；会话
        provider 仅在已配置段**全部失败后**才惰性解析并追加为末尾节点（已配置
        段为空时立即解析；主选成功时零会话查询）。event 为 None 时会话兜底跳过。

        降级规则：任一节点**调用异常、completion_text 非字符串或为空**即尝试
        下一个；非末尾节点失败记 warning（provider id + 简要原因，不打 prompt
        全文、不挂 exc_info 降噪）；整链耗尽记 error 后返回 ``("", "")``——
        **绝不抛异常**（与 summarizer 的差异：service 层需要结构化失败结果）。
        """
        nodes = list(chain)
        tried: list[str] = []
        session_resolved = False
        last_reason = ""

        idx = 0
        while True:
            if idx >= len(nodes):
                # 已配置段耗尽：惰性解析会话 provider 追加为末尾节点
                if not session_resolved:
                    session_resolved = True
                    session_pid = await self._resolve_session_provider(event)
                    if session_pid and session_pid not in tried:
                        if tried:
                            logger.warning(
                                f"[Profile] 已配置模型均失败，降级会话模型："
                                f"provider={tried[-1]}，原因：{last_reason}"
                            )
                        nodes.append(session_pid)
                        continue
                break  # 会话取不到/event 为 None/已尝试过 → 整链耗尽

            pid = nodes[idx]
            tried.append(pid)
            try:
                # AstrBot v4.5.7+ SDK：返回 LLMResponse，completion_text 属性
                # 优先取 result_chain 的纯文本拼接（见 docs/zh/dev/star/guides/ai.md）
                llm_resp = await self.context.llm_generate(
                    chat_provider_id=pid,
                    prompt=prompt,
                )
            except Exception as e:
                last_reason = f"调用异常 {type(e).__name__}: {e}"
            else:
                completion = getattr(llm_resp, "completion_text", "")
                raw = completion.strip() if isinstance(completion, str) else ""
                if raw:
                    return raw, pid
                last_reason = "返回文本为空"

            # 非末尾节点失败 → warning 降级日志（末尾失败由链耗尽 error 统一记录）
            if idx + 1 < len(nodes):
                logger.warning(
                    f"[Profile] 人物分析模型调用失败，降级下一节点："
                    f"provider={pid}，原因：{last_reason}"
                )
            idx += 1

        # 整链耗尽：error 日志后返回空结果（由 _analyze_impl 组装失败兜底）
        if tried:
            logger.error(
                f"[Profile] 人物分析模型降级链耗尽，最后失败 provider={tried[-1]}："
                f"{last_reason}"
            )
        else:
            logger.error(
                "[Profile] 人物分析模型降级链为空：主选/备用未配置且会话模型不可用"
                "（Web 全局场景无会话或会话 provider 取不到）"
            )
        return "", ""

    # ========== 失败兜底 ==========

    @staticmethod
    def _failure_result(
        target: ProfileTarget,
        stats: ProfileStats,
        messages_used: int,
        reason: str,
    ) -> ProfileResult:
        """组装「分析失败」兜底结果：单段 sections + 空 provider_id，绝不抛异常。"""
        return ProfileResult(
            target=target,
            stats=stats,
            sections=[("分析失败", f"未能生成人物画像叙述：{reason}")],
            raw_llm_text="",
            provider_id="",
            messages_used=messages_used,
        )

    # ========== 板块解析 ==========

    @staticmethod
    def _normalize_title(raw_title: str) -> str:
        """标题宽松归一：含关键词即归为规范板块标题，否则原样保留。"""
        for keyword, title in _SECTION_KEYWORD_RULES:
            if keyword in raw_title:
                return title
        return raw_title

    def _parse_sections(self, raw: str) -> list[tuple[str, str]]:
        """best-effort 按 ``## 标题`` 标题行宽松切分 LLM 输出。

        逐行扫描标题行（``##`` 及更深级别，容忍行首空白），板块内容 = 标题行
        （不含）至下一标题行（不含）之间的文本（去首尾空白）；标题经关键词
        归一为 4 个规范板块（见 :data:`_SECTION_KEYWORD_RULES`），同名板块去重
        保留首个，成功切分按规范板块顺序输出（未知标题排在规范板块之后、
        保持出现顺序）。

        兜底：无任何 ## 标题或解析异常 → 单段 ``[("人物画像", raw)]``。
        """
        try:
            lines = raw.splitlines()
            headings: list[tuple[int, str]] = []
            for line_idx, line in enumerate(lines):
                match = _HEADING_RE.match(line)
                if match:
                    headings.append((line_idx, match.group(1).strip("# \t")))

            if not headings:
                raise ValueError("未发现 ## 板块标题行")

            sections: list[tuple[str, str]] = []
            seen: set[str] = set()
            for i, (start, raw_title) in enumerate(headings):
                end = headings[i + 1][0] if i + 1 < len(headings) else len(lines)
                body = "\n".join(lines[start + 1 : end]).strip()
                title = self._normalize_title(raw_title)
                if title in seen:  # 同名板块去重保留首个（镜像 summarizer first_hits）
                    continue
                seen.add(title)
                sections.append((title, body))

            # 归一到规范顺序（未知标题排后，稳定排序保持出现顺序）
            title_order = {title: i for i, title in enumerate(SECTION_TITLES)}
            sections.sort(
                key=lambda section: title_order.get(section[0], len(SECTION_TITLES))
            )
            return sections
        except Exception as e:
            logger.warning(
                f"[Profile] 板块切分失败（{type(e).__name__}: {e}），回退单段输出"
            )
            return [("人物画像", raw)]
