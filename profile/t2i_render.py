"""自研 T2I 人物报告模板渲染核心（v0.4.0 模块 G）。

将 ``ProfileResult`` 经插件自带的 HTML+Jinja2 人物报告模板
（``profile/templates/profile_report.html``）渲染为图片消息链，是 image
输出模式的**首选渲染级**。全流程镜像 ``summary/t2i_render.py`` 的成熟范式：

1. ``_load_template``：读模板文件，按 (mtime, 内容) 实例级缓存（mtime 变化
   重读，便于手动微调模板）；缺失/读取失败记 error 返回 None；
2. ``_resolve_theme`` / ``_resolve_timeout`` / ``_resolve_cdn_providers``：
   渲染基础设施配置**与消息总结共用**（PRD §3.6 决策，不新增 profile_t2i_
   冗余键），读 ``summary_t2i_theme_mode/dark_start/light_start/timeout/
   cdn_providers``；主题 auto 时按服务器本地时间判定浅/深色；
3. ``_build_template_data``：按模块 G / 模板共享契约组装数据（报告头 /
   四统计卡 / 小时·星期活动分布 + 峰值 / 互动排行 / 板块原文 + 预转换兜底
   HTML / 免责声明 / CDN 声明）；板块兜底 HTML 经本模块自实现的
   ``_markdown_to_html``（含 GFM 表格）预转换；
4. ``_render_two_rounds``：两轮渲染——R1 PNG（超时 T）、R2 JPEG q80
   （超时 2T），T = ``summary_t2i_timeout`` 秒（5–300，非法回退 30）；
   对 ``star.html_render(..., return_url=False)`` 的返回值做魔数校验
   （PNG ``89 50 4E 47`` / JPEG ``FF D8``），防止把 T2I 服务返回的错误
   HTML 页面当成图片；bytes 落临时文件后 fromFileSystem、本地路径直接
   fromFileSystem、http(s) URL 走 fromURL（兼容直接返 URL 的部署形态）。

``render()`` 契约：**绝不向上抛异常**——任何异常（含配置读取、模板缺失、
两轮渲染全失败）均返回 None，由 ``ProfileFormatter`` 的 image 链路继续
text_to_image / 纯文本两级兜底。日志统一 ``[Profile]`` 前缀。

Markdown 兜底转换器自实现说明：PRD §3.0 约定 ``profile/`` 与 ``summary/``
两子包平行、互不依赖（仅共享 models 与 db 基础设施），故不顶层 import
``summary.formatter._markdown_to_html``，而在本模块按同款行级正则范式实现
等价逻辑（含 GFM 表格），canonical 参考见该函数。
"""

from __future__ import annotations

import html
import inspect
import math
import os
import re
import tempfile
import time
from datetime import datetime

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.star import Star

from .models import ProfileResult

# 合法 CDN provider key 与默认尝试顺序（国内镜像优先，与 summary 共用规则；
# URL 拼装由模板内联加载器完成，Python 侧只校验/透传 key）
_CDN_DEFAULT_PROVIDERS = ["bootcdn", "npmmirror", "staticfile", "jsdelivr", "unpkg"]
_CDN_VALID_KEYS = set(_CDN_DEFAULT_PROVIDERS)

# 模板 data 契约中的外部 JS 库声明（版本锁定，模板加载器按此拼 URL）
_CDN_LIBS = {
    "marked": {
        "pkg": "marked",
        "ver": "12.0.2",
        "file": "marked.min.js",
        "global": "marked",
    },
    "echarts": {
        "pkg": "echarts",
        "ver": "5.5.1",
        "file": "echarts.min.js",
        "global": "echarts",
    },
}

# 图片文件头魔数：PNG 前 4 字节 89 50 4E 47 / JPEG 前 2 字节 FF D8
_PNG_MAGIC = b"\x89PNG"
_JPEG_MAGIC = b"\xff\xd8"

# auto 主题时段默认值（HH:MM，24 小时制）
_DEFAULT_DARK_START = "22:00"
_DEFAULT_LIGHT_START = "08:00"

# 渲染超时默认值与合法范围（秒）
_DEFAULT_TIMEOUT = 30
_TIMEOUT_MIN = 5
_TIMEOUT_MAX = 300

# 错误 HTML 页面 <title> 提取（T2I 服务报错排查用）
_RE_HTML_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)

# 活动分布坐标/星期标签（weekday_dist 约定 Mon=0..Sun=6，见 ProfileStats）
_HOUR_LABELS = [str(i) for i in range(24)]
_WEEKDAY_LABELS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

# 报告页脚免责声明（PRD §8 合规要求，固定文案，模板侧经 |e 直出）
_DISCLAIMER = "本报告基于公开群聊记录由 AI 生成，仅为推测，仅供参考"

# GFM 表格识别（_markdown_to_html 用，范式同 summary.formatter）：
# 管道行 = 首尾 | 包裹；分隔符行 = 单元格仅由 -/:/空格/| 组成
_RE_TABLE_ROW = re.compile(r"^\s*\|(.+)\|\s*$")
_RE_TABLE_SEP = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$")


# ---------------------------------------------------------------------------
# 纯函数工具（可离线单测）
# ---------------------------------------------------------------------------


def _theme_for(now_minutes: int, light_minutes: int, dark_minutes: int) -> str:
    """给定「当前时刻 / 浅色起点 / 深色起点」（自 0 点起的分钟数）判定主题。

    语义：``[light_start, dark_start)`` 区间内为 ``"light"``，其余为
    ``"dark"``。区间按 24 小时环处理：

    - ``light < dark``（常规，如 08:00–22:00）：``light <= t < dark`` 浅色；
    - ``light > dark``（跨午夜，如用户配 light=22:00 dark=08:00）：区间环绕
      午夜，``t >= light`` 或 ``t < dark`` 浅色（即 22:00–次日 08:00 浅色）；
    - ``light == dark``：区间为空，恒深色。
    """
    if light_minutes == dark_minutes:
        return "dark"
    if light_minutes < dark_minutes:
        return "light" if light_minutes <= now_minutes < dark_minutes else "dark"
    return (
        "light"
        if now_minutes >= light_minutes or now_minutes < dark_minutes
        else "dark"
    )


def _hhmm_to_minutes(value: object) -> int | None:
    """``HH:MM`` 字符串 → 自 0 点起的分钟数；非法（含越界）返回 None。"""
    try:
        parsed = datetime.strptime(str(value).strip(), "%H:%M")
        return parsed.hour * 60 + parsed.minute
    except (ValueError, TypeError):
        return None


def _extract_html_title(head: bytes) -> str:
    """从内容头部字节提取 ``<title>`` 文本（T2I 服务错误页排查用），无则空串。"""
    try:
        m = _RE_HTML_TITLE.search(head.decode("utf-8", errors="ignore"))
        return m.group(1).strip() if m else ""
    except Exception:
        return ""


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


def _norm_dist(raw: object, size: int) -> list[int]:
    """分布数组归一化：非列表 / 长度不足 / 脏值 → 定长零填充，绝不抛异常。

    负数与非整数项归 0；超长截断至 ``size``（hour_dist=24 / weekday_dist=7）。
    """
    try:
        items = list(raw) if raw is not None else []
    except TypeError:
        items = []
    out: list[int] = []
    for item in items[:size]:
        try:
            value = int(item)
        except (TypeError, ValueError):
            value = 0
        out.append(value if value > 0 else 0)
    out.extend([0] * (size - len(out)))
    return out


def _clamp_index(value: object, size: int) -> int:
    """峰值索引防御式收敛：非法 / 越界 → 0（保证模板与图表索引安全）。"""
    try:
        idx = int(value)
    except (TypeError, ValueError):
        return 0
    return idx if 0 <= idx < size else 0


def _make_vbars(dist: list[int], labels: list[str], peak_idx: int) -> list[dict]:
    """生成纯 CSS 兜底竖柱数据：高度百分比相对分布最大值。

    返回项 ``{"label", "count", "percent", "peak"}``；``peak`` 仅当分布存在
    非零值且该柱为峰值索引时为 True（全零分布无峰值高亮）。
    """
    vmax = max(dist) if dist else 0
    bars: list[dict] = []
    for i, count in enumerate(dist):
        bars.append(
            {
                "label": labels[i] if i < len(labels) else str(i),
                "count": count,
                "percent": round(count / vmax * 100, 2) if vmax else 0.0,
                "peak": bool(vmax and i == peak_idx),
            }
        )
    return bars


def _markdown_to_html(text: str) -> str:
    """轻量 Markdown → HTML（标题/粗体/斜体/行内代码/列表/引用/代码块/GFM 表格）。

    服务于图片模式 CDN 全挂时的服务端预转换兜底 HTML。canonical 实现为
    ``summary/formatter.py::_markdown_to_html``；因 PRD §3.0 约定 profile/
    与 summary/ 互不依赖，此处按同款行级正则范式自实现等价逻辑，不引第三方
    依赖。输入先经 ``html.escape`` 转义，防止内容中的尖括号破坏页面结构。

    GFM 表格：当前行为管道行（``| ... |``）且下一行为分隔符行（``---`` /
    ``:---`` / ``---:`` / ``:---:`` 单元格）时，消费表头 + 分隔行 + 后续连续
    管道行，输出 ``<table>``；冒号对齐不做渲染；单元格内容走行内转换。
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
        # 管道行作表体，遇非管道行结束
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


class ProfileT2IRenderer:
    """自研 T2I 人物报告模板渲染器（image 模式首选渲染级）。

    Attributes:
        star: 插件 Star 实例，用于调用 ``html_render``。
        config_mgr: ``db_config.ConfigManager`` 实例。渲染基础设施配置与
            消息总结**共用**（PRD §3.6），经 ``get_summary_setting_typed``
            读取 5 项 ``summary_t2i_*`` 配置，不新增 profile_t2i_ 键。
    """

    def __init__(self, star: Star, config_mgr) -> None:
        self.star = star
        self.config_mgr = config_mgr
        # 模板缓存：(mtime, 内容)；mtime 未变则复用，变则重读
        self._tmpl_cache: tuple[float, str] | None = None

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    async def render(self, result: ProfileResult) -> MessageChain | None:
        """完整渲染流程：模板加载 → 主题/CDN 解析 → 数据组装 → 两轮渲染。

        Returns:
            图片消息链；任何失败（模板缺失 / 两轮全败 / 未预期异常）返回
            None，**绝不向上抛异常**，由上层兜底链路接管。
        """
        try:
            tmpl = self._load_template()
            if not tmpl:
                return None
            theme = await self._resolve_theme()
            providers = await self._resolve_cdn_providers()
            logger.info(f"[Profile] T2I 主题判定={theme}，CDN 节点序={providers}")
            data = self._build_template_data(result, theme, providers)
            return await self._render_two_rounds(tmpl, data)
        except Exception as e:
            logger.warning(
                f"[Profile] T2I 渲染核心未预期异常，返回 None 交上层兜底: {e}",
                exc_info=True,
            )
            return None

    # ------------------------------------------------------------------
    # 配置解析（与消息总结共用 summary_t2i_* 键，读取异常一律兜底默认值）
    # ------------------------------------------------------------------

    async def _read_setting(self, key: str, target_type: type):
        """类型化读取配置，异常向上抛由调用方兜底。

        ``ConfigManager.get_summary_setting_typed`` 现行签名为 ``(key)``
        （目标类型由 ``SUMMARY_TYPES`` 类常量声明）；此处经签名探测同时兼容
        ``(key, target_type)`` 签名，避免接口演进期调用失配。
        """
        fn = self.config_mgr.get_summary_setting_typed
        try:
            positional = [
                p
                for p in inspect.signature(fn).parameters.values()
                if p.kind
                in (
                    inspect.Parameter.POSITIONAL_ONLY,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                )
            ]
            if len(positional) >= 2:
                return await fn(key, target_type)
        except (TypeError, ValueError):
            pass
        return await fn(key)

    async def _resolve_theme(self) -> str:
        """解析主题：light/dark 强制模式直通；auto 按服务器本地时间判定。"""
        mode = "auto"
        try:
            raw = (
                str(await self._read_setting("summary_t2i_theme_mode", str))
                .strip()
                .lower()
            )
            if raw in ("auto", "light", "dark"):
                mode = raw
            else:
                logger.warning(
                    f"[Profile] T2I 主题模式配置 {raw!r} 非法（auto/light/dark），回退 auto"
                )
        except Exception as e:
            logger.warning(f"[Profile] 读取主题模式配置失败，回退 auto: {e}")
        if mode in ("light", "dark"):
            return mode

        # auto：解析双时段起点，非法值逐项回退默认并记 warning
        dark_minutes = _hhmm_to_minutes(_DEFAULT_DARK_START)
        light_minutes = _hhmm_to_minutes(_DEFAULT_LIGHT_START)
        try:
            raw_dark = await self._read_setting("summary_t2i_dark_start", str)
            parsed = _hhmm_to_minutes(raw_dark)
            if parsed is None:
                logger.warning(
                    f"[Profile] 深色时段起点 {raw_dark!r} 非法（需 HH:MM 24 小时制），"
                    f"回退 {_DEFAULT_DARK_START}"
                )
            else:
                dark_minutes = parsed
        except Exception as e:
            logger.warning(
                f"[Profile] 读取深色时段起点配置失败，回退 {_DEFAULT_DARK_START}: {e}"
            )
        try:
            raw_light = await self._read_setting("summary_t2i_light_start", str)
            parsed = _hhmm_to_minutes(raw_light)
            if parsed is None:
                logger.warning(
                    f"[Profile] 浅色时段起点 {raw_light!r} 非法（需 HH:MM 24 小时制），"
                    f"回退 {_DEFAULT_LIGHT_START}"
                )
            else:
                light_minutes = parsed
        except Exception as e:
            logger.warning(
                f"[Profile] 读取浅色时段起点配置失败，回退 {_DEFAULT_LIGHT_START}: {e}"
            )

        now = datetime.now()  # 服务器本地时间（bot 宿主机时区）
        return _theme_for(now.hour * 60 + now.minute, light_minutes, dark_minutes)

    async def _resolve_cdn_providers(self) -> list[str]:
        """解析 CDN 节点序：过滤未知 key（记 debug），空列表/异常回退默认序。"""
        try:
            raw = await self._read_setting("summary_t2i_cdn_providers", list)
            if isinstance(raw, list):
                providers: list[str] = []
                for item in raw:
                    key = str(item).strip().lower()
                    if not key:
                        continue
                    if key not in _CDN_VALID_KEYS:
                        logger.debug(f"[Profile] CDN 配置忽略未知节点: {item!r}")
                        continue
                    if key not in providers:  # 去重，避免同节点重试
                        providers.append(key)
                if providers:
                    return providers
        except Exception as e:
            logger.warning(f"[Profile] 读取 CDN 节点配置失败，回退默认序: {e}")
        return list(_CDN_DEFAULT_PROVIDERS)

    async def _resolve_timeout(self) -> int:
        """解析单轮渲染超时（秒）：5–300 合法，越界/异常回退 30 并记 warning。"""
        try:
            value = int(await self._read_setting("summary_t2i_timeout", int))
            if _TIMEOUT_MIN <= value <= _TIMEOUT_MAX:
                return value
            logger.warning(
                f"[Profile] T2I 渲染超时 {value} 越界"
                f"（{_TIMEOUT_MIN}–{_TIMEOUT_MAX} 秒），回退 {_DEFAULT_TIMEOUT}"
            )
        except Exception as e:
            logger.warning(
                f"[Profile] 读取 T2I 渲染超时配置失败，回退 {_DEFAULT_TIMEOUT}: {e}"
            )
        return _DEFAULT_TIMEOUT

    # ------------------------------------------------------------------
    # 模板与数据
    # ------------------------------------------------------------------

    def _load_template(self) -> str | None:
        """读取报告模板并按 (mtime, 内容) 缓存；失败记 error 返回 None。"""
        path = os.path.join(
            os.path.dirname(__file__), "templates", "profile_report.html"
        )
        try:
            mtime = os.path.getmtime(path)
            if self._tmpl_cache is not None and self._tmpl_cache[0] == mtime:
                return self._tmpl_cache[1]
            with open(path, encoding="utf-8") as f:
                content = f.read()
            self._tmpl_cache = (mtime, content)
            return content
        except Exception as e:
            logger.error(f"[Profile] 读取 T2I 人物报告模板失败: {e}")
            return None

    def _build_template_data(
        self, result: ProfileResult, theme: str, providers: list[str]
    ) -> dict:
        """按模块 G / 模板共享契约组装模板数据（键名不可改，模板侧按此开发）。

        全程防御式取值：``target`` / ``stats`` 为 None 或字段缺损时以
        0/空/零分布兜底，不抛异常。
        """
        target = getattr(result, "target", None)
        target_qq = str(getattr(target, "sender_id", "") or "") if target else ""
        target_name = str(getattr(target, "sender_name", "") or "") if target else ""
        if not target_name:
            target_name = target_qq or "未知用户"

        stats = getattr(result, "stats", None)
        if stats is not None:
            total = getattr(stats, "total", 0) or 0
            group_count = getattr(stats, "group_count", 0) or 0
            active_days = getattr(stats, "active_days", 0) or 0
            time_start = getattr(stats, "time_start", None)
            time_end = getattr(stats, "time_end", None)
            top_partners = getattr(stats, "top_partners", None) or []
            truncated = bool(getattr(stats, "truncated", False))
        else:
            total, group_count, active_days = 0, 0, 0
            time_start = time_end = None
            top_partners = []
            truncated = False

        # 平均长度：非有限值（NaN/inf）/ 负数 → 0.0，避免模板/JSON 异常
        try:
            avg_length = round(
                float(getattr(stats, "avg_length", 0) or 0) if stats else 0.0, 1
            )
            if not math.isfinite(avg_length) or avg_length < 0:
                avg_length = 0.0
        except (TypeError, ValueError):
            avg_length = 0.0

        # 活动分布：定长归一化（24h / 7weekday）+ 峰值索引收敛
        hour_dist = _norm_dist(getattr(stats, "hour_dist", None) if stats else None, 24)
        weekday_dist = _norm_dist(
            getattr(stats, "weekday_dist", None) if stats else None, 7
        )
        peak_hour = _clamp_index(getattr(stats, "peak_hour", 0) if stats else 0, 24)
        peak_weekday = _clamp_index(
            getattr(stats, "peak_weekday", 0) if stats else 0, 7
        )
        hour_max = max(hour_dist)
        weekday_max = max(weekday_dist)
        hour_bars = _make_vbars(hour_dist, _HOUR_LABELS, peak_hour)
        weekday_bars = _make_vbars(weekday_dist, _WEEKDAY_LABELS, peak_weekday)
        hour_peak_label = (
            f"{peak_hour} 点 · {hour_dist[peak_hour]} 条" if hour_max else ""
        )
        weekday_peak_label = (
            f"{_WEEKDAY_LABELS[peak_weekday]} · {weekday_dist[peak_weekday]} 条"
            if weekday_max
            else ""
        )

        # 互动对象排行柱图数据：top_partners 已降序，percent = count/max*100
        partners: list[dict] = []
        partners_max = 0
        for item in top_partners:
            try:
                sender_id, sender_name, count = item
                count = int(count)
            except Exception:
                continue
            partners.append(
                {"name": sender_name or sender_id or "未知用户", "count": count}
            )
            partners_max = max(partners_max, count)
        for bar in partners:
            bar["percent"] = (
                round(bar["count"] / partners_max * 100, 1) if partners_max else 0.0
            )

        # 板块数据：原文（模板侧 marked 客户端渲染）+ 预转换兜底 HTML（含表格）
        sections: list[dict] = []
        for item in getattr(result, "sections", None) or []:
            try:
                sec_title, sec_content = item
            except (TypeError, ValueError):
                continue
            content = str(sec_content or "")
            sections.append(
                {
                    "title": str(sec_title or ""),
                    "raw": content,
                    "fallback_html": _markdown_to_html(content),
                }
            )

        scope_desc = str(getattr(result, "scope_desc", "") or "")
        created_at = str(
            getattr(result, "created_at", "") or ""
        ) or datetime.now().strftime("%Y-%m-%d %H:%M")
        return {
            "theme": theme,
            "title": f"人物画像 · {target_name}",
            "generated_at": created_at,
            "meta": {
                "qq": target_qq or "—",
                "scope": scope_desc or "—",
                "time_start": _fmt_time(time_start),
                "time_end": _fmt_time(time_end),
                "provider": getattr(result, "provider_id", "") or "会话默认",
            },
            "stats": {
                "total": total,
                "group_count": group_count,
                "active_days": active_days,
                "avg_length": avg_length,
            },
            "hour_dist": hour_dist,
            "weekday_dist": weekday_dist,
            "peak_hour": peak_hour,
            "peak_weekday": peak_weekday,
            "hour_bars": hour_bars,
            "weekday_bars": weekday_bars,
            "hour_peak_label": hour_peak_label,
            "weekday_peak_label": weekday_peak_label,
            "partners": partners,
            "partners_max": int(partners_max),
            "sections": sections,
            "truncated": truncated,
            "relation_incomplete": not bool(
                getattr(result, "relation_context_complete", True)
            ),
            "disclaimer": _DISCLAIMER,
            "cdn": {"providers": providers, "libs": _CDN_LIBS},
        }

    # ------------------------------------------------------------------
    # 两轮渲染 + 结果校验
    # ------------------------------------------------------------------

    async def _render_two_rounds(self, tmpl: str, data: dict) -> MessageChain | None:
        """两轮渲染：R1 PNG（超时 T）→ 失败 R2 JPEG q80（超时 2T）→ 全败 None。

        ``device_scale_factor_level="ultra"``（1.8 倍设备像素比）提升输出
        分辨率，与 summary 渲染范式保持一致（T2I 服务端视口固定 1280px，
        860px 画布居中输出，放大后手机端查看更清晰）。
        """
        timeout_s = await self._resolve_timeout()
        rounds = [
            (
                1,
                {
                    "timeout": timeout_s * 1000,
                    "type": "png",
                    "full_page": True,
                    "device_scale_factor_level": "ultra",
                },
            ),
            (
                2,
                {
                    "timeout": 2 * timeout_s * 1000,
                    "type": "jpeg",
                    "quality": 80,
                    "full_page": True,
                    "device_scale_factor_level": "ultra",
                },
            ),
        ]
        for round_no, options in rounds:
            start = time.monotonic()
            logger.info(
                f"[Profile] T2I 第 {round_no} 轮渲染开始"
                f"（{options['type']}，超时 {options['timeout']}ms）"
            )
            try:
                ret = await self.star.html_render(
                    tmpl, data, return_url=False, options=options
                )
            except Exception as e:
                cost = time.monotonic() - start
                logger.warning(
                    f"[Profile] T2I 第 {round_no} 轮渲染异常（耗时 {cost:.1f}s）: {e}"
                )
                continue
            cost = time.monotonic() - start
            if self._validate_image(ret):
                logger.info(f"[Profile] T2I 第 {round_no} 轮渲染成功，耗时 {cost:.1f}s")
                try:
                    return self._to_chain(ret)
                except Exception as e:
                    logger.warning(
                        f"[Profile] T2I 第 {round_no} 轮图片消息链构造失败: {e}"
                    )
                    continue
            logger.warning(
                f"[Profile] T2I 第 {round_no} 轮渲染结果校验失败，耗时 {cost:.1f}s"
            )
        logger.warning("[Profile] T2I 两轮渲染均失败，交上层兜底链路")
        return None

    @staticmethod
    def _validate_image(ret: object) -> bool:
        """校验 ``html_render(return_url=False)`` 返回值是否为合法图片。

        - ``bytes`` → 前 4 字节验魔数（PNG ``89 50 4E 47`` / JPEG ``FF D8``）；
        - ``str`` 且为已存在文件路径 → 打开读前 512 字节验魔数；
        - ``str`` 且 ``http(s)://`` → 视为合法 URL（兼容渲染器直接返 URL 的
          部署形态）；
        - 其他（空值 / 未知对象 / T2I 服务返回的错误 HTML 页字节）→ False，
          记 warning；若能从前 512 字节提取 ``<title>`` 则一并记出，便于排查
          T2I 服务报错。
        """
        if isinstance(ret, (bytes, bytearray)):
            data = bytes(ret)
            if data.startswith(_PNG_MAGIC) or data.startswith(_JPEG_MAGIC):
                return True
            ProfileT2IRenderer._log_bad_image(data[:512])
            return False
        if isinstance(ret, str) and ret.strip():
            value = ret.strip()
            if value.startswith(("http://", "https://")):
                return True
            if os.path.exists(value):
                try:
                    with open(value, "rb") as f:
                        head = f.read(512)
                except OSError as e:
                    logger.warning(f"[Profile] T2I 读取渲染产物文件失败: {e}")
                    return False
                if head.startswith(_PNG_MAGIC) or head.startswith(_JPEG_MAGIC):
                    return True
                ProfileT2IRenderer._log_bad_image(head)
                return False
        logger.warning(f"[Profile] T2I 渲染返回空或不可识别结果: {type(ret).__name__}")
        return False

    @staticmethod
    def _log_bad_image(head: bytes) -> None:
        """魔数校验失败日志：能提取错误页 <title> 则记出，否则记头部字节 hex。"""
        title = _extract_html_title(head)
        if title:
            logger.warning(f"[Profile] T2I 渲染返回错误页面而非图片，页面标题: {title}")
        else:
            logger.warning(
                f"[Profile] T2I 渲染返回非图片内容（头部字节: {head[:16].hex()}）"
            )

    @staticmethod
    def _to_chain(ret: object) -> MessageChain:
        """校验通过的渲染产物 → 图片消息链。

        bytes 写入临时文件（后缀按实际魔数 .png/.jpg）后 fromFileSystem；
        http(s) URL 走 fromURL；本地路径直接 fromFileSystem。
        """
        if isinstance(ret, (bytes, bytearray)):
            data = bytes(ret)
            suffix = ".png" if data.startswith(_PNG_MAGIC) else ".jpg"
            tmp = tempfile.NamedTemporaryFile(
                prefix="profile_t2i_", suffix=suffix, delete=False
            )
            try:
                tmp.write(data)
            finally:
                tmp.close()
            return MessageChain(chain=[Comp.Image.fromFileSystem(tmp.name)])
        value = str(ret).strip()
        if value.startswith(("http://", "https://")):
            return MessageChain(chain=[Comp.Image.fromURL(value)])
        return MessageChain(chain=[Comp.Image.fromFileSystem(value)])
