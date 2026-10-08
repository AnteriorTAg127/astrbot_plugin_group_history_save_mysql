# ruff: noqa: I001
"""astrbot_plugin_group_history_save_mysql v0.4.0 Module I 离线单元测试。

覆盖「人物分析」持久化与清理（临时目录 + 构造 ProfileResult，不依赖 AstrBot）：

- profile/storage.py（ProfileStorage）：
  - save：group/all 双 scope 目录落盘、确定性文件名（created_at + sender_id）、
    同秒碰撞递增后缀、created_at 为空自动填充、非法 scope/群号 ValueError、
    磁盘错误返回空串、JSON 全量序列化；
  - list_profiles：摘要字段齐全、created_at 倒序、分页、坏文件跳过；
  - read：往返一致、不存在静默 None、路径穿越（../ / 绝对路径 / 非法 scope
    目录 / 坏文件名）拦截；
  - delete：删除成功/重复删除/穿越拦截；
  - cleanup：按 mtime 过期删除、保留未过期、空 scope 目录移除、目录不存在 0。
- profile/scheduler.py（ProfileCleanupScheduler）：
  - start 即跑一次（mock storage.cleanup 被调用 + 配置值透传）；
  - start/stop 幂等（重复 start 忽略、未启动 stop no-op）；
  - 配置非法回退 30 天；失败退避重试后成功。

运行方式（插件根目录，二者皆可）：
    python -m pytest "tests/v0.4.0/test_profile_storage.py" -v
    python "tests/v0.4.0/test_profile_storage.py"

范式沿用 tests/v0.4.0/test_models_capture.py：astrbot.* 全部 sys.modules
stub，且 stub 必须在 import 被测包之前完成；异步用例由 @async_test
（asyncio.run）驱动，不依赖 pytest-asyncio。
"""

import asyncio
import functools
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path


# ============================================================
# 一、stub 注入（必须在任何 from astrbot_plugin_group_history_save_mysql... import 之前）
# ============================================================


class _StubLogger:
    """记录各级日志内容，便于断言（如穿越拦截 warning）。"""

    def __init__(self):
        self.records = {"info": [], "warning": [], "error": [], "debug": []}

    def info(self, msg, *args, **kwargs):
        self.records["info"].append(str(msg))

    def warning(self, msg, *args, **kwargs):
        self.records["warning"].append(str(msg))

    def error(self, msg, *args, **kwargs):
        self.records["error"].append(str(msg))

    def debug(self, msg, *args, **kwargs):
        self.records["debug"].append(str(msg))

    def joined(self, level: str) -> str:
        return "\n".join(self.records[level])


_STUB_LOGGER = _StubLogger()

_astrbot = types.ModuleType("astrbot")
_astrbot_api = types.ModuleType("astrbot.api")
_astrbot_api.logger = _STUB_LOGGER

# ---- 多测试文件合跑兼容：仅剔除 profile 子模块缓存（切勿删除 summary.* 兄弟子包，
#      误删会触发二次导入产生重复类对象，破坏其他测试文件的 isinstance 断言） ----
for _name in list(sys.modules):
    if _name.startswith("astrbot_plugin_group_history_save_mysql.core.profile"):
        del sys.modules[_name]

sys.modules["astrbot"] = _astrbot
sys.modules["astrbot.api"] = _astrbot_api

# 让被测包可被导入：<plugins 目录> 加入 sys.path
_PLUGINS_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..")
)
if _PLUGINS_DIR not in sys.path:
    sys.path.insert(0, _PLUGINS_DIR)


# ============================================================
# 二、导入被测代码（stub 已就位）
# ============================================================

from astrbot_plugin_group_history_save_mysql.core.profile import scheduler as sched_mod  # noqa: E402
from astrbot_plugin_group_history_save_mysql.core.profile.models import (  # noqa: E402
    ProfileResult,
    ProfileStats,
    ProfileTarget,
)
from astrbot_plugin_group_history_save_mysql.core.profile.scheduler import (  # noqa: E402
    ProfileCleanupScheduler,
)
from astrbot_plugin_group_history_save_mysql.core.profile.storage import (  # noqa: E402
    ProfileStorage,
)


def async_test(fn):
    """装饰器：用 asyncio.run 运行异步测试函数（环境无 pytest-asyncio 依赖）。"""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


# ============================================================
# 三、构造辅助
# ============================================================


def make_stats(**overrides) -> ProfileStats:
    """构造一个字段完整的 ProfileStats（可覆写）。"""
    base = {
        "total": 42,
        "group_count": 2,
        "group_breakdown": [("987654", 30), ("111222", 12)],
        "time_start": datetime(2026, 7, 1, 8, 0, 0),
        "time_end": datetime(2026, 8, 1, 23, 30, 0),
        "active_days": 15,
        "hour_dist": list(range(24)),
        "weekday_dist": [1, 2, 3, 4, 5, 6, 7],
        "peak_hour": 23,
        "peak_weekday": 5,
        "avg_length": 12.5,
        "total_chars": 525,
        "emoji_ratio": 0.1,
        "question_ratio": 0.2,
        "top_partners": [("777", "李四", 9), ("888", "王五", 4)],
        "truncated": False,
    }
    base.update(overrides)
    return ProfileStats(**base)


def make_result(
    scope: str = "group",
    group_id: str = "987654",
    sender_id: str = "123456",
    sender_name: str = "张三",
    created_at: str = "2026-08-02 10:30:45",
    **overrides,
) -> ProfileResult:
    """构造一个字段完整的 ProfileResult（可覆写）。"""
    target = ProfileTarget(
        sender_id=sender_id,
        sender_name=sender_name,
        scope=scope,
        group_id=group_id if scope == "group" else "",
    )
    base = {
        "target": target,
        "stats": make_stats(),
        "sections": [("发言习惯", "内容A"), ("性格分析", "内容B")],
        "raw_llm_text": "原始 LLM 输出",
        "provider_id": "provider-x",
        "messages_used": 40,
        "sources": {"mysql": 30, "onebot": 10},
        "relation_context_complete": True,
        "scope_desc": "某群" if scope == "group" else "全局 2 个群",
        "created_at": created_at,
    }
    base.update(overrides)
    return ProfileResult(**base)


class _TmpDirMixin:
    """每个用例独立临时目录（setUp 创建，tearDown 清理）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="profile_storage_test_")
        self.base_dir = Path(self._tmp.name)
        self.storage = ProfileStorage(self.base_dir)

    def tearDown(self):
        self._tmp.cleanup()


# ============================================================
# 四、ProfileStorage — save / read
# ============================================================


class TestProfileStorageSaveRead(_TmpDirMixin, unittest.TestCase):
    def test_save_group_scope_relname_and_file_exists(self):
        @async_test
        async def _run():
            rel = await self.storage.save(make_result())
            self.assertEqual(rel, "group_987654/20260802_103045_123456.json")
            path = self.base_dir / rel
            self.assertTrue(path.is_file())

        _run()

    def test_save_all_scope_dir(self):
        @async_test
        async def _run():
            rel = await self.storage.save(make_result(scope="all"))
            self.assertTrue(rel.startswith("all/"))
            self.assertEqual(rel, "all/20260802_103045_123456.json")
            self.assertTrue((self.base_dir / rel).is_file())

        _run()

    def test_save_fills_created_at_when_empty(self):
        @async_test
        async def _run():
            result = make_result(created_at="")
            rel = await self.storage.save(result)
            self.assertNotEqual(result.created_at, "")  # 回填当前时间
            # 文件名仍符合白名单格式
            self.assertRegex(rel, r"^group_987654/\d{8}_\d{6}_123456\.json$")

        _run()

    def test_save_collision_appends_suffix(self):
        @async_test
        async def _run():
            r1 = await self.storage.save(make_result())
            r2 = await self.storage.save(make_result())  # 同秒同目标
            r3 = await self.storage.save(make_result())
            self.assertEqual(r1, "group_987654/20260802_103045_123456.json")
            self.assertEqual(r2, "group_987654/20260802_103045_123456_2.json")
            self.assertEqual(r3, "group_987654/20260802_103045_123456_3.json")
            for rel in (r1, r2, r3):
                self.assertTrue((self.base_dir / rel).is_file())

        _run()

    def test_save_invalid_group_raises_value_error(self):
        @async_test
        async def _run():
            with self.assertRaises(ValueError):
                await self.storage.save(make_result(group_id="../evil"))
            with self.assertRaises(ValueError):
                await self.storage.save(make_result(scope="weird"))

        _run()

    def test_save_disk_error_returns_empty(self):
        @async_test
        async def _run():
            # base_dir 指向「文件之下的路径」→ mkdir 必抛 OSError
            blocker = self.base_dir / "blocker_file"
            blocker.write_text("x", encoding="utf-8")
            bad_storage = ProfileStorage(blocker / "profiles")
            _STUB_LOGGER.records["warning"].clear()
            rel = await bad_storage.save(make_result())
            self.assertEqual(rel, "")
            self.assertIn("分析结果保存失败", _STUB_LOGGER.joined("warning"))

        _run()

    def test_read_roundtrip_full_payload(self):
        @async_test
        async def _run():
            rel = await self.storage.save(make_result())
            data = await self.storage.read(rel)
            self.assertIsInstance(data, dict)
            # target
            self.assertEqual(data["target"]["sender_id"], "123456")
            self.assertEqual(data["target"]["sender_name"], "张三")
            self.assertEqual(data["target"]["scope"], "group")
            self.assertEqual(data["target"]["group_id"], "987654")
            # stats（datetime → 字符串、tuple → list）
            stats = data["stats"]
            self.assertEqual(stats["total"], 42)
            self.assertEqual(stats["time_start"], "2026-07-01 08:00:00")
            self.assertEqual(stats["time_end"], "2026-08-01 23:30:00")
            self.assertEqual(stats["group_breakdown"], [["987654", 30], ["111222", 12]])
            self.assertEqual(
                stats["top_partners"], [["777", "李四", 9], ["888", "王五", 4]]
            )
            self.assertEqual(stats["hour_dist"], list(range(24)))
            self.assertEqual(stats["weekday_dist"], [1, 2, 3, 4, 5, 6, 7])
            self.assertEqual(stats["peak_hour"], 23)
            self.assertEqual(stats["avg_length"], 12.5)
            self.assertFalse(stats["truncated"])
            # 顶层元数据
            self.assertEqual(
                data["sections"], [["发言习惯", "内容A"], ["性格分析", "内容B"]]
            )
            self.assertEqual(data["raw_llm_text"], "原始 LLM 输出")
            self.assertEqual(data["provider_id"], "provider-x")
            self.assertEqual(data["messages_used"], 40)
            self.assertEqual(data["sources"], {"mysql": 30, "onebot": 10})
            self.assertTrue(data["relation_context_complete"])
            self.assertEqual(data["scope_desc"], "某群")
            self.assertEqual(data["created_at"], "2026-08-02 10:30:45")

        _run()

    def test_read_stats_none_times_serialized_null(self):
        @async_test
        async def _run():
            rel = await self.storage.save(
                make_result(stats=make_stats(time_start=None, time_end=None))
            )
            data = await self.storage.read(rel)
            self.assertIsNone(data["stats"]["time_start"])
            self.assertIsNone(data["stats"]["time_end"])

        _run()

    def test_read_missing_returns_none(self):
        @async_test
        async def _run():
            self.assertIsNone(
                await self.storage.read("group_987654/20260802_103045_1.json")
            )

        _run()

    def test_read_traversal_rejected(self):
        @async_test
        async def _run():
            # 先在 base_dir 之外放一个诱饵文件
            outside = self.base_dir.parent / "secret.json"
            outside.write_text('{"secret": true}', encoding="utf-8")
            try:
                _STUB_LOGGER.records["warning"].clear()
                attacks = [
                    "../secret.json",  # 缺 scope 目录 + 穿越
                    "group_987654/../../secret.json",  # 目录穿越
                    "all/../secret.json",
                    str(outside),  # 绝对路径
                    "group_abc/20260802_103045_1.json",  # 非法 scope 目录
                    "group_987654/evil name.json",  # 非法文件名
                    "group_987654/..%2f..%2fsecret.json",
                ]
                for name in attacks:
                    self.assertIsNone(await self.storage.read(name), name)
                self.assertIn("拒绝非法文件名访问", _STUB_LOGGER.joined("warning"))
            finally:
                outside.unlink(missing_ok=True)

        _run()

    def test_save_sender_id_sanitized(self):
        @async_test
        async def _run():
            rel = await self.storage.save(make_result(sender_id="12ab34"))
            self.assertEqual(rel, "group_987654/20260802_103045_1234.json")

        _run()


# ============================================================
# 五、ProfileStorage — list_profiles
# ============================================================


class TestProfileStorageList(_TmpDirMixin, unittest.TestCase):
    def test_list_fields_order_and_pagination(self):
        @async_test
        async def _run():
            await self.storage.save(
                make_result(sender_id="1", created_at="2026-08-01 10:00:00")
            )
            await self.storage.save(
                make_result(sender_id="2", created_at="2026-08-03 10:00:00")
            )
            await self.storage.save(
                make_result(
                    scope="all", sender_id="3", created_at="2026-08-02 10:00:00"
                )
            )

            page1 = await self.storage.list_profiles(page=1, page_size=2)
            self.assertEqual(page1["total"], 3)
            self.assertEqual(page1["page"], 1)
            self.assertEqual(page1["page_size"], 2)
            self.assertEqual(len(page1["profiles"]), 2)
            # 倒序：最新在前
            self.assertEqual(page1["profiles"][0]["sender_id"], "2")
            self.assertEqual(page1["profiles"][1]["sender_id"], "3")

            # 摘要字段齐全
            item = page1["profiles"][0]
            for key in (
                "filename",
                "target_name",
                "sender_id",
                "scope",
                "scope_desc",
                "created_at",
                "provider_id",
                "total",
            ):
                self.assertIn(key, item)
            self.assertEqual(item["target_name"], "张三")
            self.assertEqual(item["scope"], "group")
            self.assertEqual(item["provider_id"], "provider-x")
            self.assertEqual(item["total"], 42)
            self.assertTrue(item["filename"].startswith("group_987654/"))

            # 第二页 + 越界页
            page2 = await self.storage.list_profiles(page=2, page_size=2)
            self.assertEqual([p["sender_id"] for p in page2["profiles"]], ["1"])
            page3 = await self.storage.list_profiles(page=3, page_size=2)
            self.assertEqual(page3["profiles"], [])
            self.assertEqual(page3["total"], 3)

            # page < 1 归一
            page0 = await self.storage.list_profiles(page=0, page_size=20)
            self.assertEqual(page0["page"], 1)
            self.assertEqual(len(page0["profiles"]), 3)

        _run()

    def test_list_skips_bad_files(self):
        @async_test
        async def _run():
            await self.storage.save(make_result())
            gdir = self.base_dir / "group_5"
            gdir.mkdir(parents=True, exist_ok=True)
            # 合法命名但内容损坏 → warning 跳过
            (gdir / "20260101_000000_9.json").write_text("{bad json", encoding="utf-8")
            # 非法命名 → 静默忽略
            (gdir / "random.txt").write_text("x", encoding="utf-8")
            # 非法 scope 目录 → 忽略
            (self.base_dir / "group_x").mkdir()
            (self.base_dir / "group_x" / "20260101_000000_1.json").write_text(
                "{}", encoding="utf-8"
            )

            _STUB_LOGGER.records["warning"].clear()
            result = await self.storage.list_profiles()
            self.assertEqual(result["total"], 1)  # 仅合法的一条
            self.assertIn("读取/解析失败", _STUB_LOGGER.joined("warning"))

        _run()

    def test_list_empty_base_dir(self):
        @async_test
        async def _run():
            result = await self.storage.list_profiles()
            self.assertEqual(result["total"], 0)
            self.assertEqual(result["profiles"], [])

        _run()


# ============================================================
# 六、ProfileStorage — delete
# ============================================================


class TestProfileStorageDelete(_TmpDirMixin, unittest.TestCase):
    def test_delete_existing_then_missing(self):
        @async_test
        async def _run():
            rel = await self.storage.save(make_result())
            self.assertTrue(await self.storage.delete(rel))
            self.assertFalse((self.base_dir / rel).exists())
            # scope 目录清空后一并移除
            self.assertFalse((self.base_dir / "group_987654").exists())
            # 重复删除 → False
            self.assertFalse(await self.storage.delete(rel))

        _run()

    def test_delete_traversal_rejected(self):
        @async_test
        async def _run():
            outside = self.base_dir.parent / "victim.json"
            outside.write_text("{}", encoding="utf-8")
            try:
                self.assertFalse(await self.storage.delete("../victim.json"))
                self.assertFalse(await self.storage.delete("group_1/../../victim.json"))
                self.assertTrue(outside.exists())  # 诱饵未被删
            finally:
                outside.unlink(missing_ok=True)

        _run()


# ============================================================
# 七、ProfileStorage — cleanup
# ============================================================


class TestProfileStorageCleanup(_TmpDirMixin, unittest.TestCase):
    def test_cleanup_expired_by_mtime(self):
        @async_test
        async def _run():
            old_rel = await self.storage.save(
                make_result(sender_id="1", created_at="2026-01-01 00:00:00")
            )
            new_rel = await self.storage.save(
                make_result(sender_id="2", created_at="2026-08-01 00:00:00")
            )
            # 把第一个文件的 mtime 倒拨 40 天
            old_path = self.base_dir / old_rel
            old_mtime = datetime.now().timestamp() - 40 * 86400
            os.utime(old_path, (old_mtime, old_mtime))

            deleted = await self.storage.cleanup(30)
            self.assertEqual(deleted, 1)
            self.assertFalse(old_path.exists())
            self.assertTrue((self.base_dir / new_rel).is_file())

            # 再跑一次 → 无可删
            self.assertEqual(await self.storage.cleanup(30), 0)

        _run()

    def test_cleanup_removes_emptied_scope_dir(self):
        @async_test
        async def _run():
            rel = await self.storage.save(make_result())
            path = self.base_dir / rel
            old_mtime = datetime.now().timestamp() - 100 * 86400
            os.utime(path, (old_mtime, old_mtime))
            self.assertEqual(await self.storage.cleanup(30), 1)
            self.assertFalse((self.base_dir / "group_987654").exists())

        _run()

    def test_cleanup_missing_base_dir_returns_zero(self):
        @async_test
        async def _run():
            storage = ProfileStorage(self.base_dir / "not_exist")
            self.assertEqual(await storage.cleanup(30), 0)

        _run()

    def test_cleanup_ignores_foreign_dirs(self):
        @async_test
        async def _run():
            rel = await self.storage.save(make_result())
            path = self.base_dir / rel
            old_mtime = datetime.now().timestamp() - 100 * 86400
            os.utime(path, (old_mtime, old_mtime))
            # 外部目录（非白名单命名）内的文件不被触碰
            foreign = self.base_dir / "not_a_scope"
            foreign.mkdir()
            (foreign / "a.json").write_text("{}", encoding="utf-8")
            self.assertEqual(await self.storage.cleanup(30), 1)
            self.assertTrue((foreign / "a.json").exists())

        _run()


# ============================================================
# 八、ProfileCleanupScheduler
# ============================================================


class _FakeStorage:
    """记录 cleanup 调用的假存储器；可配置前 N 次抛异常。"""

    def __init__(self, fail_times: int = 0):
        self.calls: list[int] = []
        self._fail_left = fail_times

    async def cleanup(self, keep_days: int) -> int:
        if self._fail_left > 0:
            self._fail_left -= 1
            raise RuntimeError("boom")
        self.calls.append(keep_days)
        return 0


class _FakeConfigMgr:
    def __init__(self, keep_days=15):
        self._keep_days = keep_days

    async def get_profile_setting_typed(self, key: str):
        assert key == "profile_keep_days"
        return self._keep_days


class TestProfileCleanupScheduler(unittest.TestCase):
    def setUp(self):
        self._orig_interval = sched_mod._CLEANUP_INTERVAL
        # 缩短循环间隔便于测试多轮
        sched_mod._CLEANUP_INTERVAL = 0.02

    def tearDown(self):
        sched_mod._CLEANUP_INTERVAL = self._orig_interval

    def test_start_runs_cleanup_with_config_days_and_stop(self):
        @async_test
        async def _run():
            storage = _FakeStorage()
            sch = ProfileCleanupScheduler(storage, _FakeConfigMgr(keep_days=15))
            await sch.start()
            await asyncio.sleep(0.05)
            self.assertGreaterEqual(len(storage.calls), 1)
            self.assertEqual(storage.calls[0], 15)
            await sch.stop()
            self.assertIsNone(sch._task)
            # stop 后不再新增调用
            count = len(storage.calls)
            await asyncio.sleep(0.05)
            self.assertEqual(len(storage.calls), count)

        _run()

    def test_start_idempotent_and_stop_before_start_noop(self):
        @async_test
        async def _run():
            sch = ProfileCleanupScheduler(_FakeStorage(), _FakeConfigMgr())
            # 未启动即 stop → no-op
            await sch.stop()
            await sch.start()
            first_task = sch._task
            await sch.start()  # 重复 start 忽略
            self.assertIs(sch._task, first_task)
            await sch.stop()
            await sch.stop()  # 重复 stop no-op
            self.assertIsNone(sch._task)

        _run()

    def test_invalid_keep_days_falls_back_to_default(self):
        @async_test
        async def _run():
            for bad in (0, -5):
                storage = _FakeStorage()
                sch = ProfileCleanupScheduler(storage, _FakeConfigMgr(keep_days=bad))
                await sch.start()
                await asyncio.sleep(0.05)
                await sch.stop()
                self.assertGreaterEqual(len(storage.calls), 1)
                self.assertEqual(storage.calls[0], 30, f"bad={bad}")

        _run()

    def test_backoff_retry_after_failure(self):
        @async_test
        async def _run():
            storage = _FakeStorage(fail_times=1)
            sch = ProfileCleanupScheduler(storage, _FakeConfigMgr(keep_days=7))
            sch._BACKOFF_SEQUENCE = [0.01]  # 实例级覆写，加速退避
            _STUB_LOGGER.records["error"].clear()
            await sch.start()
            await asyncio.sleep(0.15)  # 足够：失败→退避 0.01→重试成功→24h(0.02)→再成功
            await sch.stop()
            self.assertGreaterEqual(len(storage.calls), 2)  # 重试成功 + 下一轮
            self.assertEqual(storage.calls[0], 7)
            self.assertIn("清理失败", _STUB_LOGGER.joined("error"))

        _run()


if __name__ == "__main__":
    unittest.main(verbosity=2)
