"""Web API 查询端点（v0.6.0 包化拆分）。

QueryMixin：聊天记录多条件查询 + 回复目标消息补充。
"""

from astrbot.api.web import json_response, request


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
        """为查询结果批量补充回复目标消息内容。

        仅按 reply_id（消息 ID）反查——reply_id 是唯一可靠的反查锚点；
        任一关联缺失或异常仅跳过该条，不阻断整体结果。
        """
        records = result.get("records") or []
        if not records:
            return

        reply_ids = [(rec.get("reply_id") or "").strip() for rec in records]
        reply_ids = [rid for rid in reply_ids if rid]

        reply_map: dict[str, dict] = {}
        if reply_ids:
            for row in await self.mysql_mgr.get_messages_by_ids(reply_ids):
                mid = str(row.get("message_id") or "")
                if mid and mid not in reply_map:
                    reply_map[mid] = row

        for rec in records:
            rid = (rec.get("reply_id") or "").strip()
            rec["reply_message"] = reply_map.get(rid)
