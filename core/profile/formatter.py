"""人物分析结果输出格式化（合并转发 / 文转图 / 纯文本）。

模块 H（v0.4.0 人物分析）：将 ProfileAnalyzer 产出的 ProfileResult 渲染为
可直接发送的 MessageChain，三种模式（``profile_output_mode``）：

- ``forward``：合并转发。1 个报告头节点 + N 个板块节点 + 1 个页脚节点，全部
  包进单个 ``Comp.Nodes``（aiocqhttp 适配器对链中每个 Node/Nodes 组件各发
  一条 ``send_group_forward_msg``——见 ``aiocqhttp_message_event.send_message``，
  散落 Node 会被单独包进 ``Nodes([seg])`` 各自成一条合并转发，故必须单个
  Nodes 聚合才能合成一条合并转发）。所有节点文本经 ``strip_markdown()``
  剥离 Markdown 标记；页脚固定附 AI 推测免责声明（PRD §8 合规）；
- ``image``：保留 Markdown。三级降级：① 自研人物报告模板（Module G 交付的
  ``profile/t2i_render.py::ProfileT2IRenderer``，构造时注入实例；其 ``render()``
  内部绝不抛异常、失败返回 None）；② ``Star.text_to_image``（默认 t2i 模板
  直接吃 Markdown）；③ 剥 Markdown 的纯文本链。每级失败记 ``[Profile]``
  warning 后降级；
- ``text``：剥 Markdown 的纯文本消息链（报告头 + 各板块 + 免责声明页脚）。

``render()`` 保证不向上抛异常：任何渲染失败均有兜底消息链。

独立性说明（PRD §3.0）：``profile/`` 与 ``summary/`` 平行、互不依赖，故本模块
**不 import** ``summary.formatter``（经 ``summary/__init__`` 会牵出整条服务链）。
``strip_markdown`` 等 Markdown 辅助函数按 ``summary/formatter.py`` 的同款算法
与转义 stash 思路自实现等价逻辑（各函数注释标明 canonical 参考）。

框架 API 签名核实记录（AstrBot 主项目源码 ``astrbot/core/message/components.py``
+ ``astrbot/core/platform/sources/aiocqhttp/aiocqhttp_message_event.py``）：

- ``MessageChain``：``from astrbot.api.event import MessageChain``，构造
  ``MessageChain(chain=[组件...])``；
- ``Comp.Node(content=[...], uin="0", name="")``：``content: list[BaseMessageComponent]``，
  ``uin: str|None="0"``（OneBot v11 合并转发要求数字 user_id），``name: str|None=""``；
- ``Comp.Nodes(nodes=[...])``：``nodes: list[Node]``；适配器 ``send_message``
  中 ``isinstance(seg, Node | Nodes)`` 各触发一次 ``send_group_forward_msg``，
  单个 Node 会被包装为 ``Nodes([seg])`` 单独发送（聚合必要性依据）；
- ``Star.text_to_image(text: str, return_url=True) -> str``（return_url=True
  返回 URL；网络渲染失败降级本地渲染时返回本地文件路径）。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.star import Star

from .models import ProfileResult

if TYPE_CHECKING:
    from ..db_config import ConfigManager
    from .t2i_render import ProfileT2IRenderer

# Node 署名兜底值：OneBot v11 合并转发要求 user_id 为数字，取不到 bot 自身
# QQ 时用官方文档合并转发示例同款占位 uin。
_FALLBACK_UIN = "10000"
# bot 昵称在框架层无稳定获取渠道，统一用固定展示名（人物分析专用）。
_FALLBACK_NAME = "人物画像助手"

# 数据源展示名映射（ProfileResult.sources 的 key → 文案）
_SOURCE_NAMES = {"mysql": "MySQL", "onebot": "OneBot"}

# 星期标签（ProfileStats.peak_weekday 约定 Mon=0..Sun=6，见 profile/models.py）
_WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

# 页脚免责声明（PRD §8 合规要求，固定文案；与 profile/t2i_render.py::_DISCLAIMER
# 逐字一致，保证三种输出形态免责措辞统一）
_DISCLAIMER = "本报告基于公开群聊记录由 AI 生成，仅为推测，仅供参考"

# 关系上下文不完整提示行（ProfileFetcher 降级时 relation_context_complete=False，
# PRD §3.2：关系板块退化为基于目标提及他人的浅层推断并明确标注）
_INCOMPLETE_HINT = "⚠ 关系上下文不完整，人物关系板块为浅层推断"

# 零宽空格（U+200B）：转发节点 / 纯文本首尾填充用。aiocqhttp 发送 Plain 时
# 会 strip() 掉首尾空白与换行（官方 send-message.md 提示），以不可见零宽
# 空格补齐可保留排版空行（范式同 summary/formatter.py）。
_ZWSP = chr(0x200B)


# ---------------------------------------------------------------------------
# 纯函数工具（canonical 参考 summary/formatter.py，PRD §3.0 禁止跨包 import）
# ---------------------------------------------------------------------------


def strip_markdown(text: str) -> str:
    """剥离常见 Markdown 标记，保留纯文本（模块级纯函数，无副作用）。

    canonical 实现为 ``summary/formatter.py::strip_markdown``；因 PRD §3.0
    约定 profile/ 与 summary/ 互不依赖，此处按同款算法与转义 stash 思路
    自实现等价逻辑。覆盖范围：

    - 行首 ``#`` ~ ``######`` 标题标记（保留文字）
    - ``**粗体**`` / ``*斜体*`` / ``__下划线__`` / ``_斜体_``
    - ``~~删除线~~``
    - 行内 ``` `代码` ``` 与 ```` ``` ```` 围栏代码块标记（保留代码内容本身，
      通过占位符保护，避免被强调/列表等规则误伤）
    - ``>`` 引用
    - ``[文字](url)`` 链接（只留文字，含 ``![](...)`` 图片）
    - 行首列表标记 ``- `` / ``* `` / ``+ `` / ``1.``
    - 反斜杠转义 ``\\*`` 等
    - 表格竖线不做特殊处理（原样保留）

    Args:
        text: 任意输入。非 str 先 ``str()`` 化；None / 空串返回空串。

    Returns:
        剥离标记后的纯文本。
    """
    if not text:
        return ""
    if not isinstance(text, str):
        text = str(text)

    # --- 先保护代码内容（围栏块 / 行内代码），后续规则不碰占位符 ---
    stashed: list[str] = []

    def _stash(content: str) -> str:
        stashed.append(content)
        return f"\x00{len(stashed) - 1}\x00"

    # 围栏代码块：按行扫描，成对 ``` / ~~~ 之间的内容整体保护
    out_lines: list[str] = []
    code_buf: list[str] = []
    in_fence = False
    for line in text.split("\n"):
        if re.match(r"^\s*(```|~~~)", line):
            if in_fence:
                out_lines.append(_stash("\n".join(code_buf)))
                code_buf = []
            in_fence = not in_fence
            continue
        (code_buf if in_fence else out_lines).append(line)
    if code_buf:  # 未闭合围栏：内容照样保留
        out_lines.append("\n".join(code_buf))
    text = "\n".join(out_lines)

    # 行内代码 `code` → 保护内容
    text = re.sub(r"`([^`\n]+)`", lambda m: _stash(m.group(1)), text)

    # 反斜杠转义 \* \_ 等 → 提前保护为字面字符（占位），
    # 避免 \* 被后续斜体规则误吃；最终还原阶段恢复为裸字符
    text = re.sub(r"\\([\\`*_~>\[\]()#+\-.!|])", lambda m: _stash(m.group(1)), text)

    # 图片/链接 [文字](url) → 只留文字（图片先于链接处理）
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)

    # 行首标题 # ~ ######（保留文字）
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.M)
    # 引用 >（支持多层 >>）
    text = re.sub(r"^[ \t]*>+[ \t]?", "", text, flags=re.M)
    # 行首无序列表标记 - / * / +（需后跟空白，避免误伤 --- 分隔线）
    text = re.sub(r"^[ \t]*[-*+][ \t]+", "", text, flags=re.M)
    # 行首有序列表 1. / 1)
    text = re.sub(r"^[ \t]*\d+[.)][ \t]+", "", text, flags=re.M)

    # 强调标记：先三再双再单，避免 ***/___ 被半截匹配
    text = re.sub(r"\*\*\*(.+?)\*\*\*", r"\1", text)
    text = re.sub(r"___(.+?)___", r"\1", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)
    text = re.sub(r"\*([^*\n]+)\*", r"\1", text)
    # 单下划线斜体：要求两侧不接单词字符，避免误伤 snake_case 标识符
    text = re.sub(r"(?<!\w)_([^_\n]+)_(?!\w)", r"\1", text)
    # 删除线
    text = re.sub(r"~~(.+?)~~", r"\1", text)

    # --- 还原被保护的代码内容 ---
    for i, content in enumerate(stashed):
        text = text.replace(f"\x00{i}\x00", content)
    return text


def _fmt_time(dt: datetime | None) -> str:
    """datetime → ``YYYY-MM-DD HH:MM``；None 或异常 → ``未知``。

    与 ``summary.formatter._fmt_time`` 等价的自实现（避免跨包耦合）。
    """
    if dt is None:
        return "未知"
    try:
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "未知"


def _fmt_weekday(value: object) -> str:
    """peak_weekday（Mon=0..Sun=6）→ 星期名；非法 / 越界 → ``未知``。"""
    try:
        idx = int(value)
    except (TypeError, ValueError):
        return "未知"
    if 0 <= idx < len(_WEEKDAY_NAMES):
        return _WEEKDAY_NAMES[idx]
    return "未知"


def _fmt_sources(sources: dict | None) -> str:
    """数据源构成 → ``MySQL 1200 + OneBot 300``；空 / None → ``未知``。"""
    parts = [
        f"{_SOURCE_NAMES.get(str(k), str(k))} {v}" for k, v in (sources or {}).items()
    ]
    return " + ".join(parts) if parts else "未知"


# ---------------------------------------------------------------------------
# 格式化器
# ---------------------------------------------------------------------------


class ProfileFormatter:
    """人物分析结果格式化器：ProfileResult → 可发送的 MessageChain。

    Attributes:
        star: 插件 Star 实例。用于调用 ``text_to_image``（image 模式二级降级），
            以及经 ``star.context`` 防御式获取 bot 自身 uin（forward 节点署名）。
        config_mgr: ``db_config.ConfigManager`` 实例。本模块当前不读取配置，
            仅为与编排层注入签名一致而持有（渲染配置共用 summary_t2i_*，
            由 renderer 侧读取）。
        renderer: ``ProfileT2IRenderer`` 实例（自研人物报告模板渲染器，image
            模式首选渲染级）。其 ``render()`` 契约绝不抛异常、失败返回 None；
            注入 None 时 image 模式跳过一级降级（行为回退 text_to_image 链路）。
    """

    def __init__(
        self,
        star: Star,
        config_mgr: ConfigManager | None = None,
        renderer: ProfileT2IRenderer | None = None,
    ) -> None:
        self.star = star
        self.config_mgr = config_mgr
        self.renderer = renderer

    async def render(self, result: ProfileResult, output_mode: str) -> MessageChain:
        """将人物分析结果渲染为消息链。

        Args:
            result: ProfileAnalyzer 产出的完整人物分析结果。
            output_mode: ``"forward"`` → 合并转发（报告头 + 各板块 + 页脚，
                所有文本剥 Markdown）；``"image"`` → 文转图（三级降级：自研
                模板 → text_to_image → 纯文本）；``"text"`` → 剥 Markdown 的
                纯文本消息链。其他取值（含空串 / None / 大小写与首尾空白）
                一律按 forward 处理。

        Returns:
            MessageChain。任何分支均不向上抛异常，渲染失败有兜底消息链。
        """
        try:
            mode = str(output_mode or "").strip().lower()
            if mode == "image":
                return await self._render_image(result)
            if mode == "text":
                return self._render_text(result)
            return self._render_forward(result)
        except Exception:
            # 双保险：各分支已自带兜底，此处保证 render 绝不抛异常
            logger.warning(
                "[Profile] render 出现未预期异常，回退纯文本消息链",
                exc_info=True,
            )
            return self._fallback_text_chain(result)

    # ------------------------------------------------------------------
    # forward 模式
    # ------------------------------------------------------------------

    def _render_forward(self, result: ProfileResult) -> MessageChain:
        """组装合并转发消息链：报告头节点 + 各板块节点 + 页脚免责声明节点。

        全部节点包进单个 ``Comp.Nodes``：aiocqhttp 适配器对链中每个
        Node/Nodes 组件各调用一次 ``send_group_forward_msg``（见
        ``aiocqhttp_message_event.send_message``），散落多个 Node 会被各自
        包成 ``Nodes([seg])`` 拆成多条合并转发，故必须用单个 Nodes 聚合。
        sections 可能少于或多于 4（解析失败时为单段），按实际遍历。
        """
        uin, name = self._bot_identity()
        nodes: list[Comp.Node] = [
            self._make_node(uin, name, self._build_header_text(result))
        ]
        for title, content in result.sections or []:
            nodes.append(self._make_node(uin, name, f"{title}\n\n{content}"))
        nodes.append(self._make_node(uin, name, self._build_footer_text(result)))
        return MessageChain(chain=[Comp.Nodes(nodes=nodes)])

    @staticmethod
    def _make_node(uin: str, name: str, text: str) -> Comp.Node:
        """构造单个转发节点：文本统一过 ``strip_markdown`` 再包 Comp.Plain。

        首尾补零宽空格 U+200B：aiocqhttp 发送 Plain 时会 strip() 掉首尾
        空白与换行（官方 send-message.md 提示），补齐以保留排版空行。
        """
        plain = strip_markdown(text)
        return Comp.Node(
            uin=uin,
            name=name,
            content=[Comp.Plain(_ZWSP + plain + _ZWSP)],
        )

    @staticmethod
    def _build_header_text(result: ProfileResult) -> str:
        """报告头节点文本：目标 / QQ / 范围 / 时间跨度 / 数据源 / 关键统计。

        关系上下文不完整（Fetcher 降级，``relation_context_complete=False``）
        时追加浅层推断提示行（PRD §3.2）。
        """
        target = result.target
        stats = result.stats
        qq = str(target.sender_id or "")
        display = str(target.sender_name or "") or qq or "未知用户"
        lines: list[str] = [f"人物画像 · {display}", ""]
        if qq:
            lines.append(f"QQ号：{qq}")
        if result.scope_desc:
            lines.append(f"分析范围：{result.scope_desc}")
        lines.append(
            f"时间跨度：{_fmt_time(stats.time_start)} ~ {_fmt_time(stats.time_end)}"
        )
        lines.append(f"数据源构成：{_fmt_sources(result.sources)}")
        lines.append(f"消息总数：{stats.total}")
        lines.append(f"活跃天数：{stats.active_days} 天")
        lines.append(f"最活跃时段：{stats.peak_hour} 点")
        lines.append(f"最活跃星期：{_fmt_weekday(stats.peak_weekday)}")
        if not result.relation_context_complete:
            lines.append(_INCOMPLETE_HINT)
        return "\n".join(lines)

    @staticmethod
    def _build_footer_text(result: ProfileResult) -> str:
        """页脚节点文本：provider 标识 + 免责声明（PRD §8 合规）。"""
        provider = str(getattr(result, "provider_id", "") or "") or "会话默认"
        return f"生成模型：{provider}\n{_DISCLAIMER}"

    # ------------------------------------------------------------------
    # text 模式
    # ------------------------------------------------------------------

    def _render_text(self, result: ProfileResult) -> MessageChain:
        """text 模式：剥 Markdown 的纯文本消息链（报告头 + 各板块 + 免责声明页脚）。"""
        text = (
            strip_markdown(self._build_full_markdown(result)) or "（人物画像内容为空）"
        )
        return MessageChain(chain=[Comp.Plain(_ZWSP + text + _ZWSP)])

    # ------------------------------------------------------------------
    # image 模式（三级降级）
    # ------------------------------------------------------------------

    def _build_full_markdown(self, result: ProfileResult) -> str:
        """image / text 模式共用完整 Markdown：报告头 + 各板块 + 页脚（保留 Markdown）。

        板块标题防御式去除行首 ``#``（宽松切分兜底时标题可能自带标记），
        避免与 ``## `` 前缀叠加。
        """
        target = result.target
        stats = result.stats
        qq = str(target.sender_id or "")
        display = str(target.sender_name or "") or qq or "未知用户"
        lines: list[str] = [f"# 人物画像 · {display}", ""]
        if qq:
            lines.append(f"- QQ号：{qq}")
        if result.scope_desc:
            lines.append(f"- 分析范围：{result.scope_desc}")
        lines.append(
            f"- 时间跨度：{_fmt_time(stats.time_start)} ~ {_fmt_time(stats.time_end)}"
        )
        lines.append(f"- 数据源构成：{_fmt_sources(result.sources)}")
        lines.append(f"- 消息总数：{stats.total}")
        lines.append(f"- 活跃天数：{stats.active_days} 天")
        lines.append(f"- 最活跃时段：{stats.peak_hour} 点")
        lines.append(f"- 最活跃星期：{_fmt_weekday(stats.peak_weekday)}")
        if not result.relation_context_complete:
            lines.append(f"- {_INCOMPLETE_HINT}")
        for sec_title, sec_content in result.sections or []:
            title = str(sec_title or "").lstrip("#").strip() or "板块"
            lines.extend(["", f"## {title}", "", str(sec_content or "")])
        provider = str(getattr(result, "provider_id", "") or "") or "会话默认"
        lines.extend(["", "---", "", f"生成模型：{provider}", "", _DISCLAIMER])
        return "\n".join(lines)

    async def _render_image(self, result: ProfileResult) -> MessageChain:
        """文转图：自研 T2I 模板 →（失败）text_to_image →（失败）纯文本兜底。

        首选自研人物报告模板（``ProfileT2IRenderer``：双主题 / CDN 容灾 /
        小时·星期活动图表，两轮渲染 + 魔数校验），其 ``render()`` 内部保证
        不抛异常、失败返回 None；构造时注入 None 则跳过该级。每级失败记
        ``[Profile]`` warning 后降级，绝不向上抛异常。
        """
        # 1) 自研 T2I 人物报告模板（两轮渲染 + 魔数校验，内部绝不抛异常）
        if self.renderer is not None:
            try:
                chain = await self.renderer.render(result)
            except Exception:
                # 契约约定 render() 绝不抛异常；此处仅防御兜底，
                # 避免违约实现击穿降级链
                logger.warning(
                    "[Profile] 自研 T2I 渲染器抛出未预期异常，降级 text_to_image",
                    exc_info=True,
                )
                chain = None
            if chain is not None:
                return chain
            logger.warning("[Profile] 自研 T2I 模板渲染失败，降级 text_to_image")

        # 2) Star.text_to_image(text, return_url=True)：默认 t2i 模板直接吃 Markdown
        try:
            chain = self._image_chain(
                await self.star.text_to_image(self._build_full_markdown(result))
            )
            if chain is not None:
                return chain
            logger.warning("[Profile] text_to_image 返回为空，回退纯文本消息链")
        except Exception:
            logger.warning(
                "[Profile] text_to_image 渲染失败，回退纯文本消息链",
                exc_info=True,
            )

        # 3) 兜底：剥 Markdown 的纯文本消息链
        return self._fallback_text_chain(result)

    @staticmethod
    def _image_chain(url: str | None) -> MessageChain | None:
        """渲染器返回值 → 图片消息链。

        ``return_url=True`` 时渲染器正常返回 http(s) URL；但网络渲染失败
        降级本地渲染（t2i/renderer.render_t2i 的 fallback 分支）时会返回
        本地文件路径，故 http(s) 走 fromURL、其余走 fromFileSystem 两路兜底。
        """
        if not url:
            return None
        if url.startswith(("http://", "https://")):
            return MessageChain(chain=[Comp.Image.fromURL(url)])
        return MessageChain(chain=[Comp.Image.fromFileSystem(url)])

    def _fallback_text_chain(self, result: ProfileResult) -> MessageChain:
        """渲染彻底失败时的兜底：剥 Markdown 的纯文本消息链。

        自身保证不抛异常：result 字段缺损（如 stats=None）时退化为静态提示，
        确保 render()「绝不向上抛异常」的契约在最末一环仍成立。
        """
        try:
            text = (
                strip_markdown(self._build_full_markdown(result))
                or "（人物画像内容为空）"
            )
        except Exception:
            logger.warning("[Profile] 兜底文本构造失败，使用静态提示", exc_info=True)
            text = "人物画像渲染失败，请稍后重试"
        return MessageChain(chain=[Comp.Plain(_ZWSP + text + _ZWSP)])

    # ------------------------------------------------------------------
    # Node 署名
    # ------------------------------------------------------------------

    def _bot_identity(self) -> tuple[str, str]:
        """获取 bot 自身 uin/name 用于合并转发节点署名，拿不到则兜底。

        范式同 ``summary.formatter.SummaryFormatter._bot_identity``：经
        ``star.context.platform_manager.platform_insts`` 遍历平台实例，
        取 ``Platform.client_self_id``（Platform 基类真实属性，aiocqhttp
        适配器启动后即 bot QQ）。OneBot v11 合并转发要求数字 user_id，
        故仅在纯数字时采用；client_self_id 默认值是 uuid hex（未就绪），
        自动落入兜底。bot 昵称在框架层无稳定获取渠道，统一用固定名。
        全程防御式 getattr + try/except，任何异常走兜底，不阻断渲染。
        """
        try:
            context = getattr(self.star, "context", None)
            mgr = getattr(context, "platform_manager", None)
            for inst in getattr(mgr, "platform_insts", None) or []:
                uin = str(getattr(inst, "client_self_id", "") or "").strip()
                if uin.isdigit():
                    return uin, _FALLBACK_NAME
        except Exception as e:
            logger.debug(f"[Profile] 获取 bot 自身信息失败，使用兜底署名: {e}")
        return _FALLBACK_UIN, _FALLBACK_NAME
