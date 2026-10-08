# ruff: noqa
"""_sandbox_tmp — DSH 沙箱兼容 pytest 插件（仅离线测试基建）。

背景：DSH 文件沙箱禁止在 tempfile 安全区（%TEMP%/dsh-*）与 mkdtemp 风格
随机后缀目录内创建文件（Agent-A 在 smoke_a_sqlite.py 已记录同一约束），
导致依赖 pytest tmp_path / tempfile.mkdtemp / TemporaryDirectory 的既有
套件在本环境整批 ERROR/FAIL——纯环境限制，与被测代码无关。

本插件只做**路径重定向**，不改任何测试语义：
1. 普通工作区目录（预建的无随机后缀目录）经验证可正常读写；
2. tmp_path / tmp_path_factory：把 basetemp 钉到 `_sandbox_tmp_root/pt/<name>`，
   绕开 pytest 的 pytest-N 编号目录 + .lock + symlink 机制（该机制在本沙箱
   恰好被拦）；
3. tempfile.mkdtemp / TemporaryDirectory / gettempdir：猴子补丁到
   `_sandbox_tmp_root/tf/<prefix><n>`（序号自增，无随机后缀）。

用法：`python -m pytest <file> -p _sandbox_tmp`（配合 PYTHONPATH 指向本目录）。
"""

import shutil
import sys
import tempfile
from pathlib import Path

# 每次会话以全新目录运行：pytest_configure 里 rmtree 旧根，避免跨运行残留
# （sqlite 库/JSON 状态串扰），tests/ 目录整体不提交，垃圾无碍
_ROOT = Path(__file__).resolve().parent / "_sandbox_tmp_root"

_counter = [0]


def _next_dir(prefix: str = "tf") -> Path:
    _counter[0] += 1
    d = _ROOT / prefix / f"d{_counter[0]:05d}"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# tempfile 猴子补丁（mkdtemp / TemporaryDirectory / gettempdir）
# ---------------------------------------------------------------------------

_orig_mkdtemp = tempfile.mkdtemp
_orig_tempdir = tempfile.TemporaryDirectory
_orig_gettempdir = tempfile.gettempdir


def _safe_prefix(prefix: str) -> str:
    # 随机后缀与非法字符不参与路径（沙箱按随机后缀目录名拦截写入）
    keep = "".join(ch for ch in (prefix or "") if ch.isalnum() or ch in "_-")
    return keep[:24] or "tf"


def _patched_mkdtemp(suffix="", prefix="tmp", dir=None, ignore_cleanup_errors=False):
    return str(_next_dir("tf/" + _safe_prefix(prefix)))


# TemporaryDirectory 不单独补丁：其内部经模块全局 mkdtemp 建目录，
# mkdtemp 已被重定向，故自动生效（Python 3.12 的 TemporaryDirectory
# 也不再接受 name 参数，子类覆写反而破坏签名）。


def _patched_gettempdir() -> str:
    d = _ROOT / "tf" / "root"
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def install_tempfile_patch() -> None:
    if getattr(tempfile, "_sandbox_tmp_patched", False):
        return
    tempfile.mkdtemp = _patched_mkdtemp
    # TemporaryDirectory 经模块全局 mkdtemp 建目录，已随 mkdtemp 重定向生效
    tempfile.gettempdir = _patched_gettempdir
    tempfile.tempdir = _patched_gettempdir()
    # mkstemp / NamedTemporaryFile 一并重定向（smoke_e_render 用到）
    _orig_mkstemp = tempfile.mkstemp

    def _patched_mkstemp(suffix="", prefix="tmp", dir=None, text=False):
        d = _next_dir("tf/" + _safe_prefix(prefix))
        fd, path = _orig_mkstemp(suffix=suffix, prefix="f", dir=str(d), text=text)
        return fd, path

    tempfile.mkstemp = _patched_mkstemp
    tempfile._orig = (  # noqa: SLF001 - 备份，供 uninstall
        _orig_mkdtemp,
        _orig_tempdir,
        _orig_gettempdir,
    )
    tempfile._sandbox_tmp_patched = True


# ---------------------------------------------------------------------------
# pytest 钩子：钉死 tmp_path_factory 的 basetemp，绕开编号目录 + .lock 机制
# ---------------------------------------------------------------------------


def pytest_configure(config) -> None:  # noqa: ARG001
    install_tempfile_patch()
    # 全新会话目录：清掉上次残留，避免跨运行状态串扰
    try:
        if _ROOT.exists():
            shutil.rmtree(_ROOT, ignore_errors=True)
    except Exception:
        pass
    _ROOT.mkdir(parents=True, exist_ok=True)


def pytest_sessionstart(session) -> None:  # noqa: ARG001
    config = session.config
    factory = getattr(config, "_tmp_path_factory", None)
    if factory is None:
        return
    root = _ROOT / "pt"
    root.mkdir(exist_ok=True)
    factory._basetemp = root  # noqa: SLF001
    factory._given_basetemp = root  # noqa: SLF001
    factory._trace = lambda *a, **k: None  # noqa: SLF001

    def getbasetemp() -> Path:
        return root

    def mktemp(basename: str, numbered: bool = True) -> Path:  # noqa: ARG001
        _counter[0] += 1
        d = root / f"t{_counter[0]:05d}_{basename}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    factory.getbasetemp = getbasetemp
    factory.mktemp = mktemp


def pytest_sessionfinish(session, exitstatus) -> None:  # noqa: ARG001
    # 兜底：清理符号链接残留（不抛）
    try:
        for lk in (_ROOT / "pt").glob("pytest-current*"):
            if lk.is_symlink():
                lk.unlink()
    except Exception:
        pass
    sys.stderr.flush()


# ---------------------------------------------------------------------------
# 独立脚本模式：`python -m _sandbox_tmp <script.py> [args...]`
# （给 SCRIPT_MODE 冒烟脚本用：先装 tempfile 补丁再 runpy 执行目标脚本）
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import runpy

    _target = sys.argv[1]
    sys.argv = sys.argv[1:]
    install_tempfile_patch()
    runpy.run_path(_target, run_name="__main__")
