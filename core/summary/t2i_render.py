"""自研 T2I 报告模板渲染核心（v0.3.2 模块 B）。

将 ``SummaryResult`` 经插件自带的 HTML+Jinja2 报告模板
（``summary/templates/summary_report.html``，模块 C 产出）渲染为图片消息链，
是 image 输出模式的**首选渲染级**。主流程：

1. ``_load_template``：读模板文件，按 (mtime, 内容) 实例级缓存（mtime 变化
   重读，便于手动微调模板）；缺失/读取失败记 error 返回 None；
2. ``_resolve_theme``：主题判定（auto/light/dark）。auto 时按服务器本地时间
   ``datetime.now()`` 判定 ``[light_start, dark_start)`` 为浅色，否则深色
   （支持跨午夜配置）；HH:MM 解析失败回退默认 22:00/08:00 并记 warning；
3. ``_resolve_cdn_providers``：读 CDN 节点序，过滤未知 key（记 debug），
   空列表/异常回退默认序（国内镜像优先）；URL 拼装规则由模板内联加载器
   持有，Python 侧只传 key 列表，不发网络请求；
4. ``_build_template_data``：按模块 B/C 共享契约组装模板数据（统计卡片 /
   柱状图 bars / 板块原文 + 预转换兜底 HTML / CDN 声明）；板块兜底 HTML
   复用 ``formatter._markdown_to_html``（含 GFM 表格）；
5. ``_render_two_rounds``：两轮渲染——R1 PNG（超时 T）、R2 JPEG q80
   （超时 2T），T = ``summary_t2i_timeout`` 秒（5–300，非法回退 30）；
   对 ``star.html_render(..., return_url=False)`` 的返回值做魔数校验
   （PNG ``89 50 4E 47`` / JPEG ``FF D8``），防止把 T2I 服务返回的错误
   HTML 页面当成图片；bytes 落临时文件后 fromFileSystem、本地路径直接
   fromFileSystem、http(s) URL 走 fromURL（兼容直接返 URL 的部署形态）。

``render()`` 契约：**绝不向上抛异常**——任何异常（含配置读取、模板缺失、
两轮渲染全失败）均返回 None，由 ``SummaryFormatter._render_image`` 继续
text_to_image / 纯文本两级兜底。``render_from_dict()``（v0.4.2 Web 后台
导出）按同流程消费存储 JSON，返回图片字节或文件路径，失败抛
``ValueError`` 由 WebAPI 端点转 502 明确报错。日志统一 ``[HistorySummary]``
前缀。

循环导入说明：本模块顶层 ``from .formatter import _markdown_to_html`` 复用
兜底转换器；formatter 反向引用 ``T2IRenderer`` 时改用 ``__init__`` 内局部
导入，故顶层单向依赖不成环。
"""

from __future__ import annotations

import asyncio
import inspect
import os
import re
import tempfile
import time
from datetime import datetime
from types import SimpleNamespace

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.star import Star

from .formatter import _SOURCE_NAMES, _fmt_time, _markdown_to_html
from .models import SummaryResult

# 合法 CDN provider key 与默认尝试顺序（国内镜像优先，规则见 PRD 3.2；
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


def _to_attr_obj(value):
    """dict → SimpleNamespace（键名即属性名），list/dict 递归转换，其余原样。

    ``render_from_dict`` 消费存储 JSON 用：存储结构里 ``stats`` 等嵌套 dict
    经本函数转为 SimpleNamespace 后，``_build_template_data`` 的 ``getattr``
    防御式取值（``getattr(stats, "total", 0)``）才能命中；不做本转换时
    getattr(dict, ...) 恒 None，全部字段落入默认值兜底。
    """
    if isinstance(value, dict):
        return SimpleNamespace(**{k: _to_attr_obj(v) for k, v in value.items()})
    if isinstance(value, list):
        return [_to_attr_obj(item) for item in value]
    return value


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


class T2IRenderer:
    """自研 T2I 报告模板渲染器（image 模式首选渲染级）。

    Attributes:
        star: 插件 Star 实例，用于调用 ``html_render``。
        config_mgr: ``db_config.ConfigManager`` 实例，经
            ``get_summary_setting_typed`` 读取 5 项 T2I 渲染配置。
    """

    def __init__(self, star: Star, config_mgr) -> None:
        self.star = star
        self.config_mgr = config_mgr
        # 模板缓存：(mtime, 内容)；mtime 未变则复用，变则重读
        self._tmpl_cache: tuple[float, str] | None = None

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    async def render(self, result: SummaryResult) -> MessageChain | None:
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
            logger.info(
                f"[HistorySummary] T2I 主题判定={theme}，CDN 节点序={providers}"
            )
            data = self._build_template_data(result, theme, providers)
            return await self._render_two_rounds(tmpl, data)
        except Exception as e:
            logger.warning(
                f"[HistorySummary] T2I 渲染核心未预期异常，返回 None 交上层兜底: {e}",
                exc_info=True,
            )
            return None

    async def render_from_dict(self, data: dict) -> bytes:
        """按存储 JSON（``SummaryStorage.read`` 产出）渲染报告图片。

        v0.4.2 Web 后台「导出图片」用：存储 JSON 与 :class:`SummaryResult`
        字段同名（stats / sections / sources / …），经 :func:`_to_attr_obj`
        递归转为 SimpleNamespace 后喂给 ``_build_template_data``（嵌套 dict
        必须一并转换，否则 getattr 防御式取值全部落到默认值）；
        时间戳为 ``YYYY-MM-DD HH:MM:SS`` 字符串，``_fmt_time`` 兼容解析。

        Args:
            data: 存储 JSON 字典（字段缺损以 0/空兜底，不抛异常）。

        Returns:
            bytes: 渲染产物字节（PNG 或 JPEG，按魔数）。

        Raises:
            ValueError: 模板缺失 / 两轮渲染全败 / 未预期异常，由 WebAPI
                端点转 502 明确报错（区别于 ``render`` 的返回 None 契约）。
        """
        tmpl = self._load_template()
        if not tmpl:
            raise ValueError("T2I 报告模板缺失")
        theme = await self._resolve_theme()
        providers = await self._resolve_cdn_providers()
        logger.info(
            f"[HistorySummary] T2I 导出主题判定={theme}，CDN 节点序={providers}"
        )
        ns = _to_attr_obj(data)
        if not isinstance(ns, SimpleNamespace):
            raise ValueError("导出数据不是 JSON 对象")
        try:
            tmpl_data = self._build_template_data(ns, theme, providers)
        except Exception as e:
            logger.warning(f"[HistorySummary] 导出模板数据组装失败: {e}", exc_info=True)
            raise ValueError("模板数据组装失败") from e
        ret = await self._render_two_rounds_bytes(tmpl, tmpl_data)
        if ret is None:
            raise ValueError("T2I 两轮渲染均失败")
        return ret

    # ------------------------------------------------------------------
    # 配置解析（读取异常一律兜底默认值，不阻断渲染）
    # ------------------------------------------------------------------

    async def _read_setting(self, key: str, target_type: type):
        """类型化读取配置，异常向上抛由调用方兜底。

        ``ConfigManager.get_summary_setting_typed`` 现行签名为 ``(key)``
        （目标类型由 ``SUMMARY_TYPES`` 类常量声明，见 db_config.py 与
        service.py 既有调用点）；此处经签名探测同时兼容 ``(key, target_type)``
        签名，避免接口演进期调用失配。
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
                    f"[HistorySummary] T2I 主题模式配置 {raw!r} 非法（auto/light/dark），回退 auto"
                )
        except Exception as e:
            logger.warning(f"[HistorySummary] 读取主题模式配置失败，回退 auto: {e}")
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
                    f"[HistorySummary] 深色时段起点 {raw_dark!r} 非法（需 HH:MM 24 小时制），"
                    f"回退 {_DEFAULT_DARK_START}"
                )
            else:
                dark_minutes = parsed
        except Exception as e:
            logger.warning(
                f"[HistorySummary] 读取深色时段起点配置失败，回退 {_DEFAULT_DARK_START}: {e}"
            )
        try:
            raw_light = await self._read_setting("summary_t2i_light_start", str)
            parsed = _hhmm_to_minutes(raw_light)
            if parsed is None:
                logger.warning(
                    f"[HistorySummary] 浅色时段起点 {raw_light!r} 非法（需 HH:MM 24 小时制），"
                    f"回退 {_DEFAULT_LIGHT_START}"
                )
            else:
                light_minutes = parsed
        except Exception as e:
            logger.warning(
                f"[HistorySummary] 读取浅色时段起点配置失败，回退 {_DEFAULT_LIGHT_START}: {e}"
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
                        logger.debug(f"[HistorySummary] CDN 配置忽略未知节点: {item!r}")
                        continue
                    if key not in providers:  # 去重，避免同节点重试
                        providers.append(key)
                if providers:
                    return providers
        except Exception as e:
            logger.warning(f"[HistorySummary] 读取 CDN 节点配置失败，回退默认序: {e}")
        return list(_CDN_DEFAULT_PROVIDERS)

    async def _resolve_timeout(self) -> int:
        """解析单轮渲染超时（秒）：5–300 合法，越界/异常回退 30 并记 warning。"""
        try:
            value = int(await self._read_setting("summary_t2i_timeout", int))
            if _TIMEOUT_MIN <= value <= _TIMEOUT_MAX:
                return value
            logger.warning(
                f"[HistorySummary] T2I 渲染超时 {value} 越界"
                f"（{_TIMEOUT_MIN}–{_TIMEOUT_MAX} 秒），回退 {_DEFAULT_TIMEOUT}"
            )
        except Exception as e:
            logger.warning(
                f"[HistorySummary] 读取 T2I 渲染超时配置失败，回退 {_DEFAULT_TIMEOUT}: {e}"
            )
        return _DEFAULT_TIMEOUT

    # ------------------------------------------------------------------
    # 模板与数据
    # ------------------------------------------------------------------

    def _load_template(self) -> str | None:
        """读取报告模板并按 (mtime, 内容) 缓存；失败记 error 返回 None。"""
        path = os.path.join(
            os.path.dirname(__file__), "templates", "summary_report.html"
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
            logger.error(f"[HistorySummary] 读取 T2I 报告模板失败: {e}")
            return None

    def _build_template_data(
        self, result: SummaryResult, theme: str, providers: list[str]
    ) -> dict:
        """按模块 B/C 共享契约组装模板数据（键名不可改，模板侧按此开发）。

        全程防御式取值：``stats`` 为 None 或字段缺损时以 0/空兜底，不抛异常。
        """
        stats = getattr(result, "stats", None)
        if stats is not None:
            total = getattr(stats, "total", 0) or 0
            participants = getattr(stats, "participant_count", 0) or 0
            time_start = getattr(stats, "time_start", None)
            time_end = getattr(stats, "time_end", None)
            top_senders = getattr(stats, "top_senders", None) or []
            truncated = bool(getattr(stats, "truncated", False))
        else:
            total, participants = 0, 0
            time_start = time_end = None
            top_senders = []
            truncated = False

        sources = getattr(result, "sources", None) or {}
        # 存储 JSON 的 sources 为 dict（{数据源键: 条数}）；render_from_dict 经
        # _to_attr_obj 会转成 SimpleNamespace，用 vars() 取回键值对兜底
        if isinstance(sources, SimpleNamespace):
            sources = vars(sources)
        try:
            source_items = [
                {"name": _SOURCE_NAMES.get(str(k), str(k)), "count": v}
                for k, v in sources.items()
            ]
        except Exception:
            source_items = []

        # 发言人排行柱状图数据：top_senders 已降序，percent = count/max*100
        bars: list[dict] = []
        bars_max = 0
        for item in top_senders:
            try:
                sender_id, sender_name, count = item
                count = int(count)
            except Exception:
                continue
            bars.append(
                {"name": sender_name or sender_id or "未知用户", "count": count}
            )
            bars_max = max(bars_max, count)
        for bar in bars:
            bar["percent"] = (
                round(bar["count"] / bars_max * 100, 1) if bars_max else 0.0
            )

        # 板块数据：原文（模板侧 marked 客户端渲染）+ 预转换兜底 HTML（含表格）
        sections: list[dict] = []
        for sec_title, sec_content in getattr(result, "sections", None) or []:
            content = str(sec_content or "")
            sections.append(
                {
                    "title": str(sec_title or ""),
                    "raw": content,
                    "fallback_html": _markdown_to_html(content),
                }
            )

        scope_desc = getattr(result, "scope_desc", "") or ""
        return {
            "theme": theme,
            "title": f"群聊总结 · {scope_desc}" if scope_desc else "群聊总结",
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "meta": {
                "scope": scope_desc or "—",
                "provider": getattr(result, "provider_id", "") or "会话默认",
                "time_start": _fmt_time(time_start),
                "time_end": _fmt_time(time_end),
            },
            "stats": {
                "total": total,
                "participants": participants,
                "sources": source_items,
                "truncated": truncated,
            },
            "bars": bars,
            "bars_max": int(bars_max),
            "sections": sections,
            "cdn": {"providers": providers, "libs": _CDN_LIBS},
        }

    # ------------------------------------------------------------------
    # 两轮渲染 + 结果校验
    # ------------------------------------------------------------------

    async def _render_two_rounds(self, tmpl: str, data: dict) -> MessageChain | None:
        """两轮渲染：R1 PNG（超时 T）→ 失败 R2 JPEG q80（超时 2T）→ 全败 None。

        ``device_scale_factor_level="ultra"``（1.8 倍设备像素比）提升输出
        分辨率——T2I 服务端视口固定 1280px，经放大后输出约 2304px 宽，
        图片在手机端缩放查看时更清晰（模板字号已按 1230px 画布放大）。
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
                f"[HistorySummary] T2I 第 {round_no} 轮渲染开始"
                f"（{options['type']}，超时 {options['timeout']}ms）"
            )
            try:
                ret = await self.star.html_render(
                    tmpl, data, return_url=False, options=options
                )
            except Exception as e:
                cost = time.monotonic() - start
                logger.warning(
                    f"[HistorySummary] T2I 第 {round_no} 轮渲染异常"
                    f"（耗时 {cost:.1f}s）: {e}"
                )
                continue
            cost = time.monotonic() - start
            if self._validate_image(ret):
                logger.info(
                    f"[HistorySummary] T2I 第 {round_no} 轮渲染成功，耗时 {cost:.1f}s"
                )
                try:
                    return self._to_chain(ret)
                except Exception as e:
                    logger.warning(
                        f"[HistorySummary] T2I 第 {round_no} 轮图片消息链构造失败: {e}"
                    )
                    continue
            logger.warning(
                f"[HistorySummary] T2I 第 {round_no} 轮渲染结果校验失败，耗时 {cost:.1f}s"
            )
        logger.warning("[HistorySummary] T2I 两轮渲染均失败，交上层兜底链路")
        return None

    async def _render_two_rounds_bytes(self, tmpl: str, data: dict) -> bytes | None:
        """两轮渲染并返回图片**字节**（Web 导出用，不构造消息链）。

        渲染选项与 ``_render_two_rounds`` 完全一致（PNG → JPEG q80 兜底、
        full_page + device_scale_factor_level="ultra"）；产物为 bytes 直接
        返回，本地文件路径读取后返回（http URL 无法转字节，返回 None）。
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
                f"[HistorySummary] T2I 导出第 {round_no} 轮渲染开始"
                f"（{options['type']}，超时 {options['timeout']}ms）"
            )
            try:
                ret = await self.star.html_render(
                    tmpl, data, return_url=False, options=options
                )
            except Exception as e:
                logger.warning(
                    f"[HistorySummary] T2I 导出第 {round_no} 轮渲染异常"
                    f"（耗时 {time.monotonic() - start:.1f}s）: {e}"
                )
                continue
            cost = time.monotonic() - start
            if self._validate_image(ret):
                logger.info(
                    f"[HistorySummary] T2I 导出第 {round_no} 轮渲染成功，"
                    f"耗时 {cost:.1f}s"
                )
                try:
                    img_bytes = await asyncio.to_thread(self._ret_to_bytes, ret)
                except Exception as e:
                    logger.warning(
                        f"[HistorySummary] T2I 导出第 {round_no} 轮产物转字节失败: {e}"
                    )
                    continue
                if img_bytes:
                    return img_bytes
            logger.warning(
                f"[HistorySummary] T2I 导出第 {round_no} 轮渲染结果校验失败，"
                f"耗时 {cost:.1f}s"
            )
        logger.warning("[HistorySummary] T2I 导出两轮渲染均失败")
        return None

    @staticmethod
    def _ret_to_bytes(ret: object) -> bytes | None:
        """渲染产物 → bytes：bytes 直出；本地文件路径读文件；http URL 返回 None。"""
        if isinstance(ret, (bytes, bytearray)):
            return bytes(ret)
        value = str(ret).strip()
        if value.startswith(("http://", "https://")):
            logger.warning(
                f"[HistorySummary] T2I 渲染返回 URL 而非本地文件，无法导出: {value}"
            )
            return None
        with open(value, "rb") as f:
            return f.read()

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
            T2IRenderer._log_bad_image(data[:512])
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
                    logger.warning(f"[HistorySummary] T2I 读取渲染产物文件失败: {e}")
                    return False
                if head.startswith(_PNG_MAGIC) or head.startswith(_JPEG_MAGIC):
                    return True
                T2IRenderer._log_bad_image(head)
                return False
        logger.warning(
            f"[HistorySummary] T2I 渲染返回空或不可识别结果: {type(ret).__name__}"
        )
        return False

    @staticmethod
    def _log_bad_image(head: bytes) -> None:
        """魔数校验失败日志：能提取错误页 <title> 则记出，否则记头部字节 hex。"""
        title = _extract_html_title(head)
        if title:
            logger.warning(
                f"[HistorySummary] T2I 渲染返回错误页面而非图片，页面标题: {title}"
            )
        else:
            logger.warning(
                f"[HistorySummary] T2I 渲染返回非图片内容（头部字节: {head[:16].hex()}）"
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
                prefix="historysummary_t2i_", suffix=suffix, delete=False
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
