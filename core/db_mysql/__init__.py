"""MySQL 数据库操作层（v0.6.0 包化拆分）。

主文件（base.py）负责连接池管理与最终访问；子功能文件按功能拆分
chat_history/images/stats/maintenance。组装范式：Mixin 多继承出单一
公开类名 MySQLManager，对外调用零改动。
"""

from .base import (
    CREATE_RETRY_BACKOFF_SECONDS,
    DDL_TIMEOUT_SECONDS,
    QUERY_TIMEOUT_SECONDS,
    RESET_PENDING_WAIT_SECONDS,
    MySQLManagerBase,
)
from .chat_history import ChatHistoryMixin
from .images import ImagesMixin
from .maintenance import MaintenanceMixin
from .pool import DynamicPool
from .stats import StatsMixin


class MySQLManager(
    MySQLManagerBase, ChatHistoryMixin, ImagesMixin, StatsMixin, MaintenanceMixin
):
    """MySQL 操作门面（组装）：连接池 + 聊天记录 + 图片 + 统计 + 维护。"""


__all__ = [
    "MySQLManager",
    "DynamicPool",
    "QUERY_TIMEOUT_SECONDS",
    "DDL_TIMEOUT_SECONDS",
    "CREATE_RETRY_BACKOFF_SECONDS",
    "RESET_PENDING_WAIT_SECONDS",
]
