"""清理基线运行/探针产生的临时目录与探针脚本（离线测试工具）。"""

import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

JUNK_DIRS = [
    "_basetemp",
    "_bt5_1788578794",
    "_bt6",
    "_btmp1",
    "_btmp2",
    "_probe_tmpdir",
    "_sandbox_tmp_root",
    "_tmp",
    "_tmp2",
    "_tmpwork",
    ".baseline_tmp",
    ".probe_ws",
    ".smoke_tmp",
    "baseline_tmp",
    "diag_a",
    "diag_child",
    "diag_link",
    "diag_link2",
    "diag_name",
    "diag_precise",
    "diaglink",
    "diagtmp",
    "ptmp",
    "ptmp2",
    "ptmp3",
    "tmp2",
    "tmpti7rmxhn",
    "tmp_v090",
]
for name in JUNK_DIRS:
    d = HERE / name
    if d.is_dir():
        shutil.rmtree(d, ignore_errors=True)
        print("removed dir", name)

JUNK_FILES = [
    "_baseline_console.txt",
    "_baseline_err.txt",
    "_diag_sandbox.py",
    "_extract_denied.py",
    "_probe_basetemp_selfmade.py",
    "_probe_child.py",
    "_probe_envtmp.py",
    "_probe_link.py",
    "_probe_names.py",
    "_probe_precise.py",
    "_probe_sqlite.py",
    "_probe5.py",
    "_probe6.py",
]
for name in JUNK_FILES:
    f = HERE / name
    if f.exists():
        f.unlink()
        print("removed file", name)
print("done")
