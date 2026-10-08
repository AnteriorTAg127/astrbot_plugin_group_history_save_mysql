"""smoke_e_render TestNodeCheck 两例的沙箱等价验证（离线测试工具）。

DSH 沙箱禁 named pipe：subprocess.run(capture_output=True) 在 _get_handles
CreatePipe 处 EPERM。本脚本把 subprocess.run 换成**文件重定向 stdio**，
语义等价地执行 node --check，验证渲染产物 JS 语法确实通过。
"""

import pathlib
import subprocess
import sys
import unittest

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "v0.5.0" / "test"))

_orig_run = subprocess.run
_ctr = [0]


def _run_fileredirect(cmd, **kw):
    kw.pop("capture_output", None)
    kw.pop("text", None)
    kw.pop("encoding", None)
    _ctr[0] += 1
    outf = HERE / ".baseline_out" / f"node_check_{_ctr[0]}.txt"
    with outf.open("w", encoding="utf-8", errors="replace") as fh:
        return _orig_run(cmd, stdout=fh, stderr=subprocess.STDOUT, **kw)


subprocess.run = _run_fileredirect

import smoke_e_render  # noqa: E402

suite = unittest.TestLoader().loadTestsFromTestCase(smoke_e_render.TestNodeCheck)
res = unittest.TextTestRunner(verbosity=2).run(suite)
sys.exit(0 if res.wasSuccessful() else 1)
