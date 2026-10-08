"""PROD-BUG-02 复现：AstrBot v26.6 是否会在插件加载时剥离 schema 外键。

模拟 star_manager 加载插件配置的完整路径：
1) 配置文件已含 active_backend_lock="sqlite"（v0.9.0 插件运行期写入后的落盘态）；
2) AstrBotConfig(config_path=..., schema=插件 schema) 构造（框架加载期行为）；
3) 观察 active_backend_lock 是否仍在内存 dict 与磁盘文件中。
"""

import json
import pathlib
import sys

sys.path.insert(0, r"F:\astrbot\AstrBot")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

WORK = pathlib.Path(__file__).resolve().parent / "repro_prod_bug_02"
WORK.mkdir(exist_ok=True)

cfg_path = WORK / "plugin_config.json"
schema_path = (
    pathlib.Path(r"F:\astrbot\AstrBot\data\plugins")
    / "astrbot_plugin_group_history_save_mysql"
    / "_conf_schema.json"
)
schema = json.loads(schema_path.read_text(encoding="utf-8"))

# 1) 模拟插件写完锁后的配置实体（含 schema 内若干项 + schema 外锁标记）
stored = {
    "mysql_host": "127.0.0.1",
    "storage_backend": "sqlite",
    "sqlite_wal_mode": True,
    "sqlite_busy_timeout_ms": 5000,
    "active_backend_lock": "sqlite",
}
cfg_path.write_text(json.dumps(stored, ensure_ascii=False, indent=4), encoding="utf-8")

from astrbot.core.config.astrbot_config import AstrBotConfig  # noqa: E402

# 2) 框架加载插件时的等价构造
ac = AstrBotConfig(config_path=str(cfg_path), schema=schema)

# 3) 观察
in_mem = "active_backend_lock" in ac
on_disk = "active_backend_lock" in json.loads(
    pathlib.Path(cfg_path).read_text(encoding="utf-8-sig")
)
print("内存 dict 仍含锁:", in_mem)
print("磁盘文件仍含锁:", on_disk)
print("storage_backend:", ac.get("storage_backend"))
print(
    "RESULT:",
    "锁被剥离 → PROD-BUG-02 成立" if not (in_mem and on_disk) else "锁保留 → 不成立",
)
