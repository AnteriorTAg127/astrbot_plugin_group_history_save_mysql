# ruff: noqa
"""v0.9.0 测试阶段：基线逐文件执行脚本（离线测试工具）。

用法（插件根目录）：
    python tests/v0.9.0/_run_baseline.py [文件名正则]
对每个测试文件单独起一个 pytest 子进程（cwd=文件所在目录，规避中文路径
收集问题），汇总末行统计与失败清单，写入 _baseline_log.txt 并打印。
"""

import re
import subprocess
import sys
from pathlib import Path

# 控制台中文/emoji 输出兜底（Windows 默认 GBK）
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parents[2]
FILES = [
    "tests/v0.1/test_config.py",
    "tests/v0.2/test_v02.py",
    "tests/v0.3/test_v03.py",
    "tests/v0.3.1/test_v031.py",
    "tests/v0.3.2/test_v032.py",
    "tests/v0.4.0/test_db_mysql_v040.py",
    "tests/v0.4.0/test_models_capture.py",
    "tests/v0.4.0/test_profile_analyzer.py",
    "tests/v0.4.0/test_profile_config_smoke.py",
    "tests/v0.4.0/test_profile_fetcher.py",
    "tests/v0.4.0/test_profile_formatter.py",
    "tests/v0.4.0/test_profile_main.py",
    "tests/v0.4.0/test_profile_service.py",
    "tests/v0.4.0/test_profile_stats.py",
    "tests/v0.4.0/test_profile_storage.py",
    "tests/v0.4.0/test_profile_webapi.py",
    "tests/v0.5.0/test_v050.py",
    "tests/v0.5.0/smoke_a_config.py",
    "tests/v0.5.0/smoke_b_repository.py",
    "tests/v0.5.0/smoke_c_parser.py",
    "tests/v0.5.0/smoke_d_snapshot.py",
    "tests/v0.5.0/smoke_e_render.py",
    "tests/v0.5.0/smoke_f_scheduler.py",
    "tests/v0.5.0/smoke_g_service.py",
    "tests/v0.5.0/smoke_h_webapi.py",
    "tests/v0.5.0/smoke_m_main.py",
    "tests/v0.5.5/test_v055.py",
    "tests/v0.6.0/test_v060.py",
    "tests/v0.6.1/test_v061.py",
    "tests/v0.7.0/test_v070.py",
    "tests/v0.9.0/smoke_a_sqlite.py",
    "tests/v0.9.0/test_v090.py",
]

# 非 pytest 组织、直接 python 运行的冒烟脚本（自打印计数 + exit code）
SCRIPT_MODE = {
    "tests/v0.1/test_config.py",
    "tests/v0.4.0/test_profile_config_smoke.py",
    "tests/v0.5.0/smoke_a_config.py",
    "tests/v0.5.0/smoke_d_snapshot.py",
    "tests/v0.9.0/smoke_a_sqlite.py",
}

# 子进程输出重定向目录（沙箱禁 named pipe，piped stdio 会 EPERM，
# 一律用文件重定向捕获）
OUT_DIR = Path(__file__).resolve().parent / ".baseline_out"

# 统计行候选：pytest 摘要 / 冒烟脚本自打印行
STAT_RE = re.compile(
    r"^\s*(?:=+[^=]*(?:passed|failed|error|no tests ran)[^=]*=+|"
    r"\d+ (?:passed|failed)(?:[,\s].*)?|"
    r"(?:冒烟|自检|结果|SMOKE|全部).{0,40}(?:通过|PASS|OK|失败|FAIL)\s*$)"
)


def run_one(rel: str) -> tuple[int, list[str], list[str]]:
    p = ROOT / Path(*rel.split("/"))
    if not p.exists():
        return -1, ["MISSING"], []
    if rel in SCRIPT_MODE:
        # 经 _sandbox_tmp 的脚本模式跑：tempfile 重定向到工作区可写目录
        cmd = [sys.executable, "-m", "_sandbox_tmp", p.name]
    else:
        cmd = [
            sys.executable,
            "-m",
            "pytest",
            p.name,
            "-q",
            "--no-header",
            "-p",
            "no:cacheprovider",
            "--tb=line",
            "-rf",
            "-p",
            "_sandbox_tmp",  # DSH 沙箱临时区兼容 shim（见 _sandbox_tmp.py）
        ]
    OUT_DIR.mkdir(exist_ok=True)
    import os

    env = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        # 让 _sandbox_tmp 插件/模块可被发现（-p 与 -m 均按模块名解析）
        "PYTHONPATH": str(Path(__file__).resolve().parent)
        + os.pathsep
        + os.environ.get("PYTHONPATH", ""),
    }
    out_file = OUT_DIR / (p.stem + ".out.txt")
    with out_file.open("w", encoding="utf-8", errors="replace") as fh:
        # 文件重定向（非管道）：沙箱禁 named pipe，piped stdio 会 EPERM
        proc = subprocess.run(
            cmd,
            cwd=p.parent,
            stdout=fh,
            stderr=subprocess.STDOUT,
            timeout=900,
            env=env,
        )
    lines = out_file.read_text(encoding="utf-8", errors="replace").splitlines()
    stats = [ln for ln in lines if STAT_RE.match(ln)]
    failed = [ln for ln in lines if ln.startswith("FAILED") or ln.startswith("ERROR")]
    return proc.returncode, stats[-3:] if stats else lines[-3:], failed


def main() -> None:
    filt = sys.argv[1] if len(sys.argv) > 1 else None
    log_path = ROOT / "tests" / "v0.9.0" / "_baseline_log.txt"
    if filt is None and log_path.exists():
        log_path.unlink()
    for rel in FILES:
        if filt and not re.search(filt, rel):
            continue
        try:
            code, stats, failed = run_one(rel)
        except Exception as e:  # noqa: BLE001 - 运行器兜底，单文件异常不中断
            code, stats, failed = -2, [f"RUNNER ERROR {e}"], []
        entry = [f"### FILE {rel} exit={code}"]
        for s in stats:
            entry.append(f"    {s.strip()}")
        for f in failed:
            entry.append(f"    FAIL {f.strip()}")
        entry.append("")
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(entry) + "\n")
        print("\n".join(entry), flush=True)
    print("LOG WRITTEN")


if __name__ == "__main__":
    main()
