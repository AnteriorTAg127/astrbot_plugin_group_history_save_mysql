"""分段快照统计管理（v0.5.5 分段快照体系，泛化自 v0.5.0 图片小时级快照）。

体系结构（PRD §3.1）：``消息 + 图片 × 小时/日/月`` 三段式预计算快照，
全部落在 config.db（SQLite，db_config.ConfigManager 管理），沿用 v0.5.0
「UPSERT 覆盖写、行键取窗口起点、快照只增盖不删」语义：

- **小时层**（近 7 天）：``image_stats_hourly`` / ``image_stats_hourly_top``
  （v0.5.0 既有，图片侧）与 ``msg_stats_hourly`` / ``msg_stats_hourly_top``
  （v0.5.5 新增，消息侧）。Top K 表含发言人维度（``stats_image_top_k``
  图片/消息共用），群级表为全量口径。
- **日层**（地平线=上月 1 日起，只存完整日）：``msg_stats_daily`` /
  ``image_stats_daily``，群级全量不受 Top K 截断。
- **月层**（永久，只存完整月）：``msg_stats_monthly`` / ``image_stats_monthly``，
  群级全量，month="YYYY-MM"。

三层归并取数（PRD §3.2，无重叠无空洞）：记 horizon = 上月 1 日 00:00——

1. **可服务判定**（:meth:`SnapshotManager.is_range_serviceable`）：
   范围起点/终点切断 horizon 之前的旧月中间时不可服务（月快照无法表达
   半月），调用方整体回退实时 SQL；「全部」预设（start=2000-01-01 月初）
   与 start >= horizon 的范围恒可服务。
2. **月层**：``[start, end)`` 内完整包含且月起点早于 horizon 的连续月份，
   读 ``snapshot_monthly_totals``。
3. **日/时层**：剩余日期（构造上全部 >= horizon）逐日逐群归并——该
   ``(date, group)`` 在小时层聚合（``snapshot_hourly_date_totals``）中有键
   用小时值，否则用日层行（``snapshot_daily_rows``）值。存在性优先天然
   免疫「昨日 daily 行要等 00:15 才生成」与「淘汰执行前 hourly/daily 短暂
   共存」两种时序；行缺失 = 该时段无数据（回填/缺档补偿保证该假设成立）。

淘汰线（PRD §3.4 F4.3，:meth:`SnapshotManager.evict_expired`）：小时层群级
双表保留近 7 天、``msg_stats_hourly_top`` 保留近 7 天、
``image_stats_hourly_top`` 保留近 31 天（保护近 30 天报告的个人图片口径）、
日层保留 horizon 起全部（上月+本月），月层永不淘汰——与三层归并的
horizon 定义严格对齐。

启动回填（PRD §3.4 F4.1，:meth:`SnapshotManager.backfill_on_startup`）：
小时层 ``[近7天 00:00, 当前整点)`` 与日层 ``[horizon, 今日 00:00)`` 批量
窗口重聚合（幂等 UPSERT，兼顾首次安装历史回填与宕机缺档补偿）；月层走
``plugin_settings`` 持久游标（``snapshot_monthly_msg_covered`` /
``snapshot_monthly_image_covered``）增量补齐；结尾顺带淘汰一次。

强制刷新限流（PRD §3.4 F4.2）：:meth:`SnapshotManager.refresh_current_hour`
进程内单调时钟 60 秒内至多真正执行一次，高频统计请求不再反复打 MySQL 聚合。

异常策略：本层绝不向上抛异常。写入侧（定时任务 / 强制刷新 / 回填 / 淘汰）
失败仅记日志（契约：强制刷新失败不阻断统计主流程）；读取侧不可服务或
异常返回 None（调用方回退实时 SQL）。:meth:`fill_counts` 注入失败时保持
image_count 零值原样返回。底层 repo 聚合已有 QUERY_TIMEOUT_SECONDS 超时
兜底，db_config 快照读写自身亦吞异常仅记日志。

时区口径：全部使用服务器本地时间（``datetime.now()``），与 StatsRepository
窗口传参口径一致，不依赖 MySQL 服务器时区。
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from astrbot.api import logger

from .models import StatsData

# 仅静态检查用；运行时不 import repository（避免拉起 db_mysql 依赖链，
# 保持 stats 轻量子模块的 stub 隔离兼容性）
if TYPE_CHECKING:
    from .repository import StatsRepository

__all__ = ["SnapshotManager", "ImageSnapshotManager"]

# 月层增量游标键（plugin_settings，值 = 已覆盖到的月份 "YYYY-MM"，默认 ""）
_CURSOR_KEYS = {
    "msg": "snapshot_monthly_msg_covered",
    "image": "snapshot_monthly_image_covered",
}


class SnapshotManager:
    """分段快照统计管理器（消息+图片 × 小时/日/月 三段式）。

    职责：写入侧定时聚合 / 强制刷新 / 启动回填 / 淘汰（双源对称），
    读取侧三层归并查询（可服务性判定 + 月/日/时取数归并），以及给
    :class:`StatsData` 注入图片数（:meth:`fill_counts`）。

    依赖注入（由 StatsService 构造）：

    - ``config_mgr``：ConfigManager（db_config），提供快照 UPSERT / 查询
      原语 / 淘汰 / 配置读写（``get_stats_setting_typed`` / ``get_setting``
      / ``set_setting``）
    - ``repo``：StatsRepository（stats/repository），提供 MySQL 窗口聚合
      （``get_image_window_counts`` / ``get_msg_window_counts`` /
      ``get_hourly_batch`` / ``get_daily_batch`` / ``get_monthly_batch``）
    """

    # 强制刷新限流：60 秒内至多真正执行一次（单调时钟戳，进程内）
    REFRESH_MIN_INTERVAL_SECONDS = 60.0
    # 小时层（含消息 Top K 表）保留天数
    HOURLY_RETENTION_DAYS = 7
    # 图片小时 Top K 表保留天数（保护近 30 天报告的个人图片口径）
    IMAGE_TOP_RETENTION_DAYS = 31

    def __init__(self, config_mgr, repo: StatsRepository):
        """初始化管理器。

        Args:
            config_mgr: ConfigManager 实例（快照读写 + Top K 配置 + 月游标）
            repo: StatsRepository 实例（chat_history / image_records 窗口聚合）
        """
        self._config = config_mgr
        self._repo = repo
        # 强制刷新限流的单调时钟戳（0.0 = 尚未执行过）
        self._last_refresh_mono = 0.0

    # ------------------------------------------------------------------
    # 时间辅助
    # ------------------------------------------------------------------

    @staticmethod
    def _hour_floor(moment: datetime) -> datetime:
        """取给定时刻所在小时的整点起点（分钟/秒/微秒归零）。"""
        return moment.replace(minute=0, second=0, microsecond=0)

    @staticmethod
    def _month_floor(moment: datetime) -> datetime:
        """取给定时刻所在自然月的 1 日 00:00。"""
        return moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    @staticmethod
    def _next_month_first(month_first: datetime) -> datetime:
        """取给定「月 1 日 00:00」的下一个自然月 1 日 00:00（跨年安全）。"""
        if month_first.month == 12:
            return datetime(month_first.year + 1, 1, 1)
        return datetime(month_first.year, month_first.month + 1, 1)

    @classmethod
    def _daily_horizon(cls, now: datetime) -> datetime:
        """日层地平线：上月 1 日 00:00。

        日快照保证覆盖 horizon 起的所有完整日，小时快照保证覆盖近 7 天
        含今天；淘汰线与三层归并可服务判定均以该值为基准。
        """
        return (cls._month_floor(now) - timedelta(days=1)).replace(day=1)

    @staticmethod
    def _parse_month_first(cursor: str) -> datetime | None:
        """解析月游标 "YYYY-MM" 为该月 1 日 00:00；空/非法返回 None。"""
        try:
            year, month = cursor.split("-")
            return datetime(int(year), int(month), 1)
        except (AttributeError, ValueError):
            return None

    # ------------------------------------------------------------------
    # 写入侧：单小时窗口聚合（双源对称）
    # ------------------------------------------------------------------

    async def _aggregate_and_upsert(self, start: datetime, end: datetime) -> None:
        """聚合窗口 ``[start, end)`` 的图片数并 UPSERT 入快照表。

        行键取窗口起点的 ``(date, hour)``；``groups`` → hour_rows（群级全量），
        ``senders`` → top_rows（repo 侧已按 Top K 截断并附最新昵称，直传不再
        加工）。空结果照常调用 ``snapshot_upsert``（空列表无副作用，保持调用
        路径单一）。任何异常仅记日志不抛出。

        Args:
            start: 窗口起点（含），本地时间
            end: 窗口终点（不含），本地时间
        """
        snap_date = start.strftime("%Y-%m-%d")
        snap_hour = start.hour
        try:
            top_k = await self._config.get_stats_setting_typed("stats_image_top_k")
            counts = await self._repo.get_image_window_counts(start, end, top_k)
            groups = counts.get("groups") or {}
            senders = counts.get("senders") or {}
            hour_rows = [
                (snap_date, snap_hour, group_id, count)
                for group_id, count in groups.items()
            ]
            top_rows = [
                (snap_date, snap_hour, group_id, sender_id, sender_name, count)
                for group_id, entries in senders.items()
                for sender_id, sender_name, count in entries
            ]
            await self._config.snapshot_upsert(hour_rows, top_rows)
            logger.info(
                f"[Stats] 图片快照已更新 {snap_date} {snap_hour}时 "
                f"（窗口 [{start} ~ {end})）：群 {len(hour_rows)} 行 / "
                f"个人 Top {len(top_rows)} 行"
            )
        except Exception as e:
            logger.error(f"[Stats] 图片快照聚合失败（窗口 [{start} ~ {end})）: {e}")

    async def _aggregate_and_upsert_msg(self, start: datetime, end: datetime) -> None:
        """聚合窗口 ``[start, end)`` 的消息数并 UPSERT 入消息小时快照表。

        与图片侧 :meth:`_aggregate_and_upsert` 同构：行键取窗口起点的
        ``(date, hour)``；``groups`` → hour_rows（群级全量），``senders`` →
        top_rows（repo 侧已按 Top K 截断并附最新昵称）。任何异常仅记日志
        不抛出。

        Args:
            start: 窗口起点（含），本地时间
            end: 窗口终点（不含），本地时间
        """
        snap_date = start.strftime("%Y-%m-%d")
        snap_hour = start.hour
        try:
            top_k = await self._config.get_stats_setting_typed("stats_image_top_k")
            counts = await self._repo.get_msg_window_counts(start, end, top_k)
            groups = counts.get("groups") or {}
            senders = counts.get("senders") or {}
            hour_rows = [
                (snap_date, snap_hour, group_id, count)
                for group_id, count in groups.items()
            ]
            top_rows = [
                (snap_date, snap_hour, group_id, sender_id, sender_name, count)
                for group_id, entries in senders.items()
                for sender_id, sender_name, count in entries
            ]
            await self._config.snapshot_upsert_msg_hour(hour_rows, top_rows)
            logger.info(
                f"[Stats] 消息快照已更新 {snap_date} {snap_hour}时 "
                f"（窗口 [{start} ~ {end})）：群 {len(hour_rows)} 行 / "
                f"个人 Top {len(top_rows)} 行"
            )
        except Exception as e:
            logger.error(f"[Stats] 消息快照聚合失败（窗口 [{start} ~ {end})）: {e}")

    # ------------------------------------------------------------------
    # 写入侧：定时任务 / 回填 / 淘汰 / 强制刷新
    # ------------------------------------------------------------------

    async def run_hourly_snapshot(self, now: datetime | None = None) -> None:
        """定时任务：聚合上一个完整小时并 UPSERT 终值（双源）。

        窗口为 ``[上小时整点, 本小时整点)``（如 now=10:05 → 聚合 9 时档；
        now=00:05 → 聚合前一天 23 时档，行键自然跨天）。先图片后消息，
        top_k 读配置 ``stats_image_top_k``（图片/消息共用）。异常仅记日志
        不抛出（调度器侧另有退避）。

        Args:
            now: 当前时刻；None 时取 ``datetime.now()``（服务器本地时间）
        """
        now = now if now is not None else datetime.now()
        try:
            this_hour = self._hour_floor(now)
            start = this_hour - timedelta(hours=1)
            await self._aggregate_and_upsert(start, this_hour)
            await self._aggregate_and_upsert_msg(start, this_hour)
        except Exception as e:
            logger.error(f"[Stats] 小时快照任务失败（now={now}）: {e}")

    async def run_daily_snapshot(self, now: datetime | None = None) -> None:
        """定时任务：聚合上一个完整自然日并 UPSERT（双源日层）。

        窗口为 ``[昨日 00:00, 今日 00:00)``；repo.get_daily_batch 分别取
        消息/图片的每群每日总数（群级全量不截断），一次
        ``snapshot_upsert_daily`` 写入两张 daily 表。日层只存完整日——
        今天的数据由小时层承载。异常仅记日志不抛出。

        Args:
            now: 当前时刻；None 时取 ``datetime.now()``（服务器本地时间）
        """
        now = now if now is not None else datetime.now()
        try:
            today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
            start = today0 - timedelta(days=1)
            msg_rows = await self._repo.get_daily_batch("msg", start, today0)
            image_rows = await self._repo.get_daily_batch("image", start, today0)
            await self._config.snapshot_upsert_daily(msg_rows, image_rows)
            logger.info(
                f"[Stats] 日级快照已更新 {start.strftime('%Y-%m-%d')}"
                f"（窗口 [{start} ~ {today0})）：消息 {len(msg_rows)} 行 / "
                f"图片 {len(image_rows)} 行"
            )
        except Exception as e:
            logger.error(f"[Stats] 日级快照任务失败（now={now}）: {e}")

    async def run_monthly_snapshot(self, now: datetime | None = None) -> None:
        """定时任务：聚合上一自然月并 UPSERT（双源月层，游标防重）。

        目标月 = 上一自然月（"YYYY-MM"）。msg / image 两源各自独立：
        读游标（``snapshot_monthly_msg_covered`` /
        ``snapshot_monthly_image_covered``），游标 == 目标月则该源跳过；
        否则窗口 ``[目标月 1 日, 本月 1 日)`` 经 repo.get_monthly_batch
        聚合，防御性过滤只留 month == 目标月的行后 UPSERT，并推进游标。
        单源失败仅记日志，不影响另一源。

        Args:
            now: 当前时刻；None 时取 ``datetime.now()``（服务器本地时间）
        """
        now = now if now is not None else datetime.now()
        try:
            this_month_first = self._month_floor(now)
            prev_month_first = (this_month_first - timedelta(days=1)).replace(day=1)
            target = prev_month_first.strftime("%Y-%m")
            for source in ("msg", "image"):
                try:
                    cursor_key = _CURSOR_KEYS[source]
                    cursor = await self._config.get_setting(cursor_key, "")
                    if cursor == target:
                        # 该源已覆盖目标月，跳过
                        continue
                    rows = await self._repo.get_monthly_batch(
                        source, prev_month_first, this_month_first
                    )
                    # 防御：窗口边界恰为月初，理论上不会有邻月行混入
                    rows = [row for row in rows if row[0] == target]
                    if source == "msg":
                        await self._config.snapshot_upsert_monthly(rows, [])
                    else:
                        await self._config.snapshot_upsert_monthly([], rows)
                    await self._config.set_setting(cursor_key, target)
                    logger.info(
                        f"[Stats] 月级快照已更新 {target}（{source} 源）："
                        f"{len(rows)} 行"
                    )
                except Exception as e:
                    logger.error(
                        f"[Stats] 月级快照任务失败（{source} 源，now={now}）: {e}"
                    )
        except Exception as e:
            logger.error(f"[Stats] 月级快照任务失败（now={now}）: {e}")

    async def evict_expired(self, now: datetime | None = None) -> None:
        """淘汰过期快照行（PRD F4.3 阈值，月层永不淘汰）。

        阈值（"YYYY-MM-DD" 字符串，DELETE WHERE date < ?）：

        - 小时层群级双表 + 消息 Top K 表：今日 - 7 天；
        - 图片 Top K 表：今日 - 31 天（保护近 30 天报告的个人图片口径）；
        - 日层双表：horizon（上月 1 日），与三层归并严格对齐。

        异常仅记日志不抛出。

        Args:
            now: 当前时刻；None 时取 ``datetime.now()``（服务器本地时间）
        """
        now = now if now is not None else datetime.now()
        try:
            today = now.replace(hour=0, minute=0, second=0, microsecond=0)
            hourly_cutoff = (
                today - timedelta(days=self.HOURLY_RETENTION_DAYS)
            ).strftime("%Y-%m-%d")
            image_top_cutoff = (
                today - timedelta(days=self.IMAGE_TOP_RETENTION_DAYS)
            ).strftime("%Y-%m-%d")
            daily_cutoff = self._daily_horizon(now).strftime("%Y-%m-%d")
            ok = await self._config.snapshot_evict(
                hourly_cutoff, hourly_cutoff, image_top_cutoff, daily_cutoff
            )
            logger.info(
                f"[Stats] 快照淘汰已执行（hourly/msg_top<{hourly_cutoff}, "
                f"image_top<{image_top_cutoff}, daily<{daily_cutoff}）："
                f"{'成功' if ok else '失败（仅日志）'}"
            )
        except Exception as e:
            logger.error(f"[Stats] 快照淘汰任务失败（now={now}）: {e}")

    async def backfill_on_startup(self, now: datetime | None = None) -> None:
        """启动窗口重聚合（PRD F4.1）：历史回填 + 宕机缺档补偿。

        四个独立步骤（单步失败仅记日志，不影响后续）：

        1. 小时层窗口重聚合 ``[今日-7天 00:00, 当前整点)``：msg / image
           各一条批量聚合（repo.get_hourly_batch，含 Top K 与窗口内最新
           昵称），UPSERT 四张 hourly 表（消息侧 snapshot_upsert_msg_hour、
           图片侧既有 snapshot_upsert）；
        2. 日层窗口重聚合 ``[horizon, 今日 00:00)``：双源
           repo.get_daily_batch → snapshot_upsert_daily；
        3. 月层增量补齐：每源独立——游标为空则窗口起点 2000-01-01
           （全历史），否则游标月份的下一个月的 1 日；终点 = 本月 1 日；
           仅当起点 < 终点时聚合 UPSERT 并推进游标至上月 "YYYY-MM"；
        4. 顺带执行一次 :meth:`evict_expired`。

        全部幂等（UPSERT 覆盖写），重启重跑无副作用；月层由持久游标防重。
        异常仅记日志不阻断启动。

        Args:
            now: 当前时刻；None 时取 ``datetime.now()``（服务器本地时间）
        """
        now = now if now is not None else datetime.now()
        logger.info("[Stats] 快照启动回填开始")
        hourly_msg_rows = hourly_img_rows = daily_rows = monthly_rows = 0
        today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
        this_month_first = self._month_floor(now)

        # 步骤 1：小时层窗口重聚合
        try:
            win_start = today0 - timedelta(days=self.HOURLY_RETENTION_DAYS)
            win_end = self._hour_floor(now)
            if win_start < win_end:
                top_k = await self._config.get_stats_setting_typed("stats_image_top_k")
                msg_hour, msg_top = await self._repo.get_hourly_batch(
                    "msg", win_start, win_end, top_k
                )
                await self._config.snapshot_upsert_msg_hour(msg_hour, msg_top)
                hourly_msg_rows = len(msg_hour)
                img_hour, img_top = await self._repo.get_hourly_batch(
                    "image", win_start, win_end, top_k
                )
                await self._config.snapshot_upsert(img_hour, img_top)
                hourly_img_rows = len(img_hour)
        except Exception as e:
            logger.error(
                f"[Stats] 启动回填小时层失败（窗口起点 {today0} 前 7 天）: {e}"
            )

        # 步骤 2：日层窗口重聚合
        horizon = self._daily_horizon(now)
        try:
            if horizon < today0:
                msg_rows = await self._repo.get_daily_batch("msg", horizon, today0)
                img_rows = await self._repo.get_daily_batch("image", horizon, today0)
                await self._config.snapshot_upsert_daily(msg_rows, img_rows)
                daily_rows = len(msg_rows) + len(img_rows)
        except Exception as e:
            logger.error(f"[Stats] 启动回填日层失败（horizon={horizon}）: {e}")

        # 步骤 3：月层增量补齐（每源独立，单源失败不影响另一源）
        prev_month = (this_month_first - timedelta(days=1)).strftime("%Y-%m")
        for source in ("msg", "image"):
            try:
                cursor_key = _CURSOR_KEYS[source]
                cursor = await self._config.get_setting(cursor_key, "")
                cursor_first = self._parse_month_first(cursor)
                if cursor_first is None:
                    # 首次安装（游标缺省）或游标非法：全历史补齐
                    win_start = datetime(2000, 1, 1)
                else:
                    win_start = self._next_month_first(cursor_first)
                if win_start < this_month_first:
                    rows = await self._repo.get_monthly_batch(
                        source, win_start, this_month_first
                    )
                    if source == "msg":
                        await self._config.snapshot_upsert_monthly(rows, [])
                    else:
                        await self._config.snapshot_upsert_monthly([], rows)
                    monthly_rows += len(rows)
                    await self._config.set_setting(cursor_key, prev_month)
            except Exception as e:
                logger.error(f"[Stats] 启动回填月层失败（{source} 源）: {e}")

        # 步骤 4：顺带淘汰一次
        try:
            await self.evict_expired(now)
        except Exception as e:
            logger.error(f"[Stats] 启动回填淘汰失败: {e}")

        logger.info(
            "[Stats] 快照启动回填结束："
            f"小时层消息 {hourly_msg_rows} 行 / 图片 {hourly_img_rows} 行，"
            f"日层 {daily_rows} 行，月层 {monthly_rows} 行"
        )

    async def refresh_current_hour(self, now: datetime | None = None) -> None:
        """强制刷新：聚合当前未完整小时 ``[本小时整点, now)`` 并 UPSERT 覆盖。

        双源（先图片后消息）。开头限流：距上次成功执行不足
        ``REFRESH_MIN_INTERVAL_SECONDS``（60 秒，单调时钟戳，进程内）直接
        跳过——高频统计请求（Web 面板反复刷新、多群推送串行 build）不再
        反复打 MySQL 聚合。与整点任务同主键 UPSERT：刷新以部分值覆盖当前
        ``(date, hour)`` 行，小时后整点任务再以终值覆盖，行数始终不变。
        now 恰在整点时窗口为空，直接跳过（不发查询、不写库）。异常仅记
        日志不抛出——契约：强制刷新失败不阻断统计主流程。

        Args:
            now: 当前时刻；None 时取 ``datetime.now()``（服务器本地时间）
        """
        now = now if now is not None else datetime.now()
        if (
            time.monotonic() - self._last_refresh_mono
            < self.REFRESH_MIN_INTERVAL_SECONDS
        ):
            # 限流命中：距上次成功执行不足 60 秒，静默跳过
            return
        hour_start = self._hour_floor(now)
        if now <= hour_start:
            # now 恰在整点（minute=second=0）：当前小时窗口为空，无数据可聚合
            return
        await self._aggregate_and_upsert(hour_start, now)
        await self._aggregate_and_upsert_msg(hour_start, now)
        self._last_refresh_mono = time.monotonic()

    # ------------------------------------------------------------------
    # 读取侧：可服务性判定 + 三层归并
    # ------------------------------------------------------------------

    def is_range_serviceable(
        self, start: datetime, end: datetime, now: datetime | None = None
    ) -> bool:
        """判定范围 ``[start, end)`` 能否由快照三层归并供数（纯逻辑同步）。

        horizon = 上月 1 日（日快照覆盖 horizon 起所有完整日）。规则：

        1. ``start.date() < horizon`` 时，start 必须恰为其所在月 1 日
           （否则切断了旧月中间，月快照无法表达半月）；
        2. 设 last_day 为范围内最后一天（end 半开边界回退一刻）：
           ``last_day < horizon`` 时，end 必须恰为其所在月 1 日（对称规则）。

        「全部」预设 start=2000-01-01（月初）与 start >= horizon 的范围
        恒可服务。

        Args:
            start: 范围起点（含）
            end: 范围终点（不含）
            now: 当前时刻；None 时取 ``datetime.now()``

        Returns:
            bool: 可服务返回 True，否则 False（调用方整体回退实时 SQL）
        """
        now = now if now is not None else datetime.now()
        horizon = self._daily_horizon(now)
        # 规则 1：起点不得切断 horizon 前旧月的中间
        if start.date() < horizon.date() and start.date() != start.date().replace(
            day=1
        ):
            return False
        # 规则 2：终点不得切断 horizon 前旧月的中间（last_day 为范围内最后一天）
        last_day = (end - timedelta(microseconds=1)).date()
        if last_day < horizon.date() and end.date() != end.date().replace(day=1):
            return False
        return True

    async def _layered_by_group(
        self, source: str, start: datetime, end: datetime, group_id: str | None = None
    ) -> dict[str, int] | None:
        """三层归并核心：按群汇总 ``[start, end)`` 的快照总数。

        归并（无重叠、无空洞）：

        - 月层：``[start, end)`` 内完整包含且月起点早于 horizon 的连续
          月份区间，``snapshot_monthly_totals`` 一次读出；
        - 日/时层：剩余日期 ``[max(start.date(), horizon.date()), last_day]``
          逐日逐群归并——该 ``(date, group)`` 在 ``snapshot_hourly_date_totals``
          结果中有键用小时值（存在性优先），否则用 ``snapshot_daily_rows``
          行值（缺行按 0）；
        - 合并月层与日/时层（同群相加）；群集合 = 月层 ∪ hourly ∪ daily
          出现的群（未出现的群不进结果）。

        Args:
            source: 快照来源，"msg"（消息）或 "image"（图片）
            start: 范围起点（含）
            end: 范围终点（不含）
            group_id: 可选群过滤；None 时聚合全部群

        Returns:
            dict[str, int] | None: {group_id: 总数}；范围不可服务或任何
            异常返回 None（调用方回退实时 SQL）
        """
        try:
            now = datetime.now()
            if not self.is_range_serviceable(start, end, now):
                return None
            horizon = self._daily_horizon(now)

            # 月层：[start, end) 内完整包含的连续月份区间
            start_month_first = self._month_floor(start)
            first_full = (
                start_month_first
                if start == start_month_first
                else self._next_month_first(start_month_first)
            )
            # end 所在月必被 end 截断（或完全在范围外），最后一个完整月
            # 恒为其前一自然月
            last_full = (self._month_floor(end) - timedelta(days=1)).replace(day=1)
            monthly_map: dict[str, int] = {}
            if first_full <= last_full and first_full < horizon:
                # 月起点早于 horizon 的月份才走月层（>= horizon 的由日/时层覆盖）
                month_hi = min(last_full, (horizon - timedelta(days=1)).replace(day=1))
                monthly_map = await self._config.snapshot_monthly_totals(
                    source,
                    first_full.strftime("%Y-%m"),
                    month_hi.strftime("%Y-%m"),
                    group_id,
                )

            # 日/时层：剩余日期（构造上全部 >= horizon）
            merged: dict[str, int] = dict(monthly_map)
            last_day = (end - timedelta(microseconds=1)).date()
            date_lo = max(start.date(), horizon.date())
            date_hi = last_day
            if date_lo <= date_hi:
                date_lo_s = date_lo.strftime("%Y-%m-%d")
                date_hi_s = date_hi.strftime("%Y-%m-%d")
                hourly_map = await self._config.snapshot_hourly_date_totals(
                    source, date_lo_s, date_hi_s, group_id
                )
                daily_map = await self._config.snapshot_daily_rows(
                    source, date_lo_s, date_hi_s, group_id
                )
                for key in set(hourly_map) | set(daily_map):
                    gid = key[1]
                    # 存在性优先：hourly 有键用 hourly，否则 daily（缺行按 0）
                    count = (
                        hourly_map[key] if key in hourly_map else daily_map.get(key, 0)
                    )
                    merged[gid] = merged.get(gid, 0) + count
            return merged
        except Exception as e:
            logger.error(f"[Stats] 快照三层归并查询失败（{source} 源）: {e}")
            return None

    async def _daily_trend(
        self, source: str, start: datetime, end: datetime, group_id: str | None = None
    ) -> dict[str, int] | None:
        """快照日趋势：逐日总数（保留日期维度，缺失日不含键）。

        与 :meth:`_layered_by_group` 同款逐日归并（该 ``(date, group)`` 在
        hourly 聚合中有键用 hourly，否则用 daily 行），但 horizon 判定更严：
        要求 ``start.date() >= horizon``（月层无法表达日粒度），否则返回
        None 由调用方回退实时 SQL。

        Args:
            source: 快照来源，"msg"（消息）或 "image"（图片）
            start: 范围起点（含）
            end: 范围终点（不含）
            group_id: 可选群过滤；None 时聚合全部群

        Returns:
            dict[str, int] | None: {"YYYY-MM-DD": 当日总数}，范围内无数据
            的日期不含键（service 层补零）；不可服务或异常返回 None
        """
        try:
            now = datetime.now()
            horizon = self._daily_horizon(now)
            if start.date() < horizon.date():
                return None
            first_day = start.date()
            last_day = (end - timedelta(microseconds=1)).date()
            if first_day > last_day:
                return {}
            first_s = first_day.strftime("%Y-%m-%d")
            last_s = last_day.strftime("%Y-%m-%d")
            hourly_map = await self._config.snapshot_hourly_date_totals(
                source, first_s, last_s, group_id
            )
            daily_map = await self._config.snapshot_daily_rows(
                source, first_s, last_s, group_id
            )
            # 逐日逐群归并（hourly 存在性优先），保留日期维度
            per_day: dict[str, dict[str, int]] = {}
            for (date_s, gid), count in hourly_map.items():
                per_day.setdefault(date_s, {})[gid] = count
            for key, count in daily_map.items():
                if key in hourly_map:
                    continue  # 该 (date, group) 已有 hourly 值，存在性优先
                per_day.setdefault(key[0], {})[key[1]] = count
            result: dict[str, int] = {}
            day = first_day
            while day <= last_day:
                date_s = day.strftime("%Y-%m-%d")
                if date_s in per_day:
                    result[date_s] = sum(per_day[date_s].values())
                day += timedelta(days=1)
            return result
        except Exception as e:
            logger.error(f"[Stats] 快照日趋势查询失败（{source} 源）: {e}")
            return None

    async def msg_total(
        self, start: datetime, end: datetime, group_id: str | None = None
    ) -> int | None:
        """消息三层归并总数。不可服务或异常返回 None（回退实时 SQL）。"""
        per_group = await self._layered_by_group("msg", start, end, group_id)
        if per_group is None:
            return None
        return sum(per_group.values())

    async def msg_per_group(
        self, start: datetime, end: datetime
    ) -> dict[str, int] | None:
        """消息三层归并按群明细。不可服务或异常返回 None（回退实时 SQL）。"""
        return await self._layered_by_group("msg", start, end)

    async def msg_daily_trend(
        self, start: datetime, end: datetime, group_id: str | None = None
    ) -> dict[str, int] | None:
        """消息快照日趋势（今天 = 小时快照求和）。

        返回 ``{"YYYY-MM-DD": 当日总数}``，缺失日不含键（service 层补零）；
        ``start.date() < horizon``（上月 1 日）或异常返回 None。
        """
        return await self._daily_trend("msg", start, end, group_id)

    async def image_total(
        self, start: datetime, end: datetime, group_id: str | None = None
    ) -> int | None:
        """图片三层归并总数。不可服务或异常返回 None（回退实时 SQL）。"""
        per_group = await self._layered_by_group("image", start, end, group_id)
        if per_group is None:
            return None
        return sum(per_group.values())

    async def image_per_group(
        self, start: datetime, end: datetime
    ) -> dict[str, int] | None:
        """图片三层归并按群明细。不可服务或异常返回 None（回退实时 SQL）。"""
        return await self._layered_by_group("image", start, end)

    async def image_daily_trend(
        self, start: datetime, end: datetime, group_id: str | None = None
    ) -> dict[str, int] | None:
        """图片快照日趋势（今天 = 小时快照求和），与 msg_daily_trend 同构。"""
        return await self._daily_trend("image", start, end, group_id)

    # ------------------------------------------------------------------
    # 统计注入
    # ------------------------------------------------------------------

    async def fill_counts(self, data: StatsData) -> StatsData:
        """给 StatsData 注入图片快照统计（原地修改并返回）。

        v0.5.5 口径升级：

        - ``sender_ranking`` / ``member`` 的 ``image_count``：**逻辑不变**，
          仍取 ``snapshot_query`` 的 by_sender（image_stats_hourly_top，
          Top K 口径，保留期 31 天）；单群视图按 ``(group_id, sender_id)``
          匹配，全部群视图跨群按 sender_id 求和后匹配；
        - ``data.total_images`` 与 ``group_ranking[].image_count``：优先走
          **图片三层归并**（:meth:`image_total` / :meth:`image_per_group`，
          群级全量、无 Top K 截断、不受 image_records 滚动清理影响）；
          归并返回 None（范围不可服务/快照异常）→ 回落 ``snapshot_query``
          的 total / by_group 口径（即 v0.5.2 行为）。

        快照中查不到的成员保持 0（Top K 口径：K 外成员图片数不计）。
        注入失败仅记日志并原样返回（image_count 保持零值），绝不阻断
        统计主流程。

        Args:
            data: StatsService 组装完成的 StatsData（query 含群/时间范围）

        Returns:
            StatsData: 原地注入后的同一实例
        """
        try:
            group_id = data.query.group_id
            start = data.query.time_range.start
            end = data.query.time_range.end
            result = await self._config.snapshot_query(start, end, group_id=group_id)
            by_group = result.get("by_group") or {}
            by_sender = result.get("by_sender") or {}
            # 发言人维度（Top K 口径）：snapshot_query by_sender，逻辑不变
            if group_id is None:
                # 全部群视图：by_sender 键为 (gid, sender_id)，跨群按 sender 求和
                per_sender: dict[str, int] = {}
                for (_gid, sender_id), count in by_sender.items():
                    per_sender[sender_id] = per_sender.get(sender_id, 0) + count
                for item in data.sender_ranking:
                    item.image_count = per_sender.get(item.sender_id, 0)
                if data.member is not None:
                    data.member.image_count = per_sender.get(data.member.sender_id, 0)
            else:
                for item in data.sender_ranking:
                    item.image_count = by_sender.get((group_id, item.sender_id), 0)
                if data.member is not None:
                    data.member.image_count = by_sender.get(
                        (group_id, data.member.sender_id), 0
                    )
            # 总图片数：优先图片三层归并，None → 回落小时层 total 口径
            layered_total = await self.image_total(start, end, group_id)
            if layered_total is not None:
                data.total_images = layered_total
            else:
                data.total_images = int(result.get("total", 0))
            # 群排行 image_count：优先图片三层归并按群，None → 回落 by_group
            # （group_ranking 仅全部群视图非空）
            layered_groups = (
                await self.image_per_group(start, end) if group_id is None else None
            )
            for item in data.group_ranking:
                if layered_groups is not None:
                    item.image_count = layered_groups.get(item.group_id, 0)
                else:
                    item.image_count = by_group.get(item.group_id, 0)
        except Exception as e:
            logger.warning(f"[Stats] 注入图片快照数据失败（保持零值不阻断）: {e}")
        return data


# v0.5.0 兼容别名（类更名 ImageSnapshotManager → SnapshotManager）
ImageSnapshotManager = SnapshotManager
