# ruff: noqa: I001
"""astrbot_plugin_group_history_save_mysql v0.3.2 离线单元测试。

覆盖 v0.3.2「自研 T2I 报告模板」渲染增强的全部可离线验证行为：

- summary/formatter.py：``_markdown_to_html`` 的 GFM 表格扩展（表格结构 /
  单元格行内转换 / 转义防注入 / 冒号对齐分隔行 / 非表格回归）；
  ``SummaryFormatter`` 新兜底链路（自研模板 → text_to_image → 纯文本）、
  config_mgr 注入开关、``_IMAGE_TMPL`` 常量删除断言；forward 模式零改动。
- summary/t2i_render.py：``T2IRenderer`` 主题判定（``_theme_for`` /
  ``_hhmm_to_minutes`` 纯函数 + 实例级配置解析回退）、CDN 节点序解析、
  渲染超时解析、魔数校验（``_validate_image``）、模板数据契约组装
  （``_build_template_data``）、两轮渲染与 ``render()`` 绝不抛异常契约。
- summary/summarizer.py：``_FORMAT_CONSTRAINT_IMAGE`` 放开表格、
  ``_FORMAT_CONSTRAINT_FORWARD`` 保持不变。
- db_config.py：SUMMARY_DEFAULTS / SUMMARY_TYPES 扩至 24 项（5 个新键与
  默认值 / 类型声明），真实 aiosqlite 冒烟（沿用 v0.3 真实 SQLite 范式）。

运行方式（插件根目录）：
    python -m pytest "tests/v0.3.2/test_v032.py" -v

范式沿用 tests/v0.3.1/test_v031.py（不 import 它，fake 自带）：异步用例
用内置 async_test 装饰器经 asyncio.run 驱动（不依赖 pytest-asyncio /
conftest.py）；astrbot.* 全部 sys.modules stub，且 stub 必须在 import 被测
包之前完成。
"""

import asyncio
import functools
import json
import os
import sys
import tempfile
import types
from datetime import datetime
from pathlib import Path

import pytest


def async_test(fn):
    """装饰器:用 asyncio.run 运行异步测试函数(环境无 pytest-asyncio 依赖)。"""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        asyncio.run(fn(*args, **kwargs))

    return wrapper


# ============================================================
# 一、stub 注入（必须在任何 from astrbot_plugin_group_history_save_mysql... import 之前）
# ============================================================


def _new_module(name: str) -> types.ModuleType:
    """创建一个空模块对象。"""
    return types.ModuleType(name)


# ---- astrbot 主包及其子包 ----
_astrbot = _new_module("astrbot")
_astrbot_api = _new_module("astrbot.api")
_astrbot_mc = _new_module("astrbot.api.message_components")
_astrbot_event = _new_module("astrbot.api.event")
_astrbot_star = _new_module("astrbot.api.star")


# ---- astrbot.api: logger（带记录能力，供 warning 断言）----
class _StubLogger:
    """记录各级日志内容，便于断言（如配置回退 warning / 魔数校验 warning）。"""

    def __init__(self):
        self.records = {"info": [], "warning": [], "error": [], "debug": []}

    def _fmt(self, msg, args):
        try:
            return msg % args if args else str(msg)
        except Exception:
            return str(msg)

    def info(self, msg, *args, **kwargs):
        self.records["info"].append(self._fmt(msg, args))

    def warning(self, msg, *args, **kwargs):
        self.records["warning"].append(self._fmt(msg, args))

    def error(self, msg, *args, **kwargs):
        self.records["error"].append(self._fmt(msg, args))

    def debug(self, msg, *args, **kwargs):
        self.records["debug"].append(self._fmt(msg, args))

    def joined(self, level: str) -> str:
        return "\n".join(self.records[level])


_STUB_LOGGER = _StubLogger()
_astrbot_api.logger = _STUB_LOGGER


# ---- astrbot.api.message_components: Plain / Image / Node / Nodes ----
class _StubPlain:
    def __init__(self, text=""):
        self.text = text


class _StubImage:
    def __init__(self, url=None, file=None):
        self.url = url
        self.file = file

    @classmethod
    def fromURL(cls, url):
        return cls(url=url)

    @classmethod
    def fromFileSystem(cls, path):
        return cls(file=path)


class _StubNode:
    def __init__(self, uin="", name="", content=None):
        self.uin = uin
        self.name = name
        self.content = content or []


class _StubNodes:
    def __init__(self, nodes=None):
        self.nodes = nodes or []


_astrbot_mc.Plain = _StubPlain
_astrbot_mc.Image = _StubImage
_astrbot_mc.Node = _StubNode
_astrbot_mc.Nodes = _StubNodes


# ---- astrbot.api.event: AstrMessageEvent / MessageChain ----
class _StubAstrMessageEvent:
    pass


class _StubMessageChain:
    """MessageChain 替身：仅承载 chain 列表。"""

    def __init__(self, chain=None):
        self.chain = chain or []


_astrbot_event.AstrMessageEvent = _StubAstrMessageEvent
_astrbot_event.MessageChain = _StubMessageChain


# ---- astrbot.api.star: Context / Star / StarTools ----
class _StubContext:
    pass


class _StubStar:
    pass


_TEMP_DATA_DIR = tempfile.mkdtemp(prefix="astrbot_hist_test_v032_")


class _StubStarTools:
    @classmethod
    def get_data_dir(cls, plugin_name=None):
        return Path(_TEMP_DATA_DIR) / (plugin_name or "test")


_astrbot_star.Context = _StubContext
_astrbot_star.Star = _StubStar
_astrbot_star.StarTools = _StubStarTools

# ---- 多测试文件合跑兼容：剔除可能被其他测试文件先行导入的插件包模块，
#      使本文件的 import 重新执行并绑定到本文件的 stub（各版本测试文件 fake 互异） ----
for _name in list(sys.modules):
    if _name == "astrbot_plugin_group_history_save_mysql" or _name.startswith(
        "astrbot_plugin_group_history_save_mysql."
    ):
        del sys.modules[_name]

# ---- 注入 sys.modules ----
sys.modules["astrbot"] = _astrbot
sys.modules["astrbot.api"] = _astrbot_api
sys.modules["astrbot.api.message_components"] = _astrbot_mc
sys.modules["astrbot.api.event"] = _astrbot_event
sys.modules["astrbot.api.star"] = _astrbot_star

# 让被测包可被导入：<plugins 目录> 加入 sys.path
_PLUGINS_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..")
)
if _PLUGINS_DIR not in sys.path:
    sys.path.insert(0, _PLUGINS_DIR)

# aiosqlite 可用性探测（db_config 真实 SQLite 测试的跳过开关）
try:
    import aiosqlite  # noqa: F401

    HAS_AIOSQLITE = True
except ImportError:
    HAS_AIOSQLITE = False

needs_aiosqlite = pytest.mark.skipif(
    not HAS_AIOSQLITE,
    reason="aiosqlite 未安装，跳过真实 SQLite 配置库测试",
)

# ============================================================
# 二、导入被测代码（stub 已就位）
# ============================================================

from astrbot_plugin_group_history_save_mysql.core.db_config import ConfigManager  # noqa: E402
from astrbot_plugin_group_history_save_mysql.core.summary import formatter as formatter_mod  # noqa: E402
from astrbot_plugin_group_history_save_mysql.core.summary import (  # noqa: E402
    t2i_render as t2i_render_mod,
)
from astrbot_plugin_group_history_save_mysql.core.summary.formatter import (  # noqa: E402
    SummaryFormatter,
    _markdown_to_html,
)
from astrbot_plugin_group_history_save_mysql.core.summary.models import (  # noqa: E402
    StatsResult,
    SummaryResult,
)
from astrbot_plugin_group_history_save_mysql.core.summary.summarizer import (  # noqa: E402
    _FORMAT_CONSTRAINT_FORWARD,
    _FORMAT_CONSTRAINT_IMAGE,
)
from astrbot_plugin_group_history_save_mysql.core.summary.t2i_render import (  # noqa: E402
    T2IRenderer,
    _hhmm_to_minutes,
    _theme_for,
)

Image = _StubImage
Plain = _StubPlain
Nodes = _StubNodes
MessageChain = _StubMessageChain

# ---- 魔数校验/两轮渲染用例的预制字节 ----
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00\x01\x02\x03" * 8
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00\x01\x02\x03" * 8
# T2I 服务错误页风格的非图片返回（含 <title>，供错误页日志断言）
HTML_PAGE_BYTES = (
    b"<html><head><title>502 Bad Gateway</title></head>"
    b"<body>render backend down</body></html>"
)
# 默认 CDN 节点序（与 t2i_render._CDN_DEFAULT_PROVIDERS 一致）
DEFAULT_CDN = ["bootcdn", "npmmirror", "staticfile", "jsdelivr", "unpkg"]


# ============================================================
# 三、测试辅助（假基础设施，自包含，不从其他版本测试文件 import）
# ============================================================


class FakeT2IConfig:
    """ConfigManager 内存替身（T2I 渲染配置读取路径）。

    以真实 SUMMARY_DEFAULTS（24 项，含 v0.3.2 新增 5 项）为底，typed 读取
    复用生产侧 ``ConfigManager._convert_summary_value`` 转换逻辑，保证假件
    行为与真实配置层一致；``typed_overrides`` 可直接指定 typed 返回值
    （值为 Exception 实例时改为抛出，模拟单键读取失败）；``exc`` 使全部
    读取抛异常（模拟配置层整体故障）。签名为 db_config 现行的单参
    ``(key)`` 形态，``T2IRenderer._read_setting`` 的签名探测应命中单参分支。
    """

    def __init__(self, overrides=None, typed_overrides=None, exc=None):
        self.store = dict(ConfigManager.SUMMARY_DEFAULTS)
        if overrides:
            self.store.update(overrides)
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
        raw = self.store.get(key, ConfigManager.SUMMARY_DEFAULTS.get(key, ""))
        target = ConfigManager.SUMMARY_TYPES.get(key, str)
        try:
            return ConfigManager._convert_summary_value(raw, target)
        except Exception:
            default_raw = ConfigManager.SUMMARY_DEFAULTS.get(key, "")
            return ConfigManager._convert_summary_value(default_raw, target)


class FakeRenderStar:
    """Star 替身：html_render 按剧本逐轮返回值（Exception 实例则抛出）并
    记录每次调用的 tmpl/data/return_url/options；text_to_image 可配成功
    返回值或异常（formatter 新链路降级用例用）。
    """

    def __init__(
        self,
        html_script=None,
        t2i_result="https://render.example/fallback.png",
        t2i_exc=None,
    ):
        self.html_script = list(html_script or [])
        self.t2i_result = t2i_result
        self.t2i_exc = t2i_exc
        self.html_calls = []
        self.t2i_calls = []

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

    async def text_to_image(self, text, return_url=True):
        self.t2i_calls.append(text)
        if self.t2i_exc is not None:
            raise self.t2i_exc
        return self.t2i_result


def make_summary_result(**overrides):
    """构造一个完整的 SummaryResult（渲染器/格式化器用例复用）。"""
    stats = StatsResult(
        total=2,
        participant_count=2,
        time_start=datetime(2026, 7, 1, 10, 0, 0),
        time_end=datetime(2026, 7, 1, 11, 0, 0),
        top_senders=[("111", "Alice", 1), ("222", "Bob", 1)],
    )
    kwargs = {
        "stats": stats,
        "sections": [
            ("📢 重要通知与结论", "通知内容"),
            ("✅ TODO / 待跟进", "任务内容"),
        ],
        "raw_llm_text": "raw llm text",
        "provider_id": "prov-x",
        "messages_used": 2,
        "sources": {"mysql": 2, "onebot": 0},
        "scope_desc": "最近 2 条消息",
    }
    kwargs.update(overrides)
    return SummaryResult(**kwargs)


def _renderer(typed_overrides=None, html_script=None, config_exc=None):
    """组装 T2IRenderer（假 star + 假 config），供配置解析类用例复用。"""
    return T2IRenderer(
        FakeRenderStar(html_script=html_script),
        FakeT2IConfig(typed_overrides=typed_overrides, exc=config_exc),
    )


def _patch_now(monkeypatch, hour, minute=0):
    """把 t2i_render 模块内的 ``datetime.now()`` 固定为指定本地时刻。

    以真实 datetime 子类替换（strptime 继承真实实现，``_hhmm_to_minutes``
    的解析行为不受影响），仅 now() 返回固定值。
    """
    fixed = datetime(2026, 7, 31, hour, minute, 0)

    class _FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed

    monkeypatch.setattr(t2i_render_mod, "datetime", _FixedDateTime)


def _new_warnings_since(index: int) -> str:
    """取 index 之后新增的 warning 日志拼接串（避免跨用例串扰）。"""
    return "\n".join(_STUB_LOGGER.records["warning"][index:])


# ============================================================
# 四、TestMarkdownTable：_markdown_to_html 的 GFM 表格扩展
# ============================================================


class TestMarkdownTable:
    def test_standard_table_structure(self):
        """标准表格（表头+分隔行+2 行表体）→ table/thead/tbody/th/td 齐全，列数正确。"""
        md = "| 名称 | 数量 |\n| --- | --- |\n| 苹果 | 3 |\n| 香蕉 | 5 |"
        out = _markdown_to_html(md)
        assert "<table>" in out and "</table>" in out
        assert "<thead>" in out and "<tbody>" in out
        assert out.count("<th>") == 2
        assert out.count("<td>") == 4
        assert "<th>名称</th><th>数量</th>" in out
        assert "<td>苹果</td><td>3</td>" in out
        assert "<td>香蕉</td><td>5</td>" in out

    def test_cell_inline_bold_and_code(self):
        """单元格内 **粗体** / `行内代码` / *斜体* 走行内转换。"""
        md = "| **粗体** | `代码` |\n| --- | --- |\n| *斜体* | 普通 |"
        out = _markdown_to_html(md)
        assert "<th><strong>粗体</strong></th>" in out
        assert "<th><code>代码</code></th>" in out
        assert "<td><em>斜体</em></td>" in out
        assert "<td>普通</td>" in out

    def test_script_cell_escaped(self):
        """含 <script> 的单元格被 html 转义（防注入），输出不出现真实标签。"""
        md = "| 注入 |\n| --- |\n| <script>alert(1)</script> |"
        out = _markdown_to_html(md)
        assert "<script>" not in out
        assert "&lt;script&gt;" in out

    def test_colon_alignment_separator(self):
        """冒号对齐分隔行（|:---|---:|）正常解析为表格（对齐不做渲染）。"""
        md = "| 左对齐 | 右对齐 |\n|:---|---:|\n| a | b |"
        out = _markdown_to_html(md)
        assert "<table>" in out
        assert "<th>左对齐</th><th>右对齐</th>" in out
        assert "<td>a</td><td>b</td>" in out

    def test_single_pipe_row_not_table(self):
        """无分隔行（仅一行 | a | b |）→ 不当作表格，按普通段落处理。"""
        out = _markdown_to_html("| a | b |")
        assert "<table>" not in out
        assert "<p>| a | b |</p>" in out

    def test_pipe_rows_without_separator_not_table(self):
        """连续管道行但第二行非分隔符 → 仍不作表格（逐行段落回归）。"""
        out = _markdown_to_html("| a | b |\n| c | d |")
        assert "<table>" not in out
        assert "<p>| a | b |</p>" in out
        assert "<p>| c | d |</p>" in out

    def test_non_table_regression(self):
        """非表格输入行为与 v0.3 一致：标题/列表/引用/代码块/行内强调。"""
        assert "<h1>标题</h1>" in _markdown_to_html("# 标题")
        assert "<h3>三级</h3>" in _markdown_to_html("### 三级")

        ul = _markdown_to_html("- 项目一\n- 项目二")
        assert "<ul>" in ul and ul.count("<li>") == 2
        assert "<li>项目一</li>" in ul

        ol = _markdown_to_html("1. 第一\n2. 第二")
        assert "<ol>" in ol and ol.count("<li>") == 2
        assert "<li>第二</li>" in ol

        assert "<blockquote>引用文字</blockquote>" in _markdown_to_html("> 引用文字")

        pre = _markdown_to_html("```\n<b>html</b>\n```")
        assert "<pre>" in pre and "</pre>" in pre
        assert "<b>" not in pre  # 代码块内容同样转义
        assert "&lt;b&gt;html&lt;/b&gt;" in pre

        p = _markdown_to_html("行内 `代码` 与 **粗体** 及 *斜体*")
        assert "<code>代码</code>" in p
        assert "<strong>粗体</strong>" in p
        assert "<em>斜体</em>" in p


# ============================================================
# 五、TestThemeResolve：主题判定（纯函数 + 实例级配置解析）
# ============================================================


class TestThemeResolve:
    # ---- 纯函数直测 ----

    def test_hhmm_to_minutes_valid(self):
        """合法 HH:MM → 自 0 点起的分钟数（含 strip 与 strptime 宽松补零）。"""
        assert _hhmm_to_minutes("08:00") == 480
        assert _hhmm_to_minutes("22:00") == 1320
        assert _hhmm_to_minutes("00:00") == 0
        assert _hhmm_to_minutes("23:59") == 1439
        assert _hhmm_to_minutes(" 08:00 ") == 480
        # 实际实现行为：strptime 不强制零填充，"8:0" 按合法 08:00 接受
        assert _hhmm_to_minutes("8:0") == 480

    def test_hhmm_to_minutes_invalid(self):
        """非法值（非数字 / 越界 / 空 / None）→ None。"""
        assert _hhmm_to_minutes("abc") is None
        assert _hhmm_to_minutes("25:00") is None
        assert _hhmm_to_minutes("12:60") is None
        assert _hhmm_to_minutes("") is None
        assert _hhmm_to_minutes(None) is None

    def test_theme_for_default_window(self):
        """默认窗口 08:00–22:00：区间内浅色，左闭右开边界。"""
        light, dark = 8 * 60, 22 * 60
        assert _theme_for(12 * 60, light, dark) == "light"  # 12:00
        assert _theme_for(23 * 60, light, dark) == "dark"  # 23:00
        assert _theme_for(7 * 60, light, dark) == "dark"  # 07:00
        assert _theme_for(8 * 60, light, dark) == "light"  # 08:00 左闭
        assert _theme_for(22 * 60, light, dark) == "dark"  # 22:00 右开

    def test_theme_for_cross_midnight(self):
        """跨午夜配置（light=22:00 dark=08:00）：区间环绕午夜自洽。"""
        light, dark = 22 * 60, 8 * 60
        assert _theme_for(23 * 60, light, dark) == "light"  # 23:00
        assert _theme_for(9 * 60, light, dark) == "dark"  # 09:00
        assert _theme_for(7 * 60, light, dark) == "light"  # 07:00 属跨午夜浅色段
        assert _theme_for(12 * 60, light, dark) == "dark"  # 12:00

    def test_theme_for_equal_starts_always_dark(self):
        """light == dark → 区间为空，恒定深色。"""
        assert _theme_for(0, 480, 480) == "dark"
        assert _theme_for(480, 480, 480) == "dark"
        assert _theme_for(1439, 480, 480) == "dark"

    # ---- 实例级（配置解析 + 本地时间打桩）----

    async def _theme_at(self, monkeypatch, hour, typed_overrides=None):
        _patch_now(monkeypatch, hour)
        renderer = _renderer(typed_overrides=typed_overrides)
        return await renderer._resolve_theme()

    @async_test
    async def test_mode_light_dark_passthrough(self, monkeypatch):
        """mode=light / dark 强制模式直通返回，不读时段配置。"""
        _patch_now(monkeypatch, 23)  # 即使本地时间为深夜
        renderer = _renderer(typed_overrides={"summary_t2i_theme_mode": "light"})
        assert await renderer._resolve_theme() == "light"
        assert renderer.config_mgr.calls == ["summary_t2i_theme_mode"]  # 短路

        renderer = _renderer(typed_overrides={"summary_t2i_theme_mode": "dark"})
        assert await renderer._resolve_theme() == "dark"

    @async_test
    async def test_mode_auto_default_window_by_local_time(self, monkeypatch):
        """auto 默认窗口：按打桩的服务器本地时间判定。"""
        assert await self._theme_at(monkeypatch, 12) == "light"
        assert await self._theme_at(monkeypatch, 23) == "dark"
        assert await self._theme_at(monkeypatch, 7) == "dark"
        assert await self._theme_at(monkeypatch, 8) == "light"
        assert await self._theme_at(monkeypatch, 22) == "dark"

    @async_test
    async def test_mode_auto_cross_midnight_config(self, monkeypatch):
        """auto 跨午夜配置自洽：light=22:00 dark=08:00。"""
        overrides = {
            "summary_t2i_dark_start": "08:00",
            "summary_t2i_light_start": "22:00",
        }
        assert await self._theme_at(monkeypatch, 23, overrides) == "light"
        assert await self._theme_at(monkeypatch, 9, overrides) == "dark"

    @async_test
    async def test_invalid_hhmm_falls_back_defaults(self, monkeypatch):
        """非法 HH:MM（"25:00" / "abc"）→ 逐项回退默认 22:00/08:00 并记 warning。"""
        _patch_now(monkeypatch, 12)  # 默认浅色窗口内
        before = len(_STUB_LOGGER.records["warning"])
        renderer = _renderer(
            typed_overrides={
                "summary_t2i_dark_start": "25:00",
                "summary_t2i_light_start": "abc",
            }
        )
        assert await renderer._resolve_theme() == "light"  # 回退默认值后 12:00 为浅色
        new_warn = _new_warnings_since(before)
        assert "深色时段起点" in new_warn and "非法" in new_warn
        assert "浅色时段起点" in new_warn and "非法" in new_warn

    @async_test
    async def test_invalid_mode_falls_back_auto(self, monkeypatch):
        """非法 mode（"xxx"）→ 回退 auto 并按本地时间判定。"""
        _patch_now(monkeypatch, 23)
        before = len(_STUB_LOGGER.records["warning"])
        renderer = _renderer(typed_overrides={"summary_t2i_theme_mode": "xxx"})
        assert await renderer._resolve_theme() == "dark"  # 23:00 按 auto 判定为深色
        new_warn = _new_warnings_since(before)
        assert "主题模式配置" in new_warn and "回退 auto" in new_warn


# ============================================================
# 六、TestCdnProviders：_resolve_cdn_providers
# ============================================================


class TestCdnProviders:
    @async_test
    async def test_default_order(self):
        """默认配置 → 国内优先 5 节点有序。"""
        assert await _renderer()._resolve_cdn_providers() == DEFAULT_CDN

    @async_test
    async def test_unknown_filtered_order_kept(self):
        """未知 key 被过滤，合法 key 保序；strip/lower 归一、空项忽略。"""
        renderer = _renderer(
            typed_overrides={
                "summary_t2i_cdn_providers": ["jsdelivr", "fakecdn", " BootCDN ", ""]
            }
        )
        assert await renderer._resolve_cdn_providers() == ["jsdelivr", "bootcdn"]

    @async_test
    async def test_empty_or_non_list_falls_back(self):
        """空列表 / 非 list → 回退默认序。"""
        renderer = _renderer(typed_overrides={"summary_t2i_cdn_providers": []})
        assert await renderer._resolve_cdn_providers() == DEFAULT_CDN

        renderer = _renderer(
            typed_overrides={"summary_t2i_cdn_providers": "not-a-list"}
        )
        assert await renderer._resolve_cdn_providers() == DEFAULT_CDN

    @async_test
    async def test_read_exception_falls_back(self):
        """读取异常 → 回退默认序并记 warning。"""
        before = len(_STUB_LOGGER.records["warning"])
        renderer = _renderer(
            typed_overrides={
                "summary_t2i_cdn_providers": RuntimeError("config db down")
            }
        )
        assert await renderer._resolve_cdn_providers() == DEFAULT_CDN
        assert "读取 CDN 节点配置失败" in _new_warnings_since(before)

    @async_test
    async def test_dedup(self):
        """同节点重复 → 保序去重。"""
        renderer = _renderer(
            typed_overrides={"summary_t2i_cdn_providers": ["unpkg", "bootcdn", "unpkg"]}
        )
        assert await renderer._resolve_cdn_providers() == ["unpkg", "bootcdn"]

    @async_test
    async def test_all_unknown_falls_back(self):
        """全部为未知 key（过滤后为空）→ 回退默认序。"""
        renderer = _renderer(typed_overrides={"summary_t2i_cdn_providers": ["x", "y"]})
        assert await renderer._resolve_cdn_providers() == DEFAULT_CDN


# ============================================================
# 七、TestTimeout：_resolve_timeout
# ============================================================


class TestTimeout:
    @async_test
    async def test_valid_values_passthrough(self):
        """合法边界与中间值（5 / 30 / 300）原样返回。"""
        for value in (5, 30, 300):
            renderer = _renderer(typed_overrides={"summary_t2i_timeout": value})
            assert await renderer._resolve_timeout() == value

    @async_test
    async def test_out_of_range_falls_back_30(self):
        """越界（4 / 301 / 999）→ 回退 30 并记 warning。"""
        before = len(_STUB_LOGGER.records["warning"])
        for value in (4, 301, 999):
            renderer = _renderer(typed_overrides={"summary_t2i_timeout": value})
            assert await renderer._resolve_timeout() == 30
        assert "越界" in _new_warnings_since(before)

    @async_test
    async def test_invalid_or_exception_falls_back_30(self):
        """非法值（"abc"）与读取异常 → 回退 30。"""
        before = len(_STUB_LOGGER.records["warning"])
        renderer = _renderer(typed_overrides={"summary_t2i_timeout": "abc"})
        assert await renderer._resolve_timeout() == 30

        renderer = _renderer(
            typed_overrides={"summary_t2i_timeout": RuntimeError("db down")}
        )
        assert await renderer._resolve_timeout() == 30
        assert "读取 T2I 渲染超时配置失败" in _new_warnings_since(before)


# ============================================================
# 八、TestValidateImage：_validate_image 魔数校验（静态方法）
# ============================================================


class TestValidateImage:
    def test_png_bytes_valid(self):
        """PNG 头 bytes / bytearray → True。"""
        assert T2IRenderer._validate_image(PNG_BYTES) is True
        assert T2IRenderer._validate_image(bytearray(PNG_BYTES)) is True

    def test_jpeg_bytes_valid(self):
        """JPEG 头 bytes → True。"""
        assert T2IRenderer._validate_image(JPEG_BYTES) is True

    def test_html_bytes_invalid(self):
        """T2I 服务返回的错误 HTML 页字节 → False。"""
        assert T2IRenderer._validate_image(HTML_PAGE_BYTES) is False

    def test_http_urls_valid(self):
        """http(s) URL 字符串 → True（兼容直接返 URL 的部署形态）。"""
        assert T2IRenderer._validate_image("https://render.example/a.png") is True
        assert T2IRenderer._validate_image("http://render.example/b.jpg") is True

    def test_file_paths(self, tmp_path):
        """已存在文件按内容验头：PNG 文件 True / HTML 文件 False / 不存在 False。"""
        png = tmp_path / "ok.png"
        png.write_bytes(PNG_BYTES)
        assert T2IRenderer._validate_image(str(png)) is True

        html_file = tmp_path / "err.html"
        html_file.write_bytes(HTML_PAGE_BYTES)
        assert T2IRenderer._validate_image(str(html_file)) is False

        assert T2IRenderer._validate_image(str(tmp_path / "nope.png")) is False

    def test_empty_and_unrecognized_invalid(self):
        """None / 空 bytes / 空串 / 纯空白 / 未知类型 → False。"""
        assert T2IRenderer._validate_image(None) is False
        assert T2IRenderer._validate_image(b"") is False
        assert T2IRenderer._validate_image("") is False
        assert T2IRenderer._validate_image("   ") is False
        assert T2IRenderer._validate_image(12345) is False


# ============================================================
# 九、TestBuildTemplateData：模板 data 契约组装
# ============================================================


class TestBuildTemplateData:
    def test_contract_keys_complete(self):
        """契约 9 键齐全；统计/柱图/板块/CDN 各字段按约定组装。"""
        renderer = _renderer()
        stats = StatsResult(
            total=10,
            participant_count=3,
            time_start=datetime(2026, 7, 1, 10, 0, 0),
            time_end=datetime(2026, 7, 1, 11, 0, 0),
            top_senders=[("111", "Alice", 3), ("222", "Bob", 1)],
        )
        result = make_summary_result(
            stats=stats,
            sections=[
                ("📢 重要通知与结论", "**加粗通知**"),
                ("✅ TODO / 待跟进", "| a | b |\n| --- | --- |\n| 1 | 2 |"),
            ],
        )
        data = renderer._build_template_data(result, "dark", ["bootcdn", "unpkg"])

        assert set(data.keys()) == {
            "theme",
            "title",
            "generated_at",
            "meta",
            "stats",
            "bars",
            "bars_max",
            "sections",
            "cdn",
        }
        assert data["theme"] == "dark"
        assert data["title"] == "群聊总结 · 最近 2 条消息"
        assert data["generated_at"]  # 非空 YYYY-MM-DD HH:MM
        assert set(data["meta"].keys()) == {
            "scope",
            "provider",
            "time_start",
            "time_end",
        }
        assert data["meta"]["provider"] == "prov-x"
        assert data["meta"]["time_start"] == "2026-07-01 10:00"

        assert data["stats"]["total"] == 10
        assert data["stats"]["participants"] == 3
        assert data["stats"]["sources"] == [
            {"name": "MySQL", "count": 2},
            {"name": "OneBot", "count": 0},
        ]
        assert data["stats"]["truncated"] is False

        assert data["bars_max"] == 3
        assert data["bars"][0] == {"name": "Alice", "count": 3, "percent": 100.0}
        assert data["bars"][1]["name"] == "Bob"
        assert data["bars"][1]["percent"] == 33.3

        assert len(data["sections"]) == 2
        for sec in data["sections"]:
            assert set(sec.keys()) == {"title", "raw", "fallback_html"}
        assert data["sections"][0]["raw"] == "**加粗通知**"
        assert "<strong>加粗通知</strong>" in data["sections"][0]["fallback_html"]
        # 兜底 HTML 含 GFM 表格转换结果
        assert "<table>" in data["sections"][1]["fallback_html"]

        assert data["cdn"]["providers"] == ["bootcdn", "unpkg"]
        assert set(data["cdn"]["libs"].keys()) == {"marked", "echarts"}

    def test_stats_none_safe(self):
        """stats=None / sources=None 不抛异常，各字段以 0/空兜底。"""
        renderer = _renderer()
        result = make_summary_result(stats=None, sources=None)
        data = renderer._build_template_data(result, "light", ["bootcdn"])
        assert data["stats"]["total"] == 0
        assert data["stats"]["participants"] == 0
        assert data["stats"]["sources"] == []
        assert data["stats"]["truncated"] is False
        assert data["bars"] == []
        assert data["bars_max"] == 0
        assert data["meta"]["time_start"] == "未知"
        assert data["title"] == "群聊总结 · 最近 2 条消息"

    def test_dirty_top_senders_skipped(self):
        """top_senders 脏条目（缺字段 / None / 条数非数字）防御式跳过。"""
        renderer = _renderer()
        stats = StatsResult(
            total=5,
            participant_count=3,
            time_start=None,
            time_end=None,
            top_senders=[
                ("111", "Alice", 3),
                ("bad-entry",),  # 长度不足 → 跳过
                None,  # 非可解包项 → 跳过
                ("222", "", 2),  # 昵称为空 → 回落 sender_id
                ("333", "Carol", "x"),  # 条数非数字 → 跳过
            ],
        )
        data = renderer._build_template_data(
            make_summary_result(stats=stats), "light", ["bootcdn"]
        )
        assert [bar["name"] for bar in data["bars"]] == ["Alice", "222"]
        assert data["bars_max"] == 3
        assert data["bars"][1]["percent"] == 66.7


# ============================================================
# 十、TestRenderChain：T2IRenderer.render 全流程
# ============================================================


class TestRenderChain:
    @async_test
    async def test_r1_exception_r2_png_success_with_doubled_timeout(self):
        """R1 抛异常、R2 返回合法 PNG → 图片链；R2 超时为 R1 两倍，jpeg q80。"""
        star = FakeRenderStar(html_script=[RuntimeError("r1 boom"), PNG_BYTES])
        renderer = T2IRenderer(star, FakeT2IConfig())
        chain = await renderer.render(make_summary_result())

        assert isinstance(chain, MessageChain)
        img = chain.chain[0]
        assert isinstance(img, Image)
        assert img.file and img.file.endswith(".png")
        assert os.path.exists(img.file)  # bytes 已落临时文件

        assert len(star.html_calls) == 2
        c1, c2 = star.html_calls
        assert c1["return_url"] is False and c2["return_url"] is False
        assert isinstance(c1["tmpl"], str) and c1["tmpl"]  # 真实模板内容
        # data 契约键齐全
        assert set(c1["data"].keys()) == {
            "theme",
            "title",
            "generated_at",
            "meta",
            "stats",
            "bars",
            "bars_max",
            "sections",
            "cdn",
        }
        o1, o2 = c1["options"], c2["options"]
        assert o1["type"] == "png" and o1["full_page"] is True
        assert o1["timeout"] == 30000  # 默认 T=30s × 1000ms
        # 分辨率提升：两轮均要求 1.8 倍设备像素比（T2I 服务端视口固定 1280px）
        assert o1["device_scale_factor_level"] == "ultra"
        assert o2["type"] == "jpeg" and o2["quality"] == 80
        assert o2["full_page"] is True
        assert o2["device_scale_factor_level"] == "ultra"
        assert o2["timeout"] == o1["timeout"] * 2  # R2 双倍超时

    @async_test
    async def test_r1_html_page_r2_jpeg_success(self):
        """R1 返回 HTML 错误页字节、R2 返回合法 JPEG → 成功（.jpg 落盘）。"""
        star = FakeRenderStar(html_script=[HTML_PAGE_BYTES, JPEG_BYTES])
        renderer = T2IRenderer(star, FakeT2IConfig())
        chain = await renderer.render(make_summary_result())
        assert chain is not None
        img = chain.chain[0]
        assert isinstance(img, Image)
        assert img.file.endswith(".jpg")  # JPEG 产物按魔数取 .jpg 后缀
        assert len(star.html_calls) == 2

    @async_test
    async def test_both_rounds_bad_content_returns_none(self):
        """两轮都返回错误 HTML 页 → None（不抛异常），日志记错误页标题。"""
        before = len(_STUB_LOGGER.records["warning"])
        star = FakeRenderStar(html_script=[HTML_PAGE_BYTES, HTML_PAGE_BYTES])
        renderer = T2IRenderer(star, FakeT2IConfig())
        assert await renderer.render(make_summary_result()) is None
        assert len(star.html_calls) == 2
        new_warn = _new_warnings_since(before)
        assert "502 Bad Gateway" in new_warn  # 错误页 <title> 被记出
        assert "两轮渲染均失败" in new_warn

    @async_test
    async def test_html_render_all_exceptions_returns_none(self):
        """html_render 全抛异常 → 两轮均尝试后返回 None（绝不向上抛）。"""
        star = FakeRenderStar(
            html_script=[RuntimeError("r1 boom"), RuntimeError("r2 boom")]
        )
        renderer = T2IRenderer(star, FakeT2IConfig())
        assert await renderer.render(make_summary_result()) is None
        assert len(star.html_calls) == 2

    @async_test
    async def test_template_missing_returns_none(self):
        """模板文件缺失（_load_template 返回 None）→ render 返回 None，不触达渲染。"""
        star = FakeRenderStar(html_script=[PNG_BYTES])
        renderer = T2IRenderer(star, FakeT2IConfig())
        renderer._load_template = lambda: None  # 模拟模板缺失/读取失败
        assert await renderer.render(make_summary_result()) is None
        assert star.html_calls == []

    @async_test
    async def test_custom_timeout_propagates_to_options(self):
        """自定义超时（7s）→ R1 options.timeout=7000；R1 成功即止（仅一轮）。"""
        star = FakeRenderStar(html_script=[PNG_BYTES])
        renderer = T2IRenderer(
            star, FakeT2IConfig(typed_overrides={"summary_t2i_timeout": 7})
        )
        chain = await renderer.render(make_summary_result())
        assert chain is not None
        assert len(star.html_calls) == 1
        assert star.html_calls[0]["options"]["timeout"] == 7000


# ============================================================
# 十一、TestFormatterImageChain：SummaryFormatter 新链路
# ============================================================


class TestFormatterImageChain:
    def test_t2i_created_only_with_config_mgr(self):
        """注入 config_mgr → t2i 为 T2IRenderer；不注入 → None（签名向后兼容）。"""
        star = FakeRenderStar()
        assert SummaryFormatter(star).t2i is None
        formatter = SummaryFormatter(star, FakeT2IConfig())
        assert isinstance(formatter.t2i, T2IRenderer)

    @async_test
    async def test_t2i_fail_degrades_to_text_to_image_local_path(self):
        """自研模板两轮全失败 → 降级 text_to_image；本地路径 → fromFileSystem。"""
        star = FakeRenderStar(
            html_script=[RuntimeError("r1"), RuntimeError("r2")],
            t2i_result="/local/fallback.png",
        )
        formatter = SummaryFormatter(star, FakeT2IConfig())
        chain = await formatter.render(make_summary_result(), "image")
        img = chain.chain[0]
        assert isinstance(img, Image)
        assert img.file == "/local/fallback.png"
        assert img.url is None
        assert len(star.html_calls) == 2  # 自研模板两轮均尝试
        assert len(star.t2i_calls) == 1  # 最终降级到 text_to_image
        assert "# 群聊总结" in star.t2i_calls[0]  # 完整 Markdown 送入渲染

    @async_test
    async def test_no_config_mgr_direct_text_to_image(self):
        """未注入 config_mgr（旧调用点）→ 跳过自研模板级，直走 text_to_image。"""
        star = FakeRenderStar(t2i_result="https://render.example/out.png")
        formatter = SummaryFormatter(star)
        chain = await formatter.render(make_summary_result(), "image")
        img = chain.chain[0]
        assert isinstance(img, Image)
        assert img.url == "https://render.example/out.png"
        assert star.html_calls == []

    @async_test
    async def test_all_render_fail_plain_fallback(self):
        """自研模板与 text_to_image 全失败 → 纯文本兜底链（render 绝不抛）。"""
        star = FakeRenderStar(
            html_script=[RuntimeError("r1"), RuntimeError("r2")],
            t2i_exc=RuntimeError("t2i down"),
        )
        formatter = SummaryFormatter(star, FakeT2IConfig())
        chain = await formatter.render(make_summary_result(), "image")
        plain = chain.chain[0]
        assert isinstance(plain, Plain)
        assert plain.text.strip()  # 文本非空
        assert "群聊总结" in plain.text
        assert "**" not in plain.text  # Markdown 已剥

    @async_test
    async def test_forward_mode_unchanged(self):
        """forward 模式零改动：单 Nodes 包裹，1 统计 + N 板块节点。"""
        formatter = SummaryFormatter(FakeRenderStar(), FakeT2IConfig())
        chain = await formatter.render(make_summary_result(), "forward")
        assert isinstance(chain, MessageChain)
        nodes = chain.chain[0]
        assert isinstance(nodes, Nodes)
        assert len(nodes.nodes) == 3  # 1 统计 + 2 板块
        stats_text = nodes.nodes[0].content[0].text
        assert "消息总数：2" in stats_text
        assert "数据源构成：MySQL 2 + OneBot 0" in stats_text
        assert "通知内容" in nodes.nodes[1].content[0].text

    def test_image_tmpl_constant_removed(self):
        """v0.3.2 已删除最小化 _IMAGE_TMPL 常量（被自研模板取代）。"""
        assert getattr(formatter_mod, "_IMAGE_TMPL", None) is None


# ============================================================
# 十二、TestFormatConstraint：summarizer 表格放开约束
# ============================================================


class TestFormatConstraint:
    def test_image_constraint_allows_table(self):
        """image 约束放开表格（含「表格」字样），且保留 v0.3 的 Markdown 许可。"""
        assert "表格" in _FORMAT_CONSTRAINT_IMAGE
        assert "Markdown 表格" in _FORMAT_CONSTRAINT_IMAGE
        assert "可以使用 Markdown 格式" in _FORMAT_CONSTRAINT_IMAGE

    def test_forward_constraint_no_table(self):
        """forward 约束保持不变：不含「表格」，仍禁用全部 Markdown。"""
        assert "表格" not in _FORMAT_CONSTRAINT_FORWARD
        assert "不要使用任何 Markdown 格式" in _FORMAT_CONSTRAINT_FORWARD


# ============================================================
# 十三、TestDbConfigV032：配置项 19 → 24
# ============================================================


class TestDbConfigV032:
    def test_keys_consistent_and_24(self):
        """SUMMARY_DEFAULTS 与 SUMMARY_TYPES 键集合一致且各 24 项。"""
        assert set(ConfigManager.SUMMARY_DEFAULTS) == set(ConfigManager.SUMMARY_TYPES)
        assert len(ConfigManager.SUMMARY_DEFAULTS) == 24
        assert len(ConfigManager.SUMMARY_TYPES) == 24

    def test_new_keys_defaults_and_types(self):
        """5 个新键默认值与类型声明正确。"""
        defaults = ConfigManager.SUMMARY_DEFAULTS
        types_ = ConfigManager.SUMMARY_TYPES
        assert defaults["summary_t2i_theme_mode"] == "auto"
        assert defaults["summary_t2i_dark_start"] == "22:00"
        assert defaults["summary_t2i_light_start"] == "08:00"
        assert defaults["summary_t2i_timeout"] == "30"
        assert json.loads(defaults["summary_t2i_cdn_providers"]) == DEFAULT_CDN

        assert types_["summary_t2i_theme_mode"] is str
        assert types_["summary_t2i_dark_start"] is str
        assert types_["summary_t2i_light_start"] is str
        assert types_["summary_t2i_timeout"] is int
        assert types_["summary_t2i_cdn_providers"] is list

    @needs_aiosqlite
    @async_test
    async def test_real_sqlite_new_keys_typed(self, tmp_path):
        """真实 aiosqlite 冒烟：initialize 后 5 个新键 typed 默认值均可读。"""
        mgr = ConfigManager()
        mgr.db_path = str(tmp_path / "config.db")  # 重定向到临时文件
        assert await mgr.initialize() is True
        try:
            assert (
                await mgr.get_summary_setting_typed("summary_t2i_theme_mode") == "auto"
            )
            assert (
                await mgr.get_summary_setting_typed("summary_t2i_dark_start") == "22:00"
            )
            assert (
                await mgr.get_summary_setting_typed("summary_t2i_light_start")
                == "08:00"
            )
            assert await mgr.get_summary_setting_typed("summary_t2i_timeout") == 30
            assert (
                await mgr.get_summary_setting_typed("summary_t2i_cdn_providers")
                == DEFAULT_CDN
            )
        finally:
            await mgr.close()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
