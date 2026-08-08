"""人物分析子包（v0.4.0）。

对外导出编排层门面 :class:`ProfileService`（Module J，profile/service.py）——
main.py 薄 handler 与 WebAPI 均经其 ``handle_command`` / ``run_analysis`` 双入口
调用；同时导出常用数据模型，便于上游（main.py / web_api.py）类型引用。

导出采用 PEP 562 惰性 ``__getattr__``：``from .service import ProfileService`` 会
连带拉起 analyzer / formatter / t2i_render 整条依赖链，若在子包 ``__init__`` 顶层
急切执行，会使任何 ``import profile.models`` 的调用方（含各模块离线单测的轻量
astrbot stub 环境）被迫加载全链而 ImportError。惰性导出保证：

- 生产环境 ``from ..profile import ProfileService`` / ``profile.ProfileService``
  正常可用（首次访问时才加载 service 及其依赖）；
- 仅导入 ``profile.models`` / ``profile.capture`` 等轻量子模块时不触发 service 链，
  与各模块单测的 stub 隔离范式（Agent-C 技巧）兼容，支持多测试文件合跑。
"""

from typing import TYPE_CHECKING

# 惰性导出名 → 所属子模块（首次访问时按需 import）
_LAZY_EXPORTS = {
    "ProfileService": ".service",
    "ProfileTarget": ".models",
    "ProfileMessage": ".models",
    "ProfileStats": ".models",
    "ProfileResult": ".models",
    "ProfileFetchOutcome": ".models",
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
    from .models import (  # noqa: F401
        ProfileFetchOutcome,
        ProfileMessage,
        ProfileResult,
        ProfileStats,
        ProfileTarget,
    )
    from .service import ProfileService  # noqa: F401
