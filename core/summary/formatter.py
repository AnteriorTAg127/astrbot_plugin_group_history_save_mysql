"""总结结果输出格式化（合并转发 / 文转图）。

模块 F（v0.3 群聊历史自动总结）：将 SummaryEngine 产出的 SummaryResult
渲染为可直接发送的 MessageChain，两种模式：

- ``forward``：合并转发。1 个统计节点 + N 个板块节点，全部包进单个
  ``Comp.Nodes``（aiocqhttp 适配器对链中每个 Node/Nodes 各发一条
  ``send_group_forward_msg``，必须单个 Nodes 包裹才能合成一条合并转发），
  所有节点文本经 ``strip_markdown()`` 剥离 Markdown 标记；
- ``image``：保留 Markdown。三级降级：① 自研报告模板（v0.3.2，
  ``summary/t2i_render.py`` 的 ``T2IRenderer``：双主题/CDN 容灾/柱状图/
  表格，两轮 ``html_render`` + 魔数校验；需构造时注入 ``config_mgr``，
  缺省时跳过此级，签名向后兼容 v0.3.1）；② ``Star.text_to_image``（默认
  t2i 模板直接支持 Markdown）；③ 剥 Markdown 的纯文本链。
  ``_markdown_to_html``（轻量正则 Markdown→HTML，v0.3.2 起扩展 GFM 表格）
  保留，供 ``T2IRenderer`` 生成板块兜底 HTML 复用（原最小化 ``_IMAGE_TMPL``
  及其 ``html_render`` 调用已被自研模板取代并删除）。

``render()`` 保证不向上抛异常：任何渲染失败均有兜底消息链。

框架 API 签名核实记录（AstrBot 主项目源码 + docs/zh/dev/star/guides）：

- ``MessageChain``：``from astrbot.api.event import MessageChain``，
  dataclass，构造 ``MessageChain(chain=[组件...])``；
- ``Comp.Node(content=[...], uin="0", name="")`` / ``Comp.Nodes(nodes=[...])``
  / ``Comp.Plain(text)`` / ``Comp.Image.fromURL(url)`` / ``fromFileSystem(path)``：
  ``import astrbot.api.message_components as Comp``；
- ``Star.text_to_image(text: str, return_url=True) -> str``（return_url=True
  返回 URL；网络渲染失败降级本地渲染时返回本地文件路径）；
- ``Star.html_render(tmpl: str, data: dict, return_url=True, options=None) -> str``
  （HTML + Jinja2 模板，不吃 Markdown）。
"""

from __future__ import annotations

import html
import re
from datetime import datetime
from typing import TYPE_CHECKING

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.star import Star

from .models import SummaryResult

if TYPE_CHECKING:
    from ..db_config import ConfigManager

# Node 署名兜底值：OneBot v11 合并转发要求 user_id 为数字，取不到 bot 自身
# QQ 时用官方文档合并转发示例同款占位 uin。
_FALLBACK_UIN = "10000"
# bot 昵称在框架层无稳定获取渠道，统一用固定展示名。
_FALLBACK_NAME = "总结助手"

# 数据源展示名映射（SummaryResult.sources 的 key → 文案）
_SOURCE_NAMES = {"mysql": "MySQL", "onebot": "OneBot"}

# GFM 表格识别（_markdown_to_html 用）：管道行 = 首尾 | 包裹；分隔符行 =
# 单元格仅由 -/:/空格/| 组成（--- / :--- / ---: / :---:，冒号对齐可选）
_RE_TABLE_ROW = re.compile(r"^\s*\|(.+)\|\s*$")
_RE_TABLE_SEP = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$")


# ---------------------------------------------------------------------------
# 纯函数工具
# ---------------------------------------------------------------------------


def strip_markdown(text: str) -> str:
    """剥离常见 Markdown 标记，保留纯文本（模块级纯函数，无副作用）。

    覆盖范围：

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
    """datetime / ``YYYY-MM-DD HH:MM:SS`` 字符串 → ``YYYY-MM-DD HH:MM``。

    存储 JSON 的 time_start/time_end 为字符串（v0.4.2 Web 导出走存储 JSON
    渲染，需兼容解析）；None 或不可解析 → ``未知``。
    """
    if dt is None:
        return "未知"
    if isinstance(dt, str):
        dt = dt.strip()
        if not dt:
            return "未知"
        if dt.endswith("T"):
            dt = dt[:-1]
        try:
            return datetime.strptime(dt, "%Y-%m-%d %H:%M:%S").strftime("%Y-%m-%d %H:%M")
        except ValueError:
            try:
                return datetime.strptime(dt, "%Y-%m-%d %H:%M").strftime(
                    "%Y-%m-%d %H:%M"
                )
            except ValueError:
                return "未知"
    try:
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "未知"


def _markdown_to_html(text: str) -> str:
    """轻量 Markdown → HTML（标题/粗体/斜体/行内代码/列表/引用/代码块/GFM 表格）。

    服务于图片模式的兜底渲染路径（v0.3.1 前为 ``html_render`` 降级转换；
    v0.3.2 起为自研 T2I 模板 CDN 全挂时的预转换 HTML，由
    ``summary/t2i_render.py`` 复用）。不引第三方依赖，用行级正则做最小可用
    转换。输入先经 ``html.escape`` 转义，防止内容中的尖括号破坏页面结构。

    GFM 表格（v0.3.2 新增）：当前行为管道行（``| ... |``）且下一行为分隔符
    行（``---`` / ``:---`` / ``---:`` / ``:---:`` 单元格）时，消费表头 +
    分隔行 + 后续连续管道行，输出 ``<table>``；冒号对齐不做渲染；单元格内容
    走行内转换（与标题/列表一致）。非表格输入的行为与 v0.3 完全一致。
    """

    def _inline(s: str) -> str:
        s = html.escape(s)
        s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
        return re.sub(r"\*([^*]+)\*", r"<em>\1</em>", s)

    def _split_row_cells(row: str) -> list[str]:
        """GFM 表格行 → 单元格列表：去首尾竖线后按 ``|`` 切分并 strip。"""
        inner = row.strip()
        if inner.startswith("|"):
            inner = inner[1:]
        if inner.endswith("|"):
            inner = inner[:-1]
        return [cell.strip() for cell in inner.split("|")]

    out: list[str] = []
    in_ul = in_ol = in_code = False

    def _close_lists() -> None:
        nonlocal in_ul, in_ol
        if in_ul:
            out.append("</ul>")
            in_ul = False
        if in_ol:
            out.append("</ol>")
            in_ol = False

    lines = str(text or "").split("\n")
    i = 0
    total = len(lines)
    while i < total:
        raw_line = lines[i]
        if re.match(r"^\s*(```|~~~)", raw_line):  # 围栏代码块开关
            if in_code:
                out.append("</pre>")
                in_code = False
            else:
                _close_lists()
                out.append("<pre>")
                in_code = True
            i += 1
            continue
        if in_code:
            out.append(html.escape(raw_line))
            i += 1
            continue

        # GFM 表格：管道行 + 下一行分隔符 → 表头 + 分隔行消费，后续连续
        # 管道行作表体，遇非管道行结束。仅在两条件同时满足时进入，
        # 非表格输入逐行走原有分支，行为不变
        if (
            _RE_TABLE_ROW.match(raw_line)
            and i + 1 < total
            and _RE_TABLE_SEP.match(lines[i + 1])
        ):
            _close_lists()
            header = _split_row_cells(raw_line)
            i += 2  # 消费表头行与分隔行（对齐冒号忽略，不做对齐渲染）
            body: list[list[str]] = []
            while i < total and lines[i].lstrip().startswith("|"):
                body.append(_split_row_cells(lines[i]))
                i += 1
            out.append("<table>")
            out.append(
                "<thead><tr>"
                + "".join(f"<th>{_inline(cell)}</th>" for cell in header)
                + "</tr></thead>"
            )
            if body:
                out.append("<tbody>")
                out.extend(
                    "<tr>"
                    + "".join(f"<td>{_inline(cell)}</td>" for cell in row)
                    + "</tr>"
                    for row in body
                )
                out.append("</tbody>")
            out.append("</table>")
            continue

        m = re.match(r"^(#{1,6})\s+(.*)$", raw_line)
        if m:
            _close_lists()
            level = len(m.group(1))
            out.append(f"<h{level}>{_inline(m.group(2))}</h{level}>")
            i += 1
            continue
        m = re.match(r"^\s*[-*+]\s+(.*)$", raw_line)
        if m:
            if not in_ul:
                _close_lists()
                out.append("<ul>")
                in_ul = True
            out.append(f"<li>{_inline(m.group(1))}</li>")
            i += 1
            continue
        m = re.match(r"^\s*\d+[.)]\s+(.*)$", raw_line)
        if m:
            if not in_ol:
                _close_lists()
                out.append("<ol>")
                in_ol = True
            out.append(f"<li>{_inline(m.group(1))}</li>")
            i += 1
            continue
        m = re.match(r"^\s*>+\s?(.*)$", raw_line)
        if m:
            _close_lists()
            out.append(f"<blockquote>{_inline(m.group(1))}</blockquote>")
            i += 1
            continue
        if not raw_line.strip():
            _close_lists()
            out.append("<br>")
            i += 1
            continue
        _close_lists()
        out.append(f"<p>{_inline(raw_line)}</p>")
        i += 1

    if in_code:
        out.append("</pre>")
    _close_lists()
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 格式化器
# ---------------------------------------------------------------------------


class SummaryFormatter:
    """总结结果格式化器：SummaryResult → 可发送的 MessageChain。

    Attributes:
        star: 插件 Star 实例。用于调用 ``text_to_image`` / ``html_render``，
            以及经 ``star.context`` 防御式获取 bot 自身 uin。
        t2i: v0.3.2 自研模板渲染器（``T2IRenderer``）。仅当构造时注入
            ``config_mgr`` 时创建；为 None 时 image 模式跳过自研模板级，
            行为回退 v0.3.1 链路（签名向后兼容）。
    """

    def __init__(self, star: Star, config_mgr: ConfigManager | None = None) -> None:
        self.star = star
        # T2IRenderer 经局部导入引入：t2i_render 顶层复用本模块的
        # _markdown_to_html，顶层互相导入会成环，故延迟到构造期导入。
        # config_mgr 缺省（v0.3.1 旧调用点）时 t2i 为 None，走旧链路。
        if config_mgr is not None:
            from .t2i_render import T2IRenderer

            self.t2i = T2IRenderer(star, config_mgr)
        else:
            self.t2i = None

    async def render(self, result: SummaryResult, mode: str) -> MessageChain:
        """将总结结果渲染为消息链。

        Args:
            result: SummaryEngine 产出的完整总结结果。
            mode: ``"forward"`` → 合并转发（统计节点 + 各板块节点，所有文本
                剥 Markdown）；``"image"`` → 文转图（保留 Markdown，渲染失败
                回退纯文本）。其他取值按 forward 处理。

        Returns:
            MessageChain。任何分支均不向上抛异常，渲染失败有兜底消息链。
        """
        try:
            if mode == "image":
                return await self._render_image(result)
            return self._render_forward(result)
        except Exception:
            # 双保险：各分支已自带兜底，此处保证 render 绝不抛异常
            logger.warning(
                "[HistorySummary] render 出现未预期异常，回退纯文本消息链",
                exc_info=True,
            )
            return self._fallback_text_chain(result)

    # ------------------------------------------------------------------
    # forward 模式
    # ------------------------------------------------------------------

    def _render_forward(self, result: SummaryResult) -> MessageChain:
        """组装合并转发消息链：1 个统计节点 + 按 sections 顺序的板块节点。

        全部节点包进单个 ``Comp.Nodes``：aiocqhttp 适配器对链中每个
        Node/Nodes 组件各调用一次 ``send_group_forward_msg``（见
        ``aiocqhttp_message_event._dispatch_send``），散落多个 Node 会被
        拆成多条合并转发，故必须用单个 Nodes 聚合。
        sections 可能少于或多于 4（解析失败时为单段），按实际遍历。
        """
        uin, name = self._bot_identity()
        nodes: list[Comp.Node] = [
            self._make_node(uin, name, self._build_stats_text(result))
        ]
        for title, content in result.sections or []:
            nodes.append(self._make_node(uin, name, f"{title}\n\n{content}"))
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
            content=[Comp.Plain("\u200b" + plain + "\u200b")],
        )

    @staticmethod
    def _build_stats_text(result: SummaryResult) -> str:
        """统计节点文本：总数/参与者/时间跨度/活跃排行/数据源构成。"""
        stats = result.stats
        lines: list[str] = []
        if result.scope_desc:  # 可选标题行
            lines.append(f"群聊总结 · {result.scope_desc}")
            lines.append("")
        lines.append(f"消息总数：{stats.total}")
        lines.append(f"参与者：{stats.participant_count} 人")
        lines.append(
            f"时间跨度：{_fmt_time(stats.time_start)} ~ {_fmt_time(stats.time_end)}"
        )
        if stats.top_senders:
            lines.append(f"活跃排行 Top {len(stats.top_senders)}：")
            for sender_id, sender_name, count in stats.top_senders:
                display = sender_name or sender_id or "未知用户"
                lines.append(f"{display} ×{count}")
        source_parts = [
            f"{_SOURCE_NAMES.get(str(k), str(k))} {v}"
            for k, v in (result.sources or {}).items()
        ]
        lines.append(
            "数据源构成：" + (" + ".join(source_parts) if source_parts else "未知")
        )
        if stats.truncated:
            lines.append("（素材超长，已截断保留最近消息）")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # image 模式
    # ------------------------------------------------------------------

    def _build_full_markdown(self, result: SummaryResult) -> str:
        """image 模式完整 Markdown：标题 + 统计块 + 各板块（保留 Markdown）。"""
        stats = result.stats
        title = f"群聊总结 · {result.scope_desc}" if result.scope_desc else "群聊总结"
        lines: list[str] = [f"# {title}", "", "## 统计", ""]
        lines.append(f"- 消息总数：{stats.total}")
        lines.append(f"- 参与者：{stats.participant_count} 人")
        lines.append(
            f"- 时间跨度：{_fmt_time(stats.time_start)} ~ {_fmt_time(stats.time_end)}"
        )
        if stats.top_senders:
            rank = "、".join(
                f"{sender_name or sender_id or '未知用户'} ×{count}"
                for sender_id, sender_name, count in stats.top_senders
            )
            lines.append(f"- 活跃排行 Top {len(stats.top_senders)}：{rank}")
        source_parts = [
            f"{_SOURCE_NAMES.get(str(k), str(k))} {v}"
            for k, v in (result.sources or {}).items()
        ]
        lines.append(
            "- 数据源构成：" + (" + ".join(source_parts) if source_parts else "未知")
        )
        if stats.truncated:
            lines.append("- （素材超长，已截断保留最近消息）")
        for sec_title, sec_content in result.sections or []:
            lines.extend(["", f"## {sec_title}", "", sec_content])
        lines.append("")
        return "\n".join(lines)

    async def _render_image(self, result: SummaryResult) -> MessageChain:
        """文转图：自研 T2I 模板 →（失败）text_to_image →（失败）纯文本兜底。

        v0.3.2 起首选自研报告模板（``T2IRenderer``：双主题/CDN 容灾/柱状图/
        表格，两轮 ``html_render`` + 魔数校验），其内部保证不抛异常、失败
        返回 None；``config_mgr`` 未注入（v0.3.1 旧调用点）时跳过该级。
        原最小化 ``_IMAGE_TMPL`` + ``html_render`` 降级级已被自研模板取代删除。
        """
        # 1) 自研 T2I 报告模板（两轮渲染 + 魔数校验，内部绝不抛异常）
        if self.t2i is not None:
            chain = await self.t2i.render(result)
            if chain is not None:
                return chain

        markdown = self._build_full_markdown(result)

        # 2) Star.text_to_image(text, return_url=True)：默认 t2i 模板直接吃 Markdown
        try:
            chain = self._image_chain(await self.star.text_to_image(markdown))
            if chain is not None:
                return chain
        except Exception:
            logger.warning(
                "[HistorySummary] text_to_image 渲染失败，回退纯文本消息链",
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

    def _fallback_text_chain(self, result: SummaryResult) -> MessageChain:
        """image 渲染彻底失败时的兜底：剥 Markdown 的纯文本消息链。

        自身保证不抛异常：result 字段缺损（如 stats=None）时退化为静态提示，
        确保 render()「绝不向上抛异常」的契约在最末一环仍成立。
        """
        try:
            text = (
                strip_markdown(self._build_full_markdown(result)) or "（总结内容为空）"
            )
        except Exception:
            logger.warning(
                "[HistorySummary] 兜底文本构造失败，使用静态提示", exc_info=True
            )
            text = "群聊总结渲染失败，请稍后重试"
        return MessageChain(chain=[Comp.Plain("\u200b" + text + "\u200b")])

    # ------------------------------------------------------------------
    # Node 署名
    # ------------------------------------------------------------------

    def _bot_identity(self) -> tuple[str, str]:
        """获取 bot 自身 uin/name 用于合并转发节点署名，拿不到则兜底。

        经 ``star.context.platform_manager.platform_insts`` 遍历平台实例，
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
            logger.debug(f"[HistorySummary] 获取 bot 自身信息失败，使用兜底署名: {e}")
        return _FALLBACK_UIN, _FALLBACK_NAME
