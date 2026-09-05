"""统计聚合查询（StatsMixin）。

自根目录 db_mysql.py 拆分：get_stats / get_daily_stats 逐字迁移。
"""

from datetime import datetime, timedelta


class StatsMixin:
    """统计聚合 Mixin：今日/总量统计、最近 N 天每日统计。"""

    async def get_stats(self) -> dict:
        """获取统计信息（今日消息数、今日图片数、总消息数、总图片数）。

        每张表用一条 SQL 完成总数与今日数的条件聚合（SUM 布尔表达式），
        避免 DATE(timestamp) 包裹列导致索引失效；"今日"区间在 bot 侧按
        datetime.now() 的当天零点计算，与写入侧时区语义保持一致。

        Returns:
            dict: 统计信息字典
        """
        stats = {
            "today_messages": 0,
            "today_images": 0,
            "total_messages": 0,
            "total_images": 0,
        }
        try:
            today_start = datetime.now().replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            tomorrow_start = today_start + timedelta(days=1)
            async with self.pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await self._execute(
                        cur,
                        "SELECT COUNT(*), "
                        "SUM(timestamp >= %s AND timestamp < %s) "
                        "FROM chat_history",
                        (today_start, tomorrow_start),
                    )
                    row = await cur.fetchone()
                    if row:
                        stats["total_messages"] = int(row[0] or 0)
                        stats["today_messages"] = int(row[1] or 0)

                    await self._execute(
                        cur,
                        "SELECT COUNT(*), "
                        "SUM(timestamp >= %s AND timestamp < %s) "
                        "FROM image_records",
                        (today_start, tomorrow_start),
                    )
                    row = await cur.fetchone()
                    if row:
                        stats["total_images"] = int(row[0] or 0)
                        stats["today_images"] = int(row[1] or 0)
        except Exception as e:
            self._log_op_error("get_stats", "获取统计信息", e)
        return stats

    async def get_daily_stats(self, days: int = 7) -> list[dict]:
        """获取最近 N 天的每日统计。

        Args:
            days: 查询天数

        Returns:
            list[dict]: 每日统计列表
        """
        result = []
        try:
            # 时间窗口在 bot 侧计算（今天零点往前推 N 天至今），
            # 不依赖 DB 服务器的 CURDATE()，避免 DB 与 bot 时区不一致
            # 导致窗口边界偏移
            cutoff = datetime.now().replace(
                hour=0, minute=0, second=0, microsecond=0
            ) - timedelta(days=days)
            async with self.pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await self._execute(
                        cur,
                        """SELECT DATE(timestamp) as date, COUNT(*) as count
                           FROM chat_history
                           WHERE timestamp >= %s
                           GROUP BY DATE(timestamp) ORDER BY date""",
                        (cutoff,),
                    )
                    msg_rows = await cur.fetchall()
                    msg_map = {str(row[0]): row[1] for row in msg_rows}

                    await self._execute(
                        cur,
                        """SELECT DATE(timestamp) as date, COUNT(*) as count
                           FROM image_records
                           WHERE timestamp >= %s
                           GROUP BY DATE(timestamp) ORDER BY date""",
                        (cutoff,),
                    )
                    img_rows = await cur.fetchall()
                    img_map = {str(row[0]): row[1] for row in img_rows}

                    all_dates = sorted(set(list(msg_map.keys()) + list(img_map.keys())))
                    for date in all_dates:
                        result.append(
                            {
                                "date": date,
                                "messages": msg_map.get(date, 0),
                                "images": img_map.get(date, 0),
                            }
                        )
        except Exception as e:
            self._log_op_error("get_daily_stats", "获取每日统计", e)
        return result

    async def get_all_groups_summary(self) -> list[dict]:
        """全量群清单：chat_history 中实际有数据的群（v0.9.0 自 profile.fetcher 内联 SQL 下沉）。

        口径：按 group_id 分组，COUNT(*) 计消息数、MAX(timestamp) 记最近
        活跃时刻；排序 COUNT(*) DESC、同数 group_id ASC（输出确定性）。
        SQLite 后端在 core/db_sqlite.py 提供同名方法（语义一致），
        供 core/profile/fetcher.py 委托调用以兼容双后端。

        Returns:
            list[dict]: [{"group_id": str, "count": int,
                "last_active": datetime | None}]

        Raises:
            Exception: 查询失败（由调用方 service 层兜底为空列表）。
        """
        sql = (
            "SELECT group_id, COUNT(*) AS cnt, MAX(timestamp) "
            "FROM chat_history GROUP BY group_id "
            "ORDER BY cnt DESC, group_id ASC"
        )
        async with self.pool.acquire() as conn:
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
