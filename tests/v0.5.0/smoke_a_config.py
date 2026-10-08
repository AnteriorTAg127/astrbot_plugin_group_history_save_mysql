# ruff: noqa: I001
"""v0.5.0 模块 A 配置层冒烟脚本（db_config.py stats/push_group/snapshot）。

用真实 aiosqlite 临时库验证：

- 建表：stats_settings / push_group / image_stats_hourly / image_stats_hourly_top
  四张新表均存在，既有表（summary_settings/profile_settings 等）播种数无回归
- 播种：STATS_DEFAULTS 8 项，DEFAULTS 与 TYPES 键集合一致
- typed 读写：默认值、写入/读取往返、bool "1"/"0" 识别、非法值回退默认、
  缺失键自动播种
- set_stats_setting 校验：范围（top_n 1–50 / cooldown 0–600 / top_k 1–100 /
  weekday 1–7）、HH:MM 格式（含 "9:00"→"09:00" 归一化、非法时刻拒绝）、
  未知键拒绝、拒绝写入后原值不变
- push_group：UPSERT 覆盖语义 + 白名单 LEFT JOIN 语义（非白名单行忽略、
  白名单群无 push_group 行默认 False、移出白名单后不再返回）
- snapshot：空列表直接返回、upsert 覆盖写（行数不变 + top 表 sender_name
  同步更新 + 新 sender 插入）、(date,hour) 半开区间过滤边界
  （跨天、整点边界、分钟截断、start>=end 空集、group_id 过滤）
- 自愈重连：close() 后再调用经 _ensure_db 自动重建连接

运行方式（插件根目录）：
    python "tests/v0.5.0/smoke_a_config.py"

astrbot.* 全部 sys.modules stub，stub 必须在 import db_config 之前完成
（范式沿用 tests/v0.4.0/test_profile_config_smoke.py）。
"""

import asyncio
import sys
import tempfile
import types
from datetime import datetime
from pathlib import Path

# ============================================================
# 一、剔除插件源缓存模块（多测试文件合跑/进程内复用兼容：无论先前以
#     顶层名 db_config 还是包名 astrbot_plugin_group_history_save_mysql.*
#     导入过，都强制重导入以绑定本脚本的 stub——进程内被 test_v050 先导入
#     时，base.py 已绑定其宿主 stub，不剔除会导致本脚本落到宿主数据目录）
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
# 二、stub 注入（必须在 import db_config 之前）
# ============================================================

_DATA_DIR = Path(tempfile.mkdtemp(prefix="stats_cfg_smoke_"))

_astrbot = types.ModuleType("astrbot")
_astrbot_api = types.ModuleType("astrbot.api")
_astrbot_star = types.ModuleType("astrbot.api.star")


class _StubLogger:
    def _noop(self, *args, **kwargs):
        pass

    info = warning = error = debug = _noop


class _StubStarTools:
    @staticmethod
    def get_data_dir(_name: str) -> Path:
        return _DATA_DIR


_astrbot_api.logger = _StubLogger()
_astrbot_star.StarTools = _StubStarTools
_astrbot.api = _astrbot_api
_astrbot_api.star = _astrbot_star
sys.modules["astrbot"] = _astrbot
sys.modules["astrbot.api"] = _astrbot_api
sys.modules["astrbot.api.star"] = _astrbot_star

# 被测模块在插件根目录
sys.path.insert(0, str(_PLUGIN_ROOT.parent))

from astrbot_plugin_group_history_save_mysql.core.db_config import ConfigManager  # noqa: E402

PASSED = 0


def ok(cond: bool, desc: str) -> None:
    global PASSED
    assert cond, f"断言失败: {desc}"
    PASSED += 1
    print(f"  [PASS] {desc}")


async def _count(mgr: ConfigManager, sql: str) -> int:
    async with mgr.db.execute(sql) as cur:
        return (await cur.fetchone())[0]


async def main() -> None:
    mgr = ConfigManager()
    print(f"临时库: {mgr.db_path}")

    # ---- 1. 初始化：建表 + 播种 ----
    ok(await mgr.initialize() is True, "initialize() 成功")

    async with mgr.db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ) as cur:
        tables = {row[0] for row in await cur.fetchall()}
    for t in (
        "stats_settings",
        "push_group",
        "image_stats_hourly",
        "image_stats_hourly_top",
    ):
        ok(t in tables, f"新表 {t} 已创建")

    count = await _count(mgr, "SELECT COUNT(*) FROM stats_settings")
    ok(count == 8, f"播种后 stats 配置计数 == 8（实际 {count}）")
    ok(len(ConfigManager.STATS_DEFAULTS) == 8, "STATS_DEFAULTS 8 项")
    ok(len(ConfigManager.STATS_TYPES) == 8, "STATS_TYPES 8 项")
    ok(
        set(ConfigManager.STATS_DEFAULTS) == set(ConfigManager.STATS_TYPES),
        "DEFAULTS 与 TYPES 键集合一致",
    )
    # 既有播种无回归
    ok(
        await _count(mgr, "SELECT COUNT(*) FROM summary_settings") == 24,
        "既有 summary_settings 播种数 24 无回归",
    )
    ok(
        await _count(mgr, "SELECT COUNT(*) FROM profile_settings") == 19,
        "既有 profile_settings 播种数 19 无回归",
    )

    # ---- 2. typed 读取默认值 ----
    ok(await mgr.get_stats_setting_typed("stats_top_n") == 10, "typed int 默认 10")
    ok(await mgr.get_stats_setting_typed("stats_cooldown") == 30, "typed int 默认 30")
    ok(
        await mgr.get_stats_setting_typed("stats_image_top_k") == 20,
        "typed int 默认 20",
    )
    ok(
        await mgr.get_stats_setting_typed("push_daily_enabled") is True,
        "typed bool 默认 True",
    )
    ok(
        await mgr.get_stats_setting_typed("push_daily_time") == "21:00",
        "typed str 默认 21:00",
    )
    ok(
        await mgr.get_stats_setting_typed("push_weekly_enabled") is False,
        "typed bool 默认 False",
    )
    ok(
        await mgr.get_stats_setting_typed("push_weekly_weekday") == 1,
        "typed int 默认 1",
    )
    ok(
        await mgr.get_stats_setting_typed("push_weekly_time") == "09:00",
        "typed str 默认 09:00",
    )

    all_settings = await mgr.get_all_stats_settings()
    ok(
        len(all_settings) == 8,
        f"get_all_stats_settings 全量 8 项（实际 {len(all_settings)}）",
    )
    ok(all_settings["stats_top_n"] == 10, "all typed int 值正确")
    ok(all_settings["push_daily_enabled"] is True, "all typed bool 值正确")

    # ---- 3. 合法写入往返（含值归一化） ----
    ok(await mgr.set_stats_setting("stats_top_n", 20) is True, "set int 20 成功")
    ok(await mgr.get_stats_setting_typed("stats_top_n") == 20, "写入后 typed 读回 20")
    ok(await mgr.set_stats_setting("stats_top_n", "15") is True, "set 字符串 '15' 成功")
    ok(await mgr.get_stats_setting_typed("stats_top_n") == 15, "字符串写入读回 int 15")
    ok(
        await mgr.set_stats_setting("push_daily_enabled", False) is True,
        "set bool False 成功",
    )
    ok(
        await mgr.get_stats_setting("push_daily_enabled") == "false",
        "bool 归一化为 'false' 存储",
    )
    ok(
        await mgr.set_stats_setting("push_weekly_weekday", 7) is True,
        "set weekday 7（上界）成功",
    )
    ok(await mgr.get_stats_setting_typed("push_weekly_weekday") == 7, "weekday 读回 7")
    ok(
        await mgr.set_stats_setting("stats_cooldown", 0) is True,
        "set cooldown 0（下界）成功",
    )
    ok(
        await mgr.set_stats_setting("stats_cooldown", 600) is True,
        "set cooldown 600（上界）成功",
    )
    # int 去前导零归一化
    ok(await mgr.set_stats_setting("stats_top_n", "010") is True, "set '010' 合法")
    ok(await mgr.get_stats_setting("stats_top_n") == "10", "'010' 归一化存储为 '10'")
    # HH:MM 归一化：一位小时数补零
    ok(
        await mgr.set_stats_setting("push_daily_time", "9:00") is True,
        "set '9:00' 合法",
    )
    ok(
        await mgr.get_stats_setting("push_daily_time") == "09:00",
        "'9:00' 归一化存储为 '09:00'",
    )
    ok(
        await mgr.set_stats_setting("push_weekly_time", "08:30") is True,
        "set '08:30' 成功",
    )
    ok(
        await mgr.get_stats_setting_typed("push_weekly_time") == "08:30",
        "时间值读回 08:30",
    )
    # bool 键接受 "1"/"0" 字符串并归一化
    ok(await mgr.set_stats_setting("push_daily_enabled", "1") is True, "set '1' 合法")
    ok(
        await mgr.get_stats_setting("push_daily_enabled") == "true",
        "'1' 归一化存储为 'true'",
    )

    # ---- 4. 非法值拒绝写入（原值不变） ----
    before = await mgr.get_stats_setting("stats_top_n")
    for bad_key, bad_value in [
        ("stats_top_n", 0),  # 低于下界 1
        ("stats_top_n", 51),  # 高于上界 50
        ("stats_top_n", "abc"),  # 非数字
        ("stats_cooldown", -1),
        ("stats_cooldown", 601),
        ("stats_image_top_k", 0),
        ("stats_image_top_k", 101),
        ("push_weekly_weekday", 0),
        ("push_weekly_weekday", 8),
        ("push_daily_time", "25:00"),  # 小时越界
        ("push_daily_time", "21:60"),  # 分钟越界
        ("push_daily_time", "2100"),  # 缺冒号
        ("push_daily_time", "9:5"),  # 分钟非两位
        ("push_weekly_time", ""),
        ("push_daily_enabled", "maybe"),  # 非法 bool
        ("unknown_key", 1),  # 未知键
    ]:
        ok(
            await mgr.set_stats_setting(bad_key, bad_value) is False,
            f"非法值被拒绝: {bad_key}={bad_value!r}",
        )
    ok(
        await mgr.get_stats_setting("stats_top_n") == before,
        "全部拒绝后 stats_top_n 原值不变",
    )
    ok(
        await mgr.get_stats_setting("unknown_key") == "",
        "未知键未入库（raw 读取返回空串）",
    )

    # ---- 5. 库内非法值 typed 回退默认 ----
    await mgr.db.execute(
        "UPDATE stats_settings SET value = ? WHERE key = ?", ("abc", "stats_top_n")
    )
    await mgr.db.execute(
        "UPDATE stats_settings SET value = ? WHERE key = ?",
        ("maybe", "push_daily_enabled"),
    )
    await mgr.db.commit()
    ok(
        await mgr.get_stats_setting_typed("stats_top_n") == 10,
        "非法 int 回退默认 10",
    )
    ok(
        await mgr.get_stats_setting_typed("push_daily_enabled") is True,
        "非法 bool 回退默认 True",
    )
    all_illegal = await mgr.get_all_stats_settings()
    ok(all_illegal["stats_top_n"] == 10, "all 内非法 int 回退")
    ok(all_illegal["push_daily_enabled"] is True, "all 内非法 bool 回退")
    # bool "1"/"0" 识别
    await mgr.db.execute(
        "UPDATE stats_settings SET value = ? WHERE key = ?",
        ("1", "push_weekly_enabled"),
    )
    await mgr.db.commit()
    ok(
        await mgr.get_stats_setting_typed("push_weekly_enabled") is True,
        'bool "1" → True',
    )

    # ---- 6. reset 恢复默认 ----
    await mgr.reset_stats_settings()
    reset_all = await mgr.get_all_stats_settings()
    ok(len(reset_all) == 8, "reset 后仍为完整 8 项")
    ok(reset_all["stats_top_n"] == 10, "reset 恢复 top_n 默认")
    ok(reset_all["stats_cooldown"] == 30, "reset 恢复 cooldown 默认")
    ok(reset_all["stats_image_top_k"] == 20, "reset 恢复 top_k 默认")
    ok(reset_all["push_daily_enabled"] is True, "reset 恢复日报开关默认")
    ok(reset_all["push_daily_time"] == "21:00", "reset 恢复日报时间默认")
    ok(reset_all["push_weekly_enabled"] is False, "reset 恢复周报开关默认")
    ok(reset_all["push_weekly_weekday"] == 1, "reset 恢复周报星期默认")
    ok(reset_all["push_weekly_time"] == "09:00", "reset 恢复周报时间默认")

    # ---- 7. 缺失键自动播种（自愈范式） ----
    await mgr.db.execute(
        "DELETE FROM stats_settings WHERE key = ?", ("push_weekly_time",)
    )
    await mgr.db.commit()
    ok(
        await mgr.get_stats_setting("push_weekly_time") == "09:00",
        "缺失键读取时自动播种默认值",
    )

    # ---- 8. push_group：UPSERT + 白名单 JOIN 语义 ----
    await mgr.add_group(111)
    await mgr.add_group(222)
    await mgr.add_group(333)
    pgs = await mgr.get_push_groups()
    ok(len(pgs) == 3, f"3 个白名单群（实际 {len(pgs)}）")
    ok(
        all(pg["enabled"] is False for pg in pgs),
        "白名单群无 push_group 行默认 False",
    )
    ok(
        [pg["group_id"] for pg in pgs] == ["111", "222", "333"],
        "group_id 字符串化且按群号升序",
    )

    ok(await mgr.set_push_group("111", True) is True, "set_push_group(111, True)")
    pgs = {pg["group_id"]: pg["enabled"] for pg in await mgr.get_push_groups()}
    ok(pgs == {"111": True, "222": False, "333": False}, "111 开启、其余默认关")

    # UPSERT 覆盖语义
    ok(
        await mgr.set_push_group("111", False) is True,
        "set_push_group(111, False) 覆盖",
    )
    ok(
        await _count(mgr, "SELECT COUNT(*) FROM push_group") == 1,
        "覆盖后 push_group 仍 1 行（UPSERT 不新增）",
    )
    pgs = {pg["group_id"]: pg["enabled"] for pg in await mgr.get_push_groups()}
    ok(pgs["111"] is False, "覆盖生效：111 关")
    await mgr.set_push_group("111", True)

    # 非白名单的 push_group 行被忽略
    await mgr.db.execute(
        "INSERT INTO push_group (group_id, enabled) VALUES (?, 1)", ("999",)
    )
    await mgr.db.commit()
    pgs = await mgr.get_push_groups()
    ok(len(pgs) == 3, "非白名单群 999 的 push_group 行被忽略")
    ok(all(pg["group_id"] != "999" for pg in pgs), "返回列表不含 999")

    # 移出白名单后不再返回
    await mgr.remove_group(333)
    pgs = [pg["group_id"] for pg in await mgr.get_push_groups()]
    ok(pgs == ["111", "222"], "群 333 移出白名单后不再返回")

    # ---- 8b. get_push_flags（v0.5.1）：任意群号批量取标志 ----
    flags = await mgr.get_push_flags([])
    ok(flags == {}, "get_push_flags([]) 空入参返回 {}")
    flags = await mgr.get_push_flags(["111", "222", "777"])
    ok(
        flags == {"111": True, "222": False, "777": False},
        f"有行的群按行取值、无行的群默认 False（实际 {flags}）",
    )
    await mgr.set_push_group("777", True)  # 不依赖白名单也能写标志行
    flags = await mgr.get_push_flags(["777"])
    ok(flags == {"777": True}, "get_push_flags 可读到非白名单群的标志行")
    await mgr.db.execute("DELETE FROM push_group WHERE group_id = '777'")
    await mgr.db.commit()

    # ---- 9. snapshot：空入参 / upsert / 覆盖语义 ----
    await mgr.snapshot_upsert([], [])  # 空列表直接返回，不抛异常即通过
    ok(True, "snapshot_upsert([], []) 空入参直接返回")

    hour_rows = [
        ("2026-08-03", 22, "g1", 5),
        ("2026-08-03", 23, "g1", 7),
        ("2026-08-04", 0, "g1", 3),
        ("2026-08-04", 1, "g2", 4),
    ]
    top_rows = [
        ("2026-08-03", 22, "g1", "u1", "张三", 2),
        ("2026-08-03", 22, "g1", "u2", "李四", 3),
        ("2026-08-04", 0, "g1", "u1", "张三", 3),
        ("2026-08-04", 1, "g2", "u9", "王五", 4),
    ]
    await mgr.snapshot_upsert(hour_rows, top_rows)
    ok(
        await _count(mgr, "SELECT COUNT(*) FROM image_stats_hourly") == 4,
        "hour 表写入 4 行",
    )
    ok(
        await _count(mgr, "SELECT COUNT(*) FROM image_stats_hourly_top") == 4,
        "top 表写入 4 行",
    )

    # ---- 10. snapshot_query：半开区间边界 ----
    # 跨天 + 整点边界：[08-03 22:00, 08-04 01:00) → 22/23/0 三个小时（end.hour=1 不含）
    r = await mgr.snapshot_query(
        datetime(2026, 8, 3, 22, 0), datetime(2026, 8, 4, 1, 0)
    )
    ok(r["total"] == 15, f"跨天查询 total == 15（实际 {r['total']}）")
    ok(r["by_group"] == {"g1": 15}, "跨天查询 by_group 仅 g1（g2 整点被 end 排除）")
    ok(
        r["by_sender"] == {("g1", "u1"): 5, ("g1", "u2"): 3},
        "跨天查询 by_sender 按 (gid, sid) 元组聚合",
    )

    # 整天边界：[08-03 00:00, 08-04 00:00) → 仅 08-03 的 22/23 两小时
    r = await mgr.snapshot_query(datetime(2026, 8, 3, 0, 0), datetime(2026, 8, 4, 0, 0))
    ok(r["total"] == 12, f"整天边界 total == 12（实际 {r['total']}）")
    ok(r["by_group"] == {"g1": 12}, "整天边界 by_group 正确")
    ok(
        r["by_sender"] == {("g1", "u1"): 2, ("g1", "u2"): 3},
        "整天边界 by_sender 正确",
    )

    # 全量跨两天：[08-03 00:00, 08-05 00:00)
    r = await mgr.snapshot_query(datetime(2026, 8, 3, 0, 0), datetime(2026, 8, 5, 0, 0))
    ok(r["total"] == 19, f"全量 total == 19（实际 {r['total']}）")
    ok(r["by_group"] == {"g1": 15, "g2": 4}, "全量 by_group 双群")
    ok(
        r["by_sender"] == {("g1", "u1"): 5, ("g1", "u2"): 3, ("g2", "u9"): 4},
        "全量 by_sender 三人",
    )

    # 整点起点边界：[08-03 23:00, 08-04 00:00) → 仅 hour=23
    r = await mgr.snapshot_query(
        datetime(2026, 8, 3, 23, 0), datetime(2026, 8, 4, 0, 0)
    )
    ok(r["total"] == 7, f"整点起点边界 total == 7（实际 {r['total']}）")

    # 分钟截断语义（契约口径）：[08-03 22:30, 08-03 23:30) → 仅 hour=22
    r = await mgr.snapshot_query(
        datetime(2026, 8, 3, 22, 30), datetime(2026, 8, 3, 23, 30)
    )
    ok(r["total"] == 5, f"分钟截断：end.hour 整小时不含（实际 {r['total']}）")

    # start >= end → 空集
    r = await mgr.snapshot_query(
        datetime(2026, 8, 4, 1, 0), datetime(2026, 8, 3, 22, 0)
    )
    ok(
        r == {"total": 0, "by_group": {}, "by_sender": {}},
        "start >= end 返回全零结构",
    )
    r = await mgr.snapshot_query(datetime(2026, 8, 4, 0, 0), datetime(2026, 8, 4, 0, 0))
    ok(r["total"] == 0, "start == end 返回空")

    # group_id 过滤
    r = await mgr.snapshot_query(
        datetime(2026, 8, 3, 0, 0), datetime(2026, 8, 5, 0, 0), group_id="g2"
    )
    ok(r["total"] == 4, f"group 过滤 total == 4（实际 {r['total']}）")
    ok(r["by_group"] == {"g2": 4}, "group 过滤 by_group 仅 g2")
    ok(r["by_sender"] == {("g2", "u9"): 4}, "group 过滤 by_sender 仅 g2 成员")

    # ---- 11. snapshot：覆盖写语义 ----
    await mgr.snapshot_upsert(
        [("2026-08-03", 22, "g1", 100)],
        [("2026-08-03", 22, "g1", "u1", "张三改", 50)],
    )
    ok(
        await _count(mgr, "SELECT COUNT(*) FROM image_stats_hourly") == 4,
        "覆盖写后 hour 表行数不变（仍 4 行）",
    )
    ok(
        await _count(mgr, "SELECT COUNT(*) FROM image_stats_hourly_top") == 4,
        "覆盖写后 top 表行数不变（仍 4 行）",
    )
    r = await mgr.snapshot_query(datetime(2026, 8, 3, 0, 0), datetime(2026, 8, 5, 0, 0))
    ok(r["total"] == 114, f"覆盖后 total == 114（实际 {r['total']}）")
    ok(r["by_group"]["g1"] == 110, "覆盖后 g1 == 110（100+7+3）")
    ok(r["by_sender"][("g1", "u1")] == 53, "覆盖后 (g1,u1) == 53（50+3）")
    async with mgr.db.execute(
        "SELECT sender_name FROM image_stats_hourly_top "
        "WHERE date = '2026-08-03' AND hour = 22 AND group_id = 'g1' "
        "AND sender_id = 'u1'"
    ) as cur:
        name = (await cur.fetchone())[0]
    ok(name == "张三改", "覆盖写同步更新 sender_name")

    # 同主键新 sender → 插入新行
    await mgr.snapshot_upsert([], [("2026-08-03", 22, "g1", "u3", "新丁", 1)])
    ok(
        await _count(mgr, "SELECT COUNT(*) FROM image_stats_hourly_top") == 5,
        "新 sender upsert 插入为第 5 行",
    )

    # ---- 12. v0.5.5 分段快照体系：6 张新表建表 ----
    async with mgr.db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ) as cur:
        tables_v055 = {row[0] for row in await cur.fetchall()}
    for t in (
        "msg_stats_hourly",
        "msg_stats_hourly_top",
        "msg_stats_daily",
        "msg_stats_monthly",
        "image_stats_daily",
        "image_stats_monthly",
    ):
        ok(t in tables_v055, f"v0.5.5 新表 {t} 已创建")

    # ---- 13. v0.5.5 UPSERT 簇：空入参 / 覆盖语义 / 幂等可重放 ----
    await mgr.snapshot_upsert_msg_hour([], [])
    await mgr.snapshot_upsert_daily([], [])
    await mgr.snapshot_upsert_monthly([], [])
    ok(True, "三个新 UPSERT 方法空入参直接返回不抛异常")

    await mgr.snapshot_upsert_msg_hour(
        [("2026-08-01", 9, "g1", 10), ("2026-08-01", 10, "g2", 4)],
        [("2026-08-01", 9, "g1", "u1", "张三", 6)],
    )
    ok(
        await _count(mgr, "SELECT COUNT(*) FROM msg_stats_hourly") == 2,
        "msg_stats_hourly 写入 2 行",
    )
    ok(
        await _count(mgr, "SELECT COUNT(*) FROM msg_stats_hourly_top") == 1,
        "msg_stats_hourly_top 写入 1 行",
    )
    # 同主键覆盖写：行数不变、值更新、sender_name 同步更新
    await mgr.snapshot_upsert_msg_hour(
        [("2026-08-01", 9, "g1", 99)],
        [("2026-08-01", 9, "g1", "u1", "张三改", 50)],
    )
    ok(
        await _count(mgr, "SELECT COUNT(*) FROM msg_stats_hourly") == 2,
        "覆盖后 msg_stats_hourly 行数不变（UPSERT）",
    )
    async with mgr.db.execute(
        "SELECT msg_count FROM msg_stats_hourly "
        "WHERE date = '2026-08-01' AND hour = 9 AND group_id = 'g1'"
    ) as cur:
        ok((await cur.fetchone())[0] == 99, "msg hour 覆盖值 10 → 99")
    async with mgr.db.execute(
        "SELECT sender_name, msg_count FROM msg_stats_hourly_top "
        "WHERE date = '2026-08-01' AND hour = 9 AND group_id = 'g1' "
        "AND sender_id = 'u1'"
    ) as cur:
        row = await cur.fetchone()
    ok(row == ("张三改", 50), "msg top 覆盖：sender_name 同步更新 + 值更新")

    await mgr.snapshot_upsert_daily(
        [("2026-08-01", "g1", 30), ("2026-08-01", "g2", 7)],
        [("2026-08-01", "g1", 5)],
    )
    ok(
        await _count(mgr, "SELECT COUNT(*) FROM msg_stats_daily") == 2,
        "msg_stats_daily 写入 2 行",
    )
    ok(
        await _count(mgr, "SELECT COUNT(*) FROM image_stats_daily") == 1,
        "image_stats_daily 写入 1 行",
    )
    await mgr.snapshot_upsert_daily([("2026-08-01", "g1", 88)], [])
    async with mgr.db.execute(
        "SELECT msg_count FROM msg_stats_daily "
        "WHERE date = '2026-08-01' AND group_id = 'g1'"
    ) as cur:
        ok((await cur.fetchone())[0] == 88, "daily 覆盖值 30 → 88（行数不变）")

    await mgr.snapshot_upsert_monthly(
        [("2026-07", "g1", 900)],
        [("2026-07", "g1", 40)],
    )
    ok(
        await _count(mgr, "SELECT COUNT(*) FROM msg_stats_monthly") == 1,
        "msg_stats_monthly 写入 1 行",
    )
    ok(
        await _count(mgr, "SELECT COUNT(*) FROM image_stats_monthly") == 1,
        "image_stats_monthly 写入 1 行",
    )
    await mgr.snapshot_upsert_monthly([("2026-07", "g1", 950)], [])
    async with mgr.db.execute(
        "SELECT msg_count FROM msg_stats_monthly "
        "WHERE month = '2026-07' AND group_id = 'g1'"
    ) as cur:
        ok((await cur.fetchone())[0] == 950, "monthly 覆盖值 900 → 950（行数不变）")

    # ---- 14. v0.5.5 查询原语簇：闭区间 / 群过滤 / 非法 source / 存在性语义 ----
    # 补数据：多月 + 多日 + 小时层多行（覆盖聚合与闭区间边界）
    await mgr.snapshot_upsert_monthly(
        [("2026-06", "g1", 100), ("2026-06", "g2", 20), ("2026-08", "g1", 300)], []
    )
    totals = await mgr.snapshot_monthly_totals("msg", "2026-06", "2026-07")
    ok(
        totals == {"g1": 100 + 950, "g2": 20},
        f"monthly_totals 闭区间 [06,07] 按群 SUM（实际 {totals}）",
    )
    totals = await mgr.snapshot_monthly_totals(
        "msg", "2026-06", "2026-08", group_id="g1"
    )
    ok(totals == {"g1": 100 + 950 + 300}, "monthly_totals 群过滤仅 g1")
    ok(
        await mgr.snapshot_monthly_totals("bad", "2026-06", "2026-07") == {},
        "monthly_totals 非法 source 返回 {} 不抛",
    )

    await mgr.snapshot_upsert_daily(
        [("2026-08-02", "g1", 11), ("2026-08-03", "g2", 13)], []
    )
    drows = await mgr.snapshot_daily_rows("msg", "2026-08-01", "2026-08-02")
    ok(
        drows
        == {
            ("2026-08-01", "g1"): 88,
            ("2026-08-01", "g2"): 7,
            ("2026-08-02", "g1"): 11,
        },
        f"daily_rows 闭区间原始行（实际 {drows}）",
    )
    drows = await mgr.snapshot_daily_rows(
        "msg", "2026-08-01", "2026-08-03", group_id="g2"
    )
    ok(
        drows == {("2026-08-01", "g2"): 7, ("2026-08-03", "g2"): 13},
        "daily_rows 群过滤仅 g2",
    )
    ok(
        await mgr.snapshot_daily_rows("bad", "2026-08-01", "2026-08-02") == {},
        "daily_rows 非法 source 返回 {} 不抛",
    )

    await mgr.snapshot_upsert_msg_hour(
        [("2026-08-01", 11, "g1", 5), ("2026-08-02", 0, "g2", 6)], []
    )
    htotals = await mgr.snapshot_hourly_date_totals(
        "msg", "2026-08-01", "2026-08-02"
    )
    ok(
        htotals
        == {
            ("2026-08-01", "g1"): 99 + 5,
            ("2026-08-01", "g2"): 4,
            ("2026-08-02", "g2"): 6,
        },
        f"hourly_date_totals 按 (date, group) SUM（实际 {htotals}）",
    )
    ok(
        ("2026-08-02", "g1") not in htotals,
        "无行的 (date, group) 不出现在结果（存在性语义）",
    )
    htotals = await mgr.snapshot_hourly_date_totals(
        "msg", "2026-08-01", "2026-08-02", group_id="g1"
    )
    ok(htotals == {("2026-08-01", "g1"): 104}, "hourly_date_totals 群过滤仅 g1")
    ok(
        await mgr.snapshot_hourly_date_totals("bad", "2026-08-01", "2026-08-02")
        == {},
        "hourly_date_totals 非法 source 返回 {} 不抛",
    )
    # 图片侧原语走 image_* 表（与消息侧隔离）
    itotals = await mgr.snapshot_monthly_totals("image", "2026-06", "2026-08")
    ok(itotals == {"g1": 40}, f"image monthly_totals 仅图片表数据（实际 {itotals}）")

    # ---- 15. v0.5.5 snapshot_evict：四阈值 DELETE + monthly 不动 ----
    await mgr.snapshot_upsert_msg_hour(
        [("2020-01-01", 0, "g1", 1), ("2026-08-04", 0, "g1", 2)],
        [("2020-01-01", 0, "g1", "u1", "旧", 1)],
    )
    await mgr.snapshot_upsert(
        [("2020-01-01", 0, "g1", 1)],
        [("2020-01-01", 0, "g1", "u1", "旧", 1)],
    )
    await mgr.snapshot_upsert_daily(
        [("2020-01-01", "g1", 1)], [("2020-01-01", "g1", 1)]
    )
    ok(
        await mgr.snapshot_evict("2026-01-01", "2026-01-01", "2026-01-01", "2026-01-01")
        is True,
        "snapshot_evict 成功返回 True",
    )
    ok(
        await _count(mgr, "SELECT COUNT(*) FROM msg_stats_hourly WHERE date < '2026-01-01'")
        == 0,
        "msg_stats_hourly 早于阈值行已删",
    )
    ok(
        await _count(mgr, "SELECT COUNT(*) FROM image_stats_hourly WHERE date < '2026-01-01'")
        == 0,
        "image_stats_hourly 早于阈值行已删",
    )
    ok(
        await _count(
            mgr, "SELECT COUNT(*) FROM msg_stats_hourly_top WHERE date < '2026-01-01'"
        )
        == 0,
        "msg_stats_hourly_top 早于阈值行已删",
    )
    ok(
        await _count(
            mgr,
            "SELECT COUNT(*) FROM image_stats_hourly_top WHERE date < '2026-01-01'",
        )
        == 0,
        "image_stats_hourly_top 早于阈值行已删",
    )
    ok(
        await _count(mgr, "SELECT COUNT(*) FROM msg_stats_daily WHERE date < '2026-01-01'")
        == 0,
        "msg_stats_daily 早于阈值行已删",
    )
    ok(
        await _count(
            mgr, "SELECT COUNT(*) FROM image_stats_daily WHERE date < '2026-01-01'"
        )
        == 0,
        "image_stats_daily 早于阈值行已删",
    )
    ok(
        await _count(mgr, "SELECT COUNT(*) FROM msg_stats_hourly WHERE date >= '2026-01-01'")
        >= 1,
        "阈值内行保留（hourly）",
    )
    ok(
        await _count(mgr, "SELECT COUNT(*) FROM msg_stats_monthly") == 4
        and await _count(mgr, "SELECT COUNT(*) FROM image_stats_monthly") == 1,
        "monthly 两表永不淘汰（行数不变）",
    )

    # ---- 16. 自愈重连：close 后经 _ensure_db 自动建连 ----
    await mgr.close()
    ok(mgr.db is None, "close 后连接置 None")
    after_reconnect = await mgr.get_all_stats_settings()
    ok(len(after_reconnect) == 8, "close 后自愈重连读取 8 项配置成功")
    await mgr.close()

    print(f"\n冒烟通过：{PASSED} 项断言全部 PASS")


if __name__ == "__main__":
    asyncio.run(main())
