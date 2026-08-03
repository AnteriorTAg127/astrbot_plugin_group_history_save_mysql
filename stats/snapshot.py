"""图片统计小时级快照管理（v0.5.0 数据分析模块 D）。

背景（PRD 2.1 F4）：``image_records`` 表按 ``image_retention_days`` 滚动清理，
过期图片删除后无法回溯统计；且个人级图片明细行数大，不宜在统计请求里实时
全量聚合。方案为「小时级增量快照」：把 MySQL ``image_records`` 的窗口聚合结果
UPSERT 进 config.db 的两张快照表（db_config.ConfigManager 管理）：

- ``image_stats_hourly``     群级每小时图片数（全量口径，不受 Top K 影响）
- ``image_stats_hourly_top`` 每小时每群个人 Top K（``stats_image_top_k`` 配置）

三个公开入口（契约见 ``开发/v0.5.0/分工.md``「接口契约 → 图片快照」）：

- :meth:`ImageSnapshotManager.run_hourly_snapshot`
  定时任务（调度器每小时第 5 分钟调用）：聚合**上一个完整小时**
  ``[上小时整点, 本小时整点)``，UPSERT 该小时的终值。
- :meth:`ImageSnapshotManager.refresh_current_hour`
  强制刷新（手动统计调用前置步骤）：聚合**当前未完整小时**
  ``[本小时整点, now)``，以部分值覆盖当前 ``(date, hour)``，保证「今日」实时。
- :meth:`ImageSnapshotManager.fill_counts`
  给组装好的 :class:`StatsData` 注入图片数（total_images、发言人排行 /
  群排行 / 个人的 image_count），一次 snapshot_query 查全时间范围。

UPSERT 语义：两张快照表主键分别为 ``(date, hour, group_id)`` 与
``(date, hour, group_id, sender_id)``，写入经 ``ConfigManager.snapshot_upsert``
走 ``INSERT ... ON CONFLICT DO UPDATE``——同一窗口重复聚合只覆盖旧值、不产生
新行：强制刷新先用部分值覆盖，整点定时任务随后用终值覆盖，天然幂等可重放；
快照行只增盖不删，``image_records`` 清理后历史统计仍可回溯。行键 ``(date, hour)``
一律取**窗口起点**所在小时（跨天窗口自然落到前一天 23 时）。

异常策略：本层绝不向上抛异常。定时任务 / 强制刷新失败仅记日志（契约：强制
刷新失败不阻断统计主流程）；:meth:`fill_counts` 注入失败时保持 image_count
零值原样返回。底层 repo 聚合已有 QUERY_TIMEOUT_SECONDS 超时兜底，
snapshot_upsert / snapshot_query 自身亦吞异常仅记日志。

时区口径：全部使用服务器本地时间（``datetime.now()``），与 StatsRepository
窗口传参口径一致，不依赖 MySQL 服务器时区。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from astrbot.api import logger

from .models import StatsData

# 仅静态检查用；运行时不 import repository（避免拉起 db_mysql 依赖链，
# 保持 stats 轻量子模块的 stub 隔离兼容性）
if TYPE_CHECKING:
    from .repository import StatsRepository

__all__ = ["ImageSnapshotManager"]


class ImageSnapshotManager:
    """图片统计小时级快照管理器（定时聚合 + 强制刷新 + 统计注入）。

    依赖注入（由 StatsService 构造）：

    - ``config_mgr``：ConfigManager（db_config），提供
      ``get_stats_setting_typed`` / ``snapshot_upsert`` / ``snapshot_query``
    - ``repo``：StatsRepository（stats/repository），提供
      ``get_image_window_counts`` 图片窗口聚合
    """

    def __init__(self, config_mgr, repo: StatsRepository):
        """初始化管理器。

        Args:
            config_mgr: ConfigManager 实例（快照读写 + Top K 配置）
            repo: StatsRepository 实例（image_records 窗口聚合）
        """
        self._config = config_mgr
        self._repo = repo

    # ------------------------------------------------------------------
    # 定时任务 / 强制刷新
    # ------------------------------------------------------------------

    @staticmethod
    def _hour_floor(moment: datetime) -> datetime:
        """取给定时刻所在小时的整点起点（分钟/秒/微秒归零）。"""
        return moment.replace(minute=0, second=0, microsecond=0)

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

    async def run_hourly_snapshot(self, now: datetime | None = None) -> None:
        """定时任务：聚合上一个完整小时并 UPSERT 终值。

        窗口为 ``[上小时整点, 本小时整点)``（如 now=10:05 → 聚合 9 时档；
        now=00:05 → 聚合前一天 23 时档，行键自然跨天）。top_k 读配置
        ``stats_image_top_k``。异常仅记日志不抛出（调度器侧另有退避）。

        Args:
            now: 当前时刻；None 时取 ``datetime.now()``（服务器本地时间）
        """
        now = now if now is not None else datetime.now()
        this_hour = self._hour_floor(now)
        await self._aggregate_and_upsert(this_hour - timedelta(hours=1), this_hour)

    async def refresh_current_hour(self, now: datetime | None = None) -> None:
        """强制刷新：聚合当前未完整小时 ``[本小时整点, now)`` 并 UPSERT 覆盖。

        与整点任务同主键 UPSERT：刷新以部分值覆盖当前 ``(date, hour)`` 行，
        小时后整点任务再以终值覆盖，行数始终不变。now 恰在整点时窗口为空，
        直接跳过（不发查询、不写库）。异常仅记日志不抛出——契约：强制刷新
        失败不阻断统计主流程。

        Args:
            now: 当前时刻；None 时取 ``datetime.now()``（服务器本地时间）
        """
        now = now if now is not None else datetime.now()
        hour_start = self._hour_floor(now)
        if now <= hour_start:
            # now 恰在整点（minute=second=0）：当前小时窗口为空，无数据可聚合
            return
        await self._aggregate_and_upsert(hour_start, now)

    # ------------------------------------------------------------------
    # 统计注入
    # ------------------------------------------------------------------

    async def fill_counts(self, data: StatsData) -> StatsData:
        """给 StatsData 注入图片快照统计（原地修改并返回）。

        一次 ``snapshot_query(start, end, group_id)`` 查全时间范围
        （快照查询按 (date, hour) 半开区间过滤，与查询范围的小时粒度对齐），
        随后注入：

        - ``data.total_images``：范围内图片总数（群级全量口径）；
        - ``sender_ranking`` 各项 ``image_count``：单群视图按
          ``(group_id, sender_id)`` 匹配；全部群视图（group_id=None）将
          by_sender 的 ``(gid, sender_id)`` 键跨群按 sender_id 求和后匹配；
        - ``group_ranking`` 各项 ``image_count``：按 by_group 匹配
          （仅全部群视图非空）；
        - ``member.image_count``：匹配规则同 sender_ranking。

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
            result = await self._config.snapshot_query(
                data.query.time_range.start,
                data.query.time_range.end,
                group_id=group_id,
            )
            by_group = result.get("by_group") or {}
            by_sender = result.get("by_sender") or {}
            data.total_images = int(result.get("total", 0))
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
            for item in data.group_ranking:
                item.image_count = by_group.get(item.group_id, 0)
        except Exception as e:
            logger.warning(f"[Stats] 注入图片快照数据失败（保持零值不阻断）: {e}")
        return data
