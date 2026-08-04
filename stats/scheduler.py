"""数据分析调度器（模块 F）：日报/周报定时推送检查 + 小时/日/月分段快照调度。

两个相互独立的 asyncio 后台循环（生命周期与退避范式沿用
``profile/scheduler.py`` / ``cleaner.py`` 的后台循环模式）：

- **推送检查循环**（每 ``PUSH_CHECK_INTERVAL`` 秒醒一次，默认 20s）：
  读取日报/周报的开关与时间配置（typed 读取，非法值配置层已回退默认），
  当前 HH:MM >= 配置时间且当日尚未触发时调用
  ``service.push_report(kind)``（kind="daily"/"weekly"）；周报额外要求
  ``now.weekday() + 1 == 配置星期``（1=周一 … 7=周日）。
- **快照循环**（每 ``SNAPSHOT_CHECK_INTERVAL`` 秒醒一次，默认 60s）：
  同一循环体内**顺序**判定三类快照任务（各自独立去重戳）：

  - 小时快照：当前分钟 >= 5 且本 ``(date, hour)`` 尚未成功跑过时调用
    ``snapshot.run_hourly_snapshot()`` 聚合上一整点数据；
  - 日快照：当前分钟 >= 15 且「昨日」尚未盖章时调用
    ``snapshot.run_daily_snapshot()`` 聚合上一完整自然日，成功后紧接
    ``snapshot.evict_expired()`` 淘汰过期快照；
  - 月快照：当前分钟 >= 25 且「上一自然月」尚未盖章时调用
    ``snapshot.run_monthly_snapshot()`` 聚合上一自然月。

  分钟阈值分段错开（5/15/25）：给上游小时快照与数据沉淀留出时间，且日任务
  须在 00:05 小时快照跑完之后；检查被延迟时同一小时/日/月内仍可补跑。

去重语义（内存戳，进程重启后自然清零，重启当日可能补触发一次，属预期）：

- 推送：``{kind: date}`` 日期戳——同一天每种 kind 至多触发一次；
  **成功推送后才盖章**，推送失败不盖章，由退避重试补发；跨日戳自然失配，
  无需手动清理。
- 小时快照：``(date, hour)`` 戳——同一小时至多成功执行一次；**成功后才盖章**，
  失败由退避重试补跑；跨小时/跨日戳失配后照常触发。
- 日快照：``date`` 戳——盖章对象是**被聚合的那一天**（即昨日，而非执行日），
  同一被聚合日至多成功一次；次日被聚合对象变化后戳自然失配恢复。
- 月快照：``"YYYY-MM"`` 戳——盖章对象是被聚合的上一自然月，同一被聚合月
  至多成功一次；次月被聚合对象变化后戳自然失配恢复（1 月的上一自然月为
  上一年 12 月，跨年正确衔接）。

退避语义：循环体内任意异常（配置读取/推送/三类快照执行）按 60→120→300→600s
退避序列等待后重试（封顶 600s），任一次成功后复位失败计数；
``CancelledError`` 一律向上抛出，保证 :meth:`stop` cancel + await 时
两个循环都能干净退出（v0.4.5 取消安全范式）。

可测性：核心判定抽为纯函数 :func:`check_push_due` /
:func:`check_snapshot_due` / :func:`check_daily_due` /
:func:`check_monthly_due`（时钟、配置、去重戳全部显式传入）；循环体只是
薄封装，时钟经 ``_now_fn`` 注入点获取，测试可换假时钟；两个检查间隔为
类属性，测试可改小以加速。
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from astrbot.api import logger

if TYPE_CHECKING:
    from collections.abc import Callable

    from ..db_config import ConfigManager
    from .service import StatsService
    from .snapshot import ImageSnapshotManager

# 快照触发分钟阈值：每小时第 5 分钟起（含）才允许执行，给上游 image_records
# 入库留出沉淀时间；同一小时内持续满足条件，由 (date, hour) 戳去重
SNAPSHOT_MINUTE_THRESHOLD = 5

# 日快照触发分钟阈值：每小时第 15 分钟起（含）才允许执行——给上游小时快照
# 与数据沉淀留出时间，且日任务须在 00:05 小时快照跑完之后；同一日内持续
# 满足条件（任意小时补跑自愈），由「被聚合的那一天（昨日）」日期戳去重
DAILY_MINUTE_THRESHOLD = 15

# 月快照触发分钟阈值：每小时第 25 分钟起（含）才允许执行——给上游小时/日
# 快照与数据沉淀留出时间；同一月内持续满足条件，由上一自然月 "YYYY-MM" 戳去重
MONTHLY_MINUTE_THRESHOLD = 25

# 推送配置键（每轮检查经 get_stats_setting_typed 逐项读取）
_PUSH_SETTING_KEYS = (
    "push_daily_enabled",
    "push_daily_time",
    "push_weekly_enabled",
    "push_weekly_weekday",
    "push_weekly_time",
)


def _parse_hhmm_to_minutes(value: Any) -> int | None:
    """解析 "HH:MM" 为自午夜起的分钟数，形状非法返回 None。

    配置写入侧已做 HH:MM 格式校验与归一化，正常路径不会走到 None 分支；
    此处仅为纯函数的防御性兜底——非法值视为「本轮不触发」而不是抛异常。

    Args:
        value: 配置值（期望 "HH:MM" 字符串）

    Returns:
        int | None: 分钟数（0–1439）；非法时 None
    """
    if not isinstance(value, str):
        return None
    parts = value.strip().split(":")
    if len(parts) != 2:
        return None
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


def check_push_due(
    now: datetime,
    settings: dict[str, Any],
    last_fired: dict[str, date],
) -> list[str]:
    """判定 ``now`` 时刻应触发哪些推送（纯函数，循环体的薄封装对象）。

    触发条件（各 kind 独立判定）：

    - 对应开关为真；
    - 当前 HH:MM >= 配置时间（到点即触发，错过精确分钟也能补上）；
    - 周报额外要求星期匹配：``now.weekday() + 1 == 配置星期``（1=周一）；
    - ``last_fired`` 日期戳去重：该 kind 当日已触发过则跳过（同一自然日
      至多一次；跨日后戳失配自然恢复）。

    Args:
        now: 当前时刻
        settings: typed 配置字典，使用键 push_daily_enabled / push_daily_time /
            push_weekly_enabled / push_weekly_weekday / push_weekly_time
        last_fired: 同日去重戳 ``{kind: 触发日期}``

    Returns:
        list[str]: 应触发的 kind 列表（"daily"/"weekly" 的子集，daily 在前）
    """
    due: list[str] = []
    today = now.date()
    now_minutes = now.hour * 60 + now.minute

    if settings.get("push_daily_enabled"):
        target = _parse_hhmm_to_minutes(settings.get("push_daily_time"))
        if (
            target is not None
            and now_minutes >= target
            and last_fired.get("daily") != today
        ):
            due.append("daily")

    if settings.get("push_weekly_enabled"):
        target = _parse_hhmm_to_minutes(settings.get("push_weekly_time"))
        try:
            cfg_weekday = int(settings.get("push_weekly_weekday", 0))
        except (TypeError, ValueError):
            cfg_weekday = -1  # 非法星期值 → 永不匹配（正常路径配置层已兜底）
        if (
            target is not None
            and 1 <= cfg_weekday <= 7
            and now.weekday() + 1 == cfg_weekday
            and now_minutes >= target
            and last_fired.get("weekly") != today
        ):
            due.append("weekly")

    return due


def check_snapshot_due(now: datetime, last_snap: tuple[date, int] | None) -> bool:
    """判定 ``now`` 时刻是否应执行小时级快照（纯函数）。

    条件：当前分钟 >= :data:`SNAPSHOT_MINUTE_THRESHOLD`（整点后第 5 分钟起，
    给上游数据沉淀留时间；检查被延迟时同一小时内仍可补跑），且本
    ``(date, hour)`` 尚未成功执行过（内存戳去重：同一小时不重复执行，
    跨小时后戳失配自然恢复）。

    Args:
        now: 当前时刻
        last_snap: 上次成功执行的 ``(date, hour)`` 戳；从未执行过为 None

    Returns:
        bool: 是否应执行快照
    """
    if now.minute < SNAPSHOT_MINUTE_THRESHOLD:
        return False
    return last_snap != (now.date(), now.hour)


def _prev_month_str(now: datetime) -> str:
    """计算 ``now`` 所在月份的上一自然月的 "YYYY-MM" 字符串。

    跨年处理：``now`` 为 1 月时上一自然月是上一年 12 月。

    Args:
        now: 当前时刻

    Returns:
        str: 上一自然月（两位补零），如 "2026-07"
    """
    if now.month == 1:
        return f"{now.year - 1}-12"
    return f"{now.year}-{now.month - 1:02d}"


def check_daily_due(now: datetime, last_daily: date | None) -> bool:
    """判定 ``now`` 时刻是否应执行日快照（纯函数）。

    条件：当前分钟 >= :data:`DAILY_MINUTE_THRESHOLD`（每小时第 15 分钟起，
    给上游小时快照与数据沉淀留时间，且须在 00:05 小时快照跑完之后；检查被
    延迟时同一日内任意小时仍可补跑），且「昨日」尚未成功盖章（盖章对象是
    **被聚合的那一天** = ``now.date() - 1 天``；同一被聚合日不重复执行，
    次日被聚合对象变化后戳自然失配恢复）。

    Args:
        now: 当前时刻
        last_daily: 上次成功聚合的「被聚合那一天」日期戳；从未执行过为 None

    Returns:
        bool: 是否应执行日快照
    """
    if now.minute < DAILY_MINUTE_THRESHOLD:
        return False
    return last_daily != now.date() - timedelta(days=1)


def check_monthly_due(now: datetime, last_monthly: str | None) -> bool:
    """判定 ``now`` 时刻是否应执行月快照（纯函数）。

    条件：当前分钟 >= :data:`MONTHLY_MINUTE_THRESHOLD`（每小时第 25 分钟起，
    给上游小时/日快照与数据沉淀留时间；检查被延迟时同一月内仍可补跑），
    且「上一自然月」尚未成功盖章（盖章为 "YYYY-MM" 字符串，由
    :func:`_prev_month_str` 计算，1 月跨年正确取上一年 12 月；同一被聚合月
    不重复执行，次月被聚合对象变化后戳自然失配恢复）。

    Args:
        now: 当前时刻
        last_monthly: 上次成功聚合的被聚合月 "YYYY-MM" 戳；从未执行过为 None

    Returns:
        bool: 是否应执行月快照
    """
    if now.minute < MONTHLY_MINUTE_THRESHOLD:
        return False
    return last_monthly != _prev_month_str(now)


class StatsScheduler:
    """数据分析调度器：推送检查循环 + 小时/日/月三类快照循环（异常退避、可取消）。"""

    # 推送检查循环唤醒间隔（秒）。类属性，测试可改小以加速
    PUSH_CHECK_INTERVAL = 20.0
    # 快照循环唤醒间隔（秒）。类属性，测试可改小以加速
    SNAPSHOT_CHECK_INTERVAL = 60.0
    # 异常退避序列（秒）：与 cleaner.py / profile/scheduler.py 一致，封顶 600s
    _BACKOFF_SEQUENCE = [60, 120, 300, 600]

    def __init__(
        self,
        service: StatsService,
        snapshot: ImageSnapshotManager,
        config_mgr: ConfigManager,
    ):
        """初始化调度器。

        Args:
            service: 编排服务（推送执行入口 push_report）
            snapshot: 快照管理器（小时/日/月快照执行与过期淘汰入口）
            config_mgr: 配置管理器（get_stats_setting_typed 读推送配置）
        """
        self.service = service
        self.snapshot = snapshot
        self.config_mgr = config_mgr
        self._push_task: asyncio.Task | None = None
        self._snapshot_task: asyncio.Task | None = None
        # 推送同日去重戳 {kind: 触发日期}；小时快照 (date, hour) 去重戳；
        # 日快照 date 去重戳（盖章对象是被聚合的那一天=昨日）；
        # 月快照 "YYYY-MM" 去重戳（盖章对象是被聚合的上一自然月）。
        # 均在成功执行后才更新（见模块 docstring 去重语义）
        self._push_last_fired: dict[str, date] = {}
        self._snapshot_last_done: tuple[date, int] | None = None
        self._daily_last_done: date | None = None
        self._monthly_last_done: str | None = None
        self._push_fail_count = 0  # 推送循环连续失败次数（成功后复位）
        self._snapshot_fail_count = 0  # 快照循环连续失败次数（成功后复位）
        # 时钟注入点：循环体经此取当前时刻，测试可替换为假时钟
        self._now_fn: Callable[[], datetime] = datetime.now

    async def start(self) -> None:
        """启动推送检查循环与小时/日/月快照循环（两个 asyncio task）。

        可重入防护：任一循环任务仍在运行时直接返回，不重复创建 task。
        """
        running = (self._push_task is not None and not self._push_task.done()) or (
            self._snapshot_task is not None and not self._snapshot_task.done()
        )
        if running:
            logger.warning("[Stats] 调度器已在运行，忽略重复 start()")
            return
        self._push_fail_count = 0
        self._snapshot_fail_count = 0
        self._push_task = asyncio.create_task(self._push_loop())
        self._snapshot_task = asyncio.create_task(self._snapshot_loop())
        logger.info(
            "[Stats] 数据分析调度器已启动"
            f"（推送检查每 {self.PUSH_CHECK_INTERVAL:g}s；"
            f"快照每 {self.SNAPSHOT_CHECK_INTERVAL:g}s 检查，"
            f"小时任务整点第 5 分钟起、日任务第 15 分钟起、"
            f"月任务第 25 分钟起执行）"
        )

    async def stop(self) -> None:
        """停止两个循环：cancel + await + 吞 CancelledError 干净退出。

        先取消两个 task 再分别 await，等待期间循环体内任意 sleep 都会被
        cancel 打断并沿 ``except asyncio.CancelledError: raise`` 干净退出；
        无任务时 no-op。
        """
        tasks = [t for t in (self._push_task, self._snapshot_task) if t is not None]
        if not tasks:
            return
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._push_task = None
        self._snapshot_task = None
        logger.info("[Stats] 数据分析调度器已停止")

    async def _push_loop(self) -> None:
        """推送检查循环：每 PUSH_CHECK_INTERVAL 秒醒一次判定并触发。

        每轮：读 5 项推送配置（typed）→ check_push_due 判定 → 逐个 kind
        执行 push_report 并在**成功后**盖当日去重戳。循环体内任意异常
        按退避序列（60→120→300→600s 封顶）等待后重试并 logger.error 输出，
        一轮成功（含无需触发的空轮）后复位失败计数；失败不盖章，因此
        失败的 kind 会在退避重试后补发。
        """
        while True:
            try:
                now = self._now_fn()
                settings = await self._read_push_settings()
                for kind in check_push_due(now, settings, self._push_last_fired):
                    await self.service.push_report(kind, now)
                    self._push_last_fired[kind] = now.date()
                    logger.info(f"[Stats] {kind} 报告推送已触发（{now:%H:%M}）")
                self._push_fail_count = 0
                await asyncio.sleep(self.PUSH_CHECK_INTERVAL)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                backoff = self._BACKOFF_SEQUENCE[
                    min(self._push_fail_count, len(self._BACKOFF_SEQUENCE) - 1)
                ]
                self._push_fail_count += 1
                logger.error(f"[Stats] 推送检查循环异常，{backoff}s 后重试: {e}")
                await asyncio.sleep(backoff)

    async def _snapshot_loop(self) -> None:
        """快照循环：每 SNAPSHOT_CHECK_INTERVAL 秒醒一次，顺序判定小时/日/月并执行。

        每轮在同一循环体内顺序判定三类任务（各自独立去重戳）：

        1. 小时：check_snapshot_due（分钟 >= 5 且本 (date, hour) 未跑过）
           → 执行 run_hourly_snapshot 并在**成功后**盖 (date, hour) 戳；
        2. 日：check_daily_due（分钟 >= 15 且昨日未盖章）→ 执行
           run_daily_snapshot，成功后紧接 evict_expired 淘汰过期快照，
           再盖昨日（**被聚合的那一天**）戳；
        3. 月：check_monthly_due（分钟 >= 25 且上一自然月未盖章）→ 执行
           run_monthly_snapshot 并在**成功后**盖上月 "YYYY-MM" 戳。

        异常退避与失败计数复位语义同推送循环；任一步失败不盖章，失败的
        任务在退避重试后补跑（三类判定相互独立，前序任务已盖的戳不受
        影响，未轮到的任务下一轮照常判定）。
        """
        while True:
            try:
                now = self._now_fn()
                # 1) 小时快照判定
                if check_snapshot_due(now, self._snapshot_last_done):
                    await self.snapshot.run_hourly_snapshot(now)
                    self._snapshot_last_done = (now.date(), now.hour)
                    logger.info(f"[Stats] 小时快照任务已触发（{now:%Y-%m-%d %H:%M}）")
                # 2) 日快照判定（成功后顺带淘汰过期快照）
                if check_daily_due(now, self._daily_last_done):
                    await self.snapshot.run_daily_snapshot(now)
                    await self.snapshot.evict_expired(now)
                    self._daily_last_done = now.date() - timedelta(days=1)
                    logger.info(f"[Stats] 日快照任务已触发（{now:%Y-%m-%d %H:%M}）")
                # 3) 月快照判定
                if check_monthly_due(now, self._monthly_last_done):
                    await self.snapshot.run_monthly_snapshot(now)
                    self._monthly_last_done = _prev_month_str(now)
                    logger.info(f"[Stats] 月快照任务已触发（{now:%Y-%m-%d %H:%M}）")
                self._snapshot_fail_count = 0
                await asyncio.sleep(self.SNAPSHOT_CHECK_INTERVAL)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                backoff = self._BACKOFF_SEQUENCE[
                    min(self._snapshot_fail_count, len(self._BACKOFF_SEQUENCE) - 1)
                ]
                self._snapshot_fail_count += 1
                logger.error(f"[Stats] 快照循环异常，{backoff}s 后重试: {e}")
                await asyncio.sleep(backoff)

    async def _read_push_settings(self) -> dict[str, Any]:
        """读取 5 项推送相关 typed 配置。

        get_stats_setting_typed 内部对非法值回退默认并记 warning，不会抛出；
        数据库异常也已在配置层兜底为默认值。

        Returns:
            dict[str, Any]: 键同 _PUSH_SETTING_KEYS，值为声明类型
        """
        return {
            key: await self.config_mgr.get_stats_setting_typed(key)
            for key in _PUSH_SETTING_KEYS
        }
