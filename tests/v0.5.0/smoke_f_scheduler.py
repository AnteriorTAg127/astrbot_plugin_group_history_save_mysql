# ruff: noqa: I001
"""v0.5.0 模块 F 调度器（stats/scheduler.py）冒烟脚本。

被测对象：``stats/scheduler.py`` —— StatsScheduler 双循环（推送检查 /
小时快照）+ 纯判定函数 check_push_due / check_snapshot_due。

覆盖点（对照任务验收清单）：

- check_push_due：日报到点/未到点、同日去重、跨日重置、周报星期匹配/不匹配、
  周报时间未到、双开同刻两者都触发、非法时间串防御（不抛异常、视为不触发）
- check_snapshot_due：xx:05 触发、xx:04 不触发、同小时去重、跨小时重置、
  跨日同小时重置
- 循环集成（fake service/snapshot 记录调用 + 假时钟注入 + 间隔类属性改小）：
  推送触发后调用与同日去重、跨日再触发；周报仅匹配星期触发、非匹配日不触发；
  快照触发与同小时去重、跨小时再触发、第 5 分钟前不触发；推送失败按退避
  重试后恢复（失败计数复位）；stop() 取消后两个 task 全部结束、事件循环
  无 pending；start() 可重入防护、stop() 幂等、stop 后可重启

范式沿用 tests/v0.5.0/smoke_e_render.py：astrbot.* 全部 sys.modules
stub（注入在任何被测包 import 之前），不依赖 AstrBot 运行时。本模块仅用
astrbot.api.logger；调度器对 service/snapshot/config_mgr 仅鸭子类型依赖，
全部以 fake 注入，不拉起 db/repository 依赖链。

运行方式（插件根目录，二者皆可）：
    python -m pytest "tests/v0.5.0/smoke_f_scheduler.py" -v
    python "tests/v0.5.0/smoke_f_scheduler.py"
"""

import asyncio
import os
import sys
import time
import types
import unittest
from datetime import date, datetime


# ============================================================
# 一、astrbot.* stub 注入（必须在导入被测包之前）
# ============================================================


class _StubLogger:
    def _noop(self, *args, **kwargs):
        pass

    info = warning = error = debug = _noop


_astrbot = types.ModuleType("astrbot")
_astrbot_api = types.ModuleType("astrbot.api")
_astrbot_api.logger = _StubLogger()
_astrbot.api = _astrbot_api
sys.modules["astrbot"] = _astrbot
sys.modules["astrbot.api"] = _astrbot_api

# ---- 剔除被测包缓存（多测试文件合跑兼容，范式同 smoke_e）----
_PKG = "astrbot_plugin_group_history_save_mysql"
for _name in list(sys.modules):
    if _name == _PKG or _name.startswith(_PKG + "."):
        del sys.modules[_name]

_PLUGINS_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..")
)
if _PLUGINS_DIR not in sys.path:
    sys.path.insert(0, _PLUGINS_DIR)

from astrbot_plugin_group_history_save_mysql.core.stats.scheduler import (  # noqa: E402
    DAILY_MINUTE_THRESHOLD,
    MONTHLY_MINUTE_THRESHOLD,
    StatsScheduler,
    check_daily_due,
    check_monthly_due,
    check_push_due,
    check_snapshot_due,
)

# 固定锚点日期（已验证 weekday()）：
#   2026-08-03 周一（weekday()==0，配置星期 1）
#   2026-08-04 周二（weekday()==1，配置星期 2）
#   2026-08-10 周一（周报跨周用例直接用 datetime 字面量）
MONDAY = date(2026, 8, 3)


def _settings(
    daily_enabled=True,
    daily_time="21:00",
    weekly_enabled=False,
    weekly_weekday=1,
    weekly_time="09:00",
):
    """构造 check_push_due 用的 typed 配置字典（键名同 db_config 8 项中的推送簇）。"""
    return {
        "push_daily_enabled": daily_enabled,
        "push_daily_time": daily_time,
        "push_weekly_enabled": weekly_enabled,
        "push_weekly_weekday": weekly_weekday,
        "push_weekly_time": weekly_time,
    }


# ============================================================
# 二、check_push_due — 纯函数判定
# ============================================================


class TestCheckPushDue(unittest.TestCase):
    def test_daily_due_at_exact_time(self):
        now = datetime(2026, 8, 4, 21, 0, 0)
        self.assertEqual(check_push_due(now, _settings(), {}), ["daily"])

    def test_daily_due_after_time(self):
        now = datetime(2026, 8, 4, 21, 30, 45)
        self.assertEqual(check_push_due(now, _settings(), {}), ["daily"])

    def test_daily_not_due_before_time(self):
        now = datetime(2026, 8, 4, 20, 59, 59)
        self.assertEqual(check_push_due(now, _settings(), {}), [])

    def test_daily_disabled(self):
        now = datetime(2026, 8, 4, 21, 30)
        self.assertEqual(check_push_due(now, _settings(daily_enabled=False), {}), [])

    def test_daily_same_day_dedupe(self):
        # 当日已触发过（内存日期戳）→ 同日不再触发
        now = datetime(2026, 8, 4, 21, 30)
        last_fired = {"daily": date(2026, 8, 4)}
        self.assertEqual(check_push_due(now, _settings(), last_fired), [])

    def test_daily_cross_day_reset(self):
        # 昨日戳不挡今日：跨日后自然恢复
        now = datetime(2026, 8, 4, 21, 0)
        last_fired = {"daily": date(2026, 8, 3)}
        self.assertEqual(check_push_due(now, _settings(), last_fired), ["daily"])

    def test_weekly_weekday_match(self):
        now = datetime(2026, 8, 3, 9, 0, 10)  # 周一，配置星期 1
        self.assertEqual(
            check_push_due(now, _settings(weekly_enabled=True), {}), ["weekly"]
        )

    def test_weekly_weekday_mismatch(self):
        now = datetime(2026, 8, 4, 9, 0, 10)  # 周二，配置星期 1 → 不匹配
        self.assertEqual(check_push_due(now, _settings(weekly_enabled=True), {}), [])

    def test_weekly_time_not_due(self):
        now = datetime(2026, 8, 3, 8, 59, 59)  # 周一但时间未到
        self.assertEqual(check_push_due(now, _settings(weekly_enabled=True), {}), [])

    def test_weekly_disabled_by_default(self):
        now = datetime(2026, 8, 3, 9, 0)  # 周一到点但周报默认关
        self.assertEqual(check_push_due(now, _settings(), {}), [])

    def test_weekly_same_day_dedupe(self):
        now = datetime(2026, 8, 3, 9, 30)
        last_fired = {"weekly": MONDAY}
        self.assertEqual(
            check_push_due(now, _settings(weekly_enabled=True), last_fired), []
        )

    def test_weekly_cross_week_reset(self):
        # 上周一的戳不挡本周一
        now = datetime(2026, 8, 3, 9, 0)
        last_fired = {"weekly": date(2026, 7, 27)}
        self.assertEqual(
            check_push_due(now, _settings(weekly_enabled=True), last_fired),
            ["weekly"],
        )

    def test_weekly_illegal_weekday_never_matches(self):
        now = datetime(2026, 8, 3, 9, 0)
        for bad in (0, 8, "abc", None):
            self.assertEqual(
                check_push_due(
                    now, _settings(weekly_enabled=True, weekly_weekday=bad), {}
                ),
                [],
                f"非法星期值 {bad!r} 不应触发",
            )

    def test_both_due_same_moment(self):
        # 日报与周报同时开启且同刻到点（周一 09:00）→ 两者都触发，daily 在前
        now = datetime(2026, 8, 3, 9, 0, 0)
        settings = _settings(
            daily_enabled=True,
            daily_time="09:00",
            weekly_enabled=True,
            weekly_weekday=1,
            weekly_time="09:00",
        )
        self.assertEqual(check_push_due(now, settings, {}), ["daily", "weekly"])

    def test_malformed_time_string_skipped(self):
        # 防御兜底：形状非法的时间串视为不触发且不抛异常
        # （正常路径配置层写入校验 + typed 回退默认，此分支仅为纯函数健壮性）
        now = datetime(2026, 8, 4, 23, 59)
        self.assertEqual(check_push_due(now, _settings(daily_time="banana"), {}), [])
        self.assertEqual(check_push_due(now, _settings(daily_time="99:99"), {}), [])
        self.assertEqual(check_push_due(now, _settings(daily_time=2100), {}), [])

    def test_independent_dedupe_stamps(self):
        # daily 戳不影响 weekly 判定（各自独立）
        now = datetime(2026, 8, 3, 9, 0)
        settings = _settings(
            daily_time="09:00", weekly_enabled=True, weekly_time="09:00"
        )
        self.assertEqual(check_push_due(now, settings, {"daily": MONDAY}), ["weekly"])


# ============================================================
# 三、check_snapshot_due — 纯函数判定
# ============================================================


class TestCheckSnapshotDue(unittest.TestCase):
    def test_due_at_minute_5(self):
        self.assertTrue(check_snapshot_due(datetime(2026, 8, 4, 10, 5, 0), None))

    def test_due_late_in_hour_catchup(self):
        # 分钟 >=5 的窗口内检查被延迟也允许补跑
        self.assertTrue(check_snapshot_due(datetime(2026, 8, 4, 10, 40, 0), None))

    def test_not_due_at_minute_4(self):
        self.assertFalse(check_snapshot_due(datetime(2026, 8, 4, 10, 4, 59), None))

    def test_same_hour_dedupe(self):
        last = (date(2026, 8, 4), 10)
        self.assertFalse(check_snapshot_due(datetime(2026, 8, 4, 10, 30), last))

    def test_cross_hour_reset(self):
        last = (date(2026, 8, 4), 9)
        self.assertTrue(check_snapshot_due(datetime(2026, 8, 4, 10, 5), last))

    def test_cross_day_same_hour_reset(self):
        last = (date(2026, 8, 3), 10)
        self.assertTrue(check_snapshot_due(datetime(2026, 8, 4, 10, 5), last))


# ============================================================
# 三b、check_daily_due / check_monthly_due — v0.5.5 纯函数判定
# ============================================================


class TestCheckDailyDue(unittest.TestCase):
    def test_due_at_minute_threshold(self):
        now = datetime(2026, 8, 5, 0, DAILY_MINUTE_THRESHOLD, 0)
        self.assertTrue(check_daily_due(now, None))

    def test_due_late_any_hour_catchup(self):
        # 任意小时补跑自愈：分钟 >= 15 的窗口内均可触发
        self.assertTrue(check_daily_due(datetime(2026, 8, 5, 13, 40), None))

    def test_not_due_before_threshold(self):
        now = datetime(2026, 8, 5, 0, DAILY_MINUTE_THRESHOLD - 1, 59)
        self.assertFalse(check_daily_due(now, None))

    def test_same_aggregated_day_dedupe(self):
        # 盖章对象是被聚合的那一天（昨日）：已盖章则同日不重复
        now = datetime(2026, 8, 5, 0, 20)
        self.assertFalse(check_daily_due(now, date(2026, 8, 4)))

    def test_cross_day_reset_new_aggregated_day(self):
        # 次日被聚合对象变化（08-05）后旧戳（08-04）自然失配
        now = datetime(2026, 8, 6, 0, 20)
        self.assertTrue(check_daily_due(now, date(2026, 8, 4)))

    def test_threshold_constant_is_15(self):
        self.assertEqual(DAILY_MINUTE_THRESHOLD, 15)


class TestCheckMonthlyDue(unittest.TestCase):
    def test_due_at_minute_threshold(self):
        now = datetime(2026, 8, 5, 1, MONTHLY_MINUTE_THRESHOLD, 0)
        self.assertTrue(check_monthly_due(now, None))

    def test_not_due_before_threshold(self):
        now = datetime(2026, 8, 5, 1, MONTHLY_MINUTE_THRESHOLD - 1, 59)
        self.assertFalse(check_monthly_due(now, None))

    def test_same_aggregated_month_dedupe(self):
        # 盖章为上一自然月 "YYYY-MM"：已盖章则同月不重复
        now = datetime(2026, 8, 5, 1, 30)
        self.assertFalse(check_monthly_due(now, "2026-07"))

    def test_cross_month_reset(self):
        # 次月被聚合对象变化后旧戳失配（9 月的上一自然月为 08）
        now = datetime(2026, 9, 1, 1, 30)
        self.assertTrue(check_monthly_due(now, "2026-07"))

    def test_january_cross_year_prev_month(self):
        # 1 月的上一自然月为上一年 12 月（跨年正确衔接）
        now = datetime(2027, 1, 10, 2, 30)
        self.assertFalse(check_monthly_due(now, "2026-12"))
        self.assertTrue(check_monthly_due(now, "2026-11"))
        self.assertTrue(check_monthly_due(now, None))

    def test_threshold_constant_is_25(self):
        self.assertEqual(MONTHLY_MINUTE_THRESHOLD, 25)


# ============================================================
# 四、循环集成 — fake service/snapshot + 假时钟 + 缩短间隔
# ============================================================


class FakeClock:
    """可拨动的假时钟：调度器 _now_fn 注入点。"""

    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class FakeConfig:
    def __init__(self, **values):
        self.values = values

    async def get_stats_setting_typed(self, key):
        return self.values.get(key)


class FakeService:
    """记录 push_report 调用；可配置前 fail_times 次调用抛异常模拟失败。"""

    def __init__(self, fail_times: int = 0):
        self.calls: list[tuple[str, datetime]] = []  # 成功调用 (kind, now)
        self.attempts = 0
        self.fail_times = fail_times

    async def push_report(self, kind, now=None):
        self.attempts += 1
        if self.attempts <= self.fail_times:
            raise RuntimeError(f"模拟推送失败 #{self.attempts}")
        self.calls.append((kind, now))


class FakeSnapshot:
    def __init__(self, daily_fail_times: int = 0):
        self.calls: list[datetime] = []  # 小时快照
        self.daily_calls: list[datetime] = []  # 日快照
        self.evict_calls: list[datetime] = []  # 淘汰
        self.monthly_calls: list[datetime] = []  # 月快照
        self.sequence: list[str] = []  # 上述四类调用的先后顺序
        self._daily_attempts = 0
        self.daily_fail_times = daily_fail_times  # 前 N 次日快照抛异常

    async def run_hourly_snapshot(self, now=None):
        self.calls.append(now)
        self.sequence.append("hourly")

    async def run_daily_snapshot(self, now=None):
        self._daily_attempts += 1
        if self._daily_attempts <= self.daily_fail_times:
            raise RuntimeError(f"模拟日快照失败 #{self._daily_attempts}")
        self.daily_calls.append(now)
        self.sequence.append("daily")

    async def evict_expired(self, now=None):
        self.evict_calls.append(now)
        self.sequence.append("evict")

    async def run_monthly_snapshot(self, now=None):
        self.monthly_calls.append(now)
        self.sequence.append("monthly")


def _config(
    daily_enabled=True,
    daily_time="21:00",
    weekly_enabled=False,
    weekly_weekday=1,
    weekly_time="09:00",
) -> FakeConfig:
    return FakeConfig(
        push_daily_enabled=daily_enabled,
        push_daily_time=daily_time,
        push_weekly_enabled=weekly_enabled,
        push_weekly_weekday=weekly_weekday,
        push_weekly_time=weekly_time,
    )


def _make_sched(clock, cfg, service=None, snapshot=None) -> StatsScheduler:
    """构造调度器并把循环间隔/退避改小（类属性设计即为此），注入假时钟。"""
    sched = StatsScheduler(service or FakeService(), snapshot or FakeSnapshot(), cfg)
    sched.PUSH_CHECK_INTERVAL = 0.01
    sched.SNAPSHOT_CHECK_INTERVAL = 0.01
    sched._BACKOFF_SEQUENCE = [0.01, 0.02, 0.04]
    sched._now_fn = clock
    return sched


async def _wait_until(cond, timeout: float = 3.0) -> bool:
    """轮询等待 cond 成立（消除固定 sleep 的时序抖动），超时返回最终判定。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        await asyncio.sleep(0.005)
    return cond()


class TestPushLoop(unittest.IsolatedAsyncioTestCase):
    async def test_fire_dedupe_and_cross_day(self):
        # 周二 21:00:10 日报到点 → 触发一次；同日多轮去重；跨日再触发
        clock = FakeClock(datetime(2026, 8, 4, 21, 0, 10))
        service = FakeService()
        snapshot = FakeSnapshot()
        sched = _make_sched(clock, _config(), service, snapshot)
        await sched.start()
        try:
            self.assertTrue(await _wait_until(lambda: len(service.calls) == 1))
            self.assertEqual(service.calls[0][0], "daily")
            # 同日去重：再过若干轮检查不重复触发
            await asyncio.sleep(0.06)
            self.assertEqual(len(service.calls), 1)
            # 跨日重置：次日同一时刻再触发一次
            clock.now = datetime(2026, 8, 5, 21, 0, 12)
            self.assertTrue(await _wait_until(lambda: len(service.calls) == 2))
            self.assertEqual(service.calls[1][0], "daily")
        finally:
            await sched.stop()
        self.assertEqual(snapshot.calls, [])  # 分钟 0 <5，快照未触发

    async def test_not_due_before_time(self):
        clock = FakeClock(datetime(2026, 8, 4, 20, 59, 30))
        service = FakeService()
        sched = _make_sched(clock, _config(), service)
        await sched.start()
        try:
            await asyncio.sleep(0.05)  # 多轮检查均不触发
            self.assertEqual(service.calls, [])
        finally:
            await sched.stop()

    async def test_weekly_fires_only_on_matching_weekday(self):
        # 周一 09:00 触发；周二不触发；下周一再触发
        clock = FakeClock(datetime(2026, 8, 3, 9, 0, 20))
        service = FakeService()
        cfg = _config(daily_enabled=False, weekly_enabled=True)
        sched = _make_sched(clock, cfg, service)
        await sched.start()
        try:
            self.assertTrue(await _wait_until(lambda: len(service.calls) == 1))
            self.assertEqual(service.calls[0][0], "weekly")
            # 次日（周二）星期不匹配 → 不触发
            clock.now = datetime(2026, 8, 4, 9, 0, 20)
            await asyncio.sleep(0.06)
            self.assertEqual(len(service.calls), 1)
            # 下周一 → 再次触发
            clock.now = datetime(2026, 8, 10, 9, 0, 25)
            self.assertTrue(await _wait_until(lambda: len(service.calls) == 2))
        finally:
            await sched.stop()

    async def test_backoff_retry_then_recovery(self):
        # 前两次 push_report 失败 → 退避重试；第三次成功，失败计数复位
        clock = FakeClock(datetime(2026, 8, 4, 21, 0, 10))
        service = FakeService(fail_times=2)
        sched = _make_sched(clock, _config(), service)
        await sched.start()
        try:
            self.assertTrue(await _wait_until(lambda: len(service.calls) == 1))
            self.assertEqual(service.attempts, 3)  # 2 失败 + 1 成功
            self.assertEqual(sched._push_fail_count, 0)  # 恢复成功后复位
            # 成功后盖章：再过若干轮不重复推送
            await asyncio.sleep(0.06)
            self.assertEqual(len(service.calls), 1)
        finally:
            await sched.stop()


class TestSnapshotLoop(unittest.IsolatedAsyncioTestCase):
    async def test_fire_dedupe_and_cross_hour(self):
        # xx:05 触发一次；同小时去重；跨小时再触发
        first = datetime(2026, 8, 4, 10, 5, 30)
        clock = FakeClock(first)
        snapshot = FakeSnapshot()
        cfg = _config(daily_enabled=False)  # 关闭推送，隔离快照断言
        sched = _make_sched(clock, cfg, FakeService(), snapshot)
        await sched.start()
        try:
            self.assertTrue(await _wait_until(lambda: len(snapshot.calls) == 1))
            # run_hourly_snapshot 收到调度器注入的 now
            self.assertEqual(snapshot.calls[0], first)
            # 同小时去重：多轮检查不重复执行
            await asyncio.sleep(0.06)
            self.assertEqual(len(snapshot.calls), 1)
            # 跨小时（11:05）→ 再执行
            clock.now = datetime(2026, 8, 4, 11, 5, 0)
            self.assertTrue(await _wait_until(lambda: len(snapshot.calls) == 2))
        finally:
            await sched.stop()

    async def test_not_due_before_5th_minute(self):
        clock = FakeClock(datetime(2026, 8, 4, 10, 4, 50))
        snapshot = FakeSnapshot()
        sched = _make_sched(clock, _config(daily_enabled=False), snapshot=snapshot)
        await sched.start()
        try:
            await asyncio.sleep(0.05)
            self.assertEqual(snapshot.calls, [])  # xx:04 不触发
            clock.now = datetime(2026, 8, 4, 10, 5, 0)
            self.assertTrue(await _wait_until(lambda: len(snapshot.calls) == 1))
        finally:
            await sched.stop()


class TestSnapshotLoopThreeTasks(unittest.IsolatedAsyncioTestCase):
    """v0.5.5：同一轮顺序判定 小时→日→月 + 各自独立去重戳"""

    async def test_same_round_sequence_hourly_daily_evict_monthly(self):
        # 分钟 26（>=25）：三类任务同轮全部到期，顺序为
        # 小时 → 日（成功后紧接淘汰）→ 月
        first = datetime(2026, 8, 5, 10, 26, 0)
        clock = FakeClock(first)
        snapshot = FakeSnapshot()
        sched = _make_sched(clock, _config(daily_enabled=False), snapshot=snapshot)
        await sched.start()
        try:
            self.assertTrue(
                await _wait_until(
                    lambda: len(snapshot.monthly_calls) == 1
                    and len(snapshot.daily_calls) == 1
                    and len(snapshot.calls) == 1
                )
            )
            self.assertEqual(
                snapshot.sequence, ["hourly", "daily", "evict", "monthly"]
            )
            # 去重戳：日戳=被聚合日（昨日）、月戳=被聚合月（上月）
            self.assertEqual(sched._daily_last_done, date(2026, 8, 4))
            self.assertEqual(sched._monthly_last_done, "2026-07")
            # 同一小时多轮：三类戳均匹配，不重复执行
            await asyncio.sleep(0.06)
            self.assertEqual(snapshot.sequence, ["hourly", "daily", "evict", "monthly"])
            # 跨小时（分钟仍 >=25）：仅小时任务再触发；日/月戳未失配
            clock.now = datetime(2026, 8, 5, 11, 26, 0)
            self.assertTrue(await _wait_until(lambda: len(snapshot.calls) == 2))
            await asyncio.sleep(0.06)
            self.assertEqual(len(snapshot.daily_calls), 1)
            self.assertEqual(len(snapshot.monthly_calls), 1)
            # 跨日（同月）：小时 + 日（新被聚合日）触发，月仍不触发
            clock.now = datetime(2026, 8, 6, 10, 26, 0)
            self.assertTrue(
                await _wait_until(
                    lambda: len(snapshot.daily_calls) == 2
                    and len(snapshot.evict_calls) == 2
                )
            )
            self.assertEqual(sched._daily_last_done, date(2026, 8, 5))
            self.assertEqual(len(snapshot.monthly_calls), 1)
        finally:
            await sched.stop()

    async def test_daily_failure_no_evict_no_stamp_and_monthly_blocked(self):
        # 日快照失败：不淘汰、不盖章，退避重试；月判定位于日之后，
        # 日持续失败期间月任务不得执行（任一步异常走既有退避）
        clock = FakeClock(datetime(2026, 8, 5, 10, 26, 0))
        snapshot = FakeSnapshot(daily_fail_times=10**9)  # 持续失败直至放开
        sched = _make_sched(clock, _config(daily_enabled=False), snapshot=snapshot)
        await sched.start()
        try:
            # 小时任务不受日失败影响：照常触发并盖章
            self.assertTrue(await _wait_until(lambda: len(snapshot.calls) == 1))
            # 日任务至少重试 2 次仍失败
            self.assertTrue(
                await _wait_until(lambda: snapshot._daily_attempts >= 2)
            )
            await asyncio.sleep(0.05)
            self.assertEqual(snapshot.evict_calls, [])  # 失败不淘汰
            self.assertIsNone(sched._daily_last_done)  # 失败不盖章
            self.assertEqual(snapshot.monthly_calls, [])  # 日被阻月不执行
            self.assertGreaterEqual(sched._snapshot_fail_count, 1)
            # 放开失败注入：下一轮重试成功 → 日盖章 + 紧接淘汰 + 月任务恢复
            snapshot.daily_fail_times = snapshot._daily_attempts
            self.assertTrue(
                await _wait_until(
                    lambda: len(snapshot.daily_calls) == 1
                    and len(snapshot.monthly_calls) == 1,
                    timeout=5.0,
                )
            )
            self.assertEqual(len(snapshot.evict_calls), 1)
            self.assertEqual(sched._daily_last_done, date(2026, 8, 4))
            self.assertEqual(sched._monthly_last_done, "2026-07")
            self.assertEqual(sched._snapshot_fail_count, 0)  # 恢复后复位
        finally:
            await sched.stop()

    async def test_minute_bands_isolate_tasks(self):
        # 分钟 10：仅小时任务到期（>=5 且 <15）；日/月不触发
        clock = FakeClock(datetime(2026, 8, 5, 10, 10, 0))
        snapshot = FakeSnapshot()
        sched = _make_sched(clock, _config(daily_enabled=False), snapshot=snapshot)
        await sched.start()
        try:
            self.assertTrue(await _wait_until(lambda: len(snapshot.calls) == 1))
            await asyncio.sleep(0.06)
            self.assertEqual(snapshot.sequence, ["hourly"])
            self.assertIsNone(sched._daily_last_done)
            self.assertIsNone(sched._monthly_last_done)
        finally:
            await sched.stop()


class TestLifecycle(unittest.IsolatedAsyncioTestCase):
    async def test_stop_cancels_all_tasks_no_pending(self):
        # 无到点事件的午间时刻启动，随后 stop：两个 task 均被取消结束
        clock = FakeClock(datetime(2026, 8, 4, 12, 0, 0))
        sched = _make_sched(clock, _config(), FakeService(), FakeSnapshot())
        await sched.start()
        push_task, snap_task = sched._push_task, sched._snapshot_task
        await sched.stop()
        self.assertTrue(push_task.done() and push_task.cancelled())
        self.assertTrue(snap_task.done() and snap_task.cancelled())
        self.assertIsNone(sched._push_task)
        self.assertIsNone(sched._snapshot_task)
        # 事件循环中不再有调度器遗留任务
        remaining = asyncio.all_tasks() - {asyncio.current_task()}
        self.assertEqual(remaining, set())
        # stop() 幂等：无任务时 no-op
        await sched.stop()

    async def test_start_reentrant_and_restartable(self):
        clock = FakeClock(datetime(2026, 8, 4, 12, 0, 0))
        sched = _make_sched(clock, _config(), FakeService(), FakeSnapshot())
        await sched.start()
        first_push, first_snap = sched._push_task, sched._snapshot_task
        # 重复 start：不重复创建 task
        await sched.start()
        self.assertIs(sched._push_task, first_push)
        self.assertIs(sched._snapshot_task, first_snap)
        await sched.stop()
        # stop 后可重启，且是全新 task
        await sched.start()
        self.assertIsNotNone(sched._push_task)
        self.assertIsNotNone(sched._snapshot_task)
        self.assertIsNot(sched._push_task, first_push)
        await sched.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
