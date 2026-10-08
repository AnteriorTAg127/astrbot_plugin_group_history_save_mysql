# ruff: noqa: I001
"""astrbot_plugin_group_history_save_mysql v0.9.0 测试：SQLite 备用后端全链路。

本版（v0.9.0）新增：① core/db_sqlite.py SQLiteManager（MySQLManager 鸭子类型
备用后端，history.db）；② core/bootstrap.py 启动锁纯函数 _resolve_backend /
_parse_bool_cfg 与 storage_info_provider 注入；③ core/sqlite_migrator.py
SQLiteMigrator 一次性迁移（/导出聊天记录）；④ webapi storage/info 端点与
前端危险横幅；⑤ commands.group_migrate 薄壳。测试沿用 v0.7.0 范式：
@async_test 装饰器（asyncio.run，无 pytest-asyncio / conftest.py）；
astrbot.* / aiomysql 全部 sys.modules stub 且注入先于一切被测导入；被测包
缓存剔除兼容多测试文件合跑；SQLite 读写侧用**真实 aiosqlite 临时文件库**
（目录为本测试目录下普通目录 tmp_v090——DSH 沙箱禁 mkdtemp 随机目录内建
文件，见 smoke_a_sqlite.py 同款说明）。

A 组（core/bootstrap.py _resolve_backend 启动锁三分支）：
- A-1 未锁定→按配置（缺省 mysql / 显式 sqlite）
- A-2 非法配置值回退 mysql
- A-3 已锁定且一致→正常
- A-4 已锁定且不一致→按锁旧后端 + mismatch=True + has_lock=True（两方向）
- A-5 归一化：空串/None/大小写/混空格/垃圾锁值

B 组（_parse_bool_cfg）：bool 直返、"true"/"TRUE"/"1"/"yes" → True、
"false"/"0" → False、垃圾值 → 默认、None、int 1/0。

C 组（core/db_sqlite.py SQLiteManager 关键契约，真实 aiosqlite）：
- C-1 ping() 顶层键 + pool 四键（commands pool_info 取值路径不炸）
- C-2 insert_chat_message：空 message_id 多条共存；同 id 幂等 True
- C-3 query_messages 输出 dict 键与 MySQL 侧同构（对照手工断言 + NULL 语义）
- C-4 get_existing_message_ids：>500 分块正常返回
- C-5 purge_all 后可再插入；get_meta/set_meta 往返；is_sqlite_backend/db_path
- C-6 busy_timeout/wal 配置归一（非法回退、越界夹取）

D 组（core/sqlite_migrator.py SQLiteMigrator；假 MySQL 池 + 真实 SQLite）：
- D-1 全量导入计数正确 + meta 标记 + 重跑被标记拦截
- D-2 hours 非法/≤0 → 用法提示；hours 窗口透传 + 不写全量标记
- D-3 防重入：迁移进行中再调 → 「迁移正在进行中」
- D-4 池建连失败 → 「连接 MySQL 失败」；批次抛异常 → 「迁移失败」且已导入保留
- D-5 重复行：图片/消息侧按群预查命中，skipped 计数正确（PROD-BUG-01 修复后口径）
- D-6 MySQL 行 timestamp 非 datetime（脏值）→ 按当前时间入库，不抛

E 组（core/webapi/storage.py api_storage_info + base.py 路由表）：
- E-1 provider None → error_response 503
- E-2 async provider dict → json_response 成功透传
- E-3 provider 抛异常 → error_response 500（不冒泡）
- E-4 路由表含 storage/info（路径/GET/总数 43）

F 组（core/commands.py group_migrate 薄壳）：
- F-1 migrator None → 「当前为 MySQL 存储模式，无需导入。」
- F-2 假 migrator 转发 hours；抛异常 → 「迁移异常」不冒泡

G 组（前端/配置/元数据静态断言）：
- G-1 index.html 含 storageBanner、三处 ?v=0.9.0、无 0.5/0.6/0.7/0.8 残留
- G-2 app.js 含 storage/info 与横幅渲染关键符号
- G-3 _conf_schema.json 合法 + storage_backend 三项契约
- G-4 metadata/main/bootstrap 版本文本与接线文案

运行方式（插件根目录，二者皆可）：
    python -m pytest "tests/v0.9.0/test_v090.py" -v
    PYTHONIOENCODING=utf-8 python "tests/v0.9.0/test_v090.py"
"""

import asyncio
import functools
import json
import shutil
import sys
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path


def async_test(fn):
    """装饰器：用 asyncio.run 运行异步测试函数（环境无 pytest-asyncio 依赖）。"""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        asyncio.run(fn(*args, **kwargs))

    return wrapper


_PKG = "astrbot_plugin_group_history_save_mysql"
_TEST_DIR = Path(__file__).resolve().parent
_PLUGIN_ROOT = _TEST_DIR.parents[1]  # tests/v0.9.0 → 插件根
_PLUGINS_DIR = str(_PLUGIN_ROOT.parent)  # data/plugins（包导入根）

# 临时文件库目录：本目录下普通目录（沙箱禁 mkdtemp 随机目录内建文件）
_TMP_DIR = _TEST_DIR / "tmp_v090"
if _TMP_DIR.exists():
    shutil.rmtree(_TMP_DIR, ignore_errors=True)
_TMP_DIR.mkdir(parents=True)


# ============================================================
# 一、astrbot.* / aiomysql stub 注入（必须先于任何被测导入）
#     范式与清单沿用 test_v070.py；合跑时复用既有 stub 对象
# ============================================================


def _new_module(name: str) -> types.ModuleType:
    return types.ModuleType(name)


_sys_astrbot = sys.modules.get("astrbot")
_sys_api = getattr(_sys_astrbot, "api", None) if _sys_astrbot is not None else None
if (
    _sys_api is not None
    and hasattr(_sys_api, "event")
    and getattr(sys.modules.get("astrbot.api.web"), "json_response", None) is not None
):
    # 合跑复用：既有 stub 链完整（含 astrbot.api.web 形状）才复用对象
    _astrbot = _sys_astrbot
    _astrbot_api = _sys_api
    _astrbot_event = _astrbot.api.event
    _astrbot_star = _astrbot.api.star
    _astrbot_mc = _astrbot.api.message_components
    _astrbot_web = sys.modules["astrbot.api.web"]
    _astrbot_core = _astrbot.core
    _astrbot_core_star = _astrbot.core.star
    _astrbot_core_star_filter = _astrbot.core.star.filter
    _astrbot_core_star_filter_command = _astrbot.core.star.filter.command
    _astrbot_core_utils = _astrbot.core.utils
    _astrbot_core_utils_io = _astrbot.core.utils.io
else:
    _astrbot = _new_module("astrbot")
    _astrbot_api = _new_module("astrbot.api")
    _astrbot_event = _new_module("astrbot.api.event")
    _astrbot_star = _new_module("astrbot.api.star")
    _astrbot_mc = _new_module("astrbot.api.message_components")
    _astrbot_web = _new_module("astrbot.api.web")
    _astrbot_core = _new_module("astrbot.core")
    _astrbot_core_star = _new_module("astrbot.core.star")
    _astrbot_core_star_filter = _new_module("astrbot.core.star.filter")
    _astrbot_core_star_filter_command = _new_module("astrbot.core.star.filter.command")
    _astrbot_core_utils = _new_module("astrbot.core.utils")
    _astrbot_core_utils_io = _new_module("astrbot.core.utils.io")


class _StubLogger:
    """记录各级日志（警告文案断言用）。"""

    def __init__(self):
        self.records = {"info": [], "warning": [], "error": [], "debug": []}

    def clear(self):
        for lines in self.records.values():
            lines.clear()

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


_STUB_LOGGER = _astrbot_api.logger if hasattr(_astrbot_api, "logger") else _StubLogger()
_astrbot_api.logger = _STUB_LOGGER
_astrbot_api.AstrBotConfig = dict

# ---- astrbot.api.web：webapi mixin 顶层 import（E 组按模块属性 patch 消费）----
_astrbot_web.error_response = lambda *a, **k: {"error": a[0] if a else ""}
_astrbot_web.json_response = lambda *a, **k: {"json": a[0] if a else None}
_astrbot_web.file_response = lambda *a, **k: {"file": a[0] if a else None}
_astrbot_web.request = None

# ---- astrbot.core：main.py 顶层 import GreedyStr ----


class _StubGreedyStr(str):
    """指令剩余文本参数标记（真实实现为 str 子类，stub 同形）。"""


_astrbot_core_star_filter_command.GreedyStr = _StubGreedyStr

# ---- astrbot.core.utils.io：webapi profile/summary mixin ----
_astrbot_core_utils_io.save_temp_img = lambda *a, **k: ""


class _StubFilter:
    EventMessageType = types.SimpleNamespace(GROUP_MESSAGE="group_message")
    PlatformAdapterType = types.SimpleNamespace(AIOCQHTTP="aiocqhttp")
    PermissionType = types.SimpleNamespace(ADMIN="admin")

    @staticmethod
    def _deco(fn):
        return fn

    event_message_type = staticmethod(lambda *a, **k: _StubFilter._deco)
    platform_adapter_type = staticmethod(lambda *a, **k: _StubFilter._deco)
    permission_type = staticmethod(lambda *a, **k: _StubFilter._deco)

    @staticmethod
    def command(*a, **k):
        return _StubFilter._deco


class _StubAstrMessageEvent:
    pass


class _StubMessageChain:
    def __init__(self, chain=None):
        self.chain = list(chain or [])


_astrbot_event.filter = _StubFilter
_astrbot_event.EventMessageType = _StubFilter.EventMessageType
_astrbot_event.PlatformAdapterType = _StubFilter.PlatformAdapterType
_astrbot_event.PermissionType = _StubFilter.PermissionType
_astrbot_event.AstrMessageEvent = _StubAstrMessageEvent
_astrbot_event.MessageChain = _StubMessageChain


class _StubContext:
    def __init__(self, *a, **k):
        pass


class _StubStar:
    def __init__(self, *a, **k):
        pass


def _stub_register(name, author, desc, version):
    def deco(cls):
        cls._registered = (name, author, desc, version)
        return cls

    return deco


class _StubStarTools:
    """StarTools 桩：get_data_dir 指到本测试目录 tmp_v090/data/<plugin>（自动建）。"""

    @classmethod
    def get_data_dir(cls, plugin_name=None):
        target = _TMP_DIR / "data" / (plugin_name or "default")
        target.mkdir(parents=True, exist_ok=True)
        return target


_astrbot_star.Context = _StubContext
_astrbot_star.Star = _StubStar
_astrbot_star.register = _stub_register
_astrbot_star.StarTools = _StubStarTools


class _StubPlain:
    def __init__(self, text=""):
        self.text = text


class _StubImage:
    def __init__(self, url=None, file=None):
        self.url = url
        self.file = file


class _StubAt:
    def __init__(self, qq="", name=""):
        self.qq = qq
        self.name = name


class _StubNodes:
    def __init__(self, nodes=None):
        self.nodes = nodes or []


class _StubNode:
    def __init__(self, uin="0", name="", content=None):
        self.uin = uin
        self.name = name
        self.content = content or []


_astrbot_mc.Plain = _StubPlain
_astrbot_mc.Image = _StubImage
_astrbot_mc.At = _StubAt
_astrbot_mc.AtAll = _StubAt
_astrbot_mc.Reply = _StubAt
_astrbot_mc.Nodes = _StubNodes
_astrbot_mc.Node = _StubNode

# ---- aiomysql：db_mysql / sqlite_migrator 顶层 import（不联网，测试注入假池）----
_sys_aiomysql = sys.modules.get("aiomysql")
if _sys_aiomysql is not None:
    _aiomysql = _sys_aiomysql
else:
    _aiomysql = _new_module("aiomysql")
    _aiomysql.connect = None
    _aiomysql.DictCursor = "DictCursor"
    _aiomysql.Connection = object
    _aiomysql.create_pool = None

# ---- 剔除非被测缓存（多测试文件合跑兼容）----
for _name in list(sys.modules):
    if _name == _PKG or _name.startswith(_PKG + "."):
        del sys.modules[_name]

sys.modules["astrbot"] = _astrbot
sys.modules["astrbot.api"] = _astrbot_api
sys.modules["astrbot.api.event"] = _astrbot_event
sys.modules["astrbot.api.star"] = _astrbot_star
sys.modules["astrbot.api.message_components"] = _astrbot_mc
sys.modules["astrbot.api.web"] = _astrbot_web
sys.modules["astrbot.core"] = _astrbot_core
sys.modules["astrbot.core.star"] = _astrbot_core_star
sys.modules["astrbot.core.star.filter"] = _astrbot_core_star_filter
sys.modules["astrbot.core.star.filter.command"] = _astrbot_core_star_filter_command
sys.modules["astrbot.core.utils"] = _astrbot_core_utils
sys.modules["astrbot.core.utils.io"] = _astrbot_core_utils_io
sys.modules["aiomysql"] = _aiomysql

# 父对象属性链补挂：合跑时其他测试文件的复用分支按 hasattr(_sys_astrbot, "api")
# 探测 stub（sys.modules 注入不会自动回写父属性），保证跨文件复用判定成立
_astrbot.api = _astrbot_api
_astrbot.core = _astrbot_core
_astrbot_api.event = _astrbot_event
_astrbot_api.star = _astrbot_star
_astrbot_api.message_components = _astrbot_mc
_astrbot_api.web = _astrbot_web
_core = _astrbot.core
_core.star = _astrbot_core_star
_core.utils = _astrbot_core_utils
_astrbot_core_star.filter = _astrbot_core_star_filter
_astrbot_core_star_filter.command = _astrbot_core_star_filter_command
_astrbot_core_utils.io = _astrbot_core_utils_io

if _PLUGINS_DIR not in sys.path:
    sys.path.insert(0, _PLUGINS_DIR)

# ============================================================
# 二、导入真实被测模块
# ============================================================

from astrbot_plugin_group_history_save_mysql.core.bootstrap import (  # noqa: E402
    _parse_bool_cfg,
    _resolve_backend,
)
from astrbot_plugin_group_history_save_mysql.core.commands import GroupCommands  # noqa: E402
from astrbot_plugin_group_history_save_mysql.core.db_sqlite import SQLiteManager  # noqa: E402
from astrbot_plugin_group_history_save_mysql.core.sqlite_migrator import (  # noqa: E402
    USAGE_TEXT,
    SQLiteMigrator,
)
from astrbot_plugin_group_history_save_mysql.core.webapi import WebAPI  # noqa: E402
from astrbot_plugin_group_history_save_mysql.core.webapi import (  # noqa: E402
    storage as webapi_storage_mod,  # E 组按模块属性 patch json/error_response 用
)

# ============================================================
# 三、共享辅助
# ============================================================

_SEQ = [0]


def _make_mgr(tag: str) -> SQLiteManager:
    """构造指向本目录 tmp_v090 唯一文件的 SQLiteManager（真实 aiosqlite）。"""
    _SEQ[0] += 1
    mgr = SQLiteManager(wal_mode=True, busy_timeout_ms=1000)
    # db_path 在构造期由 StarTools 桩确定；换成本用例专属文件名（隔离）
    mgr.db_path = str(_TMP_DIR / f"c_{tag}_{_SEQ[0]}.db")
    return mgr


def _msg(
    group_id,
    sender_id,
    content,
    message_id,
    ts=None,
    sender_name="张三",
    message_type="text",
    at_list="",
    reply_id="",
):
    return {
        "group_id": group_id,
        "sender_id": sender_id,
        "sender_name": sender_name,
        "message_type": message_type,
        "content": content,
        "message_id": message_id,
        "at_list": at_list,
        "reply_id": reply_id,
        "timestamp": ts,
    }


T_BASE = datetime(2026, 8, 19, 10, 0, 0)


def _ts(offset_seconds: int) -> datetime:
    return T_BASE + timedelta(seconds=offset_seconds)


# ============================================================
# A 组：_resolve_backend 启动锁纯函数
# （v0.9.0 调试轮两次订正：PROD-BUG-02 锁来源改 backend.lock 文件；
# PROD-BUG-03 语义修正——锁只属于 sqlite，mysql 安全态永不加锁，
# 锁文件内容仅 "sqlite" 有效，其余（含历史 "mysql" 值）一律未锁定）
# ============================================================


class TestResolveBackend(unittest.TestCase):
    def test_A1_unlocked_follows_config(self):
        """A-1 未锁定：缺省 mysql；显式 sqlite 按配置。"""
        self.assertEqual(_resolve_backend({}, ""), ("mysql", False, False, "mysql"))
        self.assertEqual(
            _resolve_backend({"storage_backend": "mysql"}, ""),
            ("mysql", False, False, "mysql"),
        )
        self.assertEqual(
            _resolve_backend({"storage_backend": "sqlite"}, ""),
            ("sqlite", False, False, "sqlite"),
        )

    def test_A2_invalid_config_falls_back_mysql(self):
        """A-2 非法值回退 mysql（不锁、不算 mismatch）。"""
        self.assertEqual(
            _resolve_backend({"storage_backend": "oracle"}, ""),
            ("mysql", False, False, "mysql"),
        )
        self.assertEqual(
            _resolve_backend({"storage_backend": ""}, ""),
            ("mysql", False, False, "mysql"),
        )
        self.assertEqual(
            _resolve_backend({"storage_backend": None}, ""),
            ("mysql", False, False, "mysql"),
        )

    def test_A3_locked_sqlite_consistent(self):
        """A-3 已锁定 sqlite 且配置一致：has_lock=True、mismatch=False。"""
        self.assertEqual(
            _resolve_backend({"storage_backend": "sqlite"}, "sqlite"),
            ("sqlite", False, True, "sqlite"),
        )

    def test_A4_locked_sqlite_mismatch_back_to_mysql(self):
        """A-4 锁 sqlite、配置改回 mysql：仍按锁 sqlite + mismatch=True。

        新语义下 mismatch 只有这一个方向（切离 sqlite 才是危险动作）。
        """
        self.assertEqual(
            _resolve_backend({"storage_backend": "mysql"}, "sqlite"),
            ("sqlite", True, True, "mysql"),
        )
        # 配置缺省 mysql 但锁 sqlite：同样按锁启动且判不一致
        self.assertEqual(
            _resolve_backend({}, "sqlite"),
            ("sqlite", True, True, "mysql"),
        )

    def test_A5_normalization(self):
        """A-5 归一化：大小写/混空格/空串锁/None 锁/垃圾锁值。"""
        # 大写混排输入归一
        self.assertEqual(
            _resolve_backend({"storage_backend": " SQLite "}, ""),
            ("sqlite", False, False, "sqlite"),
        )
        self.assertEqual(
            _resolve_backend({"storage_backend": "Sqlite"}, "SQLITE"),
            ("sqlite", False, True, "sqlite"),
        )
        # 锁内容为空串 / None → 视为未锁定
        self.assertEqual(
            _resolve_backend({"storage_backend": "sqlite"}, ""),
            ("sqlite", False, False, "sqlite"),
        )
        self.assertEqual(
            _resolve_backend({"storage_backend": "sqlite"}, None),
            ("sqlite", False, False, "sqlite"),
        )
        # 锁文件内容非法（历史脏数据）→ 等同未锁定，按配置走
        self.assertEqual(
            _resolve_backend({"storage_backend": "sqlite"}, "oracle"),
            ("sqlite", False, False, "sqlite"),
        )

    def test_A6_lock_source_is_file_not_config(self):
        """A-6（PROD-BUG-02 回归钉）：配置 dict 里塞 active_backend_lock 一律无效。

        框架 check_config_integrity 会剥离 schema 外键——锁唯一来源是
        backend.lock 文件（经 locked_raw 参数），旧配置字段不再参与判定。
        """
        self.assertEqual(
            _resolve_backend(
                {"storage_backend": "sqlite", "active_backend_lock": "mysql"}, ""
            ),
            ("sqlite", False, False, "sqlite"),
        )

    def test_A7_mysql_lock_value_is_legacy_unlocked(self):
        """A-7（PROD-BUG-03 语义修正钉）："mysql" 锁值不再锁定。

        初版语义全新安装自动跑 mysql 也会落锁，等于插件替用户做主、
        之后永远切不到 sqlite。修正版：锁只属于 sqlite，"mysql"（含历史
        实验版遗留文件）一律归未锁定，按配置自由切换。
        """
        self.assertEqual(
            _resolve_backend({"storage_backend": "mysql"}, "mysql"),
            ("mysql", False, False, "mysql"),
        )
        # 配置已被改成 sqlite：无有效锁 → 直接按 sqlite 走（正常启用路径）
        self.assertEqual(
            _resolve_backend({"storage_backend": "sqlite"}, "mysql"),
            ("sqlite", False, False, "sqlite"),
        )


# ============================================================
# B 组：_parse_bool_cfg
# ============================================================


class TestParseBoolCfg(unittest.TestCase):
    def test_B1_bool_passthrough(self):
        """B-1 bool 直返，不被字符串归一干扰。"""
        self.assertIs(_parse_bool_cfg(True), True)
        self.assertIs(_parse_bool_cfg(False), False)
        # False 是合法值，不得被 default=True 吞掉
        self.assertIs(_parse_bool_cfg(False, default=True), False)

    def test_B2_trueish_string(self):
        """B-2 "true"/"TRUE"/"1"/"yes"/"on" → True。"""
        for raw in ("true", "TRUE", " True ", "1", "yes", "on"):
            self.assertIs(_parse_bool_cfg(raw), True, raw)

    def test_B3_falseish_string(self):
        """B-3 "false"/"0"/"no"/"off" → False。"""
        for raw in ("false", "FALSE", " 0 ", "no", "off"):
            self.assertIs(_parse_bool_cfg(raw, default=True), False, raw)

    def test_B4_garbage_uses_default(self):
        """B-4 垃圾值/None/未定义字符串 → 按 default。"""
        self.assertIs(_parse_bool_cfg("garbage"), True)  # 默认 True
        self.assertIs(_parse_bool_cfg("garbage", default=False), False)
        self.assertIs(_parse_bool_cfg(None), True)
        self.assertIs(_parse_bool_cfg("", default=False), False)
        self.assertIs(_parse_bool_cfg("2", default=False), False)  # "1" 才是 True

    def test_B5_int_values(self):
        """B-5 int 1/0 经 str() 归一命中真/假路径。"""
        self.assertIs(_parse_bool_cfg(1), True)
        self.assertIs(_parse_bool_cfg(0, default=True), False)


# ============================================================
# C 组：SQLiteManager 关键契约（真实 aiosqlite 临时文件库）
# ============================================================


class TestSQLiteManagerContract(unittest.TestCase):
    @async_test
    async def test_C1_ping_contract(self):
        """C-1 ping()：顶层键与 MySQL 对齐 + pool 四键恒 1（commands 取值不炸）。"""
        mgr = _make_mgr("ping")
        self.assertTrue(await mgr.initialize())
        try:
            info = await mgr.ping()
            self.assertIs(info["connected"], True)
            self.assertIn("latency_ms", info)
            pool = info["pool"]
            for key in ("used", "current_size", "min_size", "max_size"):
                self.assertEqual(pool.get(key), 1, key)
            self.assertEqual(info["db"], mgr.db_path)
            # commands.py group_status 的取值路径逐字执行
            _ = (
                pool.get("used", 0),
                pool.get("current_size", 0),
                pool.get("min_size", 1),
                pool.get("max_size", 10),
            )
            stats = await mgr.get_stats()
            self.assertEqual(
                set(stats),
                {"today_messages", "today_images", "total_messages", "total_images"},
            )
        finally:
            await mgr.close()

    @async_test
    async def test_C2_empty_and_idempotent_message_id(self):
        """C-2 空 message_id 多条共存；非空同 id 二次插入幂等 True。"""
        mgr = _make_mgr("idem")
        self.assertTrue(await mgr.initialize())
        try:
            self.assertTrue(
                await mgr.insert_chat_message(
                    **_msg("111", "a1", "无ID一", "", ts=_ts(0))
                )
            )
            self.assertTrue(
                await mgr.insert_chat_message(
                    **_msg("111", "a1", "无ID二", "", ts=_ts(1))
                )
            )
            result = await mgr.query_messages(group_id="111")
            self.assertEqual(result["total"], 2, "空 message_id 互不去重")

            self.assertTrue(
                await mgr.insert_chat_message(
                    **_msg("111", "a2", "有ID", "m-x", ts=_ts(2))
                )
            )
            self.assertTrue(
                await mgr.insert_chat_message(
                    **_msg("111", "a2", "有ID", "m-x", ts=_ts(2))
                ),
                "重复 message_id 按已入库幂等返回 True",
            )
            result = await mgr.query_messages(group_id="111")
            self.assertEqual(result["total"], 3, "同 id 仅一行为止")
        finally:
            await mgr.close()

    @async_test
    async def test_C3_query_dict_shape_matches_mysql(self):
        """C-3 query_messages 记录键与 MySQL 侧 SELECT 列逐一对齐 + NULL 语义。"""
        mgr = _make_mgr("shape")
        self.assertTrue(await mgr.initialize())
        try:
            await mgr.insert_chat_message(
                **_msg(
                    "111", "a1", "hello", "m1", ts=_ts(0), at_list="9", reply_id="r0"
                )
            )
            await mgr.insert_chat_message(**_msg("111", "a1", "无ID", "", ts=_ts(1)))
            result = await mgr.query_messages(group_id="111")
            # 顶层键（MySQL 版同为 {"total","records"}——PRD 字面「三元组」与
            # 双后端实际契约不符，以代码事实为准，见 test_0.md 文档偏差项）
            self.assertEqual(set(result), {"total", "records"})
            rec_by_content = {r["content"]: r for r in result["records"]}
            rec_ids = rec_by_content["hello"]
            mysql_keys = {
                "id",
                "timestamp",
                "group_id",
                "sender_id",
                "sender_name",
                "message_type",
                "content",
                "message_id",
                "at_list",
                "reply_id",
            }
            self.assertEqual(set(rec_ids), mysql_keys)
            # timestamp：Unix 秒 → "YYYY-MM-DD HH:MM:SS" 字符串（与 MySQL str() 同构）
            self.assertIsInstance(rec_ids["timestamp"], str)
            datetime.strptime(rec_ids["timestamp"], "%Y-%m-%d %H:%M:%S")
            # 空 message_id 存 NULL → 读出 None（MySQL 侧空串列语义差异见头注）
            self.assertIsNone(rec_by_content["无ID"]["message_id"])
            # reply/at 透传
            self.assertEqual(rec_ids["at_list"], "9")
            self.assertEqual(rec_ids["reply_id"], "r0")
            # get_messages_by_ids 输出不含 id 键（与 MySQL 侧 SELECT 列表一致）
            rows = await mgr.get_messages_by_ids(["m1"])
            self.assertEqual(len(rows), 1)
            self.assertNotIn("id", rows[0])
            self.assertEqual(set(rows[0]), mysql_keys - {"id"})
        finally:
            await mgr.close()

    @async_test
    async def test_C4_existing_ids_chunk_over_500(self):
        """C-4 get_existing_message_ids 分块 >500 正常（单条 SQL 永不超 500 占位）。"""
        mgr = _make_mgr("chunk")
        self.assertTrue(await mgr.initialize())
        try:
            ids = [f"id{i:04d}" for i in range(600)]
            for i, mid in enumerate(ids):
                self.assertTrue(
                    await mgr.insert_chat_message(
                        **_msg("777", "s1", f"c{i}", mid, ts=_ts(i))
                    )
                )
            hit = await mgr.get_existing_message_ids("777", ids + ["ghost"])
            self.assertEqual(len(hit), 600)
            self.assertNotIn("ghost", hit)
            miss = await mgr.get_existing_message_ids("888", ids[:10])
            self.assertEqual(miss, set(), "group_id 不匹配时不得误报存在")
        finally:
            await mgr.close()

    @async_test
    async def test_C5_purge_meta_attrs(self):
        """C-5 purge_all 同构键 + 清空后可再插入；get_meta/set_meta 往返；属性面。"""
        mgr = _make_mgr("purge")
        self.assertTrue(mgr.is_sqlite_backend)
        self.assertTrue(str(mgr.db_path).endswith(".db"))
        self.assertTrue(await mgr.initialize())
        try:
            await mgr.insert_chat_message(**_msg("111", "a", "x", "p1", ts=_ts(0)))
            await mgr.insert_image_record(group_id="111", sender_id="a", image_url="u1")
            out = await mgr.purge_all()
            self.assertEqual(
                set(out), {"success", "deleted_messages", "deleted_images", "truncated"}
            )
            self.assertIs(out["success"], True)
            self.assertEqual(out["deleted_messages"], 1)
            self.assertEqual(out["deleted_images"], 1)
            self.assertIs(out["truncated"], False, "SQLite DELETE 路径恒 False")
            # 清空后自增复位 + 可再插入
            self.assertTrue(
                await mgr.insert_chat_message(**_msg("111", "a", "y", "p2", ts=_ts(1)))
            )
            stats = await mgr.get_stats()
            self.assertEqual(stats["total_messages"], 1)

            self.assertIsNone(await mgr.get_meta("no_such_key"))
            self.assertTrue(await mgr.set_meta("migrated_from_mysql", "2026-08-20"))
            self.assertEqual(await mgr.get_meta("migrated_from_mysql"), "2026-08-20")
            # UPSERT：覆盖写
            self.assertTrue(await mgr.set_meta("migrated_from_mysql", "2026-08-21"))
            self.assertEqual(await mgr.get_meta("migrated_from_mysql"), "2026-08-21")
        finally:
            await mgr.close()

    def test_C6_cfg_normalization_no_io(self):
        """C-6 busy_timeout/wal 非法值回退与夹取（构造期纯逻辑，无 I/O）。"""
        self.assertEqual(SQLiteManager(busy_timeout_ms="abc").busy_timeout_ms, 5000)
        self.assertEqual(SQLiteManager(busy_timeout_ms="1200").busy_timeout_ms, 1200)
        self.assertEqual(SQLiteManager(busy_timeout_ms="99999").busy_timeout_ms, 60000)
        self.assertEqual(SQLiteManager(busy_timeout_ms=-5).busy_timeout_ms, 0)
        self.assertEqual(SQLiteManager(busy_timeout_ms=None).busy_timeout_ms, 5000)
        self.assertIs(SQLiteManager(wal_mode="false").wal_mode, False)
        self.assertIs(SQLiteManager(wal_mode="TRUE").wal_mode, True)
        self.assertIs(SQLiteManager(wal_mode=0).wal_mode, False)


# ============================================================
# 四、迁移器假 MySQL 池基建
# ============================================================


def _chat_row(i, ts, gid, sid, name, mtype, content, mid, at="", reply=""):
    """chat_history 假行：列序与 SQLiteMigrator SELECT 一致。"""
    return (i, ts, gid, sid, name, mtype, content, mid, at, reply)


def _img_row(i, ts, gid, sid, name, url):
    return (i, ts, gid, sid, name, url)


class _MigCursor:
    def __init__(self, pool):
        self._pool = pool

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, sql, params=None):
        self._pool.queries.append((sql, tuple(params or ())))
        return 0

    async def fetchall(self):
        return await self._pool.fetch(
            self._pool.queries[-1][0], self._pool.queries[-1][1]
        )


class _MigConn:
    def __init__(self, pool):
        self._pool = pool

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def cursor(self):
        return _MigCursor(self._pool)


class _FakeMysqlPool:
    """按 SQL 路由的假 MySQL 池：支持 id 游标分页 + timestamp 窗口过滤。"""

    def __init__(
        self,
        chat_rows=None,
        image_rows=None,
        fail_chat=False,
        fail_image=False,
        gate: asyncio.Event | None = None,
    ):
        self.chat_rows = sorted(chat_rows or [], key=lambda r: r[0])
        self.image_rows = sorted(image_rows or [], key=lambda r: r[0])
        self.fail_chat = fail_chat
        self.fail_image = fail_image
        self.gate = gate  # 非 None：fetch chat 前 await（防重入用例卡点）
        self.conn = _MigConn(self)
        self.queries: list[tuple[str, tuple]] = []
        self.closed = False

    def acquire(self):
        return self.conn

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass

    async def fetch(self, sql: str, params: tuple) -> list:
        if "FROM chat_history" in sql:
            if self.gate is not None:
                await self.gate.wait()
            if self.fail_chat:
                raise RuntimeError("chat table boom")
            rows = self.chat_rows
        elif "FROM image_records" in sql:
            if self.fail_image:
                raise RuntimeError("image table boom")
            rows = self.image_rows
        else:
            raise AssertionError(f"假池收到未知 SQL: {sql[:80]}")
        last_id = params[0]
        window = params[1] if len(params) > 1 else None
        out = [r for r in rows if r[0] > last_id]
        if window is not None:
            out = [r for r in out if r[1] is not None and r[1] >= window]
        return out[:2000]


class _TestMigrator(SQLiteMigrator):
    """覆写 _create_mysql_pool 注入假池（离线测试专用）。"""

    def __init__(self, sqlite_mgr, config, pool, connect_error=None):
        super().__init__(sqlite_mgr, config)
        self._fake_pool = pool
        self._connect_error = connect_error

    async def _create_mysql_pool(self):
        if self._connect_error is not None:
            raise self._connect_error
        return self._fake_pool


# ============================================================
# D 组：SQLiteMigrator（假 MySQL 池 + 真实 SQLite 写入侧）
# ============================================================


class TestSQLiteMigrator(unittest.TestCase):
    def _mgr(self, tag):
        return _make_mgr(f"mig_{tag}")

    @async_test
    async def test_D1_full_import_meta_and_reblock(self):
        """D-1 全量导入计数正确 + set_meta 标记 + 重跑被标记拦截。"""
        mgr = self._mgr("d1")
        self.assertTrue(await mgr.initialize())
        try:
            chat = [
                _chat_row(1, _ts(0), "111", "a1", "张三", "text", "早", "m1"),
                _chat_row(2, _ts(10), "111", "a2", "李四", "text", "中", "m2", "5"),
                _chat_row(3, _ts(20), "222", "a1", "张三", "mixed", "晚", ""),
            ]
            img = [
                _img_row(1, _ts(0), "111", "a1", "张三", "http://i/1.jpg"),
                _img_row(2, _ts(30), "222", "a2", "李四", "http://i/2.jpg"),
            ]
            pool = _FakeMysqlPool(chat_rows=chat, image_rows=img)
            mig = _TestMigrator(mgr, {}, pool)
            text = await mig.migrate("")
            self.assertIn("迁移完成（全量）", text)
            self.assertIn("消息 3 条", text)
            self.assertIn("图片 2 条", text)
            self.assertIn("跳过重复 0 条", text)
            self.assertIn("耗时", text)
            # 落库核验
            self.assertEqual((await mgr.query_messages())["total"], 3)
            grp222 = await mgr.get_messages_by_ids(["m1", "m2"])
            self.assertEqual(len(grp222), 2)
            # meta 标记写入
            done_at = await mgr.get_meta("migrated_from_mysql")
            self.assertIsNotNone(done_at)
            # 重跑全量被拦截
            text2 = await mig.migrate("")
            self.assertIn("已完成过全量迁移", text2)
            self.assertIn("请指定小时数", text2)
            self.assertTrue(pool.closed, "迁移结束须关闭假池连接")
        finally:
            await mgr.close()

    @async_test
    async def test_D2_hours_validation_and_window(self):
        """D-2 hours 非法/≤0 → USAGE；合法 hours 透传窗口且**不**写全量标记。"""
        mgr = self._mgr("d2")
        self.assertTrue(await mgr.initialize())
        try:
            pool = _FakeMysqlPool(chat_rows=[], image_rows=[])
            mig = _TestMigrator(mgr, {}, pool)
            bad = await mig.migrate("abc")
            self.assertIn("小时数无效", bad)
            self.assertIn(USAGE_TEXT.splitlines()[0], bad)
            zero = await mig.migrate("0")
            self.assertIn("正整数", zero)
            neg = await mig.migrate("-3")
            self.assertIn("正整数", neg)

            # 合法窗口：仅窗口内行入库；不写 migrated_from_mysql
            chat = [
                _chat_row(1, _ts(0), "111", "a", "旧", "text", "旧", "old1"),
                _chat_row(
                    2,
                    datetime.now() - timedelta(hours=1),
                    "111",
                    "a",
                    "新",
                    "text",
                    "新",
                    "new1",
                ),
            ]
            pool2 = _FakeMysqlPool(chat_rows=chat, image_rows=[])
            mig2 = _TestMigrator(mgr, {}, pool2)
            text = await mig2.migrate("24")
            self.assertIn("迁移完成（近 24 小时）", text)
            self.assertIn("消息 1 条", text)
            self.assertIsNone(await mgr.get_meta("migrated_from_mysql"))
            # 窗口参数为 datetime 且量级正确（now-24h，容差 5 分钟）
            win = [p[1] for _, p in pool2.queries if len(p) > 1][0]
            self.assertIsInstance(win, datetime)
            self.assertTrue(
                timedelta(hours=23, minutes=55)
                < datetime.now() - win
                < timedelta(hours=24, minutes=5)
            )
        finally:
            await mgr.close()

    @async_test
    async def test_D3_reentrancy_guard(self):
        """D-3 防重入：迁移进行中再调 → 「迁移正在进行中」。"""
        mgr = self._mgr("d3")
        self.assertTrue(await mgr.initialize())
        try:
            gate = asyncio.Event()
            chat = [_chat_row(1, _ts(0), "111", "a", "x", "text", "x", "d3m1")]
            pool = _FakeMysqlPool(chat_rows=chat, image_rows=[], gate=gate)
            mig = _TestMigrator(mgr, {}, pool)
            first = asyncio.create_task(mig.migrate(""))
            # 等第一个迁移进入假池 fetch（锁已持有）
            for _ in range(200):
                if any("FROM chat_history" in sql for sql, _ in pool.queries):
                    break
                await asyncio.sleep(0.01)
            second = await mig.migrate("")
            self.assertIn("迁移正在进行中", second)
            gate.set()
            text = await first
            self.assertIn("迁移完成（全量）", text)
        finally:
            await mgr.close()

    @async_test
    async def test_D4_connect_error_and_batch_failure_keeps_data(self):
        """D-4 建连失败 → 「连接 MySQL 失败」；批次抛 → 「迁移失败」且已导入保留。"""
        mgr = self._mgr("d4")
        self.assertTrue(await mgr.initialize())
        try:
            # 建连失败
            pool = _FakeMysqlPool()
            mig_bad = _TestMigrator(
                mgr, {}, pool, connect_error=RuntimeError("refused")
            )
            text = await mig_bad.migrate("")
            self.assertIn("连接 MySQL 失败", text)
            self.assertIn("refused", text)

            # chat 成功 / image 抛：chat 已提交保留，meta 不写
            chat = [
                _chat_row(1, _ts(0), "111", "a", "一", "text", "一", "k1"),
                _chat_row(2, _ts(1), "111", "a", "二", "text", "二", "k2"),
            ]
            pool2 = _FakeMysqlPool(chat_rows=chat, image_rows=[], fail_image=True)
            mig2 = _TestMigrator(mgr, {}, pool2)
            text2 = await mig2.migrate("")
            self.assertIn("迁移失败", text2)
            self.assertIn("可重跑", text2)
            self.assertIn("image", text2)
            kept = await mgr.query_messages()
            self.assertEqual(kept["total"], 2, "失败前已导入的数据必须保留")
            self.assertIsNone(
                await mgr.get_meta("migrated_from_mysql"),
                "半途失败不得写全量标记（允许重跑）",
            )
            self.assertTrue(pool2.closed, "异常路径 finally 仍关闭池")
        finally:
            await mgr.close()

    @async_test
    async def test_D5_duplicate_rows(self):
        """D-5 重复行去重（PROD-BUG-01 修复后正确口径）。

        图片侧：预插同 URL → skipped 计数正确（按群预查生效）。
        消息侧：预插同 message_id 后迁移，按群分组预查（get_existing_message_ids
        恒群内查询契约）命中 → 重复消息计入「跳过」；数据不重复由 UNIQUE 索引兜底。
        """
        mgr = self._mgr("d5")
        self.assertTrue(await mgr.initialize())
        try:
            self.assertTrue(
                await mgr.insert_chat_message(
                    **_msg("111", "a1", "已存在", "dup1", ts=_ts(0))
                )
            )
            await mgr.insert_image_record(
                group_id="111", sender_id="a1", image_url="http://i/dup.jpg"
            )
            chat = [
                _chat_row(1, _ts(0), "111", "a1", "已存在", "text", "已存在", "dup1"),
                _chat_row(2, _ts(1), "111", "a1", "新增", "text", "新增", "fresh1"),
            ]
            img = [
                _img_row(1, _ts(0), "111", "a1", "已存在", "http://i/dup.jpg"),
                _img_row(2, _ts(1), "111", "a1", "新增", "http://i/new.jpg"),
            ]
            pool = _FakeMysqlPool(chat_rows=chat, image_rows=img)
            mig = _TestMigrator(mgr, {}, pool)
            text = await mig.migrate("")
            # 数据层：绝无重复（UNIQUE 索引 + 预查兜底）
            rows = await mgr.query_messages(group_id="111")
            self.assertEqual(rows["total"], 2, "dup1 + fresh1 各一行为止")
            urls = await mgr.get_existing_image_urls(
                "111", ["http://i/dup.jpg", "http://i/new.jpg"]
            )
            self.assertEqual(len(urls), 2)
            # 计数文本（PROD-BUG-01 修复后）：消息/图片各 1 条重复被按群预查命中
            # → inserted 各 1、skipped 合计 2
            self.assertIn("消息 1 条", text)
            self.assertIn("图片 1 条", text)
            self.assertIn("跳过重复 2 条", text)
        finally:
            await mgr.close()

    @async_test
    async def test_D6_dirty_timestamp_row_coerced(self):
        """D-6 MySQL 行 timestamp 非 datetime（脏值/NULL）→ 按当前时间入库不抛。"""
        mgr = self._mgr("d6")
        self.assertTrue(await mgr.initialize())
        try:
            chat = [
                _chat_row(1, None, "111", "a", "脏时间", "text", "脏时间", "dt1"),
                _chat_row(2, _ts(0), "111", "a", "正常", "text", "正常", "dt2"),
            ]
            mig = _TestMigrator(mgr, {}, _FakeMysqlPool(chat_rows=chat, image_rows=[]))
            text = await mig.migrate("")
            self.assertIn("迁移完成", text)
            self.assertIn("消息 2 条", text)
            rows = await mgr.query_messages(group_id="111")
            self.assertEqual(rows["total"], 2)
        finally:
            await mgr.close()


# ============================================================
# E 组：WebAPI api_storage_info + 路由表
# ============================================================


class _FakeWebContext:
    """记录 register_web_api 调用的最小 Context。"""

    def __init__(self):
        self.registered: list[tuple] = []

    def register_web_api(self, route, handler, methods, desc):
        self.registered.append((route, handler, methods, desc))


class TestStorageInfoAPI(unittest.TestCase):
    def _patch_responses(self):
        """patch webapi.storage 模块绑定的 json_response/error_response。

        模块顶层 from astrbot.api.web import 绑定的是导入时对象，且合跑时
        可能是其他测试文件的旧形状 stub——这里直接换成捕获型替身，与 stub
        形状解耦。返回还原函数。
        """
        orig_json = webapi_storage_mod.json_response
        orig_err = webapi_storage_mod.error_response

        def json_cap(payload, *a, **k):
            return {"__json__": payload}

        def err_cap(msg, *a, **k):
            return {"__error__": msg, "status_code": k.get("status_code")}

        webapi_storage_mod.json_response = json_cap
        webapi_storage_mod.error_response = err_cap

        def restore():
            webapi_storage_mod.json_response = orig_json
            webapi_storage_mod.error_response = orig_err

        return restore

    @async_test
    async def test_E1_provider_none_503(self):
        """E-1 provider 未注入 → error_response 503。"""
        restore = self._patch_responses()
        try:
            api = WebAPI(_FakeWebContext(), None, None, None)
            resp = await api.api_storage_info()
            self.assertEqual(resp["__error__"], "存储信息不可用")
            self.assertEqual(resp["status_code"], 503)
        finally:
            restore()

    @async_test
    async def test_E2_provider_ok_passthrough(self):
        """E-2 async provider 返回 dict → json_response 原样透传。"""
        restore = self._patch_responses()
        try:
            payload = {
                "backend": "sqlite",
                "locked": True,
                "config_backend": "sqlite",
                "lock_mismatch": False,
                "history_db_path": "/x/history.db",
                "stats_available": False,
            }

            async def provider():
                return payload

            api = WebAPI(
                _FakeWebContext(), None, None, None, storage_info_provider=provider
            )
            resp = await api.api_storage_info()
            self.assertEqual(resp["__json__"], payload)
        finally:
            restore()

    @async_test
    async def test_E3_provider_exception_500(self):
        """E-3 provider 抛异常 → error_response 500，绝不向 Web 层冒泡。"""
        restore = self._patch_responses()
        try:

            async def boom():
                raise RuntimeError("provider down")

            api = WebAPI(
                _FakeWebContext(), None, None, None, storage_info_provider=boom
            )
            resp = await api.api_storage_info()
            self.assertEqual(resp["__error__"], "获取存储信息失败")
            self.assertEqual(resp["status_code"], 500)
        finally:
            restore()

    def test_E4_route_registered(self):
        """E-4 路由表：storage/info 注册（GET）+ 总路由数 43（v0.9.0 +1）。"""
        context = _FakeWebContext()
        WebAPI(context, None, None, None)
        routes = [r[0] for r in context.registered]
        prefix = "/astrbot_plugin_group_history_save_mysql"
        self.assertEqual(len(routes), 43)
        self.assertIn(prefix + "/storage/info", routes)
        entry = next(r for r in context.registered if r[0] == prefix + "/storage/info")
        self.assertEqual(entry[2], ["GET"])
        self.assertTrue(callable(getattr(WebAPI, "api_storage_info", None)))


# ============================================================
# F 组：GroupCommands.group_migrate 薄壳
# ============================================================


class _FakeMigratorObj:
    def __init__(self, error=None, result="迁移完成（全量）：ok"):
        self.calls: list[str] = []
        self.error = error
        self.result = result

    async def migrate(self, hours=""):
        self.calls.append(hours)
        if self.error is not None:
            raise self.error
        return self.result


class TestGroupMigrateCommand(unittest.TestCase):
    def _commands(self, migrator):
        return GroupCommands(None, None, None, None, migrator=migrator)

    @async_test
    async def test_F1_no_migrator_replies_noop(self):
        """F-1 MySQL 模式（migrator None）→ 「当前为 MySQL 存储模式，无需导入。」"""
        out = await self._commands(None).group_migrate(None, "")
        self.assertEqual(out, "当前为 MySQL 存储模式，无需导入。")

    @async_test
    async def test_F2_forwards_hours(self):
        """F-2 假 migrator：hours 原样转发、结果透传。"""
        fake = _FakeMigratorObj()
        out = await self._commands(fake).group_migrate(None, "24")
        self.assertEqual(fake.calls, ["24"])
        self.assertEqual(out, fake.result)

    @async_test
    async def test_F3_exception_contained(self):
        """F-3 migrator 抛异常 → 「迁移异常」文案返回，不向 handler 冒泡。"""
        fake = _FakeMigratorObj(error=RuntimeError("kapow"))
        out = await self._commands(fake).group_migrate(None, "")
        self.assertTrue(out.startswith("迁移异常"), out)
        self.assertIn("kapow", out)

    @async_test
    async def test_F4_group_status_sqlite_branch(self):
        """F-4（契约联动）：sqlite mgr 下 group_status 走 SQLite 文案、不读 pool。

        用 ping 无 pool 键的假 mgr 验证 commands.py 的 is_sqlite_backend 分支
        先行短路（真实 SQLiteManager 的 pool 占位键另有 C-1 覆盖）。
        """

        class _SqliteLikeMgr:
            is_sqlite_backend = True

            async def ping(self):
                return {"connected": True, "latency_ms": 1, "db": "/x/history.db"}

            async def get_stats(self):
                return {
                    "today_messages": 0,
                    "today_images": 0,
                    "total_messages": 0,
                    "total_images": 0,
                }

        class _Cfg:
            async def get_groups(self):
                return []

            async def get_all_settings(self):
                return {"all_mode": "false", "image_retention_days": "3"}

        text = await GroupCommands(_SqliteLikeMgr(), _Cfg(), None, None).group_status(
            None
        )
        self.assertIn("存储后端: SQLite", text)
        self.assertNotIn("连接池:", text)


# ============================================================
# G 组：前端 / 配置 / 元数据 静态断言（文本级）
# ============================================================


class TestFrontendAndAssets(unittest.TestCase):
    def _read(self, *parts) -> str:
        return (_PLUGIN_ROOT.joinpath(*parts)).read_text(encoding="utf-8")

    def test_G1_index_html(self):
        """G-1 横幅容器 + ?v=0.9.0 ×3 + 旧版本残留清零。"""
        html = self._read("pages", "dashboard", "index.html")
        self.assertIn('id="storageBanner"', html)
        self.assertEqual(html.count("?v=0.9.0"), 3, "style.css/app.js/data-analysis.js")
        for stale in ("?v=0.5.", "?v=0.6.", "?v=0.7.", "?v=0.8."):
            self.assertNotIn(stale, html, f"残留 {stale}")

    def test_G2_app_js_symbols(self):
        """G-2 app.js：storage/info 拉取 + 横幅渲染 + 数据分析拦截符号。"""
        js = self._read("pages", "dashboard", "app.js")
        for sym in (
            'apiGet("storage/info")',
            "renderStorageBanner",
            "applySqlitePurgeHints",
            "storageBanner",
        ):
            self.assertIn(sym, js, sym)
        self.assertIn("SQLite 备用存储", js)
        self.assertIn("数据分析在 SQLite 存储模式下不可用", js)

    def test_G3_conf_schema_contract(self):
        """G-3 _conf_schema.json：合法 JSON + v0.9.0 三项契约。"""
        schema = json.loads(self._read("_conf_schema.json"))
        backend = schema["storage_backend"]
        self.assertEqual(backend["options"], ["mysql", "sqlite"])
        self.assertEqual(backend["default"], "mysql")
        self.assertEqual(backend["type"], "string")
        self.assertIn("危险选项", backend["hint"])
        self.assertIn("锁定", backend["hint"])
        self.assertIn("重启", backend["hint"])
        wal = schema["sqlite_wal_mode"]
        self.assertEqual(wal["type"], "bool")
        self.assertIs(wal["default"], True)
        busy = schema["sqlite_busy_timeout_ms"]
        self.assertEqual(busy["type"], "int")
        self.assertEqual(busy["default"], 5000)
        self.assertIn("0-60000", busy["hint"])

    def test_G4_metadata_and_main_version(self):
        """G-4 版本与接线：metadata v0.9.0 / main 0.9.0+指令+不可用文案 / bootstrap 锁文案。"""
        meta = self._read("metadata.yaml")
        self.assertIn("version: v0.9.0", meta)
        main_src = self._read("main.py")
        self.assertIn('"0.9.0"', main_src)
        self.assertIn('filter.command("导出聊天记录")', main_src)
        self.assertIn("数据分析模块在 SQLite 存储模式下不可用", main_src)
        bootstrap_src = self._read("core", "bootstrap.py")
        # v0.9.0 调试轮：锁落 backend.lock 文件而非配置实体（PROD-BUG-02）
        self.assertIn("backend.lock", bootstrap_src)
        self.assertNotIn("cfg[\"active_backend_lock\"]", bootstrap_src)
        self.assertIn("检测到存储后端配置", bootstrap_src)
        self.assertIn("storage_info_provider=self._storage_info", bootstrap_src)
        webapi_src = self._read("core", "webapi", "base.py")
        self.assertIn("storage/info", webapi_src)


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    unittest.main(verbosity=2)
