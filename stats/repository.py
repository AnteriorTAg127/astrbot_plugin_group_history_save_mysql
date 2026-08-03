"""MySQL 聚合仓储（v0.5.0 数据分析模块 B）。

对 chat_history / image_records 做纯 SQL 实时聚合，供编排服务 StatsService
（模块 G）组装统计卡片/图表/排行数据。设计约束（接口契约见 开发/v0.5.0/分工.md）：

- 连接获取模式与 db_mysql 保持一致：一律 ``async with self._mgr.pool.acquire()``，
  超时/取消后由 acquire 上下文自动销毁脏连接，本层**不得**自行 close；
- 每条 SELECT 的 execute 外包 ``asyncio.wait_for(..., QUERY_TIMEOUT_SECONDS)`` 兜底
  （默认缓冲游标下 fetch 只读 execute 已取回的结果，网络等待集中在 execute）；
- 超时/SQL 错误一律向上抛，由 service 层统一兜底转友好文案；
- SQL 全部参数化（%s 占位符），group_id/sender_id 为可选过滤（None 不加条件），
  严禁 f-string 拼接用户输入；
- 时间窗口统一半开区间 ``timestamp >= %s AND timestamp < %s``，datetime 传参，
  窗口边界在 bot 侧计算，不依赖 DB 服务器时区。
"""

import asyncio
from datetime import datetime

from ..db_mysql import QUERY_TIMEOUT_SECONDS


class StatsRepository:
    """chat_history / image_records 聚合查询仓储。

    口径总览（各方法明细见其 docstring）：

    - 消息口径：chat_history（text/mixed，纯图片消息不入该表）；
      图片口径：image_records（仅 get_image_window_counts 使用，快照任务专用）
    - 时间窗口一律 [start, end) 半开区间
    - 排序规则：发言人行排行 COUNT(*) DESC、同数 sender_id ASC；
      群排行 COUNT(*) DESC、同数 group_id ASC（保证输出确定性）
    """

    def __init__(self, mysql_mgr):
        """初始化仓储。

        Args:
            mysql_mgr: MySQLManager 实例（db_mysql），仅使用其公开的
                ``pool.acquire()`` 上下文获取连接
        """
        self._mgr = mysql_mgr

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    async def _execute(self, cur, sql, params=None):
        """统一 execute 入口：外包 asyncio.wait_for 超时兜底。

        与 db_mysql.MySQLManager._execute 同语义：超时抛 TimeoutError，
        上抛穿过 acquire 借用体时连接被判定协议状态不可信，归还路径自动
        销毁（可能残留未读完的响应字节），本层无需也无法自行清理。
        """
        return await asyncio.wait_for(
            cur.execute(sql, params), timeout=QUERY_TIMEOUT_SECONDS
        )

    @staticmethod
    def _window_clause(start: datetime, end: datetime, group_id=None, sender_id=None):
        """构造时间窗口 + 可选过滤的 WHERE 子句（全参数化）。

        Args:
            start: 窗口起点（含），datetime 传参
            end: 窗口终点（不含），datetime 传参
            group_id: 群号过滤，None 不加条件
            sender_id: QQ 号过滤，None 不加条件

        Returns:
            tuple[str, list]: (WHERE 子句文本（不含 WHERE 关键字）, 参数列表)
        """
        conditions = ["timestamp >= %s", "timestamp < %s"]
        params: list = [start, end]
        if group_id is not None:
            conditions.append("group_id = %s")
            params.append(group_id)
        if sender_id is not None:
            conditions.append("sender_id = %s")
            params.append(sender_id)
        return " AND ".join(conditions), params

    @staticmethod
    def _format_date(value) -> str:
        """把 DATE() 结果归一化为 YYYY-MM-DD 字符串。

        aiomysql 将 MySQL DATE 列解析为 datetime.date；防御未知驱动行为，
        无 strftime 时退回 str()。
        """
        if hasattr(value, "strftime"):
            return value.strftime("%Y-%m-%d")
        return str(value)

    async def _latest_sender_names(
        self, cur, group_id: str, sender_ids: list[str], start: datetime, end: datetime
    ) -> dict:
        """批量取上榜 sender 在窗口内的最新昵称（MySQL 5.7 兼容）。

        实现：对每个 sender 用关联子查询取「timestamp DESC、id DESC」的
        唯一一行（id 打破同秒并列，保证每 sender 恰一条），避免 MySQL 8.0
        窗口函数依赖。仅查窗口内记录：上榜者窗口内必有消息，子查询恒有结果。

        Args:
            cur: 已打开的游标（复用调用方连接）
            group_id: 群号（与排行查询同口径）
            sender_ids: 上榜 sender_id 列表（非空）
            start: 窗口起点（含）
            end: 窗口终点（不含）

        Returns:
            dict: {sender_id: sender_name}；查不到的 sender 不在其中
        """
        placeholders = ",".join(["%s"] * len(sender_ids))
        sql = (
            "SELECT t.sender_id, t.sender_name FROM chat_history AS t "
            f"WHERE t.group_id = %s AND t.timestamp >= %s AND t.timestamp < %s "
            f"AND t.sender_id IN ({placeholders}) "
            "AND t.id = ("
            "SELECT t2.id FROM chat_history AS t2 "
            "WHERE t2.group_id = t.group_id AND t2.sender_id = t.sender_id "
            "AND t2.timestamp >= %s AND t2.timestamp < %s "
            "ORDER BY t2.timestamp DESC, t2.id DESC LIMIT 1)"
        )
        params = [group_id, start, end, *sender_ids, start, end]
        await self._execute(cur, sql, params)
        rows = await cur.fetchall()
        return {str(row[0]): str(row[1] or "") for row in rows}

    # ------------------------------------------------------------------
    # 概览 / 分布 / 趋势
    # ------------------------------------------------------------------

    async def get_overview(
        self, group_id: str | None, start: datetime, end: datetime
    ) -> dict:
        """窗口总览：总消息数、活跃成员数、首末条消息时间。

        口径：COUNT(*) 计 chat_history 全量消息（text/mixed）；
        COUNT(DISTINCT sender_id) 计去重发言者；MIN/MAX(timestamp) 取窗口内
        首末条时刻（窗口无数据时为 None）。

        Args:
            group_id: 群号过滤；None = 全部群汇总
            start: 窗口起点（含）
            end: 窗口终点（不含）

        Returns:
            dict: {"total": int, "active_senders": int,
                   "first": datetime|None, "last": datetime|None}
        """
        where, params = self._window_clause(start, end, group_id=group_id)
        sql = (
            "SELECT COUNT(*), COUNT(DISTINCT sender_id), "
            "MIN(timestamp), MAX(timestamp) "
            f"FROM chat_history WHERE {where}"
        )
        async with self._mgr.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await self._execute(cur, sql, params)
                row = await cur.fetchone()
        if not row:
            return {"total": 0, "active_senders": 0, "first": None, "last": None}
        return {
            "total": int(row[0] or 0),
            "active_senders": int(row[1] or 0),
            "first": row[2],
            "last": row[3],
        }

    async def get_hourly_dist(
        self,
        group_id: str | None,
        sender_id: str | None,
        start: datetime,
        end: datetime,
    ) -> list[int]:
        """窗口内 24 小时发言分布。

        口径：HOUR(timestamp) 分组计数，归一为固定 24 项 list（索引=小时 0–23），
        无数据的小时补 0；全窗口无数据返回 24 个 0。

        Args:
            group_id: 群号过滤；None = 全部群汇总
            sender_id: QQ 号过滤；None = 不限定个人
            start: 窗口起点（含）
            end: 窗口终点（不含）

        Returns:
            list[int]: 24 项，索引 i 为 i 时的消息数
        """
        where, params = self._window_clause(
            start, end, group_id=group_id, sender_id=sender_id
        )
        sql = (
            "SELECT HOUR(timestamp), COUNT(*) "
            f"FROM chat_history WHERE {where} GROUP BY HOUR(timestamp)"
        )
        dist = [0] * 24
        async with self._mgr.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await self._execute(cur, sql, params)
                rows = await cur.fetchall()
        for row in rows:
            hour = int(row[0])
            if 0 <= hour <= 23:
                dist[hour] = int(row[1] or 0)
        return dist

    async def get_weekday_dist(
        self,
        group_id: str | None,
        sender_id: str | None,
        start: datetime,
        end: datetime,
    ) -> list[int]:
        """窗口内星期发言分布。

        口径：WEEKDAY(timestamp) 分组计数（MySQL WEEKDAY 0=周一 … 6=周日，
        与 profile StatsBuilder 的周一=0 锚定天然一致），归一为固定 7 项 list，
        无数据的星期补 0。

        Args:
            group_id: 群号过滤；None = 全部群汇总
            sender_id: QQ 号过滤；None = 不限定个人
            start: 窗口起点（含）
            end: 窗口终点（不含）

        Returns:
            list[int]: 7 项，索引 0=周一 … 6=周日
        """
        where, params = self._window_clause(
            start, end, group_id=group_id, sender_id=sender_id
        )
        sql = (
            "SELECT WEEKDAY(timestamp), COUNT(*) "
            f"FROM chat_history WHERE {where} GROUP BY WEEKDAY(timestamp)"
        )
        dist = [0] * 7
        async with self._mgr.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await self._execute(cur, sql, params)
                rows = await cur.fetchall()
        for row in rows:
            weekday = int(row[0])
            if 0 <= weekday <= 6:
                dist[weekday] = int(row[1] or 0)
        return dist

    async def get_daily_trend(
        self, group_id: str | None, start: datetime, end: datetime
    ) -> list[tuple[str, int]]:
        """窗口内每日消息量趋势。

        口径：DATE(timestamp) 分组计数，按日期升序返回；**不补零**——
        无数据的日期不出现在结果中，连续补零由 service 层完成
        （start 所在日 ~ end 前一日）。

        Args:
            group_id: 群号过滤；None = 全部群汇总
            start: 窗口起点（含）
            end: 窗口终点（不含）

        Returns:
            list[tuple[str, int]]: [("YYYY-MM-DD", count), …]，日期 ASC
        """
        where, params = self._window_clause(start, end, group_id=group_id)
        sql = (
            "SELECT DATE(timestamp), COUNT(*) "
            f"FROM chat_history WHERE {where} "
            "GROUP BY DATE(timestamp) ORDER BY DATE(timestamp) ASC"
        )
        async with self._mgr.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await self._execute(cur, sql, params)
                rows = await cur.fetchall()
        return [(self._format_date(row[0]), int(row[1] or 0)) for row in rows]

    # ------------------------------------------------------------------
    # 排行 / 个人概览
    # ------------------------------------------------------------------

    async def get_sender_ranking(
        self, group_id: str, start: datetime, end: datetime, limit: int
    ) -> list[dict]:
        """指定群的窗口内发言人排行。

        口径：窗口内 chat_history 按 sender_id 分组计数；
        排序 COUNT(*) DESC，同数按 sender_id ASC（输出确定性），LIMIT limit 截断；
        sender_name 取该 sender **窗口内最新一条**记录的昵称——排行查完后对上榜
        sender_id 发一条 IN 查询（关联子查询逐人取 timestamp DESC、id DESC 首行，
        MySQL 5.7 兼容，不用窗口函数）。

        Args:
            group_id: 群号（必填，排行只在单群内统计）
            start: 窗口起点（含）
            end: 窗口终点（不含）
            limit: 排行条数上限（调用方已夹住 1–50）

        Returns:
            list[dict]: [{"sender_id": str, "sender_name": str, "count": int}]，
            按排行顺序；昵称为空串兜底
        """
        where, params = self._window_clause(start, end, group_id=group_id)
        sql = (
            "SELECT sender_id, COUNT(*) AS cnt "
            f"FROM chat_history WHERE {where} "
            "GROUP BY sender_id ORDER BY cnt DESC, sender_id ASC LIMIT %s"
        )
        params = [*params, limit]
        async with self._mgr.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await self._execute(cur, sql, params)
                rows = await cur.fetchall()
                if not rows:
                    return []
                sender_ids = [str(row[0]) for row in rows]
                names = await self._latest_sender_names(
                    cur, group_id, sender_ids, start, end
                )
        return [
            {
                "sender_id": str(row[0]),
                "sender_name": names.get(str(row[0]), ""),
                "count": int(row[1] or 0),
            }
            for row in rows
        ]

    async def get_group_ranking(
        self, start: datetime, end: datetime, limit: int
    ) -> list[dict]:
        """窗口内群消息量排行（全部群视图专用）。

        口径：chat_history 按 group_id 分组，COUNT(*) 计消息数、
        COUNT(DISTINCT sender_id) 计活跃成员数；排序 COUNT(*) DESC，
        同数按 group_id ASC（输出确定性），LIMIT limit 截断。
        只统计窗口内实际有消息的群（未启用存储/无数据的群自然不出现）。

        Args:
            start: 窗口起点（含）
            end: 窗口终点（不含）
            limit: 排行条数上限

        Returns:
            list[dict]: [{"group_id": str, "count": int, "active_senders": int}]
        """
        sql = (
            "SELECT group_id, COUNT(*) AS cnt, COUNT(DISTINCT sender_id) "
            "FROM chat_history WHERE timestamp >= %s AND timestamp < %s "
            "GROUP BY group_id ORDER BY cnt DESC, group_id ASC LIMIT %s"
        )
        async with self._mgr.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await self._execute(cur, sql, [start, end, limit])
                rows = await cur.fetchall()
        return [
            {
                "group_id": str(row[0]),
                "count": int(row[1] or 0),
                "active_senders": int(row[2] or 0),
            }
            for row in rows
        ]

    async def get_member_overview(
        self,
        group_id: str | None,
        sender_id: str,
        start: datetime,
        end: datetime,
    ) -> dict:
        """个人窗口概览：消息数、活跃天数、最新昵称。

        口径：count = 窗口内该成员消息数；active_days = COUNT(DISTINCT
        DATE(timestamp))，即有发言的自然日数；name = 窗口内该成员最新一条
        记录的 sender_name（timestamp DESC、id DESC 首行，无记录时空串）。

        Args:
            group_id: 群号过滤；None = 该成员跨群合计
            sender_id: 目标成员 QQ 号
            start: 窗口起点（含）
            end: 窗口终点（不含）

        Returns:
            dict: {"count": int, "active_days": int, "name": str}
        """
        where, params = self._window_clause(
            start, end, group_id=group_id, sender_id=sender_id
        )
        sql_count = (
            "SELECT COUNT(*), COUNT(DISTINCT DATE(timestamp)) "
            f"FROM chat_history WHERE {where}"
        )
        sql_name = (
            "SELECT sender_name "
            f"FROM chat_history WHERE {where} "
            "ORDER BY timestamp DESC, id DESC LIMIT 1"
        )
        async with self._mgr.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await self._execute(cur, sql_count, params)
                row = await cur.fetchone()
                count = int(row[0] or 0) if row else 0
                active_days = int(row[1] or 0) if row else 0
                await self._execute(cur, sql_name, params)
                name_row = await cur.fetchone()
        name = str(name_row[0] or "") if name_row else ""
        return {"count": count, "active_days": active_days, "name": name}

    # ------------------------------------------------------------------
    # 图片窗口聚合（快照任务专用）
    # ------------------------------------------------------------------

    async def get_image_window_counts(
        self, start: datetime, end: datetime, top_k: int
    ) -> dict:
        """聚合 image_records 窗口 [start, end) 的图片数（快照任务/强制刷新专用）。

        口径：SQL 侧按 (group_id, sender_id) 分组计数（一条聚合 SQL），
        Python 侧汇总群总量并对每群按 count 降序、同数 sender_id ASC 截断
        前 top_k（避免复杂窗口函数，MySQL 5.7 兼容）；top_k 内 sender 的昵称
        再发一条 IN 查询取各人窗口内最新一条 image_records 的 sender_name
        （关联子查询 timestamp DESC、id DESC 首行，跨群取最新，同人在多群
        条目共用同一昵称）。

        注意：groups 的群总量是**截断前**的全量求和，不受 top_k 影响；
        senders 仅 Top K 内准确（与 PRD F4 快照口径一致）。

        Args:
            start: 窗口起点（含）
            end: 窗口终点（不含）
            top_k: 每群保留的个人条数上限（调用方已夹住合法范围）

        Returns:
            dict: {
                "groups": {group_id: count},
                "senders": {group_id: [(sender_id, sender_name, count), …]}
            }
        """
        sql = (
            "SELECT group_id, sender_id, COUNT(*) "
            "FROM image_records WHERE timestamp >= %s AND timestamp < %s "
            "GROUP BY group_id, sender_id"
        )
        async with self._mgr.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await self._execute(cur, sql, [start, end])
                rows = await cur.fetchall()
                if not rows:
                    return {"groups": {}, "senders": {}}

                # Python 侧分组：群总量（截断前全量累加）+ 每群个人明细
                groups: dict = {}
                per_group: dict = {}
                for row in rows:
                    gid = str(row[0])
                    sid = str(row[1])
                    cnt = int(row[2] or 0)
                    groups[gid] = groups.get(gid, 0) + cnt
                    per_group.setdefault(gid, []).append((sid, cnt))

                # 每群 count 降序（同数 sender_id ASC 保确定性）截断前 top_k
                senders: dict = {}
                for gid, entries in per_group.items():
                    entries.sort(key=lambda item: (-item[1], item[0]))
                    senders[gid] = entries[: max(0, int(top_k))]

                # 上榜 sender 最新昵称（去重后一条 IN 查询）
                top_ids = sorted(
                    {sid for entries in senders.values() for sid, _ in entries}
                )
                if top_ids:
                    names = await self._latest_image_sender_names(
                        cur, top_ids, start, end
                    )
                else:
                    names = {}
        return {
            "groups": groups,
            "senders": {
                gid: [(sid, names.get(sid, ""), cnt) for sid, cnt in entries]
                for gid, entries in senders.items()
            },
        }

    async def _latest_image_sender_names(
        self, cur, sender_ids: list[str], start: datetime, end: datetime
    ) -> dict:
        """批量取上榜 sender 在 image_records 窗口内的最新昵称（5.7 兼容）。

        与 _latest_sender_names 同构：关联子查询取「timestamp DESC、id DESC」
        唯一一行；不区分群（同人跨群条目共用最新昵称）。

        Args:
            cur: 已打开的游标（复用调用方连接）
            sender_ids: 上榜 sender_id 列表（非空）
            start: 窗口起点（含）
            end: 窗口终点（不含）

        Returns:
            dict: {sender_id: sender_name}；查不到的 sender 不在其中
        """
        placeholders = ",".join(["%s"] * len(sender_ids))
        sql = (
            "SELECT t.sender_id, t.sender_name FROM image_records AS t "
            "WHERE t.timestamp >= %s AND t.timestamp < %s "
            f"AND t.sender_id IN ({placeholders}) "
            "AND t.id = ("
            "SELECT t2.id FROM image_records AS t2 "
            "WHERE t2.sender_id = t.sender_id "
            "AND t2.timestamp >= %s AND t2.timestamp < %s "
            "ORDER BY t2.timestamp DESC, t2.id DESC LIMIT 1)"
        )
        params = [start, end, *sender_ids, start, end]
        await self._execute(cur, sql, params)
        rows = await cur.fetchall()
        return {str(row[0]): str(row[1] or "") for row in rows}
