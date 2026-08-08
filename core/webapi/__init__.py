"""Web API 后端（v0.6.0 包化拆分）。

主文件（base.py）负责路由注册与公共 helper；子功能文件按功能拆分
storage/query/summary/profile/stats。组装范式：Mixin 多继承出单一
公开类名 WebAPI，对外调用零改动。
"""

from .base import WebAPIBase, _to_jsonable, make_challenge
from .profile import ProfileMixin
from .query import QueryMixin
from .stats import StatsMixin
from .storage import StorageMixin
from .summary import SummaryMixin


class WebAPI(
    WebAPIBase, StorageMixin, QueryMixin, SummaryMixin, ProfileMixin, StatsMixin
):
    """Web API 门面（组装）：存储库 + 查询 + 总结 + 人物 + 统计。"""


__all__ = ["WebAPI", "make_challenge", "_to_jsonable"]
