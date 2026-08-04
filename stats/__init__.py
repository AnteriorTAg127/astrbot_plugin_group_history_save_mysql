"""数据分析子包（v0.5.0）。

对外导出 MySQL 聚合仓储 :class:`StatsRepository`（模块 B，stats/repository.py）与
编排服务 :class:`StatsService` / :class:`StatsBuildError`（模块 G，stats/service.py）——
main.py 经其完成指令接线与生命周期管理，WebAPI 经其完成 ``/stats/data`` 组装。

导出采用 PEP 562 惰性 ``__getattr__``（沿用 profile/ 子包范式）：
``from .repository import StatsRepository`` 会连带拉起 db_mysql（import aiomysql /
astrbot.api），若在子包 ``__init__`` 顶层急切执行，会使任何仅导入 stats 轻量子模块
（如后续模块 C 的 models / parser）的调用方（含各模块离线单测的轻量 astrbot stub
环境）被迫加载整条依赖链而 ImportError。惰性导出保证：

- 生产环境 ``from ...stats import StatsRepository`` / ``stats.StatsRepository``
  正常可用（首次访问时才加载 repository 及其依赖）；
- 仅导入 ``stats.models`` / ``stats.parser`` 等轻量子模块时不触发 repository 链，
  与各模块单测的 stub 隔离范式兼容，支持多测试文件合跑。

注意：``_LAZY_EXPORTS`` 仅登记需要以 ``from .stats import X`` 形式导出的重依赖名；
models / parser / scheduler 等轻量子模块由调用方直接按模块路径导入
（web_api.py / main.py 即 ``from .stats.models import ...``），不在此登记。
v0.5.5 起 ``SnapshotManager`` 与其兼容别名 ``ImageSnapshotManager`` 也登记为
惰性导出（首次访问才按需加载 snapshot 模块，不影响下述轻量子模块的隔离性）。
**切勿**在此文件顶层急切 import 整条依赖链。
"""

from typing import TYPE_CHECKING

# 惰性导出名 → 所属子模块（首次访问时按需 import）
_LAZY_EXPORTS = {
    "StatsRepository": ".repository",
    "StatsService": ".service",
    "StatsBuildError": ".service",
    "SnapshotManager": ".snapshot",
    "ImageSnapshotManager": ".snapshot",
}

__all__ = list(_LAZY_EXPORTS)


def __getattr__(name: str):
    """PEP 562 惰性导出：首次访问时才加载对应子模块并缓存其属性。"""
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    module = importlib.import_module(module_name, __name__)
    value = getattr(module, name)
    globals()[name] = value  # 缓存，后续访问不再走 __getattr__
    return value


if TYPE_CHECKING:  # 供静态分析/IDE 识别导出名（运行时不执行）
    from .repository import StatsRepository  # noqa: F401
    from .service import StatsBuildError, StatsService  # noqa: F401
    from .snapshot import ImageSnapshotManager, SnapshotManager  # noqa: F401
