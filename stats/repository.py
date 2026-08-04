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
      图片口径：image_records（快照任务/批量聚合专用）
    - 时间窗口一律 [start, end) 半开区间
    - 排序规则：发言人行排行 COUNT(*) DESC、同数 sender_id ASC；
      群排行 COUNT(*) DESC、同数 group_id ASC（保证输出确定性）
    - v0.5.5 批量聚合（get_hourly_batch / get_daily_batch /
      get_monthly_batch）经 source 白名单 _SOURCE_TABLES
      {"msg": chat_history, "image": image_records} 映射表名，非法值抛 ValueError
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

        实现：派生表按 sender 聚出窗口内 MAX(id)（auto_increment 单调，
        与写入顺序一致，即窗口内最后一条），再按 id 等值回表取昵称——
        一条 SQL 完成全部 sender。旧版用「外层每行执行一次相关子查询」
        取 timestamp DESC 首行，慢日志实测扫描行数被放大到 434 万行、
        单条 27 秒；派生表只物化一次窗口行，扫描量降回窗口大小。

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
            "JOIN ("
            "SELECT t2.sender_id, MAX(t2.id) AS max_id FROM chat_history AS t2 "
            "WHERE t2.group_id = %s AND t2.timestamp >= %s AND t2.timestamp < %s "
            f"AND t2.sender_id IN ({placeholders}) "
            "GROUP BY t2.sender_id"
            ") AS m ON m.sender_id = t.sender_id AND m.max_id = t.id "
            "WHERE t.group_id = %s AND t.timestamp >= %s AND t.timestamp < %s"
        )
        params = [group_id, start, end, *sender_ids, group_id, start, end]
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

    async def get_all_groups_summary(self) -> list[dict]:
        """全量群清单（v0.5.1）：chat_history 中实际有数据的群。

        all_mode（全局记录模式）下白名单表为空，群下拉/推送开关列表失去
        数据源，改以「有数据的群」为准。不限时间窗口、不截断（群数量级
        有限，走 idx_group_time 索引分组扫描）。

        口径：按 group_id 分组，COUNT(*) 计消息数、MAX(timestamp) 记最近
        活跃时刻；排序 COUNT(*) DESC、同数 group_id ASC（输出确定性）。

        Returns:
            list[dict]: [{"group_id": str, "count": int,
                "last_active": datetime | None}]
        """
        sql = (
            "SELECT group_id, COUNT(*) AS cnt, MAX(timestamp) "
            "FROM chat_history GROUP BY group_id "
            "ORDER BY cnt DESC, group_id ASC"
        )
        async with self._mgr.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await self._execute(cur, sql)
                rows = await cur.fetchall()
        return [
            {
                "group_id": str(row[0]),
                "count": int(row[1] or 0),
                "last_active": row[2],
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

        与 _latest_sender_names 同构：派生表取各 sender 窗口内 MAX(id) 再回表；
        不区分群（同人跨群条目共用最新昵称）。旧版相关子查询写法同病——
        外层逐行重跑内层子查询，本方法一并改为派生表批量取回。

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
            "JOIN ("
            "SELECT t2.sender_id, MAX(t2.id) AS max_id FROM image_records AS t2 "
            "WHERE t2.timestamp >= %s AND t2.timestamp < %s "
            f"AND t2.sender_id IN ({placeholders}) "
            "GROUP BY t2.sender_id"
            ") AS m ON m.sender_id = t.sender_id AND m.max_id = t.id "
            "WHERE t.timestamp >= %s AND t.timestamp < %s"
        )
        params = [start, end, *sender_ids, start, end]
        await self._execute(cur, sql, params)
        rows = await cur.fetchall()
        return {str(row[0]): str(row[1] or "") for row in rows}

    # ------------------------------------------------------------------
    # v0.5.5 分段快照体系：消息窗口聚合（快照 msg 源）
    # ------------------------------------------------------------------

    async def get_msg_window_counts(
        self, start: datetime, end: datetime, top_k: int
    ) -> dict:
        """聚合 chat_history 窗口 [start, end) 的消息数（消息快照任务/强制刷新专用）。

        chat_history 版 get_image_window_counts，结构与口径完全镜像：
        SQL 侧按 (group_id, sender_id) 分组计数（一条聚合 SQL），
        Python 侧汇总群总量并对每群按 count 降序、同数 sender_id ASC 截断
        前 top_k（不用窗口函数，MySQL 5.7 兼容）；top_k 内 sender 的昵称
        再发一条 IN 查询取各人窗口内最新一条 chat_history 的 sender_name
        （派生表 MAX(id) 等值回表，跨群汇总去重后取最新，同人在多群
        条目共用同一昵称）。

        注意：groups 的群总量是**截断前**的全量求和，不受 top_k 影响；
        senders 仅 Top K 内准确（与 get_image_window_counts 口径一致）。

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
            "FROM chat_history WHERE timestamp >= %s AND timestamp < %s "
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

                # 上榜 sender 最新昵称（跨群汇总去重后一条 IN 查询）
                top_ids = sorted(
                    {sid for entries in senders.values() for sid, _ in entries}
                )
                if top_ids:
                    names = await self._latest_msg_sender_names(
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

    async def _latest_msg_sender_names(
        self, cur, sender_ids: list[str], start: datetime, end: datetime
    ) -> dict:
        """批量取上榜 sender 在 chat_history 窗口内的最新昵称（5.7 兼容）。

        与 _latest_image_sender_names 同构：派生表取各 sender 窗口内 MAX(id)
        （auto_increment 单调，即窗口内最后一条）再等值回表；不区分群
        （同人跨群条目共用最新昵称），供 get_msg_window_counts 跨群场景
        汇总去重后批量取回。

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
            "SELECT t.sender_id, t.sender_name FROM chat_history AS t "
            "JOIN ("
            "SELECT t2.sender_id, MAX(t2.id) AS max_id FROM chat_history AS t2 "
            "WHERE t2.timestamp >= %s AND t2.timestamp < %s "
            f"AND t2.sender_id IN ({placeholders}) "
            "GROUP BY t2.sender_id"
            ") AS m ON m.sender_id = t.sender_id AND m.max_id = t.id "
            "WHERE t.timestamp >= %s AND t.timestamp < %s"
        )
        params = [start, end, *sender_ids, start, end]
        await self._execute(cur, sql, params)
        rows = await cur.fetchall()
        return {str(row[0]): str(row[1] or "") for row in rows}

    # ------------------------------------------------------------------
    # v0.5.5 分段快照体系：批量聚合（F4.1 启动窗口重聚合/回填专用）
    # ------------------------------------------------------------------

    # source 白名单 → 表名映射（get_hourly_batch / get_daily_batch /
    # get_monthly_batch 共用）；表名只来自该常量，不接受用户输入
    _SOURCE_TABLES = {"msg": "chat_history", "image": "image_records"}

    @classmethod
    def _resolve_source_table(cls, source: str) -> str:
        """把 source 白名单键映射为表名，非法值抛 ValueError。

        Args:
            source: 数据源键，"msg"（chat_history）或 "image"（image_records）

        Returns:
            str: 对应表名

        Raises:
            ValueError: source 不在白名单内
        """
        table = cls._SOURCE_TABLES.get(source)
        if table is None:
            raise ValueError(f"unknown source: {source!r} (expect 'msg' or 'image')")
        return table

    async def get_hourly_batch(
        self, source: str, start: datetime, end: datetime, top_k: int
    ) -> tuple[list[tuple], list[tuple]]:
        """批量小时聚合：窗口内逐 (日期, 小时, 群) 的群总量与发言人 Top K。

        启动窗口重聚合/历史回填专用（PRD F4.1）：一条 GROUP BY
        DATE(timestamp), HOUR(timestamp), group_id, sender_id 的聚合 SQL
        拉回窗口全量明细，Python 侧逐 (date, hour, group) 汇总群总量
        （hour_rows）并对 sender 按 count 降序、同数 sender_id ASC 截断
        前 top_k（top_rows）；昵称另发一条派生表查询（窗口内 GROUP BY
        sender_id 聚出 MAX(id) 等值回表，MySQL 5.7 兼容，不带 IN 列表）
        取窗口内每个 sender 最新一条记录的 sender_name（不区分群，
        同人跨群共用最新昵称），查不到的空串兜底。

        口径：窗口 [start, end) 半开；hour_rows 为截断前群总量，不受
        top_k 影响；输出按 (date, hour, group_id) 升序展开（确定性）。
        image_records 因滚动清理只剩近几天，旧窗口自然为空——接受。

        Args:
            source: 数据源键，"msg"（chat_history）或 "image"（image_records），
                非法值抛 ValueError
            start: 窗口起点（含）
            end: 窗口终点（不含）
            top_k: 每 (date, hour, group) 保留的个人条数上限
                （调用方已夹住合法范围）

        Returns:
            tuple[list[tuple], list[tuple]]: (hour_rows, top_rows)
                hour_rows: [(date_str, hour, group_id, count), …]
                top_rows: [(date_str, hour, group_id, sender_id,
                            sender_name, count), …]
                窗口无数据返回 ([], [])
        """
        table = self._resolve_source_table(source)
        sql = (
            "SELECT DATE(timestamp), HOUR(timestamp), group_id, sender_id, COUNT(*) "
            f"FROM {table} WHERE timestamp >= %s AND timestamp < %s "
            "GROUP BY DATE(timestamp), HOUR(timestamp), group_id, sender_id"
        )
        async with self._mgr.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await self._execute(cur, sql, [start, end])
                rows = await cur.fetchall()
                if not rows:
                    return ([], [])

                # Python 侧按 (date, hour, group) 汇总：群总量 + sender 明细
                hour_totals: dict = {}
                buckets: dict = {}
                for row in rows:
                    key = (self._format_date(row[0]), int(row[1]), str(row[2]))
                    sid = str(row[3])
                    cnt = int(row[4] or 0)
                    hour_totals[key] = hour_totals.get(key, 0) + cnt
                    buckets.setdefault(key, []).append((sid, cnt))

                # 逐桶 count 降序（同数 sender_id ASC 保确定性）截断前 top_k
                limit = max(0, int(top_k))
                top_buckets: dict = {}
                for key, entries in buckets.items():
                    entries.sort(key=lambda item: (-item[1], item[0]))
                    top_buckets[key] = entries[:limit]

                # 窗口内每个 sender 的最新昵称（一条派生表查询，无 IN 列表）
                top_ids = {
                    sid for entries in top_buckets.values() for sid, _ in entries
                }
                if top_ids:
                    names = await self._window_latest_sender_names(
                        cur, table, start, end
                    )
                else:
                    names = {}

        # 确定性输出：按 (date, hour, group_id) 升序展开
        hour_rows = []
        top_rows = []
        for key in sorted(hour_totals):
            date_str, hour, gid = key
            hour_rows.append((date_str, hour, gid, hour_totals[key]))
            for sid, cnt in top_buckets[key]:
                top_rows.append((date_str, hour, gid, sid, names.get(sid, ""), cnt))
        return (hour_rows, top_rows)

    async def _window_latest_sender_names(
        self, cur, table: str, start: datetime, end: datetime
    ) -> dict:
        """取窗口内每个 sender 的最新昵称（派生表 MAX(id) 回表，5.7 兼容）。

        仿 _latest_sender_names 的派生表范式，但不带 sender_id IN 列表——
        窗口内全 sender 一次 GROUP BY sender_id 聚出 MAX(id)（auto_increment
        单调，即窗口内最后一条）再等值回表，供 get_hourly_batch 批量回填
        昵称；不区分群（同人跨群共用最新昵称）。

        Args:
            cur: 已打开的游标（复用调用方连接）
            table: 表名（仅来自 _SOURCE_TABLES 白名单，无拼接风险）
            start: 窗口起点（含）
            end: 窗口终点（不含）

        Returns:
            dict: {sender_id: sender_name}
        """
        sql = (
            f"SELECT t.sender_id, t.sender_name FROM {table} AS t "
            "JOIN ("
            f"SELECT t2.sender_id, MAX(t2.id) AS max_id FROM {table} AS t2 "
            "WHERE t2.timestamp >= %s AND t2.timestamp < %s "
            "GROUP BY t2.sender_id"
            ") AS m ON m.sender_id = t.sender_id AND m.max_id = t.id "
            "WHERE t.timestamp >= %s AND t.timestamp < %s"
        )
        await self._execute(cur, sql, [start, end, start, end])
        rows = await cur.fetchall()
        return {str(row[0]): str(row[1] or "") for row in rows}

    async def get_daily_batch(
        self, source: str, start: datetime, end: datetime
    ) -> list[tuple]:
        """批量日聚合：窗口内逐 (日期, 群) 的消息/图片总数。

        启动窗口重聚合日层/历史回填专用（PRD F4.1）：一条 GROUP BY
        DATE(timestamp), group_id 的聚合 SQL，群级全量不截断；
        供日快照表（msg_stats_daily / image_stats_daily）UPSERT。

        口径：窗口 [start, end) 半开；只含窗口内实际有数据的 (date, group)；
        按日期升序、同日按 group_id 升序（输出确定性）。

        Args:
            source: 数据源键，"msg"（chat_history）或 "image"（image_records），
                非法值抛 ValueError
            start: 窗口起点（含）
            end: 窗口终点（不含）

        Returns:
            list[tuple]: [(date_str, group_id, count), …]；窗口无数据返回 []
        """
        table = self._resolve_source_table(source)
        sql = (
            "SELECT DATE(timestamp), group_id, COUNT(*) "
            f"FROM {table} WHERE timestamp >= %s AND timestamp < %s "
            "GROUP BY DATE(timestamp), group_id "
            "ORDER BY DATE(timestamp) ASC, group_id ASC"
        )
        async with self._mgr.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await self._execute(cur, sql, [start, end])
                rows = await cur.fetchall()
        return [
            (self._format_date(row[0]), str(row[1]), int(row[2] or 0)) for row in rows
        ]

    async def get_monthly_batch(
        self, source: str, start: datetime, end: datetime
    ) -> list[tuple]:
        """批量月聚合：窗口内逐 (月, 群) 的消息/图片总数。

        月层增量补齐专用（PRD F4.1）：一条 GROUP BY YEAR(timestamp),
        MONTH(timestamp), group_id 的聚合 SQL，群级全量不截断；
        供月快照表（msg_stats_monthly / image_stats_monthly）UPSERT。

        口径：窗口 [start, end) 半开；月戳两位补零 "YYYY-MM"（跨年窗口
        自然按年月分组）；只含窗口内实际有数据的 (month, group)；
        按月升序、同月按 group_id 升序（输出确定性）。
        窗口首尾的不完整月同样会被聚合，是否入库由调用方按游标判定。

        Args:
            source: 数据源键，"msg"（chat_history）或 "image"（image_records），
                非法值抛 ValueError
            start: 窗口起点（含）
            end: 窗口终点（不含）

        Returns:
            list[tuple]: [("YYYY-MM", group_id, count), …]；窗口无数据返回 []
        """
        table = self._resolve_source_table(source)
        sql = (
            "SELECT YEAR(timestamp), MONTH(timestamp), group_id, COUNT(*) "
            f"FROM {table} WHERE timestamp >= %s AND timestamp < %s "
            "GROUP BY YEAR(timestamp), MONTH(timestamp), group_id "
            "ORDER BY YEAR(timestamp) ASC, MONTH(timestamp) ASC, group_id ASC"
        )
        async with self._mgr.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await self._execute(cur, sql, [start, end])
                rows = await cur.fetchall()
        return [
            (
                f"{int(row[0])}-{int(row[1]):02d}",
                str(row[2]),
                int(row[3] or 0),
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # v0.5.5 分段快照体系：快照路径元数据查询
    # ------------------------------------------------------------------

    async def get_overview_meta(
        self, group_id: str | None, start: datetime, end: datetime
    ) -> dict:
        """窗口总览元数据：活跃成员数与首末条消息时间（v0.5.5 快照路径专用）。

        get_overview 去掉 COUNT(*) 的精简版——总消息数改由快照三层归并供数，
        实时 SQL 只补快照无法推导的维度。口径：COUNT(DISTINCT sender_id)
        计去重发言者；MIN/MAX(timestamp) 取窗口内首末条时刻（窗口无数据时
        为 None）。

        Args:
            group_id: 群号过滤；None = 全部群汇总
            start: 窗口起点（含）
            end: 窗口终点（不含）

        Returns:
            dict: {"active_senders": int, "first": datetime|None,
                   "last": datetime|None}
        """
        where, params = self._window_clause(start, end, group_id=group_id)
        sql = (
            "SELECT COUNT(DISTINCT sender_id), MIN(timestamp), MAX(timestamp) "
            f"FROM chat_history WHERE {where}"
        )
        async with self._mgr.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await self._execute(cur, sql, params)
                row = await cur.fetchone()
        if not row:
            return {"active_senders": 0, "first": None, "last": None}
        return {
            "active_senders": int(row[0] or 0),
            "first": row[1],
            "last": row[2],
        }

    async def get_group_active_senders(
        self, start: datetime, end: datetime
    ) -> dict[str, int]:
        """窗口内每群的活跃成员数（v0.5.5 群排行快照路径专用）。

        口径：chat_history 按 group_id 分组 COUNT(DISTINCT sender_id)；
        群排行迁快照后，活跃成员数无法由群级快照推导，由本方法实时补齐。
        只统计窗口内实际有消息的群。

        Args:
            start: 窗口起点（含）
            end: 窗口终点（不含）

        Returns:
            dict[str, int]: {group_id: 活跃成员数}；窗口无数据返回 {}
        """
        sql = (
            "SELECT group_id, COUNT(DISTINCT sender_id) "
            "FROM chat_history WHERE timestamp >= %s AND timestamp < %s "
            "GROUP BY group_id"
        )
        async with self._mgr.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await self._execute(cur, sql, [start, end])
                rows = await cur.fetchall()
        return {str(row[0]): int(row[1] or 0) for row in rows}
