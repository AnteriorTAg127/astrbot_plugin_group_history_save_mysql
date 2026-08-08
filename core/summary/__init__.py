"""群聊历史自动总结子包（v0.3）。

对外仅导出编排层门面 :class:`SummaryService`（模块 H 产出）：main.py（模块 K）
只依赖该类完成两条总结指令的异步委托与 start/stop 生命周期接线；内部模块
fetcher / summarizer / formatter / storage / scheduler 均由 SummaryService
自行组装，不对外暴露。
"""

from .service import SummaryService

__all__ = ["SummaryService"]
