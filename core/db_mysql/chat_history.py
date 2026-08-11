"""聊天记录读写（ChatHistoryMixin）。

自根目录 db_mysql.py 拆分：insert_chat_message / query_messages /
get_messages_by_ids 逐字迁移；新增 get_existing_message_ids
（供 v0.6.0 重载自动补库按 message_id 去重）。
"""

import asyncio
from datetime import datetime

import aiomysql

from astrbot.api import logger


class ChatHistoryMixin:
    """聊天记录读写 Mixin：插入、条件查询、按 ID 批量查询、群内已存在 ID 查重。"""

    async def insert_chat_message(
        self,
        group_id: str,
        sender_id: str,
        sender_name: str,
        message_type: str,
        content: str,
        message_id: str,
        at_list: str = "",
        reply_id: str = "",
        timestamp: datetime | None = None,
    ) -> bool:
        """插入一条聊天记录。

        Args:
            group_id: 群号（字符串形式）
            sender_id: 发送者 QQ 号（字符串形式）
            sender_name: 发送者昵称
            message_type: 消息类型（text/mixed，纯图片消息不入文本表）
            content: 文本内容
            message_id: 消息 ID
            at_list: 本条消息 @ 的 QQ 号列表，英文逗号分隔（如 "123,456"）；无则空串
            reply_id: 本条消息回复的目标消息 message_id；无则空串
            timestamp: 消息时间戳；None 取当前时间（实时消息现行为），
                补录路径传入消息原始到达时刻（F11，契约 K1）

        Returns:
            bool: 是否插入成功
        """
        try:
            # TEXT 列上限 65535 字节：超长内容截断并留标记，
            # 避免严格 sql_mode 下整条插入失败导致消息彻底丢失
            if content and len(content.encode("utf-8")) > 60000:
                content = (
                    content.encode("utf-8")[:60000].decode("utf-8", "ignore")
                    + "\n…[内容过长已截断]"
                )
            # F11：补录路径透传消息到达时刻；实时路径缺省取当前时间
            if timestamp is None:
                timestamp = datetime.now()
            async with self.pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await self._execute(
                        cur,
                        """INSERT INTO chat_history
                           (timestamp, group_id, sender_id, sender_name, message_type, content, message_id, at_list, reply_id)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                        (
                            timestamp,
                            group_id,
                            sender_id,
                            sender_name,
                            message_type,
                            content,
                            message_id,
                            at_list,
                            reply_id,
                        ),
                    )
            return True
        except Exception as e:
            logger.error(f"[HistorySave] 插入聊天记录失败: {e}")
            return False

    async def query_messages(
        self,
        group_id: str | None = None,
        sender_id: str | None = None,
        time_start: str | None = None,
        time_end: str | None = None,
        keyword: str | None = None,
        page: int = 1,
        page_size: int = 50,
        raise_on_error: bool = False,
    ) -> dict:
        """查询聊天记录（支持多条件过滤和分页）。

        Args:
            group_id: 群号过滤（可选，字符串形式）
            sender_id: QQ 号过滤（可选，字符串形式）
            time_start: 开始时间（格式 YYYY-MM-DD HH:MM:SS）
            time_end: 结束时间
            keyword: 关键词，模糊匹配 content 与 sender_name（可选）
            page: 页码（从 1 开始）
            page_size: 每页条数
            raise_on_error: True 时查询异常直接向上抛出（v0.7.0 供对外
                公共 API 精确感知失败并写失败日志）；默认 False 行为
                不变（异常记日志后返回空结果）

        Returns:
            dict: {"total": int, "records": list[dict]}
        """
        result = {"total": 0, "records": []}
        try:
            conditions = []
            params = []

            if group_id:
                conditions.append("group_id = %s")
                params.append(group_id)
            if sender_id:
                conditions.append("sender_id = %s")
                params.append(sender_id)
            if time_start:
                conditions.append("timestamp >= %s")
                params.append(time_start)
            if time_end:
                conditions.append("timestamp <= %s")
                params.append(time_end)
            if keyword:
                # 转义 LIKE 通配符，避免用户输入的 % _ 干扰匹配
                kw = (
                    keyword.replace("\\", "\\\\")
                    .replace("%", "\\%")
                    .replace("_", "\\_")
                )
                like = f"%{kw}%"
                conditions.append("(content LIKE %s OR sender_name LIKE %s)")
                params.extend([like, like])

            where_clause = " AND ".join(conditions) if conditions else "1=1"
            offset = (page - 1) * page_size

            async with self.pool.acquire() as conn:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    # 查询总数
                    await self._execute(
                        cur,
                        f"SELECT COUNT(*) as total FROM chat_history WHERE {where_clause}",
                        params,
                    )
                    row = await cur.fetchone()
                    result["total"] = row["total"] if row else 0

                    # 查询数据
                    # at_list/reply_id 为 v0.4.0 新增列，迁移前的存量行可能为 NULL，
                    # 保持原样返回，下游按 .get 兜底
                    await self._execute(
                        cur,
                        f"""SELECT id, timestamp, group_id, sender_id, sender_name,
                                   message_type, content, message_id, at_list, reply_id
                            FROM chat_history WHERE {where_clause}
                            ORDER BY timestamp DESC
                            LIMIT %s OFFSET %s""",
                        params + [page_size, offset],
                    )
                    rows = await cur.fetchall()
                    for row in rows:
                        row["timestamp"] = str(row["timestamp"])
                    result["records"] = rows
        except Exception as e:
            if raise_on_error:
                # v0.7.0：对外公共 API 需要精确感知失败（写失败日志），
                # 由调用方负责捕获；内部模块调用默认 False 行为不变
                raise
            self._log_op_error("query_messages", "查询聊天记录", e)
        return result

    async def get_messages_by_ids(self, message_ids: list[str]) -> list[dict]:
        """按 message_id 批量精确查询 chat_history（关系分析反查被回复者用）。

        空入参或全为空串 → 直接返回 []；入参中的空串先过滤，
        IN 占位符按过滤后的列表动态生成（全参数化，无拼接注入风险）。

        Args:
            message_ids: 待查询的 message_id 列表

        Returns:
            list[dict]: 命中记录列表，每条含 timestamp(str)/group_id/sender_id/
                sender_name/message_type/content/message_id/at_list/reply_id；
                空入参或查询异常时返回 []
        """
        ids = [mid for mid in (message_ids or []) if mid]
        if not ids:
            return []
        try:
            placeholders = ",".join(["%s"] * len(ids))
            async with self.pool.acquire() as conn:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await self._execute(
                        cur,
                        f"""SELECT timestamp, group_id, sender_id, sender_name,
                                   message_type, content, message_id, at_list, reply_id
                            FROM chat_history
                            WHERE message_id IN ({placeholders})""",
                        ids,
                    )
                    rows = await cur.fetchall()
                    for row in rows:
                        row["timestamp"] = str(row["timestamp"])
                    return rows
        except Exception as e:
            self._log_op_error("get_messages_by_ids", "按ID批量查询聊天记录", e)
            return []

    async def get_existing_message_ids(
        self, group_id: str, message_ids: list[str]
    ) -> set[str]:
        """按群批量查询已存在的 message_id 集合（重载补库去重用）。

        仅返回命中 chat_history.message_id 非空的 id；message_ids 为空返回空集；
        IN 列表分块（每块 ≤500）避免超长 SQL；全部参数化绑定；
        异常记 error 日志后返回空集（让上层按"全不存在"处理，不阻断补库）。
        """
        ids = [mid for mid in (message_ids or []) if mid]
        if not ids:
            return set()
        existing: set[str] = set()
        chunk_size = 500
        try:
            async with self.pool.acquire() as conn:
                async with conn.cursor() as cur:
                    # IN 列表分块查询：每块 ≤ 500，避免超长 SQL 与参数占位符过多
                    for i in range(0, len(ids), chunk_size):
                        chunk = ids[i : i + chunk_size]
                        placeholders = ",".join(["%s"] * len(chunk))
                        await self._execute(
                            cur,
                            f"""SELECT message_id FROM chat_history
                                WHERE group_id = %s AND message_id IN ({placeholders})""",
                            [group_id] + chunk,
                        )
                        rows = await cur.fetchall()
                        for row in rows:
                            if row and row[0]:
                                existing.add(row[0])
            return existing
        except Exception as e:
            self._log_op_error("get_existing_message_ids", "查询群内已存在消息ID", e)
            return set()

    async def get_all_group_ids(self) -> list[str]:
        """全量群清单（v0.6.0）：chat_history 中实际有数据的群（group_id 去重）。

        all_mode 全局记录模式下白名单无意义（录制覆盖所有群），补库群清单
        改以「有数据的群」为准——无数据的群拉协议端也是空，跳过即可。

        Returns:
            list[str]: 字符串形式 group_id 列表；异常记日志返回 []（由调用方降级）
        """
        try:
            async with self.pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await self._execute(
                        cur, "SELECT DISTINCT group_id FROM chat_history"
                    )
                    rows = await cur.fetchall()
                    return [str(row[0]) for row in rows if row and row[0] is not None]
        except Exception as e:
            self._log_op_error("get_all_group_ids", "查询有数据的群清单", e)
            return []

    async def get_recent_messages(self, group_id: str, limit: int) -> list[dict]:
        """按群取最近 limit 条已记录消息的 message_id 与 content（补库重叠边界检测用）。

        按 timestamp DESC（id DESC 兜底）取最新记录；content 供无 message_id
        的消息做内容比对兜底。补库路径会向表内插入原始时间戳早于实时记录的消息，
        自增 id 与 timestamp 不再单调同向，按 id DESC 取出的「最近 N 条」会漏掉
        真正最新的实时记录导致重叠检测失效；改以 timestamp 为第一排序键，与时间
        语义对齐（命中 idx_group_time 索引，无额外代价）。
        limit <= 0 返回空列表；异常记 error 日志后返回空列表（让上层按
        「无重叠参照」处理，退化为窗口/轮数停止，不阻断补库）。
        """
        if limit <= 0:
            return []
        try:
            async with self.pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await self._execute(
                        cur,
                        """SELECT message_id, content FROM chat_history
                           WHERE group_id = %s
                           ORDER BY timestamp DESC, id DESC LIMIT %s""",
                        [group_id, limit],
                    )
                    rows = await cur.fetchall()
                    return [
                        {
                            "message_id": str(row[0]) if row[0] is not None else "",
                            "content": str(row[1]) if row[1] is not None else "",
                        }
                        for row in rows
                    ]
        except Exception as e:
            self._log_op_error("get_recent_messages", "查询最近消息重叠参照", e)
            return []

    async def count_messages(
        self,
        group_id: str | None = None,
        sender_ids: list[str] | None = None,
        time_start: str | None = None,
        time_end: str | None = None,
    ) -> dict:
        """文本消息计数（v0.7.0 对外统计接口数据层，批量 + 高并发优化）。

        群总计（COUNT）+ 批量群内（GROUP BY sender_id）+ 批量跨群
        （GROUP BY sender_id）三组计数并行执行；批量聚合为**单条 SQL**
        （禁止 N+1 循环查询），IN 列表 ≤500 单块（对齐 get_existing_message_ids
        范式），全参数化绑定；时间条件同 query_messages 范式。
        索引利用：群总计走 idx_group_time、群内走 idx_group_sender_time、
        跨群走 idx_sender_time。

        注意：本方法异常**直接向上抛**（不吞）——仅对外公共 API
        （public_api.count_messages）调用，由其统一捕获写失败日志。

        Args:
            group_id: 限定单群（可选，字符串形式）
            sender_ids: 批量 QQ 号（可选，≤500；调用方已做上限校验）
            time_start: 开始时间（格式 YYYY-MM-DD HH:MM:SS，闭区间）
            time_end: 结束时间

        Returns:
            dict: {
                "group_total": int,      # group_id 给定时该群文本消息总数
                "senders": {             # sender_ids 给定时每人计数
                    sid: {"in_group": int, "total": int},
                    # in_group：该人在群内发言数（group_id 也给定时才有）；
                    # total：该人跨群总发言数
                },
            }
        """
        # 时间条件（公共 WHERE 片段，参数化）
        time_conds = []
        time_params: list = []
        if time_start:
            time_conds.append("timestamp >= %s")
            time_params.append(time_start)
        if time_end:
            time_conds.append("timestamp <= %s")
            time_params.append(time_end)
        time_where = " AND ".join(time_conds)
        time_clause = f" AND {time_where}" if time_where else ""

        # IN 占位符（sender_ids ≤500 单块；防御性分块对齐既有范式）
        ids = [sid for sid in (sender_ids or []) if sid]
        in_placeholders = ",".join(["%s"] * len(ids)) if ids else ""

        async def _count_group_total() -> int:
            """群总计：COUNT 走 idx_group_time。"""
            sql = f"SELECT COUNT(*) FROM chat_history WHERE group_id = %s{time_clause}"
            params = [group_id] + time_params
            async with self.pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await self._execute(cur, sql, params)
                    row = await cur.fetchone()
                    # 防御性 int 转换：不同驱动/游标类型可能返回 int/Decimal/str
                    return int(row[0]) if row and row[0] is not None else 0

        async def _count_senders_in_group() -> dict[str, int]:
            """批量群内计数：GROUP BY 单条 SQL，走 idx_group_sender_time。"""
            result: dict[str, int] = {}
            sql = (
                "SELECT sender_id, COUNT(*) FROM chat_history"
                f" WHERE group_id = %s AND sender_id IN ({in_placeholders}){time_clause}"
                " GROUP BY sender_id"
            )
            params = [group_id] + ids + time_params
            async with self.pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await self._execute(cur, sql, params)
                    for row in await cur.fetchall():
                        result[str(row[0])] = int(row[1])
            return result

        async def _count_senders_total() -> dict[str, int]:
            """批量跨群计数：GROUP BY 单条 SQL，走 idx_sender_time。"""
            result: dict[str, int] = {}
            sql = (
                "SELECT sender_id, COUNT(*) FROM chat_history"
                f" WHERE sender_id IN ({in_placeholders}){time_clause}"
                " GROUP BY sender_id"
            )
            params = ids + time_params
            async with self.pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await self._execute(cur, sql, params)
                    for row in await cur.fetchall():
                        result[str(row[0])] = int(row[1])
            return result

        # 三组计数并行执行（高并发下减少串行等待）
        tasks = []
        if group_id:
            tasks.append(_count_group_total())
        if ids:
            tasks.append(_count_senders_in_group())
            tasks.append(_count_senders_total())
        results = await asyncio.gather(*tasks) if tasks else []

        out: dict = {}
        if group_id:
            out["group_total"] = results.pop(0)
        if ids:
            in_group_map = results.pop(0)
            total_map = results.pop(0)
            out["senders"] = {
                sid: {
                    "in_group": in_group_map.get(sid, 0),
                    "total": total_map.get(sid, 0),
                }
                for sid in ids
            }
        return out
