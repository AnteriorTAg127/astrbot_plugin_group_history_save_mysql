# ruff: noqa: I001
"""v0.4.0 Module A 配置层冒烟脚本（db_config.py profile_settings）。

用真实 aiosqlite 临时库验证：建表/播种/typed 读取（含非法值回退、"1"/"0" bool）/
save 全量校验后写入（含非法值整体不写入）/reset，断言配置计数 == 19。

运行方式（插件根目录）：
    python "tests/v0.4.0/test_profile_config_smoke.py"

astrbot.* 全部 sys.modules stub，stub 必须在 import db_config 之前完成
（范式沿用 tests/v0.3.1/test_v031.py）。
"""

import asyncio
import sys
import tempfile
import types
from pathlib import Path

# ============================================================
# 一、stub 注入（必须在 import db_config 之前）
# ============================================================

_DATA_DIR = Path(tempfile.mkdtemp(prefix="profile_cfg_smoke_"))

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
_PLUGIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PLUGIN_ROOT.parent))

from astrbot_plugin_group_history_save_mysql.core.db_config import ConfigManager  # noqa: E402

PASSED = 0


def ok(cond: bool, desc: str) -> None:
    global PASSED
    assert cond, f"断言失败: {desc}"
    PASSED += 1
    print(f"  [PASS] {desc}")


async def main() -> None:
    mgr = ConfigManager()
    print(f"临时库: {mgr.db_path}")

    # ---- 1. 初始化：建表 + 播种 ----
    ok(await mgr.initialize() is True, "initialize() 成功")

    async with mgr.db.execute("SELECT COUNT(*) FROM profile_settings") as cur:
        count = (await cur.fetchone())[0]
    ok(count == 19, f"播种后配置计数 == 19（实际 {count}）")
    ok(len(ConfigManager.PROFILE_DEFAULTS) == 19, "PROFILE_DEFAULTS 19 项")
    ok(len(ConfigManager.PROFILE_TYPES) == 19, "PROFILE_TYPES 19 项")
    ok(
        set(ConfigManager.PROFILE_DEFAULTS) == set(ConfigManager.PROFILE_TYPES),
        "DEFAULTS 与 TYPES 键集合一致",
    )
    ok(
        all(
            (v is not None and v != "") or k == "profile_provider"
            for k, v in ConfigManager.PROFILE_DEFAULTS.items()
        ),
        "默认值非 null（provider 允许空串）",
    )

    # ---- 2. typed 读取默认值 ----
    ok(
        await mgr.get_profile_setting("profile_enabled") == "true",
        "raw 读取 profile_enabled == 'true'",
    )
    ok(
        await mgr.get_profile_setting_typed("profile_enabled") is True,
        "typed bool 默认 True",
    )
    ok(
        await mgr.get_profile_setting_typed("profile_max_count") == 2000,
        "typed int 默认 2000",
    )
    ok(
        await mgr.get_profile_setting_typed("profile_fallback_providers") == [],
        "typed list 默认 []",
    )
    ok(
        await mgr.get_profile_setting_typed("profile_permission") == "admin",
        "typed str 默认 admin",
    )
    ok(
        await mgr.get_profile_setting_typed("profile_feedback_text")
        == "正在生成人物画像，请稍候…",
        "typed str 中文文案",
    )

    all_settings = await mgr.get_all_profile_settings()
    ok(
        len(all_settings) == 19,
        f"get_all_profile_settings 全量 19 项（实际 {len(all_settings)}）",
    )
    ok(all_settings["profile_relation_max_partners"] == 10, "all typed int 值正确")
    ok(all_settings["profile_dim_habits"] is True, "all typed bool 值正确")

    # ---- 3. bool "1"/"0" 识别 ----
    await mgr.db.execute(
        "UPDATE profile_settings SET value = ? WHERE key = ?",
        ("1", "profile_dim_hobbies"),
    )
    await mgr.db.execute(
        "UPDATE profile_settings SET value = ? WHERE key = ?",
        ("0", "profile_dim_relations"),
    )
    await mgr.db.commit()
    ok(
        await mgr.get_profile_setting_typed("profile_dim_hobbies") is True,
        'bool "1" → True',
    )
    ok(
        await mgr.get_profile_setting_typed("profile_dim_relations") is False,
        'bool "0" → False',
    )

    # ---- 4. 非法值回退默认 ----
    await mgr.db.execute(
        "UPDATE profile_settings SET value = ? WHERE key = ?",
        ("abc", "profile_max_count"),
    )
    await mgr.db.execute(
        "UPDATE profile_settings SET value = ? WHERE key = ?",
        ("maybe", "profile_enabled"),
    )
    await mgr.db.execute(
        "UPDATE profile_settings SET value = ? WHERE key = ?",
        ('{"not": "list"}', "profile_fallback_providers"),
    )
    await mgr.db.commit()
    ok(
        await mgr.get_profile_setting_typed("profile_max_count") == 2000,
        "非法 int 回退 2000",
    )
    ok(
        await mgr.get_profile_setting_typed("profile_enabled") is True,
        "非法 bool 回退 True",
    )
    ok(
        await mgr.get_profile_setting_typed("profile_fallback_providers") == [],
        "非法 JSON 回退 []",
    )
    all_illegal = await mgr.get_all_profile_settings()
    ok(all_illegal["profile_max_count"] == 2000, "all 内非法 int 回退")
    ok(all_illegal["profile_enabled"] is True, "all 内非法 bool 回退")

    # ---- 5. save：全量校验后写入 ----
    await mgr.save_profile_settings(
        {
            "profile_enabled": False,
            "profile_max_count": 100,
            "profile_fallback_providers": ["p1", "p2"],
            "profile_feedback_text": "稍等",
        }
    )
    ok(
        await mgr.get_profile_setting_typed("profile_enabled") is False,
        "save bool 归一化写入",
    )
    ok(await mgr.get_profile_setting_typed("profile_max_count") == 100, "save int 写入")
    ok(
        await mgr.get_profile_setting_typed("profile_fallback_providers")
        == ["p1", "p2"],
        "save list JSON 序列化写入",
    )
    ok(
        await mgr.get_profile_setting("profile_feedback_text") == "稍等",
        "save str 写入",
    )

    # 非法值混入 → 整体不写入（合法项也不生效）
    before = await mgr.get_profile_setting("profile_max_count")
    await mgr.save_profile_settings(
        {"profile_max_count": 999, "profile_enabled": "not_a_bool"}
    )
    after = await mgr.get_profile_setting("profile_max_count")
    ok(before == after == "100", "含非法值整体不写入（合法项 999 也未生效）")

    # 未知键跳过、其余合法项正常写入
    await mgr.save_profile_settings({"unknown_key": 1, "profile_keep_days": 7})
    ok(
        await mgr.get_profile_setting_typed("profile_keep_days") == 7,
        "未知键跳过、合法项写入",
    )
    ok(await mgr.get_profile_setting("unknown_key") == "", "未知键未入库")

    # ---- 6. reset ----
    await mgr.reset_profile_settings()
    reset_all = await mgr.get_all_profile_settings()
    ok(len(reset_all) == 19, "reset 后仍为完整 19 项")
    ok(reset_all["profile_enabled"] is True, "reset 恢复 bool 默认")
    ok(reset_all["profile_max_count"] == 2000, "reset 恢复 int 默认")
    ok(reset_all["profile_fallback_providers"] == [], "reset 恢复 list 默认")
    ok(reset_all["profile_keep_days"] == 30, "reset 恢复 keep_days 默认 30")
    ok(
        reset_all["profile_feedback_text"] == "正在生成人物画像，请稍候…",
        "reset 恢复文案默认",
    )

    # ---- 7. 缺失键自动播种（自愈范式）----
    await mgr.db.execute(
        "DELETE FROM profile_settings WHERE key = ?", ("profile_user_cooldown",)
    )
    await mgr.db.commit()
    ok(
        await mgr.get_profile_setting("profile_user_cooldown") == "60",
        "缺失键读取时自动播种默认值",
    )

    await mgr.close()
    ok(mgr.db is None, "close 后连接置 None")

    print(f"\n冒烟通过：{PASSED} 项断言全部 PASS")


if __name__ == "__main__":
    asyncio.run(main())
