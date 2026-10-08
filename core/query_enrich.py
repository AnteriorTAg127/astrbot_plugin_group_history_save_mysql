"""查询回复关联补充共享实现（v0.8.2 R2 自双胞胎合并）。

v0.8.2 前本逻辑存在两份逐行同构的 ``_enrich_query_reply``（core/public_api.py
与 core/webapi/query.py 各一份），存在口径分叉风险；本版统一收敛为本模块
唯一共享纯函数，两边薄调用（对外函数名/方法名保留做兼容）。

依赖方向约束：本模块不导入 db 层（``core.db_mysql``）——反查通过调用方传入
的 ``get_by_ids`` 回调完成（通常为 ``mgr.get_messages_by_ids``），保持纯函数、
数据访问无关，可独立测试。
"""

from collections.abc import Awaitable, Callable


async def enrich_query_reply(
    records: list[dict],
    get_by_ids: Callable[[list[str]], Awaitable[list[dict]]],
) -> None:
    """为查询结果批量补充回复目标消息（原地回填每条 rec 的 reply_message）。

    仅按 reply_id（消息 ID）反查——reply_id 是唯一可靠的反查锚点；经
    ``get_by_ids`` 回调批量反查后回填 reply_message（取不到为 None）。

    口径（与合并前的两份实现逐行一致）：
    - 空/纯空白 reply_id 跳过反查（该条 reply_message 回填 None）；
    - 反查结果 message_id 重复时保留第一条（message_id 在 chat_history 中
      应唯一，若底层异常返回重复行，与「取不到为 None」的降级口径一致）；
    - 任一关联缺失或反查异常仅影响该条，不阻断整体结果（数据层
      get_messages_by_ids 的防御契约：异常自行降级为空列表）。

    Args:
        records: 查询结果记录列表（原地修改，每条回填 reply_message 键）
        get_by_ids: 按 message_id 批量反查的异步回调，签名
            ``(list[str]) -> Awaitable[list[dict]]``，由调用方传入
            （如 ``mgr.get_messages_by_ids``）
    """
    if not records:
        return

    reply_ids = [(rec.get("reply_id") or "").strip() for rec in records]
    reply_ids = [rid for rid in reply_ids if rid]

    reply_map: dict[str, dict] = {}
    if reply_ids:
        for row in await get_by_ids(reply_ids):
            mid = str(row.get("message_id") or "")
            if mid and mid not in reply_map:
                reply_map[mid] = row

    for rec in records:
        rid = (rec.get("reply_id") or "").strip()
        rec["reply_message"] = reply_map.get(rid)
