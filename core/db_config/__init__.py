"""本地配置存储层（v0.6.0 包化拆分）。

主文件（base.py）负责连接管理与核心设置读写；子功能文件按功能拆分
groups/summary_settings/profile_settings/stats_settings/snapshots。
组装范式：Mixin 多继承出单一公开类名 ConfigManager，对外调用零改动。
"""

from .base import ConfigManagerBase
from .groups import GroupMixin
from .profile_settings import ProfileSettingsMixin
from .snapshots import SnapshotMixin
from .stats_settings import StatsSettingsMixin
from .summary_settings import SummarySettingsMixin


class ConfigManager(
    ConfigManagerBase,
    GroupMixin,
    SummarySettingsMixin,
    ProfileSettingsMixin,
    StatsSettingsMixin,
    SnapshotMixin,
):
    """本地配置管理器（组装门面）：白名单 + 总结/人物/统计配置 + 快照。"""


__all__ = ["ConfigManager"]
