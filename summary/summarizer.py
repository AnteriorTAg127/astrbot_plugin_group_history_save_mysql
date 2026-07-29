"""总结引擎（模块 E）：统计 + LLM 调用 + 提示词占位符渲染 + 板块解析。

接收上游 fetcher（模块 D）产出的归一化消息列表 :class:`ChatMessage`，完成：

1. **规则统计**：消息总数、参与者数、时间跨度、活跃排行 Top N（基于全量消息）；
2. **素材格式化**：每行 ``[YYYY-MM-DD HH:MM] 昵称: 内容``；
3. **长度预算**：渲染后的完整 prompt 超过 :data:`MAX_PROMPT_CHARS` 时，按时间
   **保留最近的消息**逐条削减（统计不受影响，仅 ``stats.truncated`` 置 True）；
4. **Provider 解析**：配置 ``summary_provider_id`` 优先；为空回退
   ``context.get_current_chat_provider_id(event.unified_msg_origin)``；
   皆无则抛 :class:`SummaryProviderError`（由 service 层兜底文案）；
5. **占位符渲染**：替换 ``{stats}`` ``{messages}`` ``{time_range}`` ``{group_id}``
   ``{format_constraint}`` 五个占位符（模板取自配置，用户可自定义），
   渲染后正则自检并清除遗漏占位符；
6. **LLM 调用**：经 ``context.llm_generate(chat_provider_id=..., prompt=...)``
   （AstrBot v4.5.7+ SDK，见 docs/zh/dev/star/guides/ai.md），返回
   :class:`LLMResponse`，取 ``completion_text`` 属性提取纯文本；
   调用异常 ``logger.error(..., exc_info=True)`` 后**原样向上抛**，不吞；
7. **板块解析**（best-effort）：按 4 个板块标题行宽松切分 LLM 输出，
   切出少于 2 个板块则回退单段 ``[("全部", raw)]``。

契约见 开发/v0.3/分工.md「接口约定 → Summarizer」，不得私改。
"""

from __future__ import annotations

import re

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context

from ..db_config import ConfigManager
from .models import ChatMessage, StatsResult, SummaryResult

# 渲染后完整 prompt 的字符数硬上限：防止 LLM 上下文爆掉（超窗口/高成本）。
# 超限时按时间从最旧开始逐条丢弃消息、保留最近的消息，直至不超限。
MAX_PROMPT_CHARS = 60000

# 4 个板块的标准标题（含 emoji），用于归一化 LLM 输出中可能被微调的标题
SECTION_TITLES: tuple[str, ...] = (
    "📢 重要通知与结论",
    "💬 讨论要点 / 争议",
    "🎉 有趣片段",
    "✅ TODO / 待跟进",
)

# 板块宽松匹配规则：(关键词, 归一后的标准标题)。
# 匹配策略宽松——行首/行内含关键词即视为该板块标题行（LLM 可能微调标题文案）。
_SECTION_MATCH_RULES: tuple[tuple[str, str], ...] = (
    ("重要通知", SECTION_TITLES[0]),
    ("讨论要点", SECTION_TITLES[1]),
    ("有趣片段", SECTION_TITLES[2]),
    ("TODO", SECTION_TITLES[3]),
)

# format_constraint 占位符取值：forward（合并转发）需剥 Markdown，image（文转图）可保留
_FORMAT_CONSTRAINT_FORWARD = (
    "不要使用任何 Markdown 格式（不要出现 #、*、>、`、[] 等标记符号）"
)
_FORMAT_CONSTRAINT_IMAGE = "可以使用 Markdown 格式"

# 渲染自检：识别形如 {xxx} 的遗漏占位符（用户自定义模板可能含未知占位符）
_PLACEHOLDER_RE = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")

# summary_rank_top_n 的最终兜底值（配置层已保证合法，此处仅防御异常类型）
_DEFAULT_TOP_N = 5


class SummaryProviderError(Exception):
    """未配置总结 provider 且无法获取会话 provider。"""


class Summarizer:
    """总结引擎：统计、素材格式化、提示词渲染、LLM 调用与板块解析。"""

    def __init__(self, context: Context, config_mgr: ConfigManager) -> None:
        self.context = context
        self.config_mgr = config_mgr

    async def summarize(
        self,
        event: AstrMessageEvent,
        messages: list[ChatMessage],
        scope_desc: str,
        output_mode: str,
    ) -> SummaryResult:
        """对给定消息列表执行统计 + LLM 总结，产出 :class:`SummaryResult`。

        Args:
            event: 当前消息事件（用于 provider 回退与群号兜底）。
            messages: 归一化消息列表（fetcher 已去重/过滤，通常时间升序；
                内部仍按时间排序兜底，保证截断策略始终保留最近的消息）。
            scope_desc: 范围描述，如「最近 512 条」/「最近 24 小时」，渲染
                到 ``{time_range}`` 并透传进 SummaryResult。
            output_mode: 输出形态，``"forward"`` / ``"image"``，决定
                ``{format_constraint}`` 注入的格式约束文案。

        Returns:
            SummaryResult: 统计（基于全量消息）+ 板块摘要 + 元数据。
            ``sources`` 留空 dict（由 service 层注入）。

        Raises:
            SummaryProviderError: 未配置总结 provider 且无法获取会话 provider。
            RuntimeError: LLM 返回空文本。
            Exception: LLM 调用异常原样向上抛（已记 error 日志），由 service 层兜底。
        """
        # 兜底按时间升序：截断「保留最近」与时间跨度统计都依赖该顺序
        ordered = sorted(messages, key=lambda m: m.timestamp)

        top_n = await self._get_top_n()
        stats = self._build_stats(ordered, top_n)

        # provider 解析前置：无法确定 provider 时快速失败，不做无谓的渲染
        provider_id = await self._resolve_provider_id(event)

        template = await self.config_mgr.get_summary_setting("summary_prompt")
        if not template.strip():
            # 用户清空了模板：回退内置默认模板，保证占位符体系完整
            logger.warning("[HistorySummary] summary_prompt 为空，回退内置默认模板")
            template = ConfigManager.SUMMARY_DEFAULTS["summary_prompt"]

        material_lines = self._format_messages(ordered)
        values = {
            "{stats}": self._render_stats_text(stats),
            "{time_range}": scope_desc,
            "{group_id}": self._resolve_group_id(event, ordered),
            "{format_constraint}": (
                _FORMAT_CONSTRAINT_IMAGE
                if output_mode == "image"
                else _FORMAT_CONSTRAINT_FORWARD
            ),
        }
        prompt, messages_used, truncated = self._render_prompt(
            template, values, material_lines
        )
        if truncated:
            # 统计始终基于全量消息，truncated 仅标记送入 LLM 的素材被削减
            stats.truncated = True
            logger.info(
                f"[HistorySummary] 素材超出长度预算已截断：全量 {stats.total} 条，"
                f"实际送入 {messages_used} 条"
            )

        raw = await self._call_llm(provider_id, prompt)
        sections = self._parse_sections(raw)

        return SummaryResult(
            stats=stats,
            sections=sections,
            raw_llm_text=raw,
            provider_id=provider_id,
            messages_used=messages_used,
            sources={},
            scope_desc=scope_desc,
        )

    # ========== 统计 ==========

    async def _get_top_n(self) -> int:
        """读取活跃排行条数配置（int，非法/非正数回退默认 5）。"""
        value = await self.config_mgr.get_summary_setting_typed("summary_rank_top_n")
        # bool 是 int 子类需显式排除；配置层已兜底，此处仅防御
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            return _DEFAULT_TOP_N
        return value

    def _build_stats(self, messages: list[ChatMessage], top_n: int) -> StatsResult:
        """基于**全量**消息计算统计结果（不受后续长度截断影响）。

        top_senders 按 sender_id 聚合条数降序取 Top N（同数以 sender_id 升序稳定排序），
        昵称取该用户最近一次消息的 sender_name（为空回退 sender_id）。
        """
        counts: dict[str, int] = {}
        last_name: dict[str, str] = {}
        for msg in messages:
            counts[msg.sender_id] = counts.get(msg.sender_id, 0) + 1
            # 消息已按时间升序：后遇到即更近；昵称为空时保留上一个非空值
            last_name[msg.sender_id] = msg.sender_name or last_name.get(
                msg.sender_id, ""
            )

        top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:top_n]
        top_senders = [(sid, last_name.get(sid) or sid, count) for sid, count in top]

        return StatsResult(
            total=len(messages),
            participant_count=len(counts),
            time_start=messages[0].timestamp if messages else None,
            time_end=messages[-1].timestamp if messages else None,
            top_senders=top_senders,
        )

    # ========== 素材格式化与长度预算 ==========

    def _format_messages(self, messages: list[ChatMessage]) -> list[str]:
        """逐条格式化为 ``[YYYY-MM-DD HH:MM] 昵称: 内容``（昵称为空用 sender_id）。"""
        return [
            f"[{msg.timestamp:%Y-%m-%d %H:%M}] {msg.sender_name or msg.sender_id}: "
            f"{msg.content}"
            for msg in messages
        ]

    def _render_prompt(
        self,
        template: str,
        values: dict[str, str],
        material_lines: list[str],
    ) -> tuple[str, int, bool]:
        """渲染占位符并对完整 prompt 应用长度预算。

        先替换除 ``{messages}`` 外的占位符得到骨架，再估算素材可用字符数；
        超限时从**最旧**的消息开始逐条丢弃（即保留最近的消息），直至不超限。

        Returns:
            (最终 prompt, 实际送入的消息条数, 是否发生截断)
        """
        scaffold = template
        for placeholder, value in values.items():
            scaffold = scaffold.replace(placeholder, value)

        # 骨架长度（{messages} 替换为空）决定素材可用预算
        base_len = len(scaffold.replace("{messages}", ""))
        available = MAX_PROMPT_CHARS - base_len

        joined_all = "\n".join(material_lines)
        if len(joined_all) <= available:
            kept = material_lines
            truncated = False
        else:
            # 从尾部（最近的消息）向前累积，每条另计 1 个换行符（首条不计）；
            # 单条即超预算时仍保留该条作为最后手段（至少送入 1 条）
            kept_rev: list[str] = []
            used = 0
            for line in reversed(material_lines):
                add = len(line) + (1 if kept_rev else 0)
                if used + add > available and kept_rev:
                    break
                kept_rev.append(line)
                used += add
                if used > available:
                    break
            kept = list(reversed(kept_rev))
            truncated = len(kept) < len(material_lines)

        prompt = scaffold.replace("{messages}", "\n".join(kept))

        # 渲染自检：用户自定义模板可能含未知占位符，兜底清空并告警
        leftover = _PLACEHOLDER_RE.findall(prompt)
        if leftover:
            logger.warning(
                f"[HistorySummary] 渲染后 prompt 仍含未替换占位符，已置空: {leftover}"
            )
            prompt = _PLACEHOLDER_RE.sub("", prompt)

        return prompt, len(kept), truncated

    # ========== Provider 解析 ==========

    async def _resolve_provider_id(self, event: AstrMessageEvent) -> str:
        """解析总结用 LLM provider：配置优先，回退会话 provider，皆无则抛异常。

        注意：``context.get_current_chat_provider_id(umo)`` 在未找到时
        **抛出 ProviderNotFoundError**（而非返回空值），此处统一捕获视为回退失败。
        """
        configured = (
            await self.config_mgr.get_summary_setting("summary_provider_id")
        ).strip()
        if configured:
            return configured

        try:
            provider_id = await self.context.get_current_chat_provider_id(
                event.unified_msg_origin
            )
        except Exception as e:
            logger.warning(
                f"[HistorySummary] 获取会话 provider 失败（将视为未配置）: {e}"
            )
            provider_id = ""

        if not provider_id:
            raise SummaryProviderError(
                "未配置总结 provider（summary_provider_id 为空）且无法获取会话 provider"
            )
        return provider_id

    def _resolve_group_id(
        self, event: AstrMessageEvent, messages: list[ChatMessage]
    ) -> str:
        """群号取值：优先 messages 中首个非空 group_id，回退 event.get_group_id()。"""
        for msg in messages:
            if msg.group_id:
                return msg.group_id
        return event.get_group_id() or ""

    # ========== 提示词文本块 ==========

    def _render_stats_text(self, stats: StatsResult) -> str:
        """生成 ``{stats}`` 占位符的统计文本块（逐行）。"""
        lines = [
            f"消息总数: {stats.total}",
            f"参与者: {stats.participant_count}",
        ]
        if stats.time_start is not None and stats.time_end is not None:
            lines.append(
                f"时间跨度: {stats.time_start:%Y-%m-%d %H:%M} ~ "
                f"{stats.time_end:%Y-%m-%d %H:%M}"
            )
        else:
            lines.append("时间跨度: 未知")
        if stats.top_senders:
            lines.append(f"活跃排行 (Top {len(stats.top_senders)}):")
            for rank, (sid, name, count) in enumerate(stats.top_senders, 1):
                lines.append(f"{rank}. {name}({sid}): {count} 条")
        else:
            lines.append("活跃排行: 无数据")
        return "\n".join(lines)

    # ========== LLM 调用 ==========

    async def _call_llm(self, provider_id: str, prompt: str) -> str:
        """调用 LLM 并提取纯文本。

        使用 AstrBot v4.5.7+ SDK：``context.llm_generate(chat_provider_id=...,
        prompt=...)``，返回 :class:`LLMResponse`，经 ``completion_text`` 属性
        提取纯文本（该属性优先取 result_chain 的纯文本拼接）。

        Raises:
            RuntimeError: LLM 返回空文本。
            Exception: 调用异常记 error 日志后原样向上抛。
        """
        try:
            llm_resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
            )
        except Exception:
            logger.error("[HistorySummary] LLM 调用失败", exc_info=True)
            raise

        raw = (getattr(llm_resp, "completion_text", "") or "").strip()
        if not raw:
            logger.error("[HistorySummary] LLM 调用失败：返回文本为空")
            raise RuntimeError("LLM 返回空总结文本")
        return raw

    # ========== 板块解析 ==========

    def _parse_sections(self, raw: str) -> list[tuple[str, str]]:
        """best-effort 按 4 板块标题行切分 LLM 输出。

        匹配规则（宽松）：逐行扫描，行内（不区分大小写，兼容 LLM 输出
        ``Todo`` 等变体）含「重要通知 / 讨论要点 / 有趣片段 / TODO」关键词的
        **首次出现**视为该板块标题行，标题归一为模板标准标题（含 emoji）。
        板块内容 = 该标题行（不含）至下一标题行（不含）之间的文本，去首尾空白。

        兜底：匹配到的板块少于 2 个 → 视为切分失败，返回 ``[("全部", raw)]``。
        成功时按标准 4 板块顺序输出（models.py 契约）。
        """
        lines = raw.splitlines()
        first_hits: dict[str, int] = {}
        for idx, line in enumerate(lines):
            lowered = line.lower()
            for keyword, title in _SECTION_MATCH_RULES:
                if keyword.lower() in lowered and title not in first_hits:
                    first_hits[title] = idx
                    break

        if len(first_hits) < 2:
            logger.warning(
                f"[HistorySummary] 板块切分失败（仅匹配到 {len(first_hits)} 个板块标题），"
                "回退单段输出"
            )
            return [("全部", raw)]

        ordered = sorted(first_hits.items(), key=lambda kv: kv[1])
        sections: list[tuple[str, str]] = []
        for i, (title, start) in enumerate(ordered):
            end = ordered[i + 1][1] if i + 1 < len(ordered) else len(lines)
            body = "\n".join(lines[start + 1 : end]).strip()
            sections.append((title, body))

        # 归一到标准 4 板块顺序
        title_order = {title: i for i, title in enumerate(SECTION_TITLES)}
        sections.sort(key=lambda section: title_order[section[0]])
        return sections
