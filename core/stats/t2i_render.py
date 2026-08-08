"""自研 T2I 数据分析报告模板渲染核心（v0.5.0 模块 E）。

将 ``StatsData`` 经插件自带的 HTML+Jinja2 数据分析报告模板
（``stats/templates/stats_report.html``）渲染为图片**文件路径 / URL**，
供 ``/群统计`` 指令（``event.image_result(path)``）与定时推送（日报/周报
经 ``context.send_message(umo, ...)`` 构造图片消息链）三端同源复用。
全流程镜像 ``profile/t2i_render.py`` 的成熟范式（逐方法对齐）：

1. ``_load_template``：读模板文件，按 (mtime, 内容) 实例级缓存（mtime 变化
   重读，便于手动微调模板）；缺失/读取失败记 error 返回 None；
2. ``_resolve_theme`` / ``_resolve_timeout`` / ``_resolve_cdn_providers``：
   渲染基础设施配置**与消息总结共用**（PRD 2.1 F2 决策，不新增 stats_t2i_
   冗余键），读 ``summary_t2i_theme_mode/dark_start/light_start/timeout/
   cdn_providers``（经 config_mgr 的 summary 侧 typed 读取，读取方式照抄
   profile 侧 ``_read_setting`` 的签名探测范式）；主题 auto 时按服务器本地
   时间判定浅/深色；
3. ``_build_context``：按模块 E / 模板共享契约组装模板上下文（title / label /
   group_desc / cards / trend / hour_dist / weekday_dist / ranking /
   group_ranking / member / generated_at + 主题与 CDN 令牌）；数字格式化
   （千分位、比例百分号一位小数、日均一位小数）全部在 Python 侧完成，
   模板只做展示；全程防御式取值，绝不抛异常；
4. ``_render_two_rounds``：两轮渲染——R1 PNG（超时 T）、R2 JPEG q80
   （超时 2T），T = ``summary_t2i_timeout`` 秒（5–300，非法回退 30）；
   对 ``context.html_render(..., return_url=False)`` 的返回值做魔数校验
   （PNG ``89 50 4E 47`` / JPEG ``FF D8``），防止把 T2I 服务返回的错误
   HTML 页面当成图片；bytes 落临时文件返回路径、本地路径直接透传、
   http(s) URL 原样返回（兼容直接返 URL 的部署形态）。

``render_card()`` 契约：**绝不向上抛异常**——任何异常（含配置读取、模板
缺失、两轮渲染全失败）均返回 None，指令侧由模块 M 降级纯文本摘要、推送
侧由模块 G 记日志跳过该群。日志统一 ``[Stats]`` 前缀。

构造注入的 ``context`` 为鸭子类型约定：需暴露与 ``Star.html_render`` 同
签名的 ``html_render(tmpl, data, return_url=True, options=None)``（生产
链路由模块 G/M 注入插件 Star 实例，离线冒烟注入同签名 fake）。
"""

from __future__ import annotations

import asyncio
import inspect
import math
import os
import re
import tempfile
import time
from datetime import datetime
from typing import TYPE_CHECKING

from astrbot.api import logger

if TYPE_CHECKING:  # 仅类型标注用；运行时鸭子类型取值，不硬依赖 models 导入链
    from .models import StatsData

# 合法 CDN provider key 与默认尝试顺序（国内镜像优先，与 summary 共用规则；
# URL 拼装由模板内联加载器完成，Python 侧只校验/透传 key）
_CDN_DEFAULT_PROVIDERS = ["bootcdn", "npmmirror", "staticfile", "jsdelivr", "unpkg"]
_CDN_VALID_KEYS = set(_CDN_DEFAULT_PROVIDERS)

# 模板 data 契约中的外部 JS 库声明（版本锁定，与 profile_report.html 同版本；
# 模板加载器按此拼 URL）
_CDN_LIBS = {
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

# 分布坐标标签（weekday_dist 约定 Mon=0..Sun=6，与 repository WEEKDAY() 对齐）
_HOUR_LABELS = [str(i) for i in range(24)]
_WEEKDAY_LABELS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

# 报告页脚免责声明（PRD F4 口径说明：图片数为小时级快照 Top K 口径）
_DISCLAIMER = (
    "图片数为小时级快照 Top K 口径（每小时每群仅统计 Top K 内成员，"
    "K 外成员图片数可能偏少）；消息数口径为已入库聊天记录"
)

# 趋势图 CSS 兜底标签最大显示个数（超出按间隔抽稀，防 366 天标签重叠）
_TREND_MAX_LABELS = 12

# 排行图表用昵称最大字符数（ECharts y 轴宽度有限，超长截断；悬停仍显全名）
_RANK_CHART_NAME_MAX = 8


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


def _resolve_peak(dist: list[int], provided: object) -> int | None:
    """峰值索引解析：提供值合法（0 <= x < len(dist)）优先；非法/None 时回退
    分布最大值索引；分布全 0（或空）→ None（无峰值）。"""
    size = len(dist)
    try:
        idx = int(provided)  # type: ignore[arg-type]
        if 0 <= idx < size:
            return idx
    except (TypeError, ValueError):
        pass
    vmax = max(dist) if dist else 0
    if vmax > 0:
        return dist.index(vmax)
    return None


def _fmt_int(value: object) -> str:
    """整数千分位格式化；非法/负数 → ``"0"``（计数不可能为负，防御兜底）。"""
    try:
        n = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "0"
    return f"{n:,}" if n > 0 else "0"


def _fmt_ratio(value: object) -> str:
    """占比（0.0–1.0）→ 百分号一位小数（如 ``"12.3%"``）；非法收敛到 [0, 1]。"""
    try:
        x = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        x = 0.0
    if not math.isfinite(x):
        x = 0.0
    x = min(max(x, 0.0), 1.0)
    return f"{x * 100:.1f}%"


def _fmt_avg(value: object) -> str:
    """日均条数 → 一位小数字符串（如 ``"12.3"``）；非法/负数 → ``"0.0"``。"""
    try:
        x = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        x = 0.0
    if not math.isfinite(x) or x < 0:
        x = 0.0
    return f"{x:.1f}"


def _hour_text(peak: int | None) -> str:
    """峰值时段卡片文案：``"21 时"``；无峰值（全 0 分布）→ ``"—"``。"""
    return f"{peak} 时" if peak is not None else "—"


def _peak_hour_label(dist: list[int], peak: int | None) -> str:
    """小时分布峰值胶囊文案：``"21 时 · 123 条"``；无峰值 → 空串。"""
    if peak is None or peak >= len(dist) or dist[peak] <= 0:
        return ""
    return f"{peak} 时 · {dist[peak]} 条"


def _peak_weekday_label(dist: list[int], peak: int | None) -> str:
    """星期分布峰值胶囊文案：``"周三 · 45 条"``；无峰值 → 空串。"""
    if peak is None or peak >= len(dist) or dist[peak] <= 0:
        return ""
    return f"{_WEEKDAY_LABELS[peak]} · {dist[peak]} 条"


def _make_vbars(dist: list[int], labels: list[str], peak_idx: int | None) -> list[dict]:
    """生成纯 CSS 兜底竖柱数据：高度百分比相对分布最大值。

    返回项 ``{"label", "count", "percent", "peak"}``；``peak`` 仅当分布存在
    非零值且该柱为峰值索引时为 True（全零分布无峰值高亮；``peak_idx`` 可为
    None）。
    """
    vmax = max(dist) if dist else 0
    bars: list[dict] = []
    for i, count in enumerate(dist):
        bars.append(
            {
                "label": labels[i] if i < len(labels) else str(i),
                "count": count,
                "percent": round(count / vmax * 100, 2) if vmax else 0.0,
                "peak": bool(vmax and peak_idx is not None and i == peak_idx),
            }
        )
    return bars


class StatsT2IRenderer:
    """自研 T2I 数据分析报告模板渲染器（指令图片卡 / 定时推送共用）。

    Attributes:
        context: 鸭子类型注入——需暴露 ``Star.html_render`` 同签名的
            ``html_render(tmpl, data, return_url=True, options=None)``。
            生产链路由模块 G/M 注入插件 Star 实例；离线冒烟注入同签名 fake。
        config_mgr: ``db_config.ConfigManager`` 实例。渲染基础设施配置与
            消息总结**共用**（PRD 2.1 F2），经 ``get_summary_setting_typed``
            读取 5 项 ``summary_t2i_*`` 配置，不新增 stats_t2i_ 键。
    """

    def __init__(self, context, config_mgr) -> None:
        self.context = context
        self.config_mgr = config_mgr
        # 模板缓存：(mtime, 内容)；mtime 未变则复用，变则重读
        self._tmpl_cache: tuple[float, str] | None = None

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    async def render_card(self, data: StatsData, title: str) -> str | None:
        """完整渲染流程：模板加载 → 主题/CDN 解析 → 上下文组装 → 两轮渲染。

        Returns:
            图片文件路径或 http(s) URL（可直接 ``event.image_result(path)`` /
            构造图片消息链）；任何失败（模板缺失 / 两轮全败 / 未预期异常）
            返回 None，**绝不向上抛异常**，由上层兜底链路接管。
        """
        try:
            tmpl = self._load_template()
            if not tmpl:
                return None
            theme = await self._resolve_theme()
            providers = await self._resolve_cdn_providers()
            logger.info(f"[Stats] T2I 主题判定={theme}，CDN 节点序={providers}")
            ctx = self._build_context(data, title, theme, providers)
            return await self._render_two_rounds(tmpl, ctx)
        except Exception as e:
            logger.error(
                f"[Stats] T2I 渲染核心未预期异常，返回 None 交上层兜底: {e}",
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
                    f"[Stats] T2I 主题模式配置 {raw!r} 非法（auto/light/dark），回退 auto"
                )
        except Exception as e:
            logger.warning(f"[Stats] 读取主题模式配置失败，回退 auto: {e}")
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
                    f"[Stats] 深色时段起点 {raw_dark!r} 非法（需 HH:MM 24 小时制），"
                    f"回退 {_DEFAULT_DARK_START}"
                )
            else:
                dark_minutes = parsed
        except Exception as e:
            logger.warning(
                f"[Stats] 读取深色时段起点配置失败，回退 {_DEFAULT_DARK_START}: {e}"
            )
        try:
            raw_light = await self._read_setting("summary_t2i_light_start", str)
            parsed = _hhmm_to_minutes(raw_light)
            if parsed is None:
                logger.warning(
                    f"[Stats] 浅色时段起点 {raw_light!r} 非法（需 HH:MM 24 小时制），"
                    f"回退 {_DEFAULT_LIGHT_START}"
                )
            else:
                light_minutes = parsed
        except Exception as e:
            logger.warning(
                f"[Stats] 读取浅色时段起点配置失败，回退 {_DEFAULT_LIGHT_START}: {e}"
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
                        logger.debug(f"[Stats] CDN 配置忽略未知节点: {item!r}")
                        continue
                    if key not in providers:  # 去重，避免同节点重试
                        providers.append(key)
                if providers:
                    return providers
        except Exception as e:
            logger.warning(f"[Stats] 读取 CDN 节点配置失败，回退默认序: {e}")
        return list(_CDN_DEFAULT_PROVIDERS)

    async def _resolve_timeout(self) -> int:
        """解析单轮渲染超时（秒）：5–300 合法，越界/异常回退 30 并记 warning。"""
        try:
            value = int(await self._read_setting("summary_t2i_timeout", int))
            if _TIMEOUT_MIN <= value <= _TIMEOUT_MAX:
                return value
            logger.warning(
                f"[Stats] T2I 渲染超时 {value} 越界"
                f"（{_TIMEOUT_MIN}–{_TIMEOUT_MAX} 秒），回退 {_DEFAULT_TIMEOUT}"
            )
        except Exception as e:
            logger.warning(
                f"[Stats] 读取 T2I 渲染超时配置失败，回退 {_DEFAULT_TIMEOUT}: {e}"
            )
        return _DEFAULT_TIMEOUT

    # ------------------------------------------------------------------
    # 模板与上下文组装
    # ------------------------------------------------------------------

    def _load_template(self) -> str | None:
        """读取报告模板并按 (mtime, 内容) 缓存；失败记 error 返回 None。"""
        path = os.path.join(os.path.dirname(__file__), "templates", "stats_report.html")
        try:
            mtime = os.path.getmtime(path)
            if self._tmpl_cache is not None and self._tmpl_cache[0] == mtime:
                return self._tmpl_cache[1]
            with open(path, encoding="utf-8") as f:
                content = f.read()
            self._tmpl_cache = (mtime, content)
            return content
        except Exception as e:
            logger.error(f"[Stats] 读取 T2I 数据分析报告模板失败: {e}")
            return None

    def _build_context(
        self,
        data: StatsData,
        title: str,
        theme: str = "light",
        providers: list[str] | None = None,
    ) -> dict:
        """按模块 E / 模板共享契约组装模板上下文（键名不可改，模板侧按此开发）。

        全程防御式取值：``data`` 为 None 或字段缺损时以 0/空/零分布兜底，
        绝不抛异常。数字格式化（千分位 / 比例百分号一位小数 / 日均一位小数）
        在本方法内完成，模板只做展示。

        Returns:
            契约键：``theme / title / label / group_desc / cards /
            trend / trend_bars / trend_label_every / hour_dist / weekday_dist /
            peak_hour / peak_weekday / hour_bars / weekday_bars /
            hour_peak_label / weekday_peak_label / ranking /
            member_in_ranking / group_ranking / member / generated_at /
            disclaimer / cdn``。
        """
        query = getattr(data, "query", None)
        time_range = getattr(query, "time_range", None) if query is not None else None
        label = str(getattr(time_range, "label", "") or "")
        group_id = getattr(query, "group_id", None) if query is not None else None
        member_id = getattr(query, "member_id", None) if query is not None else None

        group_id_s = str(group_id).strip() if group_id is not None else ""
        group_desc = f"群号 {group_id_s}" if group_id_s else "全部群"

        # 四统计卡（峰值时段：hourly_dist 全 0 时 peak_hour 为 None → 「—」）
        hour_dist = _norm_dist(getattr(data, "hourly_dist", None), 24)
        weekday_dist = _norm_dist(getattr(data, "weekday_dist", None), 7)
        peak_hour = _resolve_peak(hour_dist, getattr(data, "peak_hour", None))
        peak_weekday = _resolve_peak(weekday_dist, None)
        cards = {
            "total_messages": _fmt_int(getattr(data, "total_messages", 0)),
            "total_images": _fmt_int(getattr(data, "total_images", 0)),
            "active_senders": _fmt_int(getattr(data, "active_senders", 0)),
            "peak_hour_text": _hour_text(peak_hour),
        }

        # 每日趋势（trend 原始序列供 ECharts tojson 注入；trend_bars 为 CSS 兜底）
        trend, trend_bars, trend_label_every = self._build_trend(
            getattr(data, "daily_trend", None)
        )

        # 发言人排行（member 维度时高亮目标成员）
        ranking = self._build_ranking(getattr(data, "sender_ranking", None), member_id)
        member_in_ranking = any(item["highlight"] for item in ranking)

        # 群排行（仅全部群视图非空；空 → None，模板整区不渲染）
        group_ranking = self._build_group_ranking(getattr(data, "group_ranking", None))

        # 个人维度（可选；member_id 非 None 时非空）
        member = self._build_member(getattr(data, "member", None))

        generated_at = getattr(data, "generated_at", None)
        if isinstance(generated_at, datetime):
            generated_at_s = generated_at.strftime("%Y-%m-%d %H:%M")
        elif isinstance(generated_at, str) and generated_at.strip():
            generated_at_s = generated_at.strip()
        else:
            generated_at_s = datetime.now().strftime("%Y-%m-%d %H:%M")

        return {
            "theme": theme if theme in ("light", "dark") else "light",
            "title": str(title or "") or "群聊数据统计",
            "label": label,
            "group_desc": group_desc,
            "cards": cards,
            "trend": trend,
            "trend_bars": trend_bars,
            "trend_label_every": trend_label_every,
            "hour_dist": hour_dist,
            "weekday_dist": weekday_dist,
            "peak_hour": peak_hour,
            "peak_weekday": peak_weekday,
            "hour_bars": _make_vbars(hour_dist, _HOUR_LABELS, peak_hour),
            "weekday_bars": _make_vbars(weekday_dist, _WEEKDAY_LABELS, peak_weekday),
            "hour_peak_label": _peak_hour_label(hour_dist, peak_hour),
            "weekday_peak_label": _peak_weekday_label(weekday_dist, peak_weekday),
            "ranking": ranking,
            "member_in_ranking": member_in_ranking,
            "group_ranking": group_ranking,
            "member": member,
            "generated_at": generated_at_s,
            "disclaimer": _DISCLAIMER,
            "cdn": {
                "providers": list(providers)
                if providers
                else list(_CDN_DEFAULT_PROVIDERS),
                "libs": _CDN_LIBS,
            },
        }

    @staticmethod
    def _build_trend(raw: object) -> tuple[list[dict], list[dict], int]:
        """每日趋势归一化。

        Returns:
            ``(trend, trend_bars, label_every)``：

            - ``trend``：``[{"date": "YYYY-MM-DD", "count": int}]``（ECharts
              经 tojson 注入；脏条目跳过、负数归 0）；
            - ``trend_bars``：CSS 兜底竖柱数据（label 取 MM-DD 短标签、
              percent 相对最大值）；
            - ``label_every``：CSS 兜底标签抽稀间隔（最多显示约 12 个标签）。
        """
        trend: list[dict] = []
        for item in raw or []:  # type: ignore[union-attr]
            if isinstance(item, dict):
                date_s = str(item.get("date", "") or "").strip()
                count = item.get("count", 0)
            else:
                date_s = str(getattr(item, "date", "") or "").strip()
                count = getattr(item, "count", 0)
            try:
                count_i = int(count)
            except (TypeError, ValueError):
                count_i = 0
            if not date_s:
                continue
            trend.append({"date": date_s, "count": count_i if count_i > 0 else 0})

        vmax = max((t["count"] for t in trend), default=0)
        bars: list[dict] = []
        for t in trend:
            date_s = t["date"]
            bars.append(
                {
                    # X 轴短标签：YYYY-MM-DD → MM-DD（不足 10 位原样）
                    "label": date_s[5:] if len(date_s) >= 10 else date_s,
                    "count": t["count"],
                    "percent": round(t["count"] / vmax * 100, 2) if vmax else 0.0,
                }
            )
        label_every = max(1, math.ceil(len(bars) / _TREND_MAX_LABELS)) if bars else 1
        return trend, bars, label_every

    @staticmethod
    def _build_ranking(raw: object, member_id: object) -> list[dict]:
        """发言人排行归一化：千分位文案 + 相对最大值百分比 + 目标成员高亮。

        返回项 ``{"id", "name", "chart_name"(图表用截断昵称), "count"(int),
        "count_text", "image_count"(int), "image_count_text", "percent",
        "highlight"}``；``highlight`` 仅 member 维度且 sender_id 命中时为 True。
        """
        member_id_s = str(member_id) if member_id is not None else ""
        items: list[dict] = []
        vmax = 0
        for item in raw or []:  # type: ignore[union-attr]
            sid = str(getattr(item, "sender_id", "") or "")
            name = str(getattr(item, "sender_name", "") or "") or sid or "未知用户"
            try:
                count = int(getattr(item, "count", 0) or 0)
            except (TypeError, ValueError):
                count = 0
            try:
                image_count = int(getattr(item, "image_count", 0) or 0)
            except (TypeError, ValueError):
                image_count = 0
            count = max(count, 0)
            image_count = max(image_count, 0)
            vmax = max(vmax, count)
            items.append(
                {
                    "id": sid,
                    "name": name,
                    "chart_name": (
                        name
                        if len(name) <= _RANK_CHART_NAME_MAX
                        else name[: _RANK_CHART_NAME_MAX - 1] + "…"
                    ),
                    "count": count,
                    "count_text": f"{count:,}",
                    "image_count": image_count,
                    "image_count_text": f"{image_count:,}",
                    "percent": 0.0,
                    "highlight": bool(member_id_s) and sid == member_id_s,
                }
            )
        for item in items:
            item["percent"] = round(item["count"] / vmax * 100, 1) if vmax else 0.0
        return items

    @staticmethod
    def _build_group_ranking(raw: object) -> list[dict] | None:
        """群排行归一化（仅全部群视图）；空列表 → None（模板整区不渲染）。

        返回项 ``{"group_id", "count"(int), "count_text", "image_count_text",
        "active_senders", "percent"}``。
        """
        items: list[dict] = []
        vmax = 0
        for item in raw or []:  # type: ignore[union-attr]
            gid = str(getattr(item, "group_id", "") or "").strip()
            if not gid:
                continue
            try:
                count = int(getattr(item, "count", 0) or 0)
            except (TypeError, ValueError):
                count = 0
            try:
                image_count = int(getattr(item, "image_count", 0) or 0)
            except (TypeError, ValueError):
                image_count = 0
            try:
                active_senders = int(getattr(item, "active_senders", 0) or 0)
            except (TypeError, ValueError):
                active_senders = 0
            count = max(count, 0)
            vmax = max(vmax, count)
            items.append(
                {
                    "group_id": gid,
                    "count": count,
                    "count_text": f"{count:,}",
                    "image_count_text": f"{max(image_count, 0):,}",
                    "active_senders": max(active_senders, 0),
                    "percent": 0.0,
                }
            )
        if not items:
            return None
        for item in items:
            item["percent"] = round(item["count"] / vmax * 100, 1) if vmax else 0.0
        return items

    def _build_member(self, m: object) -> dict | None:
        """个人维度上下文（可选）；``member`` 为 None → None（模板整区不渲染）。

        返回键：``id / name / count_text / image_count_text / ratio_text /
        rank_text / active_days / avg_text / hour_dist / weekday_dist /
        peak_hour / peak_weekday / hour_bars / weekday_bars /
        hour_peak_label / weekday_peak_label``。
        """
        if m is None:
            return None
        hour_dist = _norm_dist(getattr(m, "hourly_dist", None), 24)
        weekday_dist = _norm_dist(getattr(m, "weekday_dist", None), 7)
        peak_hour = _resolve_peak(hour_dist, None)
        peak_weekday = _resolve_peak(weekday_dist, None)

        rank = getattr(m, "rank", None)
        try:
            rank_i = int(rank)  # type: ignore[arg-type]
            rank_text = str(rank_i) if rank_i > 0 else "—"
        except (TypeError, ValueError):
            rank_text = "—"
        try:
            active_days = max(int(getattr(m, "active_days", 0) or 0), 0)
        except (TypeError, ValueError):
            active_days = 0

        sender_id = str(getattr(m, "sender_id", "") or "")
        name = str(getattr(m, "sender_name", "") or "") or sender_id or "未知用户"
        return {
            "id": sender_id,
            "name": name,
            "count_text": _fmt_int(getattr(m, "count", 0)),
            "image_count_text": _fmt_int(getattr(m, "image_count", 0)),
            "ratio_text": _fmt_ratio(getattr(m, "ratio", 0)),
            "rank_text": rank_text,
            "active_days": active_days,
            "avg_text": _fmt_avg(getattr(m, "avg_per_day", 0)),
            "hour_dist": hour_dist,
            "weekday_dist": weekday_dist,
            "peak_hour": peak_hour,
            "peak_weekday": peak_weekday,
            "hour_bars": _make_vbars(hour_dist, _HOUR_LABELS, peak_hour),
            "weekday_bars": _make_vbars(weekday_dist, _WEEKDAY_LABELS, peak_weekday),
            "hour_peak_label": _peak_hour_label(hour_dist, peak_hour),
            "weekday_peak_label": _peak_weekday_label(weekday_dist, peak_weekday),
        }

    # ------------------------------------------------------------------
    # 两轮渲染 + 结果校验
    # ------------------------------------------------------------------

    async def _render_two_rounds(self, tmpl: str, data: dict) -> str | None:
        """两轮渲染：R1 PNG（超时 T）→ 失败 R2 JPEG q80（超时 2T）→ 全败 None。

        ``device_scale_factor_level="ultra"``（1.8 倍设备像素比）提升输出
        分辨率，与 summary/profile 渲染范式保持一致（T2I 服务端视口固定
        1280px，860px 画布居中输出，放大后手机端查看更清晰）。

        Returns:
            校验通过的产物路径/URL；bytes 落临时文件（后缀按魔数 .png/.jpg）。
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
                f"[Stats] T2I 第 {round_no} 轮渲染开始"
                f"（{options['type']}，超时 {options['timeout']}ms）"
            )
            try:
                ret = await self.context.html_render(
                    tmpl, data, return_url=False, options=options
                )
            except Exception as e:
                cost = time.monotonic() - start
                logger.warning(
                    f"[Stats] T2I 第 {round_no} 轮渲染异常（耗时 {cost:.1f}s）: {e}"
                )
                continue
            cost = time.monotonic() - start
            if self._validate_image(ret):
                logger.info(f"[Stats] T2I 第 {round_no} 轮渲染成功，耗时 {cost:.1f}s")
                try:
                    path = await asyncio.to_thread(self._ret_to_path, ret)
                except Exception as e:
                    logger.warning(
                        f"[Stats] T2I 第 {round_no} 轮渲染产物落路径失败: {e}"
                    )
                    continue
                if path:
                    return path
                logger.warning(f"[Stats] T2I 第 {round_no} 轮产物转路径为空")
                continue
            logger.warning(
                f"[Stats] T2I 第 {round_no} 轮渲染结果校验失败，耗时 {cost:.1f}s"
            )
        logger.error("[Stats] T2I 两轮渲染均失败，交上层兜底链路")
        return None

    @staticmethod
    def _ret_to_path(ret: object) -> str | None:
        """渲染产物 → 路径/URL：bytes 写临时文件（后缀按魔数）；http(s) URL
        原样返回；已存在的本地路径透传；其余（空值/不可识别）→ None。"""
        if isinstance(ret, (bytes, bytearray)):
            data = bytes(ret)
            suffix = ".png" if data.startswith(_PNG_MAGIC) else ".jpg"
            tmp = tempfile.NamedTemporaryFile(
                prefix="stats_t2i_", suffix=suffix, delete=False
            )
            try:
                tmp.write(data)
            finally:
                tmp.close()
            return tmp.name
        value = str(ret).strip()
        if not value:
            return None
        if value.startswith(("http://", "https://")):
            return value
        if os.path.exists(value):
            return value
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
            StatsT2IRenderer._log_bad_image(data[:512])
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
                    logger.warning(f"[Stats] T2I 读取渲染产物文件失败: {e}")
                    return False
                if head.startswith(_PNG_MAGIC) or head.startswith(_JPEG_MAGIC):
                    return True
                StatsT2IRenderer._log_bad_image(head)
                return False
        logger.warning(f"[Stats] T2I 渲染返回空或不可识别结果: {type(ret).__name__}")
        return False

    @staticmethod
    def _log_bad_image(head: bytes) -> None:
        """魔数校验失败日志：能提取错误页 <title> 则记出，否则记头部字节 hex。"""
        title = _extract_html_title(head)
        if title:
            logger.warning(f"[Stats] T2I 渲染返回错误页面而非图片，页面标题: {title}")
        else:
            logger.warning(
                f"[Stats] T2I 渲染返回非图片内容（头部字节: {head[:16].hex()}）"
            )
