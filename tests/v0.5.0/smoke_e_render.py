# ruff: noqa: I001
"""v0.5.0 模块 E T2I 渲染与报告模板冒烟脚本。

被测对象：
- ``stats/t2i_render.py``（StatsT2IRenderer：两轮渲染 / 魔数校验 /
  summary_t2i_* 配置复用 / 模板 mtime 缓存 / 绝不抛异常契约）
- ``stats/templates/stats_report.html``（860px 双主题报告模板：四统计卡 /
  每日趋势 / 发言人排行 / 24h·星期分布 / 群排行 / 个人区 / 页脚免责）

覆盖点（任务验收清单）：
- jinja2 双环境（autoescape on/off）加载模板渲染均成功；
- ``_build_context`` 组装断言：24/7 分布定长（脏/短输入归一化）、排行数据
  落地（昵称/消息数/图片数/百分比/高亮）、数字格式化（千分位/百分号一位
  小数/日均一位小数）、峰值时段 None → 「—」；
- member 区条件渲染（member=None 不输出）、group_ranking 条件渲染；
- 注入尝试（昵称含 ``</script>`` 与引号/标签注入）经 tojson/|e 无害化；
- 双主题 data-theme 判定（light/dark）；
- 渲染器本体：fake context（html_render 按剧本返回）验证两轮渲染
  （R1 png T / R2 jpeg q80 2T）、魔数校验、bytes 落盘、URL 直通、
  两轮全败返 None、html_render 异常返 None 绝不冒泡；
- summary_t2i_* 五键读取路径、超时越界回退、模板 mtime 缓存；
- 模板内 JS 抽出后经 ``node --check`` 语法校验（无 node 时跳过）。

范式沿用 tests/v0.5.0/smoke_b_repository.py：astrbot.* 全部
sys.modules stub（注入在任何被测包 import 之前），不依赖 AstrBot 运行时；
数据用模块 C 真实 dataclass（stats/models.py，纯模型无依赖链）。

运行方式（插件根目录，二者皆可）：
    python -m pytest "tests/v0.5.0/smoke_e_render.py" -v
    python "tests/v0.5.0/smoke_e_render.py"
"""

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from datetime import datetime
from types import SimpleNamespace


# ============================================================
# 一、astrbot.* stub 注入（必须在导入被测包之前）
# ============================================================


def _new_module(name: str) -> types.ModuleType:
    return types.ModuleType(name)


_astrbot = _new_module("astrbot")
_astrbot_api = _new_module("astrbot.api")


class _StubLogger:
    """记录各级日志内容，便于断言（配置回退 warning / 魔数校验 warning 等）。"""

    def __init__(self):
        self.records = {"info": [], "warning": [], "error": [], "debug": []}

    def _log(self, level, msg, args):
        try:
            text = msg % args if args else str(msg)
        except Exception:
            text = str(msg)
        self.records[level].append(text)

    def info(self, msg, *args, **kw):
        self._log("info", msg, args)

    def warning(self, msg, *args, **kw):
        self._log("warning", msg, args)

    def error(self, msg, *args, **kw):
        self._log("error", msg, args)

    def debug(self, msg, *args, **kw):
        self._log("debug", msg, args)


_STUB_LOGGER = _StubLogger()
_astrbot_api.logger = _STUB_LOGGER
_astrbot.api = _astrbot_api

sys.modules["astrbot"] = _astrbot
sys.modules["astrbot.api"] = _astrbot_api

# ---- 剔除被测包缓存（多测试文件合跑兼容）----
_PKG = "astrbot_plugin_group_history_save_mysql"
for _name in list(sys.modules):
    if _name == _PKG or _name.startswith(_PKG + "."):
        del sys.modules[_name]

_PLUGINS_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..")
)
if _PLUGINS_DIR not in sys.path:
    sys.path.insert(0, _PLUGINS_DIR)


# ============================================================
# 二、导入被测代码与真实数据模型（stub 已就位；models 为纯模型无依赖链）
# ============================================================

import jinja2  # noqa: E402

from astrbot_plugin_group_history_save_mysql.core.stats import t2i_render as T2I  # noqa: E402
from astrbot_plugin_group_history_save_mysql.core.stats.models import (  # noqa: E402
    GroupRankItem,
    MemberStats,
    SenderRankItem,
    StatsData,
    StatsQuery,
    StatsTimeRange,
)

# ---- 魔数校验/两轮渲染用例的预制字节 ----
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00\x01\x02\x03" * 8
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00\x01\x02\x03" * 8
# T2I 服务错误页风格的非图片返回（含 <title>，供错误页日志断言）
HTML_PAGE_BYTES = (
    b"<html><head><title>502 Bad Gateway</title></head>"
    b"<body>render backend down</body></html>"
)

_TEMPLATE_PATH = os.path.join(
    os.path.dirname(T2I.__file__), "templates", "stats_report.html"
)

# 恶意昵称：</script> 逃逸 + 引号/属性注入 + 标签注入
_EVIL_NAME = '</script><script>alert("xss")</script>"><img src=x onerror=alert(1)>'

_HAS_NODE = shutil.which("node") is not None
_needs_node = unittest.skipIf(not _HAS_NODE, "node 不可用，跳过 JS 语法校验")


def _run(coro):
    """asyncio.run 驱动异步用例（环境无 pytest-asyncio 依赖）。"""
    return asyncio.run(coro)


# ============================================================
# 三、假基础设施
# ============================================================


class FakeConfigMgr:
    """ConfigManager 内存替身（仅 summary 侧 T2I 五键 typed 读取路径）。

    ``typed_overrides`` 直接指定 typed 返回值（值为 Exception 实例时改为抛出，
    模拟单键读取失败）；``exc`` 使全部读取抛异常（模拟配置层整体故障）。
    签名为 db_config 现行的单参 ``(key)`` 形态，渲染器 ``_read_setting`` 的
    签名探测应命中单参分支。
    """

    DEFAULTS = {
        "summary_t2i_theme_mode": "light",
        "summary_t2i_dark_start": "22:00",
        "summary_t2i_light_start": "08:00",
        "summary_t2i_timeout": 30,
        "summary_t2i_cdn_providers": [
            "bootcdn",
            "npmmirror",
            "staticfile",
            "jsdelivr",
            "unpkg",
        ],
    }

    def __init__(self, typed_overrides=None, exc=None):
        self.typed_overrides = dict(typed_overrides or {})
        self.exc = exc
        self.calls = []

    async def get_summary_setting_typed(self, key):
        self.calls.append(key)
        if self.exc is not None:
            raise self.exc
        if key in self.typed_overrides:
            value = self.typed_overrides[key]
            if isinstance(value, BaseException):
                raise value
            return value
        return self.DEFAULTS[key]


class FakeContext:
    """渲染宿主替身：html_render 按剧本逐轮返回值（Exception 实例则抛出），
    并记录每次调用的 tmpl/data/return_url/options。"""

    def __init__(self, html_script=None):
        self.html_script = list(html_script or [])
        self.html_calls = []

    async def html_render(self, tmpl, data, return_url=True, options=None):
        self.html_calls.append(
            {"tmpl": tmpl, "data": data, "return_url": return_url, "options": options}
        )
        if not self.html_script:
            raise AssertionError("html_render 调用次数超出剧本预期")
        ret = self.html_script.pop(0)
        if isinstance(ret, BaseException):
            raise ret
        return ret


def make_stats_data(
    *,
    group_id="654321",
    member_id=None,
    peak_hour=21,
    zero_dist=False,
    with_member=False,
    with_group_ranking=False,
    malicious=False,
    evil_rank_name=_EVIL_NAME,
) -> StatsData:
    """按模块 C 真实 dataclass 构造完整 StatsData（契约字段一个不少）。"""
    hourly = (
        [0] * 24
        if zero_dist
        else [
            3,
            1,
            0,
            0,
            0,
            2,
            8,
            20,
            35,
            42,
            55,
            60,
            58,
            50,
            47,
            44,
            49,
            57,
            66,
            72,
            80,
            76,
            40,
            15,
        ]
    )
    if zero_dist:
        peak_hour = None
    weekday = [0] * 7 if zero_dist else [120, 131, 108, 99, 145, 210, 187]
    name_top = evil_rank_name if malicious else "张三"
    ranking = [
        SenderRankItem(
            sender_id="10001", sender_name=name_top, count=1234, image_count=56
        ),
        SenderRankItem(
            sender_id="10002", sender_name="李四", count=987, image_count=12
        ),
        SenderRankItem(sender_id="10003", sender_name="王五", count=456, image_count=0),
    ]
    member = None
    if with_member:
        member = MemberStats(
            sender_id="10001",
            sender_name=name_top,
            count=1234,
            image_count=56,
            ratio=0.1234,
            rank=1,
            active_days=6,
            avg_per_day=205.6666,
            hourly_dist=hourly,
            weekday_dist=weekday,
        )
    group_ranking = []
    if with_group_ranking:
        group_ranking = [
            GroupRankItem(
                group_id="654321", count=5000, image_count=200, active_senders=42
            ),
            GroupRankItem(
                group_id="111222", count=3000, image_count=90, active_senders=17
            ),
        ]
    query = StatsQuery(
        group_id=group_id,
        member_id=member_id,
        time_range=StatsTimeRange(
            start=datetime(2026, 7, 29, 0, 0, 0),
            end=datetime(2026, 8, 4, 0, 0, 0),
            label="近7天",
        ),
        top_n=10,
    )
    return StatsData(
        query=query,
        total_messages=1234567,
        total_images=8910,
        active_senders=42,
        peak_hour=peak_hour,
        first_msg_time=datetime(2026, 7, 29, 8, 1, 2),
        last_msg_time=datetime(2026, 8, 3, 23, 59, 1),
        daily_trend=[
            {"date": "2026-07-29", "count": 100},
            {"date": "2026-07-30", "count": 250},
            {"date": "2026-07-31", "count": 0},
            {"date": "2026-08-01", "count": 320},
            {"date": "2026-08-02", "count": 180},
            {"date": "2026-08-03", "count": 275},
        ],
        hourly_dist=hourly,
        weekday_dist=weekday,
        sender_ranking=ranking,
        group_ranking=group_ranking,
        member=member,
        generated_at=datetime(2026, 8, 4, 12, 0, 0),
    )


def make_renderer(script=None, cfg_overrides=None, cfg_exc=None):
    """构造 (renderer, fake_context, fake_config) 三元组。"""
    ctx = FakeContext(script)
    cfg = FakeConfigMgr(typed_overrides=cfg_overrides, exc=cfg_exc)
    return T2I.StatsT2IRenderer(ctx, cfg), ctx, cfg


def render_html(ctx_dict, autoescape):
    """用指定 autoescape 开关的 jinja2 环境渲染模板文件。"""
    with open(_TEMPLATE_PATH, encoding="utf-8") as f:
        tmpl_src = f.read()
    env = jinja2.Environment(autoescape=autoescape)
    return env.from_string(tmpl_src).render(**ctx_dict)


def extract_script(html_text):
    """抽出模板尾部 <script>…</script> 内联 JS 原文（node --check 用）。"""
    start = html_text.rindex("<script>")
    end = html_text.rindex("</script>")
    assert start < end, "脚本块定界异常"
    return html_text[start + len("<script>") : end]


# ============================================================
# 四、_build_context 上下文组装
# ============================================================


class TestBuildContext(unittest.TestCase):
    def _ctx(self, **kw):
        renderer, _, _ = make_renderer()
        return renderer._build_context(make_stats_data(**kw), "群统计 · 测试群")

    def test_contract_keys_present(self):
        ctx = self._ctx()
        for key in (
            "theme",
            "title",
            "label",
            "group_desc",
            "cards",
            "trend",
            "hour_dist",
            "weekday_dist",
            "peak_hour",
            "peak_weekday",
            "hour_bars",
            "weekday_bars",
            "ranking",
            "group_ranking",
            "member",
            "generated_at",
            "disclaimer",
            "cdn",
        ):
            self.assertIn(key, ctx, f"契约键缺失: {key}")

    def test_dist_fixed_length(self):
        ctx = self._ctx()
        self.assertEqual(len(ctx["hour_dist"]), 24)
        self.assertEqual(len(ctx["weekday_dist"]), 7)
        self.assertEqual(len(ctx["hour_bars"]), 24)
        self.assertEqual(len(ctx["weekday_bars"]), 7)

    def test_dist_short_or_dirty_input_normalized(self):
        """短/脏分布输入归一化为定长零填充，绝不抛异常。"""
        data = make_stats_data()
        data.hourly_dist = [5, "x", -3, None]  # 脏值
        data.weekday_dist = [1, 2]  # 长度不足
        renderer, _, _ = make_renderer()
        ctx = renderer._build_context(data, "t")
        self.assertEqual(len(ctx["hour_dist"]), 24)
        self.assertEqual(ctx["hour_dist"][0], 5)
        self.assertEqual(ctx["hour_dist"][1], 0)  # "x" → 0
        self.assertEqual(ctx["hour_dist"][2], 0)  # 负数 → 0
        self.assertEqual(len(ctx["weekday_dist"]), 7)
        self.assertEqual(ctx["weekday_dist"], [1, 2, 0, 0, 0, 0, 0])

    def test_cards_number_formatting(self):
        ctx = self._ctx()
        self.assertEqual(ctx["cards"]["total_messages"], "1,234,567")
        self.assertEqual(ctx["cards"]["total_images"], "8,910")
        self.assertEqual(ctx["cards"]["active_senders"], "42")
        self.assertEqual(ctx["cards"]["peak_hour_text"], "21 时")

    def test_peak_hour_none_dash(self):
        """峰值时段 None（全零分布）→ 卡片文案「—」。"""
        ctx = self._ctx(zero_dist=True)
        self.assertIsNone(ctx["peak_hour"])
        self.assertIsNone(ctx["peak_weekday"])
        self.assertEqual(ctx["cards"]["peak_hour_text"], "—")
        self.assertEqual(ctx["hour_peak_label"], "")

    def test_label_and_group_desc(self):
        ctx = self._ctx()
        self.assertEqual(ctx["label"], "近7天")
        self.assertEqual(ctx["group_desc"], "群号 654321")
        ctx_all = self._ctx(group_id=None, with_group_ranking=True)
        self.assertEqual(ctx_all["group_desc"], "全部群")

    def test_ranking_data_landed(self):
        ctx = self._ctx()
        self.assertEqual(len(ctx["ranking"]), 3)
        top = ctx["ranking"][0]
        self.assertEqual(top["name"], "张三")
        self.assertEqual(top["count"], 1234)
        self.assertEqual(top["count_text"], "1,234")
        self.assertEqual(top["image_count"], 56)
        self.assertEqual(top["image_count_text"], "56")
        self.assertEqual(top["percent"], 100.0)  # 最大值 → 100%
        self.assertFalse(top["highlight"])
        self.assertEqual(ctx["ranking"][2]["percent"], round(456 / 1234 * 100, 1))

    def test_ranking_highlight_member_target(self):
        """member 维度：排行中目标成员 highlight=True。"""
        ctx = self._ctx(member_id="10002", with_member=True)
        flags = [r["highlight"] for r in ctx["ranking"]]
        self.assertEqual(flags, [False, True, False])
        self.assertTrue(ctx["member_in_ranking"])

    def test_group_ranking_conditional(self):
        """群视图 group_ranking 恒空列表 → None（模板整区不渲染）。"""
        self.assertIsNone(self._ctx()["group_ranking"])
        ctx = self._ctx(group_id=None, with_group_ranking=True)
        self.assertIsNotNone(ctx["group_ranking"])
        self.assertEqual(len(ctx["group_ranking"]), 2)
        self.assertEqual(ctx["group_ranking"][0]["count_text"], "5,000")
        self.assertEqual(ctx["group_ranking"][0]["active_senders"], 42)
        self.assertEqual(ctx["group_ranking"][0]["percent"], 100.0)

    def test_member_none_vs_present(self):
        self.assertIsNone(self._ctx()["member"])
        m = self._ctx(member_id="10001", with_member=True)["member"]
        self.assertIsNotNone(m)
        self.assertEqual(m["count_text"], "1,234")
        self.assertEqual(m["image_count_text"], "56")
        self.assertEqual(m["ratio_text"], "12.3%")  # 百分号一位小数
        self.assertEqual(m["rank_text"], "1")
        self.assertEqual(m["active_days"], 6)
        self.assertEqual(m["avg_text"], "205.7")  # 日均一位小数
        self.assertEqual(len(m["hour_dist"]), 24)
        self.assertEqual(len(m["weekday_dist"]), 7)

    def test_member_rank_none_dash(self):
        """全部群视图 rank=None → 「—」。"""
        data = make_stats_data(group_id=None, member_id="10001", with_member=True)
        data.member.rank = None
        renderer, _, _ = make_renderer()
        ctx = renderer._build_context(data, "t")
        self.assertEqual(ctx["member"]["rank_text"], "—")

    def test_trend_shape(self):
        ctx = self._ctx()
        self.assertEqual(len(ctx["trend"]), 6)
        self.assertEqual(ctx["trend"][0], {"date": "2026-07-29", "count": 100})
        self.assertEqual(len(ctx["trend_bars"]), 6)
        self.assertEqual(ctx["trend_bars"][0]["label"], "07-29")  # MM-DD 短标签
        self.assertEqual(ctx["trend_bars"][3]["percent"], 100.0)  # 320 为最大
        self.assertGreaterEqual(ctx["trend_label_every"], 1)

    def test_generated_at_formatted(self):
        ctx = self._ctx()
        self.assertEqual(ctx["generated_at"], "2026-08-04 12:00")

    def test_theme_and_cdn(self):
        renderer, _, _ = make_renderer()
        ctx = renderer._build_context(
            make_stats_data(), "t", theme="dark", providers=["jsdelivr"]
        )
        self.assertEqual(ctx["theme"], "dark")
        self.assertEqual(ctx["cdn"]["providers"], ["jsdelivr"])
        self.assertIn("echarts", ctx["cdn"]["libs"])
        self.assertEqual(ctx["cdn"]["libs"]["echarts"]["ver"], "5.5.1")
        # 非法主题回退 light；providers 空回退默认序
        ctx2 = renderer._build_context(make_stats_data(), "t", theme="green")
        self.assertEqual(ctx2["theme"], "light")
        self.assertEqual(ctx2["cdn"]["providers"], T2I._CDN_DEFAULT_PROVIDERS)

    def test_broken_data_never_raises(self):
        """极端兜底：SimpleNamespace 空壳数据（字段全缺）组装不抛异常。"""
        renderer, _, _ = make_renderer()
        ctx = renderer._build_context(SimpleNamespace(), "")
        self.assertEqual(ctx["cards"]["total_messages"], "0")
        self.assertEqual(ctx["cards"]["peak_hour_text"], "—")
        self.assertEqual(len(ctx["hour_dist"]), 24)
        self.assertEqual(ctx["ranking"], [])
        self.assertIsNone(ctx["member"])
        self.assertIsNone(ctx["group_ranking"])
        ctx_none = renderer._build_context(None, None)
        self.assertEqual(ctx_none["title"], "群聊数据统计")


# ============================================================
# 五、模板渲染（jinja2 双环境）
# ============================================================


class TestTemplateRender(unittest.TestCase):
    def _full_ctx(self, **kw):
        renderer, _, _ = make_renderer()
        return renderer._build_context(
            make_stats_data(
                member_id="10001",
                with_member=True,
                with_group_ranking=True,
                group_id=None,
                **kw,
            ),
            "群统计 · 测试",
        )

    def test_dual_env_render(self):
        """autoescape on/off 双环境均渲染成功，关键区块齐全。"""
        for autoescape in (True, False):
            html_text = render_html(self._full_ctx(), autoescape)
            self.assertIn("📊", html_text)
            self.assertIn("总消息数", html_text)
            self.assertIn("发言人排行", html_text)
            self.assertIn("群排行", html_text)
            self.assertIn('id="member-section"', html_text)
            self.assertIn("每日趋势", html_text)
            self.assertIn("活跃时段分布", html_text)
            self.assertIn("由 群聊数据分析 生成", html_text)
            self.assertIn("快照", html_text)  # 免责声明口径说明

    def test_member_section_conditional(self):
        """member=None 时个人区整块不输出。

        注意：页面尾部内联 JS 恒定引用 member-* 图表 id（getElementById 按 id
        兜底查找），故断言 DOM 属性形态 ``id="…"`` 缺失而非裸字符串。
        """
        renderer, _, _ = make_renderer()
        ctx = renderer._build_context(make_stats_data(), "t")
        html_text = render_html(ctx, autoescape=False)
        self.assertNotIn('id="member-section"', html_text)
        self.assertNotIn('id="member-hour-chart"', html_text)
        self.assertNotIn('id="member-weekday-css"', html_text)
        self.assertNotIn("👤 个人数据", html_text)

    def test_group_ranking_conditional(self):
        """group_ranking=None 时群排行区整块不输出。"""
        renderer, _, _ = make_renderer()
        ctx = renderer._build_context(make_stats_data(), "t")  # 单群视图
        html_text = render_html(ctx, autoescape=False)
        self.assertNotIn("🏅 群排行", html_text)

    def test_ranking_empty_section_hidden(self):
        """ranking 空列表时发言人排行区不输出。

        断言用 DOM 专属标记（带 emoji 的标题 / ``id="rank-css"`` 属性）：
        内联 JS 恒定含 renderRankChart 与「发言人排行」字样注释，裸串不可用。
        """
        data = make_stats_data()
        data.sender_ranking = []
        renderer, _, _ = make_renderer()
        ctx = renderer._build_context(data, "t")
        html_text = render_html(ctx, autoescape=False)
        self.assertNotIn("🏆 发言人排行", html_text)
        self.assertNotIn('id="rank-css"', html_text)
        self.assertNotIn('id="rank-chart"', html_text)
        # 有排行时这些标记应出现（正向对照）
        ctx2 = renderer._build_context(make_stats_data(), "t")
        html2 = render_html(ctx2, autoescape=False)
        self.assertIn("🏆 发言人排行", html2)
        self.assertIn('id="rank-css"', html2)

    def test_dual_theme_data_theme(self):
        renderer, _, _ = make_renderer()
        data = make_stats_data()
        light = render_html(
            renderer._build_context(data, "t", theme="light"), autoescape=False
        )
        dark = render_html(
            renderer._build_context(data, "t", theme="dark"), autoescape=False
        )
        self.assertIn('data-theme="light"', light)
        self.assertIn('data-theme="dark"', dark)

    def test_peak_none_dash_rendered(self):
        """峰值时段 None → 「—」直出卡片。"""
        renderer, _, _ = make_renderer()
        ctx = renderer._build_context(make_stats_data(zero_dist=True), "t")
        html_text = render_html(ctx, autoescape=False)
        self.assertIn('<div class="stat-value">—</div>', html_text)

    def test_xss_injection_neutralized(self):
        """恶意昵称（</script> 逃逸 + 属性/标签注入）双环境无害化。"""
        for autoescape in (True, False):
            ctx = self._ctx_malicious()
            html_text = render_html(ctx, autoescape)

            # 1) 页面只允许存在模板自身的一个 <script> 块
            self.assertEqual(html_text.count("<script>"), 1)
            self.assertEqual(html_text.count("</script>"), 1)
            # 2) 注入标签不得以原始形态出现
            self.assertNotIn("<img src=x", html_text)
            self.assertNotIn("<script>alert", html_text)
            # 3) 属性注入载荷只能以整体转义形态存在（尖括号已转义 → 无法
            #    构造新元素，onerror= 只是无害文本片段）
            self.assertIn("&lt;img src=x onerror=alert(1)&gt;", html_text)
            # 4) script 内 tojson 注入区：尖括号均被 \uXXXX 转义
            js_text = extract_script(html_text)
            self.assertNotIn("</script", js_text)
            self.assertNotIn("<script", js_text)
            self.assertNotIn("<img", js_text)
            self.assertIn("\\u003c/script\\u003e", js_text)
            # 5) 恶意昵称仍完整可展示（转义形态出现在排行行）
            self.assertIn("&lt;/script&gt;", html_text)

    def _ctx_malicious(self):
        renderer, _, _ = make_renderer()
        return renderer._build_context(
            make_stats_data(malicious=True, member_id="10001", with_member=True),
            '标题注入"><svg onload=alert(2)>',
        )

    def test_title_injection_escaped(self):
        ctx = self._ctx_malicious()
        html_text = render_html(ctx, autoescape=False)
        self.assertNotIn("<svg onload", html_text)
        self.assertIn("&lt;svg", html_text)

    def test_member_highlight_row(self):
        """member 维度排行目标行带高亮 class。"""
        ctx = self._full_ctx()
        html_text = render_html(ctx, autoescape=False)
        self.assertIn("rank-row--hl", html_text)
        self.assertIn("高亮：", html_text)

    def test_css_fallback_always_in_dom(self):
        """CSS 兜底图表容器永远在 DOM（trend/rank/hour/weekday/member）。"""
        html_text = render_html(self._full_ctx(), autoescape=False)
        for dom_id in (
            "trend-css",
            "rank-css",
            "hour-css",
            "weekday-css",
            "member-hour-css",
            "member-weekday-css",
        ):
            self.assertIn(f'id="{dom_id}"', html_text)

    def test_canvas_width_860(self):
        html_text = render_html(self._full_ctx(), autoescape=False)
        self.assertIn("width: 860px", html_text)

    def test_cdn_loader_and_version_locked(self):
        html_text = render_html(self._full_ctx(), autoescape=False)
        self.assertIn('"ver": "5.5.1"', html_text)
        self.assertIn("bootcdn", html_text)
        self.assertIn("unpkg", html_text)


# ============================================================
# 六、渲染器本体（fake context：两轮 / 魔数 / 异常返 None）
# ============================================================


class TestRenderCard(unittest.TestCase):
    def test_first_round_png_success(self):
        renderer, ctx, cfg = make_renderer(script=[PNG_BYTES])
        path = _run(renderer.render_card(make_stats_data(), "群统计"))
        self.assertIsInstance(path, str)
        self.assertTrue(os.path.exists(path))
        with open(path, "rb") as f:
            self.assertTrue(f.read(8).startswith(b"\x89PNG"))
        self.assertTrue(path.endswith(".png"))
        self.assertEqual(len(ctx.html_calls), 1)
        call = ctx.html_calls[0]
        self.assertFalse(call["return_url"])
        self.assertEqual(call["options"]["type"], "png")
        self.assertEqual(call["options"]["timeout"], 30000)  # T=30s → ms
        self.assertEqual(call["options"]["device_scale_factor_level"], "ultra")
        # 模板上下文契约键已注入 html_render
        self.assertIn("cards", call["data"])
        self.assertIn("ranking", call["data"])
        # summary 侧五键读取路径
        self.assertIn("summary_t2i_theme_mode", cfg.calls)
        os.unlink(path)

    def test_second_round_jpeg_fallback(self):
        """R1 返错误页（魔数败）→ R2 JPEG q80 成功，超时翻倍 2T。"""
        renderer, ctx, _ = make_renderer(script=[HTML_PAGE_BYTES, JPEG_BYTES])
        path = _run(renderer.render_card(make_stats_data(), "群统计"))
        self.assertIsInstance(path, str)
        with open(path, "rb") as f:
            self.assertTrue(f.read(2) == b"\xff\xd8")
        self.assertTrue(path.endswith(".jpg"))
        self.assertEqual(len(ctx.html_calls), 2)
        r2 = ctx.html_calls[1]["options"]
        self.assertEqual(r2["type"], "jpeg")
        self.assertEqual(r2["quality"], 80)
        self.assertEqual(r2["timeout"], 60000)  # 2T
        # 错误页 <title> 被记入 warning 便于排查
        self.assertIn("502 Bad Gateway", "\n".join(_STUB_LOGGER.records["warning"]))
        os.unlink(path)

    def test_both_rounds_fail_returns_none(self):
        renderer, ctx, _ = make_renderer(script=[HTML_PAGE_BYTES, HTML_PAGE_BYTES])
        self.assertIsNone(_run(renderer.render_card(make_stats_data(), "t")))
        self.assertEqual(len(ctx.html_calls), 2)
        self.assertIn("两轮渲染均失败", "\n".join(_STUB_LOGGER.records["error"]))

    def test_html_render_exceptions_return_none(self):
        """html_render 抛异常（单轮/双轮）一律返 None，绝不冒泡。"""
        renderer, ctx, _ = make_renderer(
            script=[RuntimeError("boom1"), RuntimeError("boom2")]
        )
        self.assertIsNone(_run(renderer.render_card(make_stats_data(), "t")))
        self.assertEqual(len(ctx.html_calls), 2)

    def test_first_round_exception_second_success(self):
        renderer, ctx, _ = make_renderer(script=[RuntimeError("boom"), PNG_BYTES])
        path = _run(renderer.render_card(make_stats_data(), "t"))
        self.assertIsInstance(path, str)
        self.assertEqual(len(ctx.html_calls), 2)
        os.unlink(path)

    def test_url_passthrough(self):
        """渲染器直接返 URL 的部署形态：URL 原样透传（视为合法）。"""
        url = "https://t2i.example.com/abc.png"
        renderer, ctx, _ = make_renderer(script=[url])
        self.assertEqual(_run(renderer.render_card(make_stats_data(), "t")), url)
        self.assertEqual(len(ctx.html_calls), 1)

    def test_config_layer_total_failure_returns_none(self):
        """配置层整体故障（读取全抛异常）不阻断渲染：配置回退默认值。"""
        renderer, ctx, _ = make_renderer(
            script=[PNG_BYTES], cfg_exc=RuntimeError("db down")
        )
        path = _run(renderer.render_card(make_stats_data(), "t"))
        self.assertIsInstance(path, str)  # 全默认值下仍渲染成功
        os.unlink(path)

    def test_build_context_unexpected_exception_returns_none(self):
        """上下文组装内部未预期异常 → error 日志 + None，不冒泡。"""
        renderer, _, _ = make_renderer(script=[PNG_BYTES])

        def boom(*args, **kwargs):
            raise ValueError("组装爆炸")

        renderer._build_context = boom
        self.assertIsNone(_run(renderer.render_card(make_stats_data(), "t")))
        self.assertIn("未预期异常", "\n".join(_STUB_LOGGER.records["error"]))

    def test_render_data_with_member_full_pipeline(self):
        """member + group_ranking 全量数据跑通渲染管线。"""
        renderer, ctx, _ = make_renderer(script=[PNG_BYTES])
        data = make_stats_data(
            group_id=None,
            member_id="10001",
            with_member=True,
            with_group_ranking=True,
        )
        path = _run(renderer.render_card(data, "群统计 · 全部群"))
        self.assertIsInstance(path, str)
        injected = ctx.html_calls[0]["data"]
        self.assertIsNotNone(injected["member"])
        self.assertIsNotNone(injected["group_ranking"])
        os.unlink(path)


# ============================================================
# 七、魔数校验 / 配置解析 / 模板缓存
# ============================================================


class TestValidateImage(unittest.TestCase):
    def test_png_jpeg_bytes_valid(self):
        self.assertTrue(T2I.StatsT2IRenderer._validate_image(PNG_BYTES))
        self.assertTrue(T2I.StatsT2IRenderer._validate_image(bytearray(PNG_BYTES)))
        self.assertTrue(T2I.StatsT2IRenderer._validate_image(JPEG_BYTES))

    def test_html_bytes_invalid(self):
        self.assertFalse(T2I.StatsT2IRenderer._validate_image(HTML_PAGE_BYTES))

    def test_http_url_valid(self):
        self.assertTrue(
            T2I.StatsT2IRenderer._validate_image("https://t2i.example.com/x.png")
        )

    def test_file_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            png = os.path.join(tmp, "a.png")
            with open(png, "wb") as f:
                f.write(PNG_BYTES)
            self.assertTrue(T2I.StatsT2IRenderer._validate_image(png))
            html_file = os.path.join(tmp, "b.html")
            with open(html_file, "wb") as f:
                f.write(HTML_PAGE_BYTES)
            self.assertFalse(T2I.StatsT2IRenderer._validate_image(html_file))
        self.assertFalse(
            T2I.StatsT2IRenderer._validate_image(os.path.join(tmp, "不存在.png"))
        )

    def test_empty_and_unrecognized_invalid(self):
        self.assertFalse(T2I.StatsT2IRenderer._validate_image(""))
        self.assertFalse(T2I.StatsT2IRenderer._validate_image(None))
        self.assertFalse(T2I.StatsT2IRenderer._validate_image(12345))


class TestConfigResolve(unittest.TestCase):
    def test_summary_t2i_keys_read(self):
        """render_card 全链路读取 summary_t2i_* 五键。

        主题设 auto：light/dark 强制模式下 _resolve_theme 短路返回，不读
        dark_start/light_start（与 profile 行为一致），auto 才走完整五键。
        """
        renderer, _, cfg = make_renderer(
            script=[PNG_BYTES],
            cfg_overrides={"summary_t2i_theme_mode": "auto"},
        )
        path = _run(renderer.render_card(make_stats_data(), "t"))
        os.unlink(path)
        for key in (
            "summary_t2i_theme_mode",
            "summary_t2i_dark_start",
            "summary_t2i_light_start",
            "summary_t2i_cdn_providers",
            "summary_t2i_timeout",
        ):
            self.assertIn(key, cfg.calls)

    def test_theme_mode_passthrough_and_fallback(self):
        renderer, _, _ = make_renderer(cfg_overrides={"summary_t2i_theme_mode": "dark"})
        self.assertEqual(_run(renderer._resolve_theme()), "dark")
        renderer2, _, _ = make_renderer(
            cfg_overrides={"summary_t2i_theme_mode": "LIGHT"}
        )
        self.assertEqual(_run(renderer2._resolve_theme()), "light")
        renderer3, _, _ = make_renderer(
            cfg_overrides={"summary_t2i_theme_mode": "purple"}
        )
        self.assertIn(_run(renderer3._resolve_theme()), ("light", "dark"))  # 回退 auto

    def test_theme_auto_cross_midnight(self):
        """auto + light=22:00/dark=08:00（跨午夜浅色）：深夜应判浅色。"""
        renderer, _, _ = make_renderer(
            cfg_overrides={
                "summary_t2i_theme_mode": "auto",
                "summary_t2i_light_start": "22:00",
                "summary_t2i_dark_start": "08:00",
            }
        )
        # 纯函数层断言跨午夜语义（23:00 → light；12:00 → dark）
        self.assertEqual(T2I._theme_for(23 * 60, 22 * 60, 8 * 60), "light")
        self.assertEqual(T2I._theme_for(12 * 60, 22 * 60, 8 * 60), "dark")
        self.assertIn(_run(renderer._resolve_theme()), ("light", "dark"))

    def test_timeout_range_and_fallback(self):
        renderer, _, _ = make_renderer(cfg_overrides={"summary_t2i_timeout": 45})
        self.assertEqual(_run(renderer._resolve_timeout()), 45)
        renderer2, _, _ = make_renderer(cfg_overrides={"summary_t2i_timeout": 9999})
        self.assertEqual(_run(renderer2._resolve_timeout()), 30)  # 越界回退
        renderer3, _, _ = make_renderer(cfg_overrides={"summary_t2i_timeout": "abc"})
        self.assertEqual(_run(renderer3._resolve_timeout()), 30)  # 非法回退
        renderer4, _, _ = make_renderer(
            cfg_overrides={"summary_t2i_timeout": RuntimeError("读取失败")}
        )
        self.assertEqual(_run(renderer4._resolve_timeout()), 30)  # 异常回退

    def test_cdn_providers_filter_dedup_fallback(self):
        renderer, _, _ = make_renderer(
            cfg_overrides={
                "summary_t2i_cdn_providers": [
                    "JsDelivr",
                    "jsdelivr",
                    "未知节点",
                    "",
                    "unpkg",
                ]
            }
        )
        self.assertEqual(_run(renderer._resolve_cdn_providers()), ["jsdelivr", "unpkg"])
        renderer2, _, _ = make_renderer(cfg_overrides={"summary_t2i_cdn_providers": []})
        self.assertEqual(
            _run(renderer2._resolve_cdn_providers()), T2I._CDN_DEFAULT_PROVIDERS
        )
        renderer3, _, _ = make_renderer(
            cfg_overrides={"summary_t2i_cdn_providers": RuntimeError("boom")}
        )
        self.assertEqual(
            _run(renderer3._resolve_cdn_providers()), T2I._CDN_DEFAULT_PROVIDERS
        )


class TestTemplateCache(unittest.TestCase):
    def test_mtime_cache_hit_and_reload(self):
        renderer, _, _ = make_renderer()
        t1 = renderer._load_template()
        self.assertIsNotNone(t1)
        cache_before = renderer._tmpl_cache
        t2 = renderer._load_template()
        self.assertIs(t2, t1)  # 同一字符串对象 → 命中缓存未重读
        self.assertEqual(renderer._tmpl_cache, cache_before)

        # mtime 变化（内容不变，仅前推文件时间戳）→ 触发重读并刷新缓存
        mtime_now = os.path.getmtime(_TEMPLATE_PATH)
        try:
            os.utime(_TEMPLATE_PATH, (mtime_now + 5, mtime_now + 5))
            t3 = renderer._load_template()
            self.assertEqual(t3, t1)  # 内容一致
            self.assertEqual(renderer._tmpl_cache[0], mtime_now + 5)
        finally:
            os.utime(_TEMPLATE_PATH, (mtime_now, mtime_now))

    def test_template_missing_returns_none(self):
        """模板缺失 → error 日志 + None（不抛异常）。"""
        renderer, _, _ = make_renderer()
        original = T2I.os.path.join

        def fake_join(*parts):
            result = original(*parts)
            return result.replace("stats_report.html", "不存在.html")

        T2I.os.path.join = fake_join
        try:
            self.assertIsNone(renderer._load_template())
        finally:
            T2I.os.path.join = original
        self.assertIn("模板失败", "\n".join(_STUB_LOGGER.records["error"]))


# ============================================================
# 八、纯函数工具
# ============================================================


class TestPureHelpers(unittest.TestCase):
    def test_theme_for_windows(self):
        self.assertEqual(T2I._theme_for(12 * 60, 8 * 60, 22 * 60), "light")
        self.assertEqual(T2I._theme_for(23 * 60, 8 * 60, 22 * 60), "dark")
        self.assertEqual(T2I._theme_for(7 * 60, 8 * 60, 22 * 60), "dark")
        self.assertEqual(T2I._theme_for(12 * 60, 12 * 60, 12 * 60), "dark")  # 恒深

    def test_hhmm_to_minutes(self):
        self.assertEqual(T2I._hhmm_to_minutes("21:30"), 21 * 60 + 30)
        self.assertIsNone(T2I._hhmm_to_minutes("25:00"))
        self.assertIsNone(T2I._hhmm_to_minutes("abc"))
        self.assertIsNone(T2I._hhmm_to_minutes(None))

    def test_fmt_helpers(self):
        self.assertEqual(T2I._fmt_int(1234567), "1,234,567")
        self.assertEqual(T2I._fmt_int("890"), "890")
        self.assertEqual(T2I._fmt_int(-5), "0")
        self.assertEqual(T2I._fmt_int("x"), "0")
        self.assertEqual(T2I._fmt_ratio(0.1234), "12.3%")
        self.assertEqual(T2I._fmt_ratio(1), "100.0%")
        self.assertEqual(T2I._fmt_ratio(3.5), "100.0%")  # 上界收敛
        self.assertEqual(T2I._fmt_ratio(-1), "0.0%")  # 下界收敛
        self.assertEqual(T2I._fmt_ratio("nan"), "0.0%")
        self.assertEqual(T2I._fmt_avg(205.6666), "205.7")
        self.assertEqual(T2I._fmt_avg(-2), "0.0")
        self.assertEqual(T2I._fmt_avg(None), "0.0")

    def test_resolve_peak(self):
        dist = [0, 5, 3]
        self.assertEqual(T2I._resolve_peak(dist, 2), 2)  # 合法提供值优先
        self.assertEqual(T2I._resolve_peak(dist, None), 1)  # 回退最大值索引
        self.assertEqual(T2I._resolve_peak(dist, 9), 1)  # 越界回退
        self.assertIsNone(T2I._resolve_peak([0, 0], None))  # 全零 → None

    def test_hour_text(self):
        self.assertEqual(T2I._hour_text(21), "21 时")
        self.assertEqual(T2I._hour_text(0), "0 时")
        self.assertEqual(T2I._hour_text(None), "—")

    def test_make_vbars_peak_nullable(self):
        bars = T2I._make_vbars([0, 10, 5], ["a", "b", "c"], 1)
        self.assertEqual(bars[1]["percent"], 100.0)
        self.assertTrue(bars[1]["peak"])
        self.assertFalse(bars[2]["peak"])
        bars_none = T2I._make_vbars([0, 0], ["a", "b"], None)
        self.assertFalse(any(b["peak"] for b in bars_none))
        self.assertEqual(bars_none[0]["percent"], 0.0)


# ============================================================
# 九、模板内 JS 语法校验（node --check）
# ============================================================


class TestNodeCheck(unittest.TestCase):
    @_needs_node
    def test_rendered_js_syntax_member_present(self):
        """含 member 分支的渲染产物 JS 经 node --check。"""
        renderer, _, _ = make_renderer()
        ctx = renderer._build_context(
            make_stats_data(
                member_id="10001",
                with_member=True,
                with_group_ranking=True,
                group_id=None,
                malicious=True,
            ),
            "t",
        )
        html_text = render_html(ctx, autoescape=False)
        self._node_check(extract_script(html_text))

    @_needs_node
    def test_rendered_js_syntax_member_absent(self):
        """无 member 分支（{% else %} 空数组路径）同样过 node --check。"""
        renderer, _, _ = make_renderer()
        ctx = renderer._build_context(make_stats_data(zero_dist=True), "t")
        html_text = render_html(ctx, autoescape=False)
        self._node_check(extract_script(html_text))

    @staticmethod
    def _node_check(js_text):
        with tempfile.NamedTemporaryFile(
            "w", suffix=".js", encoding="utf-8", delete=False
        ) as f:
            f.write(js_text)
            js_path = f.name
        try:
            proc = subprocess.run(
                ["node", "--check", js_path],
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert proc.returncode == 0, (
                f"node --check 失败:\n{proc.stderr}\n{proc.stdout}"
            )
        finally:
            os.unlink(js_path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
