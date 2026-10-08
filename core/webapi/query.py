"""Web API 查询端点（v0.6.0 包化拆分）。

QueryMixin：聊天记录多条件查询 + 回复目标消息补充。
"""

from astrbot.api.web import json_response, request

from ..query_enrich import enrich_query_reply


class QueryMixin:
    """查询端点 Mixin（v0.6.0 拆分自 web_api.py）。"""

    async def api_query(self):
        """查询聊天记录（支持多条件过滤和分页）。

        每条记录附带 ``reply_message`` 关联信息：本条回复目标消息
        （{"sender_id", "sender_name", "content"}，取不到为 None）。
        说明：at_list（被 @ 的 QQ）仅作记录存储，@ ID 无法可靠反查
        对应消息（@ 了某人不代表其某条消息与本次互动相关），故不作为
        关联上下文展示（见 v0.4.0 PRD 备注）。
        """
        group_id = request.query.get("group_id")
        sender_id = request.query.get("sender_id")
        time_start = request.query.get("time_start")
        time_end = request.query.get("time_end")
        keyword = (request.query.get("keyword") or "").strip() or None
        # 显式转换，避免框架 type=int 行为不一致
        page_str = request.query.get("page") or "1"
        try:
            page = int(page_str)
        except (ValueError, TypeError):
            page = 1
        page_size_str = request.query.get("page_size") or "50"
        try:
            page_size = int(page_size_str)
        except (ValueError, TypeError):
            page_size = 50

        # 参数校验
        if page < 1:
            page = 1
        if page_size < 1:
            page_size = 50
        if page_size > 200:
            page_size = 200

        # 群号/QQ 号为文本字段，空字符串视同未提供
        group_id = group_id or None
        sender_id = sender_id or None

        result = await self.mysql_mgr.query_messages(
            group_id=group_id,
            sender_id=sender_id,
            time_start=time_start,
            time_end=time_end,
            keyword=keyword,
            page=page,
            page_size=page_size,
        )
        await self._enrich_query_reply(result)
        return json_response(result)

    async def _enrich_query_reply(self, result: dict) -> None:
        """为查询结果批量补充回复目标消息（v0.8.2 R2：委托 core/query_enrich 共享实现）。

        与 core/public_api.py 的 _enrich_query_reply 共用同一份逻辑，杜绝
        Web 与对外 API 口径分叉；本方法名保留做兼容。反查经
        self.mysql_mgr.get_messages_by_ids 回调注入，共享模块不依赖 db 层。
        """
        records = result.get("records") or []
        await enrich_query_reply(records, self.mysql_mgr.get_messages_by_ids)
