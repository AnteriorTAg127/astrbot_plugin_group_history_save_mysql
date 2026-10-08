# ruff: noqa: I001
"""astrbot_plugin_group_history_save_mysql v0.4.0 Module H 离线单元测试。

覆盖「人物分析」输出格式化层（profile/formatter.py，ProfileFormatter）：

- forward 模式：单个 Comp.Nodes 聚合（节点数 = 报告头 + sections + 页脚）；
  报告头含目标昵称/QQ/范围/时间跨度/数据源构成/关键统计（总数/活跃天数/
  peak_hour/peak_weekday）；relation_context_complete=False 时头部附不完整
  提示；板块节点文本剥 Markdown；页脚含 provider 标识 + 免责声明；节点
  署名（bot uin 数字采用 / 非数字兜底）与零宽空格首尾填充。
- image 三级降级：① renderer 成功直接返回（不调 text_to_image）；② renderer
  None/异常 → text_to_image（URL / 本地路径两路）；③ 两者皆失败 → 纯文本兜底；
  text_to_image 返空同样落三级。每级失败记 [Profile] warning。
- text 模式：单 Plain 消息链，粗体/标题/列表符号剥除、免责声明齐全。
- 非法 output_mode（含空/None/大小写空白）回退 forward；" IMAGE " 等归一化生效。
- 绝不抛异常：target/stats 缺损的坏 result 三模式均回退静态提示。
- strip_markdown：粗体/斜体/标题/列表/删除线/链接/引用剥离，行内代码与围栏
  代码块 stash 保护，反斜杠转义保护，snake_case 不误伤。

运行方式（插件根目录，二者皆可）：
    python -m pytest "tests/v0.4.0/test_profile_formatter.py" -v
    python "tests/v0.4.0/test_profile_formatter.py"

范式沿用 tests/v0.4.0/test_models_capture.py（Agent-C stub 隔离技巧）：
astrbot.* 全部 sys.modules stub，注入前仅剔除 profile 子模块（不删 summary.*
等兄弟子包，避免破坏多文件合跑时其他版本测试的缓存类对象）；不依赖 AstrBot
运行时，star/renderer 用 fake 对象脚本化。

注意：profile/__init__.py（Module J 交付后）在包导入时即导出 ProfileService，
会连带加载 service → analyzer/capture/fetcher/scheduler/stats/storage/
t2i_render 与 ..db_config 整条链，故本文件 stub 须覆盖全链运行时依赖
（AstrMessageEvent / Context / StarTools / At / AtAll / Reply 等）——
formatter 本身仅依赖 .models，多出来的 stub 为包级 __init__ 牵引所致。
"""

import asyncio
import os
import sys
import types
import unittest
from datetime import datetime


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


# ---- astrbot.api: logger（带记录能力，供降级 warning 断言）----
class _StubLogger:
    """记录各级日志内容，便于断言（如 image 各级降级 warning）。"""

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


# ---- astrbot.api.message_components: Plain / Node / Nodes / Image / At /
#      AtAll / Reply ----
# 字段形态经核实 astrbot/core/message/components.py：
#   Plain(text)；Node(content=[...], uin="0", name="")；Nodes(nodes=[Node...])；
#   Image.fromURL(url) / Image.fromFileSystem(path)；At.qq（"all" 代表全体）；
#   Reply.id。At/AtAll/Reply 为 profile/__init__ → service → capture 链路
#   的运行时 isinstance 依赖，本文件不直接断言但必须提供。
class _StubPlain:
    type = "Plain"

    def __init__(self, text="", convert=True, **_):
        self.text = text


class _StubNode:
    type = "Node"

    def __init__(self, content=None, uin="0", name="", **_):
        self.content = content if content is not None else []
        self.uin = uin
        self.name = name


class _StubNodes:
    type = "Nodes"

    def __init__(self, nodes=None, **_):
        self.nodes = nodes if nodes is not None else []


class _StubImage:
    type = "Image"

    def __init__(self, file="", **_):
        self.file = file

    @staticmethod
    def fromURL(url, **_):
        if not url.startswith(("http://", "https://")):
            raise ValueError("not a valid url")
        return _StubImage(file=url)

    @staticmethod
    def fromFileSystem(path, **_):
        return _StubImage(file="file://" + path)


class _StubAt:
    type = "At"

    def __init__(self, qq="", name="", **_):
        self.qq = qq
        self.name = name


class _StubAtAll(_StubAt):
    def __init__(self, **_):
        super().__init__(qq="all")


class _StubReply:
    type = "Reply"

    def __init__(self, id="", **_):
        self.id = id


_astrbot_mc.Plain = _StubPlain
_astrbot_mc.Node = _StubNode
_astrbot_mc.Nodes = _StubNodes
_astrbot_mc.Image = _StubImage
_astrbot_mc.At = _StubAt
_astrbot_mc.AtAll = _StubAtAll
_astrbot_mc.Reply = _StubReply


# ---- astrbot.api.event: MessageChain（dataclass，chain 可位置/关键字传入）+
#      AstrMessageEvent（service/analyzer 运行时类型引用）----
class _StubMessageChain:
    def __init__(self, chain=None, **_):
        self.chain = list(chain) if chain is not None else []


class _StubAstrMessageEvent:
    pass


_astrbot_event.MessageChain = _StubMessageChain
_astrbot_event.AstrMessageEvent = _StubAstrMessageEvent


# ---- astrbot.api.star: Star / Context（formatter/analyzer 类型引用）+
#      StarTools（service 与 db_config 运行时依赖；get_data_dir 仅实例化
#      ConfigManager 时调用，本测试不触发，防御性返回临时目录）----
class _StubStar:
    pass


class _StubContext:
    pass


class _StubStarTools:
    @classmethod
    def get_data_dir(cls, plugin_name=None):
        import tempfile
        from pathlib import Path

        return Path(tempfile.gettempdir()) / (plugin_name or "test")


_astrbot_star.Star = _StubStar
_astrbot_star.Context = _StubContext
_astrbot_star.StarTools = _StubStarTools


# ---- 多测试文件合跑兼容：仅剔除 profile 子模块（而非整个插件包），使其重新
#      执行并绑定到本文件的 stub。切忌删除 summary.* / db_config 等兄弟模块——
#      它们可能已被其他版本测试文件导入并缓存，误删会触发二次导入产生重复类
#      对象，破坏其 isinstance 断言（Agent-C stub 隔离技巧）。 ----
for _name in list(sys.modules):
    if _name.startswith("astrbot_plugin_group_history_save_mysql.core.profile"):
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


# ============================================================
# 二、导入被测代码（stub 已就位）
# ============================================================

from astrbot_plugin_group_history_save_mysql.core.profile.formatter import (  # noqa: E402
    _DISCLAIMER,
    _FALLBACK_NAME,
    _FALLBACK_UIN,
    _INCOMPLETE_HINT,
    _ZWSP,
    ProfileFormatter,
    strip_markdown,
)
from astrbot_plugin_group_history_save_mysql.core.profile.models import (  # noqa: E402
    ProfileResult,
    ProfileStats,
    ProfileTarget,
)


# ============================================================
# 三、fake 依赖（star / renderer / 平台实例）
# ============================================================


class _FakeStar:
    """模拟 Star 实例：text_to_image 脚本化（返回值 / 抛异常）。

    context 默认 None → forward 节点署名走兜底 uin/name；需要验证真实署名
    时注入 _FakeContext。
    """

    def __init__(self, t2i_result=None, t2i_exc=None):
        self.t2i_result = t2i_result  # str | None
        self.t2i_exc = t2i_exc  # BaseException | None
        self.t2i_calls = []  # [text]
        self.context = None

    async def text_to_image(self, text, return_url=True):
        self.t2i_calls.append(text)
        if self.t2i_exc is not None:
            raise self.t2i_exc
        return self.t2i_result


class _FakeRenderer:
    """模拟 ProfileT2IRenderer：render 脚本化（消息链 / None / 抛异常）。"""

    def __init__(self, chain=None, exc=None):
        self.chain = chain
        self.exc = exc
        self.calls = 0

    async def render(self, result):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return self.chain


class _FakePlatformInst:
    def __init__(self, self_id):
        self.client_self_id = self_id


class _FakePlatformMgr:
    def __init__(self, insts):
        self.platform_insts = insts


class _FakeContext:
    def __init__(self, mgr):
        self.platform_manager = mgr


# ============================================================
# 四、构造辅助
# ============================================================


def _make_result(**overrides):
    """构造一个典型的单群 ProfileResult（2 个板块，含 Markdown 标记）。"""
    hour_dist = [0] * 24
    hour_dist[23] = 100
    weekday_dist = [0] * 7
    weekday_dist[5] = 60  # 周六
    target = ProfileTarget(
        sender_id="123456", sender_name="张三", scope="group", group_id="987654"
    )
    stats = ProfileStats(
        total=1500,
        group_count=1,
        group_breakdown=[("987654", 1500)],
        time_start=datetime(2026, 7, 1, 8, 0),
        time_end=datetime(2026, 7, 31, 23, 59),
        active_days=25,
        hour_dist=hour_dist,
        weekday_dist=weekday_dist,
        peak_hour=23,
        peak_weekday=5,
        avg_length=18.5,
        total_chars=27750,
        emoji_ratio=0.12,
        question_ratio=0.08,
        top_partners=[("222222", "李四", 120)],
    )
    params = {
        "target": target,
        "stats": stats,
        "sections": [
            ("发言习惯", "**高频**发言者，常用 `口头禅`"),
            ("性格分析", "- 外向\n- 幽默"),
        ],
        "raw_llm_text": "raw",
        "provider_id": "provider-a",
        "messages_used": 1000,
        "sources": {"mysql": 1200, "onebot": 300},
        "relation_context_complete": True,
        "scope_desc": "某群",
        "created_at": "2026-08-02 10:00",
    }
    params.update(overrides)
    return ProfileResult(**params)


def _make_broken_result():
    """字段缺损的坏 result：验证「绝不抛异常」契约。"""
    return ProfileResult(
        target=None,
        stats=None,
        sections=None,
        raw_llm_text="",
        provider_id="",
        messages_used=0,
    )


def _make_formatter(star=None, renderer=None):
    """构造 ProfileFormatter（config_mgr 本模块不消费，传占位对象）。"""
    return ProfileFormatter(star or _FakeStar(), object(), renderer)


def _run(coro):
    """执行协程（与 v0.2/v0.3 离线测试同范式：asyncio.run，不依赖 pytest-asyncio）。"""
    return asyncio.run(coro)


def _nodes_of(chain):
    """取 forward 消息链的节点列表（并断言单个 Nodes 包裹形态）。"""
    assert len(chain.chain) == 1, "forward 链应恰含 1 个组件"
    comp = chain.chain[0]
    assert isinstance(comp, _StubNodes), "forward 链应为单个 Comp.Nodes"
    return comp.nodes


def _node_text(node):
    """拼接节点内全部 Plain 文本。"""
    return "".join(c.text for c in node.content if isinstance(c, _StubPlain))


def _plain_text(chain):
    """取纯文本消息链的文本（并断言单 Plain 形态）。"""
    assert len(chain.chain) == 1, "文本链应恰含 1 个组件"
    comp = chain.chain[0]
    assert isinstance(comp, _StubPlain), "文本链应为单个 Comp.Plain"
    return comp.text


# ============================================================
# 五、测试用例
# ============================================================


class TestStripMarkdown(unittest.TestCase):
    """strip_markdown 纯函数（canonical 参考 summary/formatter.py）。"""

    def test_bold_italic_underline(self):
        self.assertEqual(
            strip_markdown("**粗体** 与 *斜体* 与 __下划线__"),
            "粗体 与 斜体 与 下划线",
        )

    def test_headings(self):
        self.assertEqual(
            strip_markdown("# 标题一\n## 标题二\n###### 标题六"),
            "标题一\n标题二\n标题六",
        )

    def test_list_markers(self):
        self.assertEqual(
            strip_markdown("- 项一\n* 项二\n+ 项三\n1. 项四\n2) 项五"),
            "项一\n项二\n项三\n项四\n项五",
        )

    def test_strikethrough(self):
        self.assertEqual(strip_markdown("~~删除~~"), "删除")

    def test_link_and_image(self):
        self.assertEqual(
            strip_markdown("[文字](http://a.com) 与 ![图说](http://b.png)"),
            "文字 与 图说",
        )

    def test_inline_code_protected(self):
        # 行内代码：反引号剥除，内容受 stash 保护不被强调规则误伤
        self.assertEqual(strip_markdown("行 `a*b*c` 尾"), "行 a*b*c 尾")

    def test_fence_block_protected(self):
        # 围栏代码块：标记剥除，块内 ** 原样保留
        self.assertEqual(strip_markdown("```\n**不加粗**\n```"), "**不加粗**")

    def test_escape_protected(self):
        bs = chr(92)  # 反斜杠，避免源码转义歧义
        self.assertEqual(
            strip_markdown(f"a{bs}*字面{bs}*b"),
            "a*字面*b",
        )

    def test_quote(self):
        self.assertEqual(strip_markdown("> 引用文字"), "引用文字")

    def test_snake_case_preserved(self):
        # 单下划线斜体规则要求两侧非单词字符，snake_case 不受影响
        self.assertEqual(strip_markdown("foo_bar_baz"), "foo_bar_baz")

    def test_empty_none_nonstr(self):
        self.assertEqual(strip_markdown(""), "")
        self.assertEqual(strip_markdown(None), "")
        self.assertEqual(strip_markdown(123), "123")


class TestForwardMode(unittest.TestCase):
    """forward 模式：单 Comp.Nodes 聚合（头 + sections + 页脚）。"""

    def test_single_nodes_wrapper_and_count(self):
        chain = _run(_make_formatter().render(_make_result(), "forward"))
        nodes = _nodes_of(chain)
        # 节点数 = 报告头 1 + sections 2 + 页脚 1
        self.assertEqual(len(nodes), 4)

    def test_sections_variable_count(self):
        zero = _run(_make_formatter().render(_make_result(sections=[]), "forward"))
        self.assertEqual(len(_nodes_of(zero)), 2)  # 头 + 页脚
        five = _run(
            _make_formatter().render(
                _make_result(sections=[(f"板块{i}", "正文") for i in range(5)]),
                "forward",
            )
        )
        self.assertEqual(len(_nodes_of(five)), 7)  # 头 + 5 + 页脚

    def test_header_content(self):
        nodes = _nodes_of(_run(_make_formatter().render(_make_result(), "forward")))
        header = _node_text(nodes[0])
        for expected in (
            "人物画像 · 张三",
            "QQ号：123456",
            "分析范围：某群",
            "时间跨度：2026-07-01 08:00 ~ 2026-07-31 23:59",
            "数据源构成：MySQL 1200 + OneBot 300",
            "消息总数：1500",
            "活跃天数：25 天",
            "最活跃时段：23 点",
            "最活跃星期：周六",
        ):
            self.assertIn(expected, header)
        # 关系上下文完整时不应出现不完整提示
        self.assertNotIn("⚠", header)

    def test_relation_incomplete_hint(self):
        result = _make_result(relation_context_complete=False)
        nodes = _nodes_of(_run(_make_formatter().render(result, "forward")))
        self.assertIn(_INCOMPLETE_HINT, _node_text(nodes[0]))
        self.assertIn("关系上下文不完整", _node_text(nodes[0]))

    def test_section_nodes_stripped(self):
        nodes = _nodes_of(_run(_make_formatter().render(_make_result(), "forward")))
        sec1 = _node_text(nodes[1])
        self.assertIn("发言习惯", sec1)  # 标题保留
        self.assertIn("高频发言者", sec1)  # 粗体剥除
        self.assertIn("口头禅", sec1)  # 行内代码内容保留
        self.assertNotIn("**", sec1)
        self.assertNotIn("`", sec1)
        sec2 = _node_text(nodes[2])
        self.assertIn("外向", sec2)
        self.assertIn("幽默", sec2)
        self.assertNotIn("- 外向", sec2)  # 列表符号剥除

    def test_footer_content(self):
        nodes = _nodes_of(_run(_make_formatter().render(_make_result(), "forward")))
        footer = _node_text(nodes[-1])
        self.assertIn("生成模型：provider-a", footer)
        self.assertIn(_DISCLAIMER, footer)
        self.assertIn("仅供参考", footer)

    def test_footer_default_provider(self):
        result = _make_result(provider_id="")
        nodes = _nodes_of(_run(_make_formatter().render(result, "forward")))
        self.assertIn("生成模型：会话默认", _node_text(nodes[-1]))

    def test_node_signature_fallback(self):
        # star.context=None → 兜底 uin/name（OneBot v11 合并转发需数字 uin）
        nodes = _nodes_of(_run(_make_formatter().render(_make_result(), "forward")))
        self.assertEqual(nodes[0].uin, _FALLBACK_UIN)
        self.assertEqual(nodes[0].uin, "10000")
        self.assertEqual(nodes[0].name, _FALLBACK_NAME)

    def test_node_signature_real_uin(self):
        star = _FakeStar()
        star.context = _FakeContext(_FakePlatformMgr([_FakePlatformInst("987654321")]))
        nodes = _nodes_of(_run(_make_formatter(star).render(_make_result(), "forward")))
        self.assertEqual(nodes[0].uin, "987654321")

    def test_node_signature_nonnumeric_fallback(self):
        star = _FakeStar()
        star.context = _FakeContext(
            _FakePlatformMgr([_FakePlatformInst("uuid-hex-not-ready")])
        )
        nodes = _nodes_of(_run(_make_formatter(star).render(_make_result(), "forward")))
        self.assertEqual(nodes[0].uin, _FALLBACK_UIN)

    def test_node_text_zwsp_padding(self):
        nodes = _nodes_of(_run(_make_formatter().render(_make_result(), "forward")))
        text = _node_text(nodes[0])
        self.assertTrue(text.startswith(_ZWSP))
        self.assertTrue(text.endswith(_ZWSP))


class TestTextMode(unittest.TestCase):
    """text 模式：剥 Markdown 的单 Plain 消息链。"""

    def test_single_plain_chain(self):
        chain = _run(_make_formatter().render(_make_result(), "text"))
        text = _plain_text(chain)
        self.assertTrue(text)

    def test_content_stripped_and_complete(self):
        text = _plain_text(_run(_make_formatter().render(_make_result(), "text")))
        # 报告头 / 板块 / 页脚要素齐全
        for expected in (
            "人物画像 · 张三",
            "QQ号：123456",
            "发言习惯",
            "高频发言者",
            "性格分析",
            "生成模型：provider-a",
            _DISCLAIMER,
        ):
            self.assertIn(expected, text)
        # Markdown 标记已剥除
        self.assertNotIn("**", text)
        self.assertNotIn("# 人物画像", text)  # 标题标记剥除
        self.assertNotIn("## ", text)
        self.assertNotIn("- QQ号", text)  # 列表标记剥除

    def test_zwsp_padding(self):
        text = _plain_text(_run(_make_formatter().render(_make_result(), "text")))
        self.assertTrue(text.startswith(_ZWSP))
        self.assertTrue(text.endswith(_ZWSP))


class TestImageFallback(unittest.TestCase):
    """image 模式三级降级：自研模板 → text_to_image → 纯文本。"""

    def test_l1_renderer_success_returns_directly(self):
        sentinel = _StubMessageChain([_StubPlain("IMG-SENTINEL")])
        star = _FakeStar(t2i_exc=RuntimeError("二级不应被调用"))
        renderer = _FakeRenderer(chain=sentinel)
        chain = _run(_make_formatter(star, renderer).render(_make_result(), "image"))
        self.assertIs(chain, sentinel)
        self.assertEqual(renderer.calls, 1)
        self.assertEqual(star.t2i_calls, [])  # 一级成功不触发二级

    def test_l1_none_falls_to_l2_url(self):
        star = _FakeStar(t2i_result="https://t2i.example/x.png")
        renderer = _FakeRenderer(chain=None)
        chain = _run(_make_formatter(star, renderer).render(_make_result(), "image"))
        self.assertEqual(renderer.calls, 1)
        self.assertEqual(len(star.t2i_calls), 1)
        comp = chain.chain[0]
        self.assertIsInstance(comp, _StubImage)
        self.assertEqual(comp.file, "https://t2i.example/x.png")
        # 二级吃完整 Markdown：报告头 + 板块 + 页脚免责声明
        markdown = star.t2i_calls[0]
        self.assertIn("# 人物画像 · 张三", markdown)
        self.assertIn("## 发言习惯", markdown)
        self.assertIn("**高频**发言者", markdown)  # Markdown 保留
        self.assertIn(_DISCLAIMER, markdown)

    def test_l2_local_path_fromfilesystem(self):
        star = _FakeStar(t2i_result="/tmp/profile.png")
        chain = _run(
            _make_formatter(star, _FakeRenderer(chain=None)).render(
                _make_result(), "image"
            )
        )
        comp = chain.chain[0]
        self.assertIsInstance(comp, _StubImage)
        self.assertEqual(comp.file, "file:///tmp/profile.png")

    def test_l1_exception_falls_to_l2(self):
        _STUB_LOGGER.records["warning"].clear()
        star = _FakeStar(t2i_result="https://t2i.example/y.png")
        renderer = _FakeRenderer(exc=RuntimeError("boom"))
        chain = _run(_make_formatter(star, renderer).render(_make_result(), "image"))
        self.assertEqual(chain.chain[0].file, "https://t2i.example/y.png")
        self.assertIn("未预期异常", _STUB_LOGGER.joined("warning"))

    def test_both_fail_falls_to_l3_plain(self):
        _STUB_LOGGER.records["warning"].clear()
        star = _FakeStar(t2i_exc=RuntimeError("t2i down"))
        chain = _run(
            _make_formatter(star, _FakeRenderer(chain=None)).render(
                _make_result(), "image"
            )
        )
        text = _plain_text(chain)
        self.assertIn("人物画像 · 张三", text)
        self.assertIn(_DISCLAIMER, text)
        self.assertNotIn("**", text)  # 三级纯文本已剥 Markdown
        warnings = _STUB_LOGGER.joined("warning")
        self.assertIn("自研 T2I 模板渲染失败", warnings)
        self.assertIn("text_to_image 渲染失败", warnings)

    def test_l2_empty_return_falls_to_l3(self):
        _STUB_LOGGER.records["warning"].clear()
        star = _FakeStar(t2i_result="")  # 空返回 → _image_chain None → 三级
        chain = _run(
            _make_formatter(star, _FakeRenderer(chain=None)).render(
                _make_result(), "image"
            )
        )
        self.assertIsInstance(chain.chain[0], _StubPlain)
        self.assertIn("返回为空", _STUB_LOGGER.joined("warning"))

    def test_renderer_none_skips_l1_without_warning(self):
        _STUB_LOGGER.records["warning"].clear()
        star = _FakeStar(t2i_result="https://t2i.example/z.png")
        chain = _run(_make_formatter(star, None).render(_make_result(), "image"))
        self.assertEqual(chain.chain[0].file, "https://t2i.example/z.png")
        # renderer=None 是跳过一级而非失败，不应记一级失败 warning
        self.assertNotIn("自研 T2I 模板渲染失败", _STUB_LOGGER.joined("warning"))


class TestModeDispatch(unittest.TestCase):
    """output_mode 分发：非法值回退 forward，大小写/空白归一化。"""

    def test_invalid_modes_fall_back_to_forward(self):
        for mode in ("xml", "markdown", "", None):
            chain = _run(_make_formatter().render(_make_result(), mode))
            nodes = _nodes_of(chain)
            self.assertEqual(len(nodes), 4, f"mode={mode!r} 应回退 forward")

    def test_mode_normalization(self):
        # 大小写与首尾空白归一化后生效
        star = _FakeStar(t2i_result="https://t2i.example/n.png")
        chain = _run(
            _make_formatter(star, _FakeRenderer(chain=None)).render(
                _make_result(), " IMAGE "
            )
        )
        self.assertIsInstance(chain.chain[0], _StubImage)

        text_chain = _run(_make_formatter().render(_make_result(), "Text"))
        self.assertIsInstance(text_chain.chain[0], _StubPlain)

        fwd_chain = _run(_make_formatter().render(_make_result(), "FORWARD"))
        self.assertIsInstance(fwd_chain.chain[0], _StubNodes)


class TestNeverRaises(unittest.TestCase):
    """「绝不抛异常」契约：坏 result（target/stats=None）三模式均兜底。"""

    def test_image_broken_result_static_prompt(self):
        star = _FakeStar(t2i_exc=RuntimeError("down"))
        chain = _run(_make_formatter(star, None).render(_make_broken_result(), "image"))
        self.assertEqual(
            _plain_text(chain).strip(_ZWSP), "人物画像渲染失败，请稍后重试"
        )

    def test_forward_broken_result_static_prompt(self):
        chain = _run(_make_formatter().render(_make_broken_result(), "forward"))
        self.assertEqual(
            _plain_text(chain).strip(_ZWSP), "人物画像渲染失败，请稍后重试"
        )

    def test_text_broken_result_static_prompt(self):
        chain = _run(_make_formatter().render(_make_broken_result(), "text"))
        self.assertEqual(
            _plain_text(chain).strip(_ZWSP), "人物画像渲染失败，请稍后重试"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
