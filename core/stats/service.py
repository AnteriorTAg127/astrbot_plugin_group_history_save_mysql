"""统计编排服务（v0.5.0 数据分析模块 G，v0.5.5 群级查询迁分段快照）。

三端同源的数据组装与推送编排层，串联模块 B（MySQL 聚合仓储）、模块 D
（分段快照：消息+图片 × 小时/日/月三段式）、模块 E（T2I 渲染）与模块 F
（调度器，惰性引用）：

- :meth:`StatsService.build_stats`：**唯一**数据组装入口（Web ``/stats/data``、
  ``/群统计`` 指令、定时推送三端共用），顺序为——强制刷新当前小时快照 →
  群级维度快照取数判定（总消息数 / 每日趋势 / 群排行 count 走**消息快照
  三层归并**（月+日+时），快照返回 None（范围不可服务/快照异常）精确回退
  对应 repo 实时 SQL 路径）→ ``asyncio.gather`` 并发聚合查询（按 query
  维度裁剪）→ daily_trend 连续补零 → 个人维度 ratio/rank/avg 计算 →
  ``snapshot.fill_counts`` 注入图片数；任何底层异常统一包装为
  :class:`StatsBuildError`（友好文案）上抛；活跃成员数、首末条时间、
  发言人排行、个人维度保持实时 SQL（群级快照无法推导）；
- :meth:`StatsService.overview_daily_stats`：Web 概览「最近 N 天趋势」快照
  口径供数（v0.5.5，days ≤ 31 时消息/图片日快照逐日组装，供数前触发一次
  限流强制刷新保证「今天」柱子实时；越界/快照不可用/任何异常返回 None，
  调用方回落 MySQL 实时路径）；
- :meth:`StatsService.check_cooldown`：``/群统计`` 每群冷却（时长读
  ``stats_cooldown``，0=不限流），放行即盖章（``time.monotonic()``）；
- :meth:`StatsService.record_group_umo` / :meth:`StatsService.get_group_umo`：
  推送目标 umo 内存缓存（main.py 群消息路径实时登记，重启后由首条群消息重建）；
- :meth:`StatsService.push_report`：日报/周报推送——遍历群级开关开启的群，
  build+render 后经 ``context.send_message(umo, chain)`` 主动发送图片报告卡；
  无 umo warning 跳过；渲染失败降级纯文本摘要再失败则跳过；群间 1s 防限频；
  单群任何异常仅记日志不阻断整批；
- :meth:`StatsService.start` / :meth:`StatsService.stop`：惰性创建/停止
  ``stats.scheduler.StatsScheduler``（模块 F 并行开发，按契约
  ``StatsScheduler(service, snapshot, config_mgr)`` 惰性引用），均可重入、
  异常不外抛；stop 同时取消安全清理启动回填任务句柄；
- :meth:`StatsService.startup_backfill`：启动历史回填后台任务薄封装
  （v0.5.5，``create_task(snapshot.backfill_on_startup())`` 自持句柄，
  重复调用幂等，stop 时 cancel + await）。

签名说明：契约草图中 ``check_cooldown`` 为同步 def，但冷却时长需经
``db_config.get_stats_setting_typed``（async-only）实时读取，故实现为异步
方法（``await service.check_cooldown(gid)``），与 profile/summary 侧异步
冷却检查范式一致；模块 M 接线时按实际签名 await 调用。

日志统一 ``[Stats]`` 前缀。契约见 ``开发/v0.5.0/分工.md``「接口契约 →
编排服务（模块 G）」；v0.5.5 迁移点与回退矩阵见 ``开发/v0.5.5/分工.md``
「E. stats/service.py 迁移点与回退矩阵」，字段与注释与其保持一致。
"""

from __future__ import annotations

import asyncio
import math
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import MessageChain

from .models import (
    GroupRankItem,
    MemberStats,
    SenderRankItem,
    StatsData,
    StatsQuery,
    StatsTimeRange,
)
from .repository import StatsRepository
from .t2i_render import StatsT2IRenderer

if TYPE_CHECKING:
    from astrbot.api.star import Context, Star

    from ..db_config import ConfigManager
    from ..db_mysql import MySQLManager
    from .snapshot import ImageSnapshotManager

__all__ = ["StatsBuildError", "StatsService"]

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# build_stats 统一兜底文案（任何底层异常均包装为此文案上抛）
_BUILD_ERROR_MSG = "统计数据查询失败，请稍后重试"

# 配置读取异常时的最终兜底值（配置层本身已含默认值回退，此处仅防御异常类型）
_DEFAULT_COOLDOWN_SECONDS = 30
_DEFAULT_TOP_N = 10

# 排行条数合法范围（与 db_config.STATS_RANGES["stats_top_n"] 一致）
_TOP_N_MIN = 1
_TOP_N_MAX = 50

# 个人维度名次查找用的完整排行深度（PRD 排行上限 50）
_MEMBER_RANK_SCAN_LIMIT = 50

# 推送逐群串行发送的群间间隔（秒），防协议端限频
_PUSH_INTERVAL_SECONDS = 1.0

# 推送报告卡标题 / 纯文本摘要种类名
_PUSH_TITLES = {"daily": "群聊数据日报", "weekly": "群聊数据周报"}
_PUSH_KIND_NAMES = {"daily": "日报", "weekly": "周报"}


class StatsBuildError(Exception):
    """统计数据组装失败（附带面向用户的友好文案）。

    ``build_stats`` 内任何底层异常（repo 查询超时/SQL 错误、快照注入异常等）
    统一包装为本异常上抛；Web/指令/推送三端捕获后直接回 :attr:`message`
    文案或记日志跳过，无需关心底层细节。
    """


class StatsService:
    """数据分析编排服务：build_stats 单一组装出口 + 冷却 + umo 缓存 + 推送。

    v0.5.5 起群级查询（总消息数 / 每日趋势 / 群排行 count）迁分段快照读数，
    快照不可用时精确回退实时 SQL（回退矩阵见 开发/v0.5.5/分工.md）；Web
    概览趋势经 :meth:`overview_daily_stats` 走快照口径。

    Attributes:
        repo: MySQL 聚合仓储（模块 B）实例，公开以便测试注入替换。
        snapshot: 分段快照管理器（模块 D，消息+图片 × 小时/日/月）实例，
            公开以便测试注入替换。
        renderer: T2I 渲染器（模块 E）实例，公开以便测试注入替换。
    """

    def __init__(
        self,
        context: Context,
        mysql_mgr: MySQLManager,
        config_mgr: ConfigManager,
        star: Star,
    ) -> None:
        """构造服务并自建上游模块实例（均暴露同名公开属性）。

        Args:
            context: AstrBot ``Context``（主动推送经 ``send_message(umo, chain)``）。
            mysql_mgr: ``db_mysql.MySQLManager`` 实例（仅透传给仓储）。
            config_mgr: ``db_config.ConfigManager`` 实例（stats 配置/推送开关）。
            star: 插件 Star 实例（T2I 渲染器经其 ``html_render`` 鸭子类型约定工作，
                范式同 summary/profile 服务的 star 参数）。
        """
        self._context = context
        self._config_mgr = config_mgr

        self.repo = StatsRepository(mysql_mgr)
        # 模块 D 与本模块并行开发，按分工契约签名（config_mgr, repo）惰性引入，
        # 避免模块顶层急切 import 拉长依赖链（范式同 summary/service.py 的局部导入）
        from .snapshot import ImageSnapshotManager

        self.snapshot: ImageSnapshotManager = ImageSnapshotManager(
            config_mgr, self.repo
        )
        self.renderer = StatsT2IRenderer(star, config_mgr)

        # 推送目标缓存：group_id → 最新 event.unified_msg_origin（仅内存不落盘，
        # 重启后由首条群消息重建，重建前该群推送跳过并 warning）
        self._umo_cache: dict[str, str] = {}
        # 指令冷却盖章表：group_id → time.monotonic() 时间戳（仅内存）
        self._cooldown: dict[str, float] = {}
        # 调度器（模块 F）：start() 时惰性创建，未启动为 None
        self._scheduler = None
        # 启动回填后台任务句柄（v0.5.5）：startup_backfill() 创建，
        # stop() 取消安全清理；None = 未创建或已清理
        self._backfill_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """惰性创建并启动统计调度器（模块 F）。

        可重入：已启动时直接返回；启动失败仅记 error 不阻断插件启动
        （统计查询/推送手动链路不依赖调度器），且 ``_scheduler`` 保持
        None 以便后续重试。
        """
        if self._scheduler is not None:
            return
        try:
            # 模块 F 并行开发，按契约签名（service, snapshot, config_mgr）惰性引用
            from .scheduler import StatsScheduler

            scheduler = StatsScheduler(self, self.snapshot, self._config_mgr)
            await scheduler.start()
            self._scheduler = scheduler
            logger.info("[Stats] 统计调度器已启动（定时推送 + 小时级图片快照）")
        except Exception:
            logger.error(
                "[Stats] 统计调度器启动失败（不阻断统计查询能力）", exc_info=True
            )

    async def stop(self) -> None:
        """停止调度器、取消启动回填任务并清空内存缓存（umo / 冷却盖章表）。

        可重入、异常不外抛：调度器停止异常仅记日志，缓存清理恒执行。
        """
        # 启动回填任务取消安全清理（v0.4.5 范式：cancel 后必须 await 等待真正
        # 退出并吞 CancelledError，避免悬挂任务；句柄已自然结束同样置 None）
        if self._backfill_task is not None and not self._backfill_task.done():
            self._backfill_task.cancel()
            try:
                await self._backfill_task
            except asyncio.CancelledError:
                pass
        self._backfill_task = None
        try:
            if self._scheduler is not None:
                try:
                    await self._scheduler.stop()
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logger.warning("[Stats] 停止统计调度器异常", exc_info=True)
                self._scheduler = None
        finally:
            self._umo_cache.clear()
            self._cooldown.clear()

    async def startup_backfill(self) -> None:
        """启动快照历史回填（v0.5.5 PRD F4.1，MySQL 初始化成功后由 main.py 调用）。

        薄封装：``asyncio.create_task(snapshot.backfill_on_startup())`` 并自持
        句柄（:meth:`stop` 负责取消安全清理）。幂等：已有句柄未结束时直接
        返回，不新建任务；句柄已自然结束（完成/异常）则允许再次发起。回填
        自身异常由快照层吞掉仅记日志（契约），绝不阻断插件启动。
        """
        if self._backfill_task is not None and not self._backfill_task.done():
            return
        self._backfill_task = asyncio.create_task(self.snapshot.backfill_on_startup())

    # ------------------------------------------------------------------
    # umo 缓存（推送目标）
    # ------------------------------------------------------------------

    def record_group_umo(self, group_id: str, umo: str) -> None:
        """登记群 → umo 映射（群消息监听路径实时调用）。

        空串/None 的 group_id 或 umo 一律不记录（防御脏数据污染缓存）。
        """
        gid = str(group_id or "").strip()
        value = str(umo or "").strip()
        if not gid or not value:
            return
        self._umo_cache[gid] = value

    def get_group_umo(self, group_id: str) -> str | None:
        """读取群 → umo 映射；无缓存（或入参为空）返回 None。"""
        gid = str(group_id or "").strip()
        if not gid:
            return None
        return self._umo_cache.get(gid)

    # ------------------------------------------------------------------
    # 指令侧冷却
    # ------------------------------------------------------------------

    async def check_cooldown(self, group_id: str) -> bool:
        """``/群统计`` 每群冷却检查（放行即盖章）。

        时长读 ``stats_cooldown`` 配置（0=不限流，恒放行不盖章）；
        冷却期内返回 False（**不盖章**，不刷新剩余时间）；放行返回 True
        并以 ``time.monotonic()`` 盖章。

        Args:
            group_id: 群号（内部 strip 后作缓存键）。

        Returns:
            bool: True=放行并已盖章；False=仍在冷却期内。
        """
        gid = str(group_id or "").strip()
        cooldown = await self._int_setting("stats_cooldown", _DEFAULT_COOLDOWN_SECONDS)
        if cooldown <= 0:
            return True  # 0 = 不限流
        now = time.monotonic()
        last = self._cooldown.get(gid)
        if last is not None and now - last < cooldown:
            return False
        self._cooldown[gid] = now
        return True

    # ------------------------------------------------------------------
    # 核心数据组装（Web / 指令 / 推送共用入口）
    # ------------------------------------------------------------------

    async def build_stats(self, query: StatsQuery) -> StatsData:
        """组装完整统计数据（三端同源的唯一入口）。

        流程：强制刷新当前小时快照 → 群级维度快照取数判定（total_messages /
        daily_trend / 群排行 count 走消息快照三层归并（月+日+时）；快照返回
        None（范围不可服务/快照异常）精确回退对应 repo 实时 SQL，回退矩阵见
        ``开发/v0.5.5/分工.md``）→ ``asyncio.gather`` 并发聚合查询（按 query
        维度裁剪：群维度查发言人排行、全部群视图查群排行、个人维度附加
        member_overview 与 sender 维度分布/完整排行；快照路径的 overview 仅查
        活跃数与首末条时间 meta、群排行仅查每群活跃数）→ daily_trend 连续补零
        （start 所在日 ~ end 前一日）→ ratio/rank/peak_hour/avg_per_day 计算 →
        ``snapshot.fill_counts`` 注入图片数。

        Args:
            query: 统计查询条件（group_id=None 为全部群汇总；member_id
                非 None 为个人维度；top_n 内部再夹住 1–50 防御）。

        Returns:
            StatsData：完整组装结果（``generated_at`` 为组装时刻）。

        Raises:
            StatsBuildError: 任何底层异常（查询超时/SQL 错误/快照注入失败
                等）统一包装为友好文案上抛。
        """
        try:
            return await self._build(query)
        except StatsBuildError:
            raise
        except Exception as e:
            logger.error(f"[Stats] build_stats 异常: {e}", exc_info=True)
            raise StatsBuildError(_BUILD_ERROR_MSG) from e

    async def _build(self, query: StatsQuery) -> StatsData:
        """build_stats 内部实现（异常由外层统一包装为 StatsBuildError）。

        v0.5.5 数据源切换（PRD F2 回退矩阵）：total_messages / daily_trend /
        群排行 count 三处先问消息快照三层归并（毫秒级 SQLite 读，gather 前
        完成判定），快照返回 None（范围不可服务/快照异常）的维度精确回退
        对应 repo 实时 SQL 路径，输出字段与 v0.5.2 逐字段一致。
        """
        group_id = query.group_id
        member_id = query.member_id
        start = query.time_range.start
        end = query.time_range.end
        top_n = self._clamp_top_n(query.top_n)

        # 1) 强制刷新当前小时快照（契约：内部已 60s 限流、异常仅日志不抛；
        #    若意外抛出由外层统一兜底为 StatsBuildError）
        await self.snapshot.refresh_current_hour()

        # 2) 快照取数判定（v0.5.5 群级迁快照；None = 该维度回退实时 SQL）
        snap_total = await self.snapshot.msg_total(start, end, group_id)
        snap_trend = await self.snapshot.msg_daily_trend(start, end, group_id)
        snap_per_group = None
        if group_id is None and member_id is None:
            snap_per_group = await self.snapshot.msg_per_group(start, end)

        # 3) 并发聚合查询（快照路径的实时配套 / 回退协程一并塞入 tasks，
        #    保持单次 gather 结构）
        tasks: dict = {
            "hourly": self.repo.get_hourly_dist(group_id, None, start, end),
            "weekday": self.repo.get_weekday_dist(group_id, None, start, end),
        }
        if snap_total is None:
            # 回退：整体走 v0.5.2 全量实时查询（含 COUNT(*)）
            tasks["overview"] = self.repo.get_overview(group_id, start, end)
        else:
            # 快照路径：total 由快照供数，实时 SQL 只补活跃数与首末条时间
            tasks["overview_meta"] = self.repo.get_overview_meta(group_id, start, end)
        if snap_trend is None:
            tasks["trend"] = self.repo.get_daily_trend(group_id, start, end)
        if member_id is not None:
            # 个人维度：附加个人概览 + sender 维度双分布；
            # 完整排行（limit=50）兼顾名次查找与展示排行切片（单次查询两用）
            tasks["member_overview"] = self.repo.get_member_overview(
                group_id, member_id, start, end
            )
            tasks["member_hourly"] = self.repo.get_hourly_dist(
                group_id, member_id, start, end
            )
            tasks["member_weekday"] = self.repo.get_weekday_dist(
                group_id, member_id, start, end
            )
            if group_id is not None:
                tasks["ranking_full"] = self.repo.get_sender_ranking(
                    group_id, start, end, _MEMBER_RANK_SCAN_LIMIT
                )
        elif group_id is not None:
            tasks["sender_ranking"] = self.repo.get_sender_ranking(
                group_id, start, end, top_n
            )
        elif snap_per_group is None:
            # 全部群视图回退：v0.5.2 群排行实时查询（含 COUNT(*) 与活跃数）
            tasks["group_ranking"] = self.repo.get_group_ranking(start, end, top_n)
        else:
            # 全部群视图快照路径：count 由快照供数，活跃数无法由群级快照
            # 推导，实时补齐（Python 侧合并见下文）
            tasks["group_active"] = self.repo.get_group_active_senders(start, end)

        keys = list(tasks)
        values = await asyncio.gather(*(tasks[k] for k in keys))
        got = dict(zip(keys, values))

        # 4) 总览（快照 total + 实时 meta；回退时整体取 repo overview）/
        #    分布 / 趋势补零 / 峰值
        if snap_total is None:
            overview = got.get("overview") or {}
        else:
            overview = {"total": snap_total, **(got.get("overview_meta") or {})}
        total = int(overview.get("total", 0) or 0)
        hourly = list(got.get("hourly") or [])
        weekday = list(got.get("weekday") or [])
        # 快照日趋势 dict → [(date, count)] 交给既有补零逻辑（顺序不敏感）
        trend_raw = (
            list(snap_trend.items()) if snap_trend is not None else got.get("trend")
        )
        trend = self._fill_trend(trend_raw, start, end)
        peak_hour = self._peak_of(hourly)

        # 5) 排行（个人维度展示排行取完整排行前 top_n；全部群视图无发言人排行）
        if member_id is not None:
            sender_rows = (
                (got.get("ranking_full") or [])[:top_n] if group_id is not None else []
            )
        elif group_id is not None:
            sender_rows = got.get("sender_ranking") or []
        else:
            sender_rows = []
        sender_ranking = [self._sender_item(row) for row in sender_rows]
        if group_id is None and member_id is None:
            if snap_per_group is None:
                rank_rows = got.get("group_ranking") or []
            else:
                # 快照路径：Python 侧合并 count 与活跃数，排序口径与
                # repo.get_group_ranking 一致（count DESC、同数 group_id ASC）
                active = got.get("group_active") or {}
                rank_rows = [
                    {
                        "group_id": gid,
                        "count": cnt,
                        "active_senders": active.get(gid, 0),
                    }
                    for gid, cnt in snap_per_group.items()
                ]
                rank_rows.sort(key=lambda row: (-row["count"], row["group_id"]))
                rank_rows = rank_rows[:top_n]
            group_ranking = [self._group_item(row) for row in rank_rows]
        else:
            group_ranking = []

        # 6) 个人维度 MemberStats 组装
        member = None
        if member_id is not None:
            mo = got.get("member_overview") or {}
            m_count = int(mo.get("count", 0) or 0)
            ratio = (m_count / total) if total > 0 else 0.0  # total=0 → 0.0
            rank = None  # 全部群视图（group_id=None）恒 None
            if group_id is not None:
                for idx, row in enumerate(got.get("ranking_full") or [], start=1):
                    if str(row.get("sender_id", "")) == str(member_id):
                        rank = idx
                        break
            member = MemberStats(
                sender_id=str(member_id),
                sender_name=str(mo.get("name") or ""),
                count=m_count,
                image_count=0,  # 由 snapshot.fill_counts 注入
                ratio=ratio,
                rank=rank,
                active_days=int(mo.get("active_days", 0) or 0),
                avg_per_day=m_count / self._range_days(start, end),
                hourly_dist=list(got.get("member_hourly") or []),
                weekday_dist=list(got.get("member_weekday") or []),
            )

        data = StatsData(
            query=query,
            total_messages=total,
            total_images=0,  # 由 snapshot.fill_counts 注入
            active_senders=int(overview.get("active_senders", 0) or 0),
            peak_hour=peak_hour,
            first_msg_time=overview.get("first"),
            last_msg_time=overview.get("last"),
            daily_trend=trend,
            hourly_dist=hourly,
            weekday_dist=weekday,
            sender_ranking=sender_ranking,
            group_ranking=group_ranking,
            member=member,
            generated_at=datetime.now(),
        )
        # 7) 注入图片数（total_images / 排行 / 群排行 / 个人 image_count）
        return await self.snapshot.fill_counts(data)

    # ------------------------------------------------------------------
    # 渲染（透传模块 E）
    # ------------------------------------------------------------------

    async def render(self, data: StatsData, title: str) -> str | None:
        """渲染报告卡图片（透传 ``renderer.render_card``）。

        Returns:
            图片文件路径或 http(s) URL；渲染失败返回 None（render_card
            契约绝不抛异常）。
        """
        return await self.renderer.render_card(data, title)

    # ------------------------------------------------------------------
    # Web 概览趋势（v0.5.5 快照口径，PRD F3）
    # ------------------------------------------------------------------

    async def overview_daily_stats(
        self, days: int, now: datetime | None = None
    ) -> list[dict] | None:
        """Web 概览「最近 N 天趋势」快照口径供数（PRD F3，供 api_daily_stats）。

        days ≤ 31 时由消息/图片日快照供数（今天 = 小时快照求和）：供数前
        触发一次 ``refresh_current_hour``（60s 限流在快照层内部），保证
        「今天」柱子实时；窗口 ``[今日 00:00 - days 天, 明日 00:00)``，
        逐日组装 ``{"date", "messages", "images"}``，窗口内缺失日补 0，
        按日期升序返回。快照行只增盖不删，image_records 滚动清理不再影响
        趋势（快照行在清理前已固化）。

        Args:
            days: 趋势天数；<1 或 >31（快照日趋势仅服务 31 天内）返回 None。
            now: 当前时刻（缺省 ``datetime.now()``），独立成参便于测试注入。

        Returns:
            list[dict] | None: 逐日条目列表；days 越界 / 快照不可用
            （msg 或 image 任一趋势返回 None）/ 任何异常返回 None
            （契约：调用方 WebAPI 回落 MySQL 实时路径）。
        """
        try:
            if days < 1 or days > 31:
                return None
            if now is None:
                now = datetime.now()
            today = datetime(now.year, now.month, now.day)
            start = today - timedelta(days=days)
            end = today + timedelta(days=1)
            await self.snapshot.refresh_current_hour(now)
            msgs = await self.snapshot.msg_daily_trend(start, end)
            imgs = await self.snapshot.image_daily_trend(start, end)
            if msgs is None or imgs is None:
                return None
            items: list[dict] = []
            cur = start.date()
            last = today.date()
            while cur <= last:
                key = cur.strftime("%Y-%m-%d")
                items.append(
                    {
                        "date": key,
                        "messages": msgs.get(key, 0),
                        "images": imgs.get(key, 0),
                    }
                )
                cur += timedelta(days=1)
            return items
        except Exception as e:
            logger.warning(f"[Stats] 概览趋势快照供数失败（回落 MySQL 实时路径）: {e}")
            return None

    # ------------------------------------------------------------------
    # 推送侧（调度器调用）
    # ------------------------------------------------------------------

    async def is_all_mode(self) -> bool:
        """读取全局记录模式开关（all_mode，v0.5.1 群列表模式感知配套）。

        Returns:
            bool: 配置异常时按 False（白名单模式）处理，保守不扩大列表源。
        """
        try:
            settings = await self._config_mgr.get_all_settings()
            return settings.get("all_mode", "false") == "true"
        except Exception as e:
            logger.warning(f"[Stats] 读取 all_mode 失败（按白名单模式处理）: {e}")
            return False

    async def resolve_push_groups(self) -> list[dict]:
        """群级推送开关列表（模式感知，Web 展示与推送发送共用）。

        白名单模式下以 group_config 白名单为基准（get_push_groups 原语义）；
        all_mode 全局模式下白名单为空，改以 chat_history 有数据的群为基准，
        开关状态经 get_push_flags 批量读取（默认关）。

        Returns:
            list[dict]: [{"group_id": str, "enabled": bool}]（all_mode 下附带
            "count" 消息数供前端展示）；底层异常时 all_mode 分支返回 []。
        """
        if not await self.is_all_mode():
            return await self._config_mgr.get_push_groups()
        try:
            data_groups = await self.repo.get_all_groups_summary()
        except Exception as e:
            logger.error(f"[Stats] 查询有数据的群失败（推送列表为空）: {e}")
            return []
        ids = [g["group_id"] for g in data_groups]
        flags = await self._config_mgr.get_push_flags(ids)
        return [
            {
                "group_id": g["group_id"],
                "enabled": bool(flags.get(g["group_id"])),
                "count": g["count"],
            }
            for g in data_groups
        ]

    async def resolve_dropdown_groups(self) -> list[dict]:
        """Web 群下拉列表：白名单 ∪ 有数据的群（去重，消息数降序）。

        白名单模式：白名单群 + 残留历史数据群；all_mode：即有数据的群。
        保证两种模式下下拉框都能列出可查看统计的群（修复 all_mode 下
        只剩「全部群」的问题）。

        Returns:
            list[dict]: [{"group_id": str, "enabled": bool, "count": int | None}]；
            enabled 为白名单启用态（不在白名单记 True，all_mode 无白名单概念）；
            count 为历史消息总数（仅白名单且无数据的群为 None）。
        """
        try:
            whitelist = await self._config_mgr.get_groups()
        except Exception as e:
            logger.warning(f"[Stats] 读取白名单失败（下拉仅含有数据的群）: {e}")
            whitelist = []
        try:
            data_groups = await self.repo.get_all_groups_summary()
        except Exception as e:
            logger.warning(f"[Stats] 查询有数据的群失败（下拉仅含白名单）: {e}")
            data_groups = []
        merged: dict[str, dict] = {}
        for entry in whitelist or []:
            gid = str(entry.get("group_id", "") or "").strip()
            if gid:
                merged[gid] = {
                    "group_id": gid,
                    "enabled": bool(entry.get("enabled")),
                    "count": None,
                }
        for entry in data_groups or []:
            gid = entry["group_id"]
            if gid in merged:
                merged[gid]["count"] = entry["count"]
            else:
                merged[gid] = {
                    "group_id": gid,
                    "enabled": True,
                    "count": entry["count"],
                }
        return sorted(
            merged.values(),
            key=lambda item: (-(item["count"] or 0), item["group_id"]),
        )

    async def push_report(self, kind: str, now: datetime | None = None) -> None:
        """定时推送日报/周报（全局开关与到点判定由调度器负责，本方法只管发）。

        - kind="daily"：时间范围 [今日 00:00, 明日 00:00)，label「今日」；
        - kind="weekly"：上一整周 [上周一 00:00, 本周一 00:00)，label「上周」；
        - 遍历 ``resolve_push_groups()`` 中 enabled 的群（all_mode 下以有数据
          的群为基准，见该方法注释）：无 umo warning 跳过；
          build_stats 失败记日志跳过；渲染失败降级纯文本摘要（总消息数 +
          Top3 发言人一行文本），文本发送再失败则跳过；成功经
          ``context.send_message(umo, chain)`` 发送图片链；
        - 群间 ``asyncio.sleep(1)`` 防限频；单群任何异常仅记日志不阻断整批；
        - 本方法绝不向上抛异常（调度器侧另有退避兜底）。

        Args:
            kind: "daily" 或 "weekly"；未知值记 warning 忽略。
            now: 当前时刻（缺省 ``datetime.now()``），独立成参便于测试注入。
        """
        try:
            await self._push_report(kind, now)
        except Exception:
            logger.error(
                f"[Stats] 推送任务（{kind}）未预期异常，本轮终止", exc_info=True
            )

    async def _push_report(self, kind: str, now: datetime | None) -> None:
        """push_report 内部实现（外层统一兜底）。"""
        if now is None:
            now = datetime.now()
        time_range = self._push_range(kind, now)
        if time_range is None:
            logger.warning(f"[Stats] 未知推送类型: {kind!r}（期望 daily/weekly），忽略")
            return

        try:
            groups = await self.resolve_push_groups()
        except Exception as e:
            logger.error(f"[Stats] 读取群推送开关失败，本轮终止: {e}")
            return
        targets = [g for g in (groups or []) if g and g.get("enabled")]
        if not targets:
            logger.info(f"[Stats] 推送（{kind}）：无已开启推送的群，跳过")
            return

        top_n = self._clamp_top_n(
            await self._int_setting("stats_top_n", _DEFAULT_TOP_N)
        )
        logger.info(f"[Stats] 推送（{kind}）开始，目标群 {len(targets)} 个")

        total_cnt = len(targets)
        ok_cnt = 0
        for idx, entry in enumerate(targets):
            gid = str(entry.get("group_id", "") or "").strip()
            if gid:
                try:
                    if await self._push_one_group(kind, gid, time_range, top_n):
                        ok_cnt += 1
                except Exception:
                    logger.error(
                        f"[Stats] 推送（{kind}）群 {gid} 异常，跳过该群", exc_info=True
                    )
            if idx < total_cnt - 1:
                await asyncio.sleep(_PUSH_INTERVAL_SECONDS)
        logger.info(f"[Stats] 推送（{kind}）完成：{ok_cnt}/{total_cnt} 个群成功")

    async def _push_one_group(
        self, kind: str, gid: str, time_range: StatsTimeRange, top_n: int
    ) -> bool:
        """单群推送：build → render → 发送；渲染失败降级纯文本摘要。

        Returns:
            bool: 是否成功发送（图片或降级文本）；无 umo / build 失败返回
            False（由调用方计入失败）。发送异常向上抛由整批循环兜底。
        """
        umo = self.get_group_umo(gid)
        if not umo:
            logger.warning(
                f"[Stats] 推送（{kind}）群 {gid} 无 umo 缓存，跳过"
                "（该群有消息经过本插件后自动恢复）"
            )
            return False

        query = StatsQuery(
            group_id=gid, member_id=None, time_range=time_range, top_n=top_n
        )
        try:
            data = await self.build_stats(query)
        except StatsBuildError as e:
            logger.error(f"[Stats] 推送（{kind}）群 {gid} 统计组装失败，跳过: {e}")
            return False

        image = await self.render(data, _PUSH_TITLES[kind])
        if image:
            await self._context.send_message(umo, self._image_chain(image))
            logger.info(
                f"[Stats] 推送（{kind}）群 {gid} 图片报告已发送（范围 {time_range.label}）"
            )
            return True

        # 渲染失败 → 降级纯文本摘要；发送再失败由整批循环记日志跳过
        logger.warning(f"[Stats] 推送（{kind}）群 {gid} T2I 渲染失败，降级纯文本摘要")
        text = self._fallback_text(data, time_range.label, kind)
        # 零宽空格包裹防 aiocqhttp plain 段 strip（与 profile/summary 一致）
        await self._context.send_message(
            umo, MessageChain(chain=[Comp.Plain("​" + text + "​")])
        )
        logger.info(f"[Stats] 推送（{kind}）群 {gid} 纯文本摘要已发送")
        return True

    # ------------------------------------------------------------------
    # 内部工具（纯函数优先，便于离线测试）
    # ------------------------------------------------------------------

    @staticmethod
    def _push_range(kind: str, now: datetime) -> StatsTimeRange | None:
        """推送时间范围（PRD F3 口径）：

        - daily：[今日 00:00, 明日 00:00)，label「今日」；
        - weekly：上一整周 [上周一 00:00, 本周一 00:00)，label「上周」
          （无论周报配置在星期几触发，均取最近一个已完结的整周）；
        - 未知 kind 返回 None。
        """
        today = datetime(now.year, now.month, now.day)
        if kind == "daily":
            return StatsTimeRange(
                start=today, end=today + timedelta(days=1), label="今日"
            )
        if kind == "weekly":
            this_monday = today - timedelta(days=today.weekday())  # 周一=0
            last_monday = this_monday - timedelta(days=7)
            return StatsTimeRange(start=last_monday, end=this_monday, label="上周")
        return None

    @staticmethod
    def _fill_trend(raw, start: datetime, end: datetime) -> list[dict]:
        """每日趋势连续补零：start 所在日 ~ end 前一日逐日输出。

        repo 返回 ``[("YYYY-MM-DD", count)]`` 不补零；本方法转为
        ``[{"date": "YYYY-MM-DD", "count": int}]`` 并对无数据日期补 0。
        脏条目（不可解包/非整数）跳过；end 早于 start+1 天时返回空列表。
        """
        counts: dict[str, int] = {}
        for row in raw or []:
            try:
                date_s, count = row
                counts[str(date_s)] = max(int(count), 0)
            except (TypeError, ValueError):
                continue
        trend: list[dict] = []
        last_date = (end - timedelta(days=1)).date()
        cur = start.date()
        while cur <= last_date:
            key = cur.strftime("%Y-%m-%d")
            trend.append({"date": key, "count": counts.get(key, 0)})
            cur += timedelta(days=1)
        return trend

    @staticmethod
    def _peak_of(dist: list[int]) -> int | None:
        """峰值小时：分布最大值索引（并列取最早小时）；全 0（或空）→ None。"""
        vmax = max(dist) if dist else 0
        if vmax <= 0:
            return None
        return dist.index(vmax)

    @staticmethod
    def _range_days(start: datetime, end: datetime) -> int:
        """时间范围跨度的自然日数（向上取整，至少 1），供日均计算。"""
        seconds = (end - start).total_seconds()
        return max(math.ceil(seconds / 86400), 1)

    @staticmethod
    def _clamp_top_n(value) -> int:
        """排行条数夹住 1–50；非法值回退默认 10。"""
        try:
            n = int(value)
        except (TypeError, ValueError):
            return _DEFAULT_TOP_N
        return min(max(n, _TOP_N_MIN), _TOP_N_MAX)

    @staticmethod
    def _sender_item(row: dict) -> SenderRankItem:
        """repo 发言人行（dict）→ SenderRankItem（image_count 留待快照注入）。"""
        return SenderRankItem(
            sender_id=str(row.get("sender_id", "") or ""),
            sender_name=str(row.get("sender_name", "") or ""),
            count=int(row.get("count", 0) or 0),
        )

    @staticmethod
    def _group_item(row: dict) -> GroupRankItem:
        """repo 群排行行（dict）→ GroupRankItem（image_count 留待快照注入）。"""
        return GroupRankItem(
            group_id=str(row.get("group_id", "") or ""),
            count=int(row.get("count", 0) or 0),
            image_count=0,
            active_senders=int(row.get("active_senders", 0) or 0),
        )

    @staticmethod
    def _image_chain(path_or_url: str) -> MessageChain:
        """图片路径/URL → 消息链（构造方式与 profile/summary formatter 一致）。"""
        value = str(path_or_url)
        if value.startswith(("http://", "https://")):
            return MessageChain(chain=[Comp.Image.fromURL(value)])
        return MessageChain(chain=[Comp.Image.fromFileSystem(value)])

    @staticmethod
    def _fallback_text(data: StatsData, label: str, kind: str) -> str:
        """渲染失败降级的纯文本摘要（一行）：总消息数 + Top3 发言人。"""
        kind_name = _PUSH_KIND_NAMES.get(kind, "日报")
        tops = []
        for item in (data.sender_ranking or [])[:3]:
            name = item.sender_name or item.sender_id
            tops.append(f"{name} {item.count}条")
        top_text = "、".join(tops) if tops else "暂无发言数据"
        return (
            f"【群聊{kind_name}·{label}】总消息数：{data.total_messages}，"
            f"Top3 发言人：{top_text}（图片卡片渲染失败，本条为纯文本摘要）"
        )

    async def _int_setting(self, key: str, default: int) -> int:
        """typed 读取 int 配置；异常/非 int 回退兜底值（最终防御）。"""
        try:
            value = await self._config_mgr.get_stats_setting_typed(key)
        except Exception as e:
            logger.warning(f"[Stats] 读取配置 {key} 失败，回退 {default}: {e}")
            return default
        if isinstance(value, bool) or not isinstance(value, int):
            return default
        return value
