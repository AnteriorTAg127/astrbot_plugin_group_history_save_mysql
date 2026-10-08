# ruff: noqa: I001
"""v0.5.0 模块 D 图片统计快照冒烟脚本（stats/snapshot.py）。

真实 aiosqlite 临时库（真实 ConfigManager，构造签名无参——StarTools.get_data_dir
经 stub 指向临时目录）+ FakeImageRepo（编排好的窗口聚合结果 / 可注入异常）验证：

- 整点边界：now=10:00:00 时 run_hourly_snapshot 聚合 9 点档（窗口
  [09:00, 10:00)、行键 (date, 9)）；refresh_current_hour 窗口为空直接跳过
  （不发查询、不写库）
- 跨天：now=次日 00:05 聚合前一天 23 时档，行键落前一天 (date, 23)
- 强制刷新 + UPSERT 覆盖语义：同小时 refresh 两次，hour 表行数不变值更新、
  top 表 sender_name 同步更新且新 sender 插入为新增行
  （v0.5.5 起二次刷新前先重置 60s 单调限流戳以走覆盖路径，并追加限流
  命中静默跳过断言——F4.2 设计内行为变更）
- Top K：top_k 读 stats_image_top_k 配置透传 repo；repo 已截断的 senders
  直传落库；群总量不受 Top K 影响
- 空结果照常调用（无副作用）；repo 抛异常时 run/refresh 均仅记日志不抛出
- fill_counts：单群视图 (gid, sid) 精确匹配、全部群视图跨群求和、
  group_ranking/member 注入、快照缺失成员保持 0、消息数字段不受影响、
  无快照数据全部保持 0、member=None 不崩

运行方式（插件根目录，二者皆可）：
    python -m pytest "tests/v0.5.0/smoke_d_snapshot.py" -v
    python "tests/v0.5.0/smoke_d_snapshot.py"

astrbot.* 全部 sys.modules stub，stub 必须在 import db_config / stats.snapshot
之前完成（范式沿用 smoke_a_config.py）；剔除插件源缓存模块保证多冒烟合跑互不干扰。
"""

import asyncio
import sys
import tempfile
import types
from datetime import datetime
from pathlib import Path

# ============================================================
# 一、剔除插件源缓存模块（多测试文件合跑兼容：无论先前以顶层名
#     db_config/stats 还是包名 astrbot_plugin_group_history_save_mysql.*
#     导入过，都强制重导入以绑定本脚本的 stub）
# ============================================================

_PLUGIN_ROOT = Path(__file__).resolve().parents[2]
_PKG = "astrbot_plugin_group_history_save_mysql"

for _name, _mod in list(sys.modules.items()):
    _file = getattr(_mod, "__file__", None)
    if not _file:
        continue
    try:
        _inside = Path(_file).resolve().is_relative_to(_PLUGIN_ROOT)
    except OSError:
        _inside = False
    if _inside and (
        _name in ("db_config", "stats")
        or _name.startswith("stats.")
        or _name == _PKG
        or _name.startswith(_PKG + ".")
    ):
        del sys.modules[_name]

# ============================================================
# 二、astrbot.* stub 注入（必须在 import db_config / stats.snapshot 之前）
# ============================================================

_DATA_DIR = Path(tempfile.mkdtemp(prefix="stats_snap_smoke_"))

_astrbot = types.ModuleType("astrbot")
_astrbot_api = types.ModuleType("astrbot.api")
_astrbot_star = types.ModuleType("astrbot.api.star")


class _StubLogger:
    """记录各级日志，便于断言「异常仅记日志不抛出」策略。"""

    def __init__(self):
        self.records = {"info": [], "warning": [], "error": [], "debug": []}

    def _log(self, level, msg, args):
        try:
            text = msg % args if args else str(msg)
        except Exception:
            text = str(msg)
        self.records[level].append(text)

    def info(self, msg, *args, **kw):
        self._log("info", msg, args)

    def warning(self, msg, *args, **kw):
        self._log("warning", msg, args)

    def error(self, msg, *args, **kw):
        self._log("error", msg, args)

    def debug(self, msg, *args, **kw):
        self._log("debug", msg, args)


_STUB_LOGGER = _StubLogger()


class _StubStarTools:
    @staticmethod
    def get_data_dir(_name: str) -> Path:
        return _DATA_DIR


_astrbot_api.logger = _STUB_LOGGER
_astrbot_star.StarTools = _StubStarTools
_astrbot.api = _astrbot_api
_astrbot_api.star = _astrbot_star
sys.modules["astrbot"] = _astrbot
sys.modules["astrbot.api"] = _astrbot_api
sys.modules["astrbot.api.star"] = _astrbot_star

if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT.parent))

from astrbot_plugin_group_history_save_mysql.core.db_config import ConfigManager  # noqa: E402

from astrbot_plugin_group_history_save_mysql.core.stats.models import (  # noqa: E402
    GroupRankItem,
    MemberStats,
    SenderRankItem,
    StatsData,
    StatsQuery,
    StatsTimeRange,
)
from astrbot_plugin_group_history_save_mysql.core.stats.snapshot import ImageSnapshotManager  # noqa: E402

PASSED = 0


def ok(cond: bool, desc: str) -> None:
    global PASSED
    assert cond, f"断言失败: {desc}"
    PASSED += 1
    print(f"  [PASS] {desc}")


# ============================================================
# 三、Fake Repo 与数据构造辅助
# ============================================================


class FakeImageRepo:
    """编排好的窗口聚合结果；可注入异常验证「仅日志不抛出」。

    v0.5.5 双源化：run_hourly_snapshot / refresh_current_hour 先图片后消息
    两路聚合，本替身补齐消息侧入口 get_msg_window_counts（与图片侧共用
    编排结果/异常注入，调用分别记录）。
    """

    def __init__(self):
        self.calls: list[tuple] = []  # [(start, end, top_k)] 图片侧
        self.msg_calls: list[tuple] = []  # [(start, end, top_k)] 消息侧
        self._result = {"groups": {}, "senders": {}}

    def set_result(self, result):
        """dict 结果或 Exception 实例（下次调用时抛出）。"""
        self._result = result

    async def _invoke(self, record, start, end, top_k):
        record.append((start, end, top_k))
        if isinstance(self._result, BaseException):
            raise self._result
        return self._result

    async def get_image_window_counts(self, start, end, top_k):
        return await self._invoke(self.calls, start, end, top_k)

    async def get_msg_window_counts(self, start, end, top_k):
        return await self._invoke(self.msg_calls, start, end, top_k)


async def _count(cfg: ConfigManager, sql: str) -> int:
    async with cfg.db.execute(sql) as cur:
        return (await cur.fetchone())[0]


async def _hour_rows(cfg: ConfigManager, date: str, hour: int) -> list:
    async with cfg.db.execute(
        "SELECT group_id, image_count FROM image_stats_hourly "
        "WHERE date = ? AND hour = ? ORDER BY group_id",
        (date, hour),
    ) as cur:
        return await cur.fetchall()


async def _top_rows(cfg: ConfigManager, date: str, hour: int) -> list:
    async with cfg.db.execute(
        "SELECT group_id, sender_id, sender_name, image_count "
        "FROM image_stats_hourly_top WHERE date = ? AND hour = ? "
        "ORDER BY group_id, sender_id",
        (date, hour),
    ) as cur:
        return await cur.fetchall()


def make_data(
    group_id,
    member_id,
    start: datetime,
    end: datetime,
    label: str,
    ranking: list,
    group_ranking: list | None = None,
    member: MemberStats | None = None,
) -> StatsData:
    return StatsData(
        query=StatsQuery(
            group_id=group_id,
            member_id=member_id,
            time_range=StatsTimeRange(start=start, end=end, label=label),
        ),
        total_messages=1000,
        total_images=0,
        active_senders=4,
        peak_hour=21,
        first_msg_time=None,
        last_msg_time=None,
        daily_trend=[],
        hourly_dist=[0] * 24,
        weekday_dist=[0] * 7,
        sender_ranking=ranking,
        group_ranking=group_ranking if group_ranking is not None else [],
        member=member,
        generated_at=datetime(2026, 8, 4, 12, 0, 0),
    )


def make_member(sender_id: str) -> MemberStats:
    return MemberStats(
        sender_id=sender_id,
        sender_name="张三",
        count=100,
        image_count=0,
        ratio=0.1,
        rank=1,
        active_days=1,
        avg_per_day=100.0,
        hourly_dist=[0] * 24,
        weekday_dist=[0] * 7,
    )


# ============================================================
# 四、冒烟主流程
# ============================================================


async def main() -> None:
    cfg = ConfigManager()
    print(f"临时库: {cfg.db_path}")
    assert await cfg.initialize() is True, "ConfigManager 初始化失败"

    repo = FakeImageRepo()
    snap = ImageSnapshotManager(cfg, repo)

    # ---- 1. 整点边界：now=10:00 聚合 9 点档；refresh 窗口为空跳过 ----
    print("\n[1] 整点边界")
    repo.set_result(
        {
            "groups": {"g1": 6, "g2": 4},
            "senders": {
                "g1": [("u1", "张三", 4), ("u2", "李四", 2)],
                "g2": [("u1", "张三", 3), ("u9", "王五", 1)],
            },
        }
    )
    await snap.run_hourly_snapshot(now=datetime(2026, 8, 4, 10, 0, 0))
    ok(len(repo.calls) == 1, "run_hourly_snapshot 调用图片侧 repo 一次")
    ok(len(repo.msg_calls) == 1, "双源化：消息侧聚合同窗调用一次（v0.5.5）")
    mstart, mend, mtop_k = repo.msg_calls[-1]
    ok(
        mstart == datetime(2026, 8, 4, 9, 0)
        and mend == datetime(2026, 8, 4, 10, 0)
        and mtop_k == 20,
        "消息侧窗口与 top_k 与图片侧一致",
    )
    ok(
        await _count(cfg, "SELECT COUNT(*) FROM msg_stats_hourly") == 2,
        "msg_stats_hourly 双源落库 2 行（v0.5.5）",
    )
    start, end, top_k = repo.calls[-1]
    ok(start == datetime(2026, 8, 4, 9, 0), "窗口起点 = 上小时整点 09:00")
    ok(end == datetime(2026, 8, 4, 10, 0), "窗口终点 = 本小时整点 10:00（不含）")
    ok(top_k == 20, "top_k 取配置默认值 20")
    ok(
        await _hour_rows(cfg, "2026-08-04", 9) == [("g1", 6), ("g2", 4)],
        "hour 行键 = 窗口起点 (2026-08-04, 9)，群级全量落库",
    )
    ok(
        await _top_rows(cfg, "2026-08-04", 9)
        == [
            ("g1", "u1", "张三", 4),
            ("g1", "u2", "李四", 2),
            ("g2", "u1", "张三", 3),
            ("g2", "u9", "王五", 1),
        ],
        "top 行按 (date, hour, group, sender) 落库",
    )
    ok(
        await _count(cfg, "SELECT COUNT(*) FROM image_stats_hourly") == 2,
        "hour 表共 2 行",
    )
    ok(
        await _count(cfg, "SELECT COUNT(*) FROM image_stats_hourly_top") == 4,
        "top 表共 4 行",
    )
    # refresh：now 恰在整点 → 窗口为空，跳过（不发查询、不写库）
    await snap.refresh_current_hour(now=datetime(2026, 8, 4, 10, 0, 0))
    ok(len(repo.calls) == 1, "now 恰在整点时 refresh 跳过（未发查询）")
    ok(
        await _count(cfg, "SELECT COUNT(*) FROM image_stats_hourly") == 2,
        "跳过后快照行数不变",
    )

    # ---- 2. 跨天：次日 00:05 聚合前一天 23 时档 ----
    print("\n[2] 跨天窗口")
    repo.set_result({"groups": {"g1": 2}, "senders": {"g1": [("u1", "张三", 2)]}})
    await snap.run_hourly_snapshot(now=datetime(2026, 8, 5, 0, 5))
    start, end, _ = repo.calls[-1]
    ok(
        start == datetime(2026, 8, 4, 23, 0) and end == datetime(2026, 8, 5, 0, 0),
        "跨天窗口 = [08-04 23:00, 08-05 00:00)",
    )
    ok(
        await _hour_rows(cfg, "2026-08-04", 23) == [("g1", 2)],
        "跨天行键落前一天 (2026-08-04, 23)",
    )
    ok(
        await _top_rows(cfg, "2026-08-04", 23) == [("g1", "u1", "张三", 2)],
        "跨天 top 行键一致",
    )
    ok(
        await _count(cfg, "SELECT COUNT(*) FROM image_stats_hourly") == 3,
        "hour 表共 3 行",
    )

    # ---- 3. 强制刷新 + UPSERT 覆盖语义（同小时两次） ----
    print("\n[3] 强制刷新与 UPSERT 覆盖")
    repo.set_result({"groups": {"g1": 3}, "senders": {"g1": [("u1", "张三", 3)]}})
    await snap.refresh_current_hour(now=datetime(2026, 8, 4, 10, 20))
    start, end, _ = repo.calls[-1]
    ok(
        start == datetime(2026, 8, 4, 10, 0) and end == datetime(2026, 8, 4, 10, 20),
        "refresh 窗口 = [本小时整点, now)",
    )
    ok(
        await _hour_rows(cfg, "2026-08-04", 10) == [("g1", 3)],
        "首次刷新写入当前小时部分值",
    )
    ok(
        await _count(cfg, "SELECT COUNT(*) FROM image_stats_hourly") == 4,
        "hour 表共 4 行",
    )
    ok(
        await _count(cfg, "SELECT COUNT(*) FROM image_stats_hourly_top") == 6,
        "top 表共 6 行",
    )
    repo.set_result(
        {
            "groups": {"g1": 9},
            "senders": {"g1": [("u1", "张三改", 5), ("u3", "新丁", 4)]},
        }
    )
    # v0.5.5 F4.2：强制刷新 60s 单调限流——重置限流戳模拟「距上次已超 60s」，
    # 使二次刷新真正执行以验证 UPSERT 覆盖路径
    snap._last_refresh_mono = 0.0
    await snap.refresh_current_hour(now=datetime(2026, 8, 4, 10, 45))
    ok(
        await _hour_rows(cfg, "2026-08-04", 10) == [("g1", 9)],
        "二次刷新覆盖同 (date, hour) 部分值 3 → 9",
    )
    ok(
        await _count(cfg, "SELECT COUNT(*) FROM image_stats_hourly") == 4,
        "覆盖后 hour 表行数不变（UPSERT 不新增）",
    )
    ok(
        await _top_rows(cfg, "2026-08-04", 10)
        == [("g1", "u1", "张三改", 5), ("g1", "u3", "新丁", 4)],
        "top 覆盖：sender_name 同步更新，新 sender 插入",
    )
    ok(
        await _count(cfg, "SELECT COUNT(*) FROM image_stats_hourly_top") == 7,
        "top 表 7 行（u1 覆盖 + u3 新增）",
    )
    # v0.5.5 F4.2 限流路径：60s 内再次刷新被静默跳过（双源均不发查询）
    calls_before = (len(repo.calls), len(repo.msg_calls))
    await snap.refresh_current_hour(now=datetime(2026, 8, 4, 10, 50))
    ok(
        (len(repo.calls), len(repo.msg_calls)) == calls_before,
        "60s 限流命中：重复刷新被跳过（未发任何查询，v0.5.5 F4.2）",
    )

    # ---- 4. Top K：配置透传 + 截断结果直传 ----
    print("\n[4] Top K 透传")
    ok(await cfg.set_stats_setting("stats_image_top_k", 2) is True, "top_k 配置改为 2")
    repo.set_result(
        {
            "groups": {"g1": 30},
            "senders": {"g1": [("u1", "张三", 20), ("u2", "李四", 10)]},
        }
    )
    await snap.run_hourly_snapshot(now=datetime(2026, 8, 4, 12, 7))
    start, end, top_k = repo.calls[-1]
    ok(
        start == datetime(2026, 8, 4, 11, 0) and end == datetime(2026, 8, 4, 12, 0),
        "窗口 = 11 点档",
    )
    ok(top_k == 2, "top_k 读配置值 2 透传 repo")
    ok(
        await _top_rows(cfg, "2026-08-04", 11)
        == [("g1", "u1", "张三", 20), ("g1", "u2", "李四", 10)],
        "repo 已截断的 Top K 结果直传落库",
    )
    ok(
        await _hour_rows(cfg, "2026-08-04", 11) == [("g1", 30)],
        "群级总量不受 Top K 影响",
    )
    ok(
        await cfg.set_stats_setting("stats_image_top_k", 20) is True,
        "恢复 top_k 默认 20",
    )

    # ---- 5. 空结果照常调用；repo 异常仅日志不抛 ----
    print("\n[5] 空结果与异常策略")
    repo.set_result({"groups": {}, "senders": {}})
    await snap.run_hourly_snapshot(now=datetime(2026, 8, 4, 13, 5))
    ok(
        await _count(cfg, "SELECT COUNT(*) FROM image_stats_hourly") == 5,
        "空结果照常调用 upsert 且无副作用（hour 表仍 5 行）",
    )
    ok(
        await _count(cfg, "SELECT COUNT(*) FROM image_stats_hourly_top") == 9,
        "空结果后 top 表仍 9 行",
    )
    err_before = len(_STUB_LOGGER.records["error"])
    repo.set_result(RuntimeError("模拟 MySQL 不可用"))
    await snap.run_hourly_snapshot(now=datetime(2026, 8, 4, 14, 5))
    ok(True, "repo 抛异常时 run_hourly_snapshot 不抛出")
    snap._last_refresh_mono = 0.0  # v0.5.5：重置限流戳确保 refresh 真正执行
    await snap.refresh_current_hour(now=datetime(2026, 8, 4, 14, 30))
    ok(True, "repo 抛异常时 refresh_current_hour 不抛出")
    ok(
        await _count(cfg, "SELECT COUNT(*) FROM image_stats_hourly") == 5,
        "异常后快照行数不变",
    )
    ok(
        len(_STUB_LOGGER.records["error"]) == err_before + 4,
        "两次异常入口各记两条 error（双源：图片+消息，v0.5.5）",
    )

    # ---- 6. fill_counts 单群视图：(gid, sid) 精确匹配 ----
    print("\n[6] fill_counts 单群视图")
    day_start, day_end = datetime(2026, 8, 4, 0, 0), datetime(2026, 8, 5, 0, 0)
    ranking = [
        SenderRankItem("u1", "张三", 100),
        SenderRankItem("u2", "李四", 50),
        SenderRankItem("u3", "新丁", 20),
        SenderRankItem("u4", "路人", 5),
    ]
    data = make_data(
        "g1", "u1", day_start, day_end, "2026-08-04", ranking, member=make_member("u1")
    )
    ret = await snap.fill_counts(data)
    ok(ret is data, "原地修改并返回同一实例")
    ok(data.total_images == 47, f"g1 当日图片总数 47（实际 {data.total_images}）")
    ok(ranking[0].image_count == 31, "u1 = 4+5+20+2 = 31")
    ok(ranking[1].image_count == 12, "u2 = 2+10 = 12")
    ok(ranking[2].image_count == 4, "u3 = 4")
    ok(ranking[3].image_count == 0, "快照缺失成员 u4 保持 0")
    ok(data.member.image_count == 31, "member.image_count 注入 31")
    ok(ranking[0].count == 100, "消息数字段不受注入影响")
    ok(data.group_ranking == [], "单群视图群排行保持空")

    # ---- 7. fill_counts 全部群视图：跨群求和 + 群排行注入 ----
    print("\n[7] fill_counts 全部群视图")
    ranking_all = [SenderRankItem("u1", "张三", 100), SenderRankItem("u9", "王五", 30)]
    group_ranking = [GroupRankItem("g1", 800), GroupRankItem("g2", 200)]
    data_all = make_data(
        None,
        "u1",
        day_start,
        day_end,
        "2026-08-04",
        ranking_all,
        group_ranking=group_ranking,
        member=make_member("u1"),
    )
    ret_all = await snap.fill_counts(data_all)
    ok(ret_all is data_all, "全部群视图原地返回")
    ok(
        data_all.total_images == 51,
        f"全部群图片总数 51 = 47+4（实际 {data_all.total_images}）",
    )
    ok(group_ranking[0].image_count == 47, "群排行 g1 注入 47")
    ok(group_ranking[1].image_count == 4, "群排行 g2 注入 4")
    ok(ranking_all[0].image_count == 34, "u1 跨群求和 31+3 = 34")
    ok(ranking_all[1].image_count == 1, "u9 = 1")
    ok(data_all.member.image_count == 34, "全部群视图 member 跨群求和 34")

    # ---- 8. fill_counts 无快照数据：全部保持 0 ----
    print("\n[8] fill_counts 无快照数据")
    ranking_empty = [SenderRankItem("u1", "张三", 100)]
    data_none = make_data(
        "g1",
        "u1",
        datetime(2026, 7, 1, 0, 0),
        datetime(2026, 7, 2, 0, 0),
        "2026-07-01",
        ranking_empty,
        member=make_member("u1"),
    )
    await snap.fill_counts(data_none)
    ok(data_none.total_images == 0, "无快照数据 total_images 保持 0")
    ok(ranking_empty[0].image_count == 0, "排行 image_count 保持 0")
    ok(data_none.member.image_count == 0, "member image_count 保持 0")
    ok(ranking_empty[0].count == 100, "消息数不变")

    # ---- 9. fill_counts 最小数据（member=None / 空排行） ----
    print("\n[9] fill_counts 最小数据")
    data_min = make_data(None, None, day_start, day_end, "2026-08-04", [])
    ret_min = await snap.fill_counts(data_min)
    ok(ret_min is data_min, "member=None / 空排行不崩并原地返回")
    ok(data_min.total_images == 51, "total_images 正常注入")

    await cfg.close()
    print(f"\n冒烟通过：{PASSED} 项断言全部 PASS")


if __name__ == "__main__":
    asyncio.run(main())
