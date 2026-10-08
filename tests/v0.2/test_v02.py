"""astrbot_plugin_group_history_save_mysql v0.2 离线测试。

覆盖 v0.2 四项变更中可离线验证的部分：
- F5: main.extract_image_urls（OneBot 原始事件图片链接提取 + 回退 + 畸形输入健壮性）
- F6: web_api.api_query 查询参数文本化（group_id/sender_id 空串转 None，非空透传）
- F8: web_api.make_challenge / api_purge_challenge / api_purge（加减法验证清空流程）

db_mysql.py 的 DDL/迁移/insert_image_record/purge_all 依赖真实 MySQL，
仅做静态核对（见 test_0.md），不在本文件离线执行。

运行方式（插件根目录）：
    python -m pytest "tests/v0.2/test_v02.py" -v

注意：所有 sys.modules stub 必须在 import 被测包之前完成。
"""

import asyncio
import functools
import os
import re
import sys
import tempfile
import time
import types
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
_astrbot_web = _new_module("astrbot.api.web")
_astrbot_core = _new_module("astrbot.core")
_astrbot_core_utils = _new_module("astrbot.core.utils")
_astrbot_core_utils_path = _new_module("astrbot.core.utils.astrbot_path")


# ---- astrbot.api: logger + AstrBotConfig ----
class _StubLogger:
    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass

    def debug(self, *args, **kwargs):
        pass


class _StubAstrBotConfig:
    pass


_astrbot_api.logger = _StubLogger()
_astrbot_api.AstrBotConfig = _StubAstrBotConfig


# ---- astrbot.api.message_components: Image / Plain（isinstance 依赖这两个类）----
class _StubImage:
    pass


class _StubPlain:
    pass


_astrbot_mc.Image = _StubImage
_astrbot_mc.Plain = _StubPlain


# ---- astrbot.api.event: AstrMessageEvent + filter（装饰器工厂返回恒等装饰器）----
class _StubAstrMessageEvent:
    pass


def _identity_decorator_factory(*args, **kwargs):
    """返回一个恒等装饰器（原样返回被装饰对象）。"""

    def _decorator(func):
        return func

    return _decorator


class _StubEventType:
    GROUP_MESSAGE = "GROUP_MESSAGE"


class _StubPermissionType:
    ADMIN = "ADMIN"


class _StubPlatformAdapterType:
    AIOCQHTTP = "AIOCQHTTP"


class _StubFilter:
    EventMessageType = _StubEventType
    PermissionType = _StubPermissionType
    PlatformAdapterType = _StubPlatformAdapterType

    event_message_type = staticmethod(_identity_decorator_factory)
    platform_adapter_type = staticmethod(_identity_decorator_factory)
    permission_type = staticmethod(_identity_decorator_factory)
    command = staticmethod(_identity_decorator_factory)


_astrbot_event.AstrMessageEvent = _StubAstrMessageEvent
_astrbot_event.filter = _StubFilter()


# ---- astrbot.api.star: Context / Star / register ----
class _StubContext:
    pass


class _StubStar:
    def __init__(self, context):
        pass


def _stub_register(*args, **kwargs):
    """插件注册装饰器工厂，返回恒等类装饰器。"""

    def _decorator(cls):
        return cls

    return _decorator


_astrbot_star.Context = _StubContext
_astrbot_star.Star = _StubStar
_astrbot_star.register = _stub_register


# ---- astrbot.api.web: request（占位，测试中 monkeypatch）/ json_response / error_response ----
class _StubRequest:
    """占位 request；每个测试用 monkeypatch 替换 web_api.request。"""

    async def json(self, default=None):
        return default

    query = None


def _stub_json_response(data, status_code: int = 200):
    return {"__json__": True, "data": data, "status": status_code}


def _stub_error_response(message, status_code: int = 400):
    return {"__error__": True, "message": message, "status": status_code}


def _stub_file_response(*args, **kwargs):
    return {"__file__": True}


_astrbot_web.request = _StubRequest()
_astrbot_web.json_response = _stub_json_response
_astrbot_web.error_response = _stub_error_response
_astrbot_web.file_response = _stub_file_response

# ---- astrbot.core.utils.astrbot_path: get_astrbot_plugin_data_path ----
_TEMP_DATA_DIR = tempfile.mkdtemp(prefix="astrbot_hist_test_")
_astrbot_core_utils_path.get_astrbot_plugin_data_path = lambda: _TEMP_DATA_DIR

# ---- astrbot.core.utils.io: save_temp_img（core/webapi/profile|summary 顶层导入）----
_astrbot_core_utils_io = _new_module("astrbot.core.utils.io")
_astrbot_core_utils_io.save_temp_img = lambda *a, **k: ""
sys.modules["astrbot.core.utils.io"] = _astrbot_core_utils_io


# ---- astrbot.api.star: StarTools（db_config.py v0.2.1 起使用）----
class _StubStarTools:
    @classmethod
    def get_data_dir(cls, plugin_name=None):
        return Path(_TEMP_DATA_DIR) / (plugin_name or "test")


_astrbot_star.StarTools = _StubStarTools

# ---- aiomysql（未安装，stub 掉；db_mysql.py 顶层导入并在注解中引用）----
_aiomysql = _new_module("aiomysql")
_aiomysql.Connection = object
_aiomysql.DictCursor = object

# ---- 注入 sys.modules ----
sys.modules["astrbot"] = _astrbot
sys.modules["astrbot.api"] = _astrbot_api
sys.modules["astrbot.api.message_components"] = _astrbot_mc
sys.modules["astrbot.api.event"] = _astrbot_event
sys.modules["astrbot.api.star"] = _astrbot_star
sys.modules["astrbot.api.web"] = _astrbot_web
sys.modules["astrbot.core"] = _astrbot_core
sys.modules["astrbot.core.utils"] = _astrbot_core_utils
sys.modules["astrbot.core.utils.astrbot_path"] = _astrbot_core_utils_path
sys.modules["aiomysql"] = _aiomysql

# 让被测包可被导入：F:\...\data\plugins 加入 sys.path
_PLUGINS_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..")
)
if _PLUGINS_DIR not in sys.path:
    sys.path.insert(0, _PLUGINS_DIR)

# ============================================================
# 二、导入被测代码（stub 已就位）
# ============================================================

import astrbot_plugin_group_history_save_mysql.core.webapi as web_api  # noqa: E402
from astrbot_plugin_group_history_save_mysql.core.webapi import (  # noqa: E402
    WebAPI,
    make_challenge,
)
from astrbot_plugin_group_history_save_mysql.core.webapi.base import (  # noqa: E402
    CHALLENGE_TTL,
    PLUGIN_NAME,
)
from astrbot_plugin_group_history_save_mysql.core.parsing import (  # noqa: E402
    extract_image_urls,
)

# 被测包的 message_components 就是 stub 的类
Image = _StubImage
Plain = _StubPlain


# ============================================================
# 三、测试辅助
# ============================================================


def make_image(url=None, file=None):
    """构造带 url/file 属性的消息链 Image 组件实例。"""
    inst = Image()
    inst.url = url
    inst.file = file
    return inst


class MockRequest:
    """模拟 astrbot 的 request 对象。"""

    def __init__(self, payload=None, query_params=None):
        self._payload = payload if payload is not None else {}
        self._query_params = query_params or {}

    async def json(self, default=None):
        return self._payload

    @property
    def query(self):
        params = self._query_params

        class _Query:
            def get(self, key, default=None, type=None):
                value = params.get(key, default)
                if type is not None and value is not None:
                    try:
                        return type(value)
                    except (ValueError, TypeError):
                        return default
                return value

        return _Query()


def _patch_request(monkeypatch, stub):
    """v0.6.0 拆分后 request 按 webapi mixin 模块绑定：同步 patch 使用它的模块。

    旧版 web_api.py 单文件内 ``request`` 为模块级全局，patch 包属性即生效；
    拆分后 base/storage/query/summary/profile/stats 各模块以
    ``from astrbot.api.web import request`` 在 import 时绑定同名全局，
    必须逐一 patch 各 mixin 模块的模块级 ``request`` 才能命中运行路径。
    """
    import astrbot_plugin_group_history_save_mysql.core.webapi.query as _query_mod
    import astrbot_plugin_group_history_save_mysql.core.webapi.storage as _storage_mod

    monkeypatch.setattr(_query_mod, "request", stub)
    monkeypatch.setattr(_storage_mod, "request", stub)


class MockContext:
    """记录 register_web_api 调用的最小 Context。"""

    def __init__(self):
        self.registered = []

    def register_web_api(self, route, handler, methods, desc):
        self.registered.append((route, handler, methods, desc))


class MockMySQL:
    """记录调用次数与入参的 MySQL 管理器替身。"""

    def __init__(self, purge_result=None, query_result=None):
        self.purge_calls = 0
        self._purge_result = purge_result or {
            "success": True,
            "deleted_messages": 3,
            "deleted_images": 2,
        }
        self.query_calls = []
        self._query_result = query_result or {"total": 0, "data": []}

    async def purge_all(self):
        self.purge_calls += 1
        return self._purge_result

    async def query_messages(self, **kwargs):
        self.query_calls.append(kwargs)
        return self._query_result


@pytest.fixture()
def api():
    """构造 WebAPI 实例（MockContext + MockMySQL，config/cleaner 不参与本组用例）。"""
    context = MockContext()
    mysql = MockMySQL()
    return WebAPI(context, mysql, None, None), context, mysql


# ============================================================
# 四、extract_image_urls 用例（F5）
# ============================================================


class TestExtractImageUrls:
    def test_tc_e1_raw_message_url(self):
        """TC-E1: 标准 OneBot 事件，image 段 data.url 为 https 链接 → 返回该链接。"""
        url = "https://gchat.qpic.cn/gchatpic_new/xxx/0"
        msg_obj = types.SimpleNamespace(
            raw_message={
                "message": [
                    {"type": "text", "data": {"text": "hello"}},
                    {"type": "image", "data": {"file": "xxx.image", "url": url}},
                ]
            }
        )
        assert extract_image_urls(msg_obj, []) == [url]

    def test_tc_e2_file_fallback_lagrange(self):
        """TC-E2: image 段无 url，data.file 为 http 链接（Lagrange 兼容）→ 返回 file 值。"""
        file_url = "http://gchat.qpic.cn/gchatpic_new/yyy/0"
        msg_obj = types.SimpleNamespace(
            raw_message={"message": [{"type": "image", "data": {"file": file_url}}]}
        )
        assert extract_image_urls(msg_obj, []) == [file_url]

    def test_tc_e3_local_paths_rejected(self):
        """TC-E3: raw 段与消息链组件均为本地路径 → 返回空列表（本地路径一律拒绝）。"""
        local = r"C:\Users\User\AppData\Local\Temp\astrbot\cache\img\tmp.jpg"
        msg_obj = types.SimpleNamespace(
            raw_message={
                "message": [
                    {
                        "type": "image",
                        "data": {"file": "abc.image", "url": local},
                    }
                ]
            }
        )
        chain = [make_image(url=local, file=local)]
        assert extract_image_urls(msg_obj, chain) == []

    def test_tc_e4_fallback_to_chain(self):
        """TC-E4: raw_message 为 None/非 dict/无 message 键/空数组 → 回退消息链 http 链接。"""
        chain = [
            Plain(),
            make_image(url="https://gchat.qpic.cn/aaa", file=None),
            make_image(url="http://example.com/bbb.png", file=None),
            make_image(url=None, file="https://gchat.qpic.cn/ccc"),
            make_image(url=r"D:\tmp\local.jpg", file=None),  # 本地路径不收集
        ]

        # raw_message = None
        obj_none = types.SimpleNamespace(raw_message=None)
        assert extract_image_urls(obj_none, chain) == [
            "https://gchat.qpic.cn/aaa",
            "http://example.com/bbb.png",
            "https://gchat.qpic.cn/ccc",
        ]

        # raw_message 非 dict
        obj_not_dict = types.SimpleNamespace(raw_message="not-a-dict")
        assert extract_image_urls(obj_not_dict, chain) == [
            "https://gchat.qpic.cn/aaa",
            "http://example.com/bbb.png",
            "https://gchat.qpic.cn/ccc",
        ]

        # 无 message 键
        obj_no_key = types.SimpleNamespace(raw_message={"post_type": "message"})
        assert extract_image_urls(obj_no_key, chain) == [
            "https://gchat.qpic.cn/aaa",
            "http://example.com/bbb.png",
            "https://gchat.qpic.cn/ccc",
        ]

        # message 为空数组
        obj_empty = types.SimpleNamespace(raw_message={"message": []})
        assert extract_image_urls(obj_empty, chain) == [
            "https://gchat.qpic.cn/aaa",
            "http://example.com/bbb.png",
            "https://gchat.qpic.cn/ccc",
        ]

        # message 只有非 image 段（提取结果为空）→ 同样回退
        obj_text_only = types.SimpleNamespace(
            raw_message={"message": [{"type": "text", "data": {"text": "hi"}}]}
        )
        assert extract_image_urls(obj_text_only, chain) == [
            "https://gchat.qpic.cn/aaa",
            "http://example.com/bbb.png",
            "https://gchat.qpic.cn/ccc",
        ]

    def test_tc_e5_malformed_segments(self):
        """TC-E5: 畸形段混入 → 不抛异常，正常段仍被提取。"""
        url_ok = "https://gchat.qpic.cn/ok"
        segments = [
            None,
            42,
            "str-seg",
            {"type": "image"},  # 缺 data
            {"type": "image", "data": "not-a-dict"},  # data 非 dict
            {"type": "image", "data": {"url": 12345}},  # url 非 str
            {"type": "image", "data": {"url": None, "file": 42}},  # url/file 均非法
            {"type": "image", "data": {"url": url_ok}},  # 正常段
        ]
        msg_obj = types.SimpleNamespace(raw_message={"message": segments})
        assert extract_image_urls(msg_obj, []) == [url_ok]

    def test_tc_e6_multiple_images_in_order(self):
        """TC-E6: 多个图片段 → 多个链接按序返回。"""
        u1 = "https://gchat.qpic.cn/1"
        u2 = "https://gchat.qpic.cn/2"
        u3 = "http://example.com/3.png"
        msg_obj = types.SimpleNamespace(
            raw_message={
                "message": [
                    {"type": "text", "data": {"text": "x"}},
                    {"type": "image", "data": {"url": u1}},
                    {"type": "image", "data": {"url": u2}},
                    {"type": "image", "data": {"file": u3}},
                ]
            }
        )
        assert extract_image_urls(msg_obj, []) == [u1, u2, u3]

    def test_tc_e7_no_raw_message_attr(self):
        """TC-E7: message_obj 完全没有 raw_message 属性（object()）→ 不抛异常。"""
        chain = [make_image(url="https://gchat.qpic.cn/fallback", file=None)]
        assert extract_image_urls(object(), chain) == ["https://gchat.qpic.cn/fallback"]
        # 消息链也为空时返回空列表
        assert extract_image_urls(object(), []) == []
        assert extract_image_urls(object(), None) == []


# ============================================================
# 五、make_challenge 用例（F8）
# ============================================================


class TestMakeChallenge:
    def test_tc_c1_format(self):
        """TC-C1: 抽样 200 次，question 格式正确，challenge_id 为 32 位十六进制。"""
        pattern = re.compile(r"^\d{1,2} [+\-] \d{1,2} = \?$")
        hex32 = re.compile(r"^[0-9a-f]{32}$")
        for _ in range(200):
            challenge_id, question, answer = make_challenge()
            assert pattern.match(question), f"question 格式非法: {question!r}"
            assert hex32.match(challenge_id), f"challenge_id 非法: {challenge_id!r}"
            assert isinstance(answer, int)

    def test_tc_c2_answer_consistency(self):
        """TC-C2: 解析 question 中的 a/op/b，answer 与之一致且 >= 0。"""
        for _ in range(200):
            _, question, answer = make_challenge()
            m = re.match(r"^(\d{1,2}) ([+\-]) (\d{1,2}) = \?$", question)
            assert m is not None
            a, op, b = int(m.group(1)), m.group(2), int(m.group(3))
            expected = a + b if op == "+" else a - b
            assert answer == expected, f"{question} → {answer} != {expected}"
            assert answer >= 0


# ============================================================
# 六、清空验证端到端流程（F8：api_purge_challenge / api_purge）
# ============================================================


class TestPurgeFlow:
    @async_test
    async def test_tc_p1_get_challenge(self, api, monkeypatch):
        """TC-P1: GET challenge → 返回 challenge_id/question，并存入 _purge_challenges。"""
        instance, context, _ = api
        _patch_request(monkeypatch, MockRequest())

        resp = await instance.api_purge_challenge()
        assert resp["__json__"] is True
        data = resp["data"]
        assert re.match(r"^[0-9a-f]{32}$", data["challenge_id"])
        assert re.match(r"^\d{1,2} [+\-] \d{1,2} = \?$", data["question"])
        # 已存入内存字典，过期时间约为 now + 300s
        entry = instance._purge_challenges[data["challenge_id"]]
        assert entry[1] > time.monotonic()
        assert entry[1] <= time.monotonic() + CHALLENGE_TTL + 1
        # 路由注册验证（顺带）：purge/challenge 与 purge 均已注册
        routes = [r[0] for r in context.registered]
        assert f"/{PLUGIN_NAME}/purge/challenge" in routes
        assert f"/{PLUGIN_NAME}/purge" in routes

    @async_test
    async def test_tc_p2_correct_answer(self, api, monkeypatch):
        """TC-P2: 正确答案 POST → success=True + 删除计数，purge_all 被调用 1 次。"""
        instance, _, mysql = api
        _patch_request(monkeypatch, MockRequest())

        challenge = (await instance.api_purge_challenge())["data"]
        cid = challenge["challenge_id"]
        answer = instance._purge_challenges[cid][0]  # 取真实答案

        _patch_request(monkeypatch, MockRequest(payload={"challenge_id": cid, "answer": answer}))
        resp = await instance.api_purge()
        assert resp["__json__"] is True
        assert resp["data"]["success"] is True
        assert resp["data"]["deleted_messages"] == 3
        assert resp["data"]["deleted_images"] == 2
        assert mysql.purge_calls == 1
        # 一次性消费：challenge 已删除
        assert cid not in instance._purge_challenges

    @async_test
    async def test_tc_p3_replay_same_challenge(self, api, monkeypatch):
        """TC-P3: 复用同一 challenge_id 再次 POST（答案正确）→ 400 验证已失效。"""
        instance, _, mysql = api
        _patch_request(monkeypatch, MockRequest())

        challenge = (await instance.api_purge_challenge())["data"]
        cid = challenge["challenge_id"]
        answer = instance._purge_challenges[cid][0]

        payload = {"challenge_id": cid, "answer": answer}
        _patch_request(monkeypatch, MockRequest(payload=payload))
        resp1 = await instance.api_purge()
        assert resp1["__json__"] is True
        assert mysql.purge_calls == 1

        # 复用同一 challenge_id
        _patch_request(monkeypatch, MockRequest(payload=payload))
        resp2 = await instance.api_purge()
        assert resp2["__error__"] is True
        assert resp2["status"] == 400
        assert "验证已失效" in resp2["message"]
        assert mysql.purge_calls == 1  # 未再次清空

    @async_test
    async def test_tc_p4_wrong_answer_consumes(self, api, monkeypatch):
        """TC-P4: 错误答案 → 400 答案不正确；challenge 被消费，再提交也是失效。"""
        instance, _, mysql = api
        _patch_request(monkeypatch, MockRequest())

        challenge = (await instance.api_purge_challenge())["data"]
        cid = challenge["challenge_id"]
        answer = instance._purge_challenges[cid][0]
        wrong = answer + 1  # 必然错误

        _patch_request(monkeypatch, MockRequest(payload={"challenge_id": cid, "answer": wrong}))
        resp = await instance.api_purge()
        assert resp["__error__"] is True
        assert resp["status"] == 400
        assert "答案不正确" in resp["message"]
        assert mysql.purge_calls == 0
        assert cid not in instance._purge_challenges

        # 再次提交（哪怕正确答案）→ 验证已失效
        _patch_request(monkeypatch, MockRequest(payload={"challenge_id": cid, "answer": answer}))
        resp2 = await instance.api_purge()
        assert resp2["__error__"] is True
        assert resp2["status"] == 400
        assert "验证已失效" in resp2["message"]
        assert mysql.purge_calls == 0

    @async_test
    async def test_tc_p5_non_numeric_answer(self, api, monkeypatch):
        """TC-P5: answer 为非数字字符串 → 400，不崩溃。"""
        instance, _, mysql = api
        _patch_request(monkeypatch, MockRequest())

        challenge = (await instance.api_purge_challenge())["data"]
        cid = challenge["challenge_id"]

        _patch_request(monkeypatch, MockRequest(payload={"challenge_id": cid, "answer": "abc"}))
        resp = await instance.api_purge()
        assert resp["__error__"] is True
        assert resp["status"] == 400
        assert mysql.purge_calls == 0
        assert cid not in instance._purge_challenges

    @async_test
    async def test_tc_p6_expired_challenge(self, api, monkeypatch):
        """TC-P6: challenge 过期 → 正确答案也返回 400 验证已过期。"""
        instance, _, mysql = api
        _patch_request(monkeypatch, MockRequest())

        challenge = (await instance.api_purge_challenge())["data"]
        cid = challenge["challenge_id"]
        answer = instance._purge_challenges[cid][0]

        # 手工改为已过期
        instance._purge_challenges[cid] = (answer, time.monotonic() - 1)

        _patch_request(monkeypatch, MockRequest(payload={"challenge_id": cid, "answer": answer}))
        resp = await instance.api_purge()
        assert resp["__error__"] is True
        assert resp["status"] == 400
        assert "验证已过期" in resp["message"]
        assert mysql.purge_calls == 0

    @async_test
    async def test_tc_p7_cleanup_on_generation(self, api, monkeypatch):
        """TC-P7: 生成新 challenge 时清理全部过期项。"""
        instance, _, _ = api
        _patch_request(monkeypatch, MockRequest())

        # 塞入一个已过期的假项与一个未过期项
        instance._purge_challenges["fake_expired"] = (1, time.monotonic() - 100)
        valid_entry = (42, time.monotonic() + 100)
        instance._purge_challenges["still_valid"] = valid_entry

        resp = await instance.api_purge_challenge()
        assert resp["__json__"] is True

        assert "fake_expired" not in instance._purge_challenges
        # 未过期项原样保留
        assert instance._purge_challenges.get("still_valid") == valid_entry
        # 新 challenge 已加入
        new_cid = resp["data"]["challenge_id"]
        assert new_cid in instance._purge_challenges


# ============================================================
# 七、api_query 参数字符串化（F6）
# ============================================================


class TestApiQuery:
    @async_test
    async def test_tc_q1_string_params(self, monkeypatch):
        """TC-Q1: group_id 透传 "123"；sender_id 空串变 None。"""
        context = MockContext()
        mysql = MockMySQL(query_result={"total": 0, "data": []})
        instance = WebAPI(context, mysql, None, None)

        query_params = {
            "group_id": "123",
            "sender_id": "",
            "time_start": "2026-07-01 00:00:00",
            "time_end": "2026-07-28 23:59:59",
            "page": "2",
            "page_size": "20",
        }
        _patch_request(monkeypatch, MockRequest(query_params=query_params))

        resp = await instance.api_query()
        assert resp["__json__"] is True
        assert resp["data"] == {"total": 0, "data": []}
        assert len(mysql.query_calls) == 1

        kwargs = mysql.query_calls[0]
        assert kwargs["group_id"] == "123"  # 非空字符串透传
        assert kwargs["sender_id"] is None  # 空串视同未提供
        assert kwargs["time_start"] == "2026-07-01 00:00:00"
        assert kwargs["time_end"] == "2026-07-28 23:59:59"
        assert kwargs["page"] == 2  # type=int 转换
        assert kwargs["page_size"] == 20

    @async_test
    async def test_tc_q1b_both_empty(self, monkeypatch):
        """TC-Q1b: group_id 与 sender_id 均为空串 → 均为 None（补充用例）。"""
        context = MockContext()
        mysql = MockMySQL()
        instance = WebAPI(context, mysql, None, None)

        _patch_request(monkeypatch, MockRequest(query_params={"group_id": "", "sender_id": ""}))
        resp = await instance.api_query()
        assert resp["__json__"] is True

        kwargs = mysql.query_calls[0]
        assert kwargs["group_id"] is None
        assert kwargs["sender_id"] is None
        assert kwargs["page"] == 1  # 默认值
        assert kwargs["page_size"] == 50

    @async_test
    async def test_tc_q2_keyword(self, monkeypatch):
        """TC-Q2: keyword 非空 strip 后透传；纯空白 → None。"""
        context = MockContext()
        mysql = MockMySQL()
        instance = WebAPI(context, mysql, None, None)

        _patch_request(monkeypatch, MockRequest(query_params={"keyword": "  你好  "}))
        await instance.api_query()
        assert mysql.query_calls[0]["keyword"] == "你好"  # strip 后透传

        mysql.query_calls.clear()
        _patch_request(monkeypatch, MockRequest(query_params={"keyword": "   "}))
        await instance.api_query()
        assert mysql.query_calls[0]["keyword"] is None  # 纯空白视同未提供
