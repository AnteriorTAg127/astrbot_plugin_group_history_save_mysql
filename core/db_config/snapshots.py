"""统计快照 Mixin（SnapshotMixin）。

图片小时级快照（v0.5.0）与「消息 + 图片 × 小时/日/月」分段快照体系
（v0.5.5）的 UPSERT 写入、查询原语与淘汰。
"""

from datetime import datetime

from astrbot.api import logger


class SnapshotMixin:
    """统计快照读写 Mixin：snapshot_upsert* / snapshot_query / snapshot_*_totals 等。"""

    # ========== 图片统计快照（v0.5.0） ==========

    async def snapshot_upsert(
        self, hour_rows: list[tuple], top_rows: list[tuple]
    ) -> None:
        """UPSERT 图片统计小时级快照行（同主键覆盖写，可重复执行）。

        Args:
            hour_rows: (date, hour, group_id, image_count) 行列表（群级全量）
            top_rows: (date, hour, group_id, sender_id, sender_name, image_count)
                行列表（每小时每群个人 Top K，覆盖时同步更新 sender_name）

        两列表均为空时直接返回；异常仅记日志不抛出（定时任务调用方依赖）。
        """
        if not hour_rows and not top_rows:
            return
        try:
            await self._ensure_db()
            if hour_rows:
                await self.db.executemany(
                    "INSERT INTO image_stats_hourly "
                    "(date, hour, group_id, image_count) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(date, hour, group_id) "
                    "DO UPDATE SET image_count = excluded.image_count",
                    hour_rows,
                )
            if top_rows:
                await self.db.executemany(
                    "INSERT INTO image_stats_hourly_top "
                    "(date, hour, group_id, sender_id, sender_name, image_count) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(date, hour, group_id, sender_id) "
                    "DO UPDATE SET image_count = excluded.image_count, "
                    "sender_name = excluded.sender_name",
                    top_rows,
                )
            await self.db.commit()
        except Exception as e:
            logger.error(f"[Stats] 写入图片统计快照失败: {e}")

    async def snapshot_query(
        self, start: datetime, end: datetime, group_id: str | None = None
    ) -> dict:
        """按 (date, hour) 半开区间 [start, end) 聚合查询图片统计快照。

        区间展开：date > start_date 或 (date == start_date 且 hour >= start.hour)；
        end 不含：date < end_date 或 (date == end_date 且 hour < end.hour)。
        total/by_group 取自 image_stats_hourly（群级全量口径），
        by_sender 取自 image_stats_hourly_top（Top K 个人口径）。

        Args:
            start: 起始时间（含）
            end: 结束时间（不含）
            group_id: 可选群过滤；None 时聚合全部群

        Returns:
            dict: {"total": int, "by_group": {gid: int},
                   "by_sender": {(gid, sender_id): int}}；异常返回全零结构
        """
        empty = {"total": 0, "by_group": {}, "by_sender": {}}
        try:
            await self._ensure_db()
            start_date = start.strftime("%Y-%m-%d")
            end_date = end.strftime("%Y-%m-%d")
            # (date, hour) 半开区间条件，比较值全部参数化绑定
            time_cond = (
                "((date > ?) OR (date = ? AND hour >= ?)) AND "
                "((date < ?) OR (date = ? AND hour < ?))"
            )
            params: tuple = (
                start_date,
                start_date,
                start.hour,
                end_date,
                end_date,
                end.hour,
            )
            group_cond = ""
            if group_id is not None:
                group_cond = " AND group_id = ?"
                params = params + (str(group_id),)
            result: dict = {"total": 0, "by_group": {}, "by_sender": {}}
            # 群级全量：总图片数与分群聚合
            async with self.db.execute(
                "SELECT group_id, SUM(image_count) FROM image_stats_hourly "
                f"WHERE {time_cond}{group_cond} GROUP BY group_id",
                params,
            ) as cursor:
                for gid, count in await cursor.fetchall():
                    result["by_group"][gid] = count
                    result["total"] += count
            # 个人 Top K：(group_id, sender_id) 维度聚合
            async with self.db.execute(
                "SELECT group_id, sender_id, SUM(image_count) "
                "FROM image_stats_hourly_top "
                f"WHERE {time_cond}{group_cond} "
                "GROUP BY group_id, sender_id",
                params,
            ) as cursor:
                for gid, sid, count in await cursor.fetchall():
                    result["by_sender"][(gid, sid)] = count
            return result
        except Exception as e:
            logger.error(f"[Stats] 查询图片统计快照失败: {e}")
            return empty

    # ========== 分段快照体系（v0.5.5） ==========

    async def snapshot_upsert_msg_hour(
        self, hour_rows: list[tuple], top_rows: list[tuple]
    ) -> None:
        """UPSERT 消息统计小时级快照行（同主键覆盖写，可重复执行）。

        v0.5.5 分段快照体系消息侧小时层，与图片侧 snapshot_upsert 语义对称：
        hour_rows 写入 msg_stats_hourly（群级全量），top_rows 写入
        msg_stats_hourly_top（每小时每群个人 Top K）。

        Args:
            hour_rows: (date, hour, group_id, msg_count) 行列表（群级全量）
            top_rows: (date, hour, group_id, sender_id, sender_name, msg_count)
                行列表（每小时每群个人 Top K，覆盖时同步更新 sender_name）

        两列表均为空时直接返回；异常仅记日志不抛出（定时任务调用方依赖）。
        """
        if not hour_rows and not top_rows:
            return
        try:
            await self._ensure_db()
            if hour_rows:
                await self.db.executemany(
                    "INSERT INTO msg_stats_hourly "
                    "(date, hour, group_id, msg_count) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(date, hour, group_id) "
                    "DO UPDATE SET msg_count = excluded.msg_count",
                    hour_rows,
                )
            if top_rows:
                await self.db.executemany(
                    "INSERT INTO msg_stats_hourly_top "
                    "(date, hour, group_id, sender_id, sender_name, msg_count) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(date, hour, group_id, sender_id) "
                    "DO UPDATE SET msg_count = excluded.msg_count, "
                    "sender_name = excluded.sender_name",
                    top_rows,
                )
            await self.db.commit()
        except Exception as e:
            logger.error(f"[Stats] 写入消息统计快照失败: {e}")

    async def snapshot_upsert_daily(
        self, msg_rows: list[tuple], image_rows: list[tuple]
    ) -> None:
        """UPSERT 消息/图片统计日级快照行（同主键覆盖写，可重复执行）。

        v0.5.5 分段快照体系日层：行形如 (date, group_id, count)，为群级每日
        总数全量（不受 Top K 截断），只存完整自然日（今天的数据由小时层承载）；
        msg_rows 写入 msg_stats_daily，image_rows 写入 image_stats_daily。

        Args:
            msg_rows: (date, group_id, count) 行列表（写入 msg_stats_daily）
            image_rows: (date, group_id, count) 行列表（写入 image_stats_daily）

        两列表均为空时直接返回；异常仅记日志不抛出（定时任务调用方依赖）。
        """
        if not msg_rows and not image_rows:
            return
        try:
            await self._ensure_db()
            if msg_rows:
                await self.db.executemany(
                    "INSERT INTO msg_stats_daily (date, group_id, msg_count) "
                    "VALUES (?, ?, ?) "
                    "ON CONFLICT(date, group_id) "
                    "DO UPDATE SET msg_count = excluded.msg_count",
                    msg_rows,
                )
            if image_rows:
                await self.db.executemany(
                    "INSERT INTO image_stats_daily (date, group_id, image_count) "
                    "VALUES (?, ?, ?) "
                    "ON CONFLICT(date, group_id) "
                    "DO UPDATE SET image_count = excluded.image_count",
                    image_rows,
                )
            await self.db.commit()
        except Exception as e:
            logger.error(f"[Stats] 写入统计日级快照失败: {e}")

    async def snapshot_upsert_monthly(
        self, msg_rows: list[tuple], image_rows: list[tuple]
    ) -> None:
        """UPSERT 消息/图片统计月级快照行（同主键覆盖写，可重复执行）。

        v0.5.5 分段快照体系月层：行形如 (month, group_id, count)，month 为
        "YYYY-MM" 格式，只存完整自然月，永久保留不淘汰；msg_rows 写入
        msg_stats_monthly，image_rows 写入 image_stats_monthly。

        Args:
            msg_rows: (month, group_id, count) 行列表（写入 msg_stats_monthly）
            image_rows: (month, group_id, count) 行列表（写入 image_stats_monthly）

        两列表均为空时直接返回；异常仅记日志不抛出（定时任务调用方依赖）。
        """
        if not msg_rows and not image_rows:
            return
        try:
            await self._ensure_db()
            if msg_rows:
                await self.db.executemany(
                    "INSERT INTO msg_stats_monthly (month, group_id, msg_count) "
                    "VALUES (?, ?, ?) "
                    "ON CONFLICT(month, group_id) "
                    "DO UPDATE SET msg_count = excluded.msg_count",
                    msg_rows,
                )
            if image_rows:
                await self.db.executemany(
                    "INSERT INTO image_stats_monthly "
                    "(month, group_id, image_count) VALUES (?, ?, ?) "
                    "ON CONFLICT(month, group_id) "
                    "DO UPDATE SET image_count = excluded.image_count",
                    image_rows,
                )
            await self.db.commit()
        except Exception as e:
            logger.error(f"[Stats] 写入统计月级快照失败: {e}")

    async def snapshot_monthly_totals(
        self,
        source: str,
        month_start: str,
        month_end: str,
        group_id: str | None = None,
    ) -> dict[str, int]:
        """按群累计月级快照总数（month 闭区间）。

        v0.5.5 分段快照体系查询原语：读取 *_stats_monthly 表，按 "YYYY-MM"
        闭区间 [month_start, month_end] 对每群 SUM 累计。

        Args:
            source: 快照来源，"msg"（消息）或 "image"（图片）
            month_start: 月份起点 "YYYY-MM"（含）
            month_end: 月份终点 "YYYY-MM"（含）
            group_id: 可选群过滤；None 时聚合全部群

        Returns:
            dict[str, int]: {group_id: 累计数}；source 非法或异常时返回空
            dict（仅记日志不抛出）
        """
        # source → (表名, 计数列名) 白名单映射：表名/列名仅从白名单取用，
        # 杜绝非法 source 值拼接进 SQL 的注入风险
        source_map = {
            "msg": ("msg_stats_monthly", "msg_count"),
            "image": ("image_stats_monthly", "image_count"),
        }
        entry = source_map.get(source)
        if entry is None:
            logger.warning(
                f"[Stats] snapshot_monthly_totals 收到非法 source: {source!r}"
            )
            return {}
        table, count_col = entry
        try:
            await self._ensure_db()
            # 闭区间比较值全部参数化绑定
            params: tuple = (month_start, month_end)
            group_cond = ""
            if group_id is not None:
                group_cond = " AND group_id = ?"
                params = params + (str(group_id),)
            async with self.db.execute(
                f"SELECT group_id, SUM({count_col}) FROM {table} "
                f"WHERE month >= ? AND month <= ?{group_cond} "
                "GROUP BY group_id",
                params,
            ) as cursor:
                rows = await cursor.fetchall()
            return {row[0]: row[1] for row in rows}
        except Exception as e:
            logger.error(f"[Stats] 查询月级快照失败: {e}")
            return {}

    async def snapshot_daily_rows(
        self,
        source: str,
        date_start: str,
        date_end: str,
        group_id: str | None = None,
    ) -> dict[tuple[str, str], int]:
        """读取日级快照原始行（date 闭区间）。

        v0.5.5 分段快照体系查询原语：读取 *_stats_daily 表，取 "YYYY-MM-DD"
        闭区间 [date_start, date_end] 内的各 (date, group_id) 原始行。

        Args:
            source: 快照来源，"msg"（消息）或 "image"（图片）
            date_start: 日期起点 "YYYY-MM-DD"（含）
            date_end: 日期终点 "YYYY-MM-DD"（含）
            group_id: 可选群过滤；None 时取全部群

        Returns:
            dict[tuple[str, str], int]: {(date, group_id): count}；source 非法
            或异常时返回空 dict（仅记日志不抛出）
        """
        # source → (表名, 计数列名) 白名单映射：表名/列名仅从白名单取用，
        # 杜绝非法 source 值拼接进 SQL 的注入风险
        source_map = {
            "msg": ("msg_stats_daily", "msg_count"),
            "image": ("image_stats_daily", "image_count"),
        }
        entry = source_map.get(source)
        if entry is None:
            logger.warning(f"[Stats] snapshot_daily_rows 收到非法 source: {source!r}")
            return {}
        table, count_col = entry
        try:
            await self._ensure_db()
            params: tuple = (date_start, date_end)
            group_cond = ""
            if group_id is not None:
                group_cond = " AND group_id = ?"
                params = params + (str(group_id),)
            async with self.db.execute(
                f"SELECT date, group_id, {count_col} FROM {table} "
                f"WHERE date >= ? AND date <= ?{group_cond}",
                params,
            ) as cursor:
                rows = await cursor.fetchall()
            return {(row[0], row[1]): row[2] for row in rows}
        except Exception as e:
            logger.error(f"[Stats] 查询日级快照失败: {e}")
            return {}

    async def snapshot_hourly_date_totals(
        self,
        source: str,
        date_start: str,
        date_end: str,
        group_id: str | None = None,
    ) -> dict[tuple[str, str], int]:
        """小时级快照按 (date, group_id) SUM 聚合（date 闭区间）。

        v0.5.5 分段快照体系查询原语：读取 *_stats_hourly 群级全量表
        （不含 Top K 表），按 "YYYY-MM-DD" 闭区间 [date_start, date_end]
        逐 (date, group_id) SUM。某 (date, group_id) 无行即不出现在结果中
        （存在性语义由调用方使用）。

        Args:
            source: 快照来源，"msg"（消息）或 "image"（图片）
            date_start: 日期起点 "YYYY-MM-DD"（含）
            date_end: 日期终点 "YYYY-MM-DD"（含）
            group_id: 可选群过滤；None 时聚合全部群

        Returns:
            dict[tuple[str, str], int]: {(date, group_id): count}；source 非法
            或异常时返回空 dict（仅记日志不抛出）
        """
        # source → (表名, 计数列名) 白名单映射：表名/列名仅从白名单取用，
        # 杜绝非法 source 值拼接进 SQL 的注入风险
        source_map = {
            "msg": ("msg_stats_hourly", "msg_count"),
            "image": ("image_stats_hourly", "image_count"),
        }
        entry = source_map.get(source)
        if entry is None:
            logger.warning(
                f"[Stats] snapshot_hourly_date_totals 收到非法 source: {source!r}"
            )
            return {}
        table, count_col = entry
        try:
            await self._ensure_db()
            params: tuple = (date_start, date_end)
            group_cond = ""
            if group_id is not None:
                group_cond = " AND group_id = ?"
                params = params + (str(group_id),)
            async with self.db.execute(
                f"SELECT date, group_id, SUM({count_col}) FROM {table} "
                f"WHERE date >= ? AND date <= ?{group_cond} "
                "GROUP BY date, group_id",
                params,
            ) as cursor:
                rows = await cursor.fetchall()
            return {(row[0], row[1]): row[2] for row in rows}
        except Exception as e:
            logger.error(f"[Stats] 查询小时级快照日期聚合失败: {e}")
            return {}

    async def snapshot_evict(
        self,
        hourly_cutoff: str,
        msg_top_cutoff: str,
        image_top_cutoff: str,
        daily_cutoff: str,
    ) -> bool:
        """淘汰过期快照行（DELETE WHERE date < ?，monthly 两表不动）。

        v0.5.5 分段快照体系淘汰（PRD F4.3）：
        - msg_stats_hourly 与 image_stats_hourly 使用 hourly_cutoff；
        - msg_stats_hourly_top 使用 msg_top_cutoff；
        - image_stats_hourly_top 使用 image_top_cutoff；
        - msg_stats_daily 与 image_stats_daily 使用 daily_cutoff；
        - msg_stats_monthly / image_stats_monthly 永不淘汰，此处不触碰。

        Args:
            hourly_cutoff: 小时层淘汰阈值 "YYYY-MM-DD"（删除 date 小于该值的行）
            msg_top_cutoff: 消息小时 Top K 表淘汰阈值 "YYYY-MM-DD"
            image_top_cutoff: 图片小时 Top K 表淘汰阈值 "YYYY-MM-DD"
            daily_cutoff: 日层淘汰阈值 "YYYY-MM-DD"

        Returns:
            bool: 全部删除成功并 commit 后返回 True；任一异常仅记日志返回
            False（不阻断统计主流程）
        """
        try:
            await self._ensure_db()
            await self.db.execute(
                "DELETE FROM msg_stats_hourly WHERE date < ?", (hourly_cutoff,)
            )
            await self.db.execute(
                "DELETE FROM image_stats_hourly WHERE date < ?", (hourly_cutoff,)
            )
            await self.db.execute(
                "DELETE FROM msg_stats_hourly_top WHERE date < ?",
                (msg_top_cutoff,),
            )
            await self.db.execute(
                "DELETE FROM image_stats_hourly_top WHERE date < ?",
                (image_top_cutoff,),
            )
            await self.db.execute(
                "DELETE FROM msg_stats_daily WHERE date < ?", (daily_cutoff,)
            )
            await self.db.execute(
                "DELETE FROM image_stats_daily WHERE date < ?", (daily_cutoff,)
            )
            await self.db.commit()
            return True
        except Exception as e:
            logger.error(f"[Stats] 淘汰快照失败: {e}")
            return False
