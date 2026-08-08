"""图片记录读写（ImagesMixin）。

自根目录 db_mysql.py 拆分：insert_image_record / clean_old_images 逐字迁移。
"""

from datetime import datetime, timedelta

from astrbot.api import logger

# DDL 超时常量（DDL_TIMEOUT_SECONDS）定义于 base.py
from .base import DDL_TIMEOUT_SECONDS


class ImagesMixin:
    """图片记录读写 Mixin：插入图片记录、按保留天数清理旧图片。"""

    async def insert_image_record(
        self,
        group_id: str,
        sender_id: str,
        image_url: str,
        sender_name: str = "",
        timestamp: datetime | None = None,
    ) -> bool:
        """插入一条图片记录。

        Args:
            group_id: 群号（字符串形式）
            sender_id: 发送者 QQ 号（字符串形式）
            image_url: 图片 URL
            sender_name: 发送者昵称（可选，默认空字符串）
            timestamp: 消息时间戳；None 取当前时间（实时消息现行为）。
                v0.6.0 补库路径透传消息真实时间，保证图片时间窗统计正确

        Returns:
            bool: 是否插入成功
        """
        # image_url 列为 VARCHAR(1024)：超长链接跳过入库并告警，
        # 避免严格模式下整条插入失败（且不影响同消息的文本记录）
        if len(image_url) > 1024:
            logger.warning(
                f"[HistorySave] 图片链接超长（{len(image_url)} 字符），已跳过入库: "
                f"{image_url[:80]}..."
            )
            return False
        try:
            # v0.6.0 补库路径透传消息到达时刻；实时路径缺省取当前时间
            if timestamp is None:
                timestamp = datetime.now()
            async with self.pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await self._execute(
                        cur,
                        """INSERT INTO image_records
                           (timestamp, group_id, sender_id, sender_name, image_url)
                           VALUES (%s, %s, %s, %s, %s)""",
                        (timestamp, group_id, sender_id, sender_name, image_url),
                    )
            return True
        except Exception as e:
            logger.error(f"[HistorySave] 插入图片记录失败: {e}")
            return False

    async def get_existing_image_urls(self, group_id: str, urls: list[str]) -> set[str]:
        """按群批量查询已存在的图片 URL 集合（v0.6.0 重载补库图片去重用）。

        过滤空串；IN 列表分块（每块 ≤500）避免超长 SQL；全部参数化绑定；
        异常 ``_log_op_error`` 后返回空集（让上层按"全不存在"处理，不阻断补库）。
        """
        items = [url for url in (urls or []) if url]
        if not items:
            return set()
        existing: set[str] = set()
        chunk_size = 500
        try:
            async with self.pool.acquire() as conn:
                async with conn.cursor() as cur:
                    # IN 列表分块查询：每块 ≤ 500，避免超长 SQL 与参数占位符过多
                    for i in range(0, len(items), chunk_size):
                        chunk = items[i : i + chunk_size]
                        placeholders = ",".join(["%s"] * len(chunk))
                        await self._execute(
                            cur,
                            f"""SELECT image_url FROM image_records
                                WHERE group_id = %s AND image_url IN ({placeholders})""",
                            [group_id] + chunk,
                        )
                        rows = await cur.fetchall()
                        for row in rows:
                            if row and row[0]:
                                existing.add(row[0])
            return existing
        except Exception as e:
            self._log_op_error("get_existing_image_urls", "查询群内已存在图片链接", e)
            return set()

    async def clean_old_images(self, retention_days: int) -> int:
        """清理指定天数之前的图片记录。

        Args:
            retention_days: 保留天数

        Returns:
            int: 删除的记录数，失败返回 -1
        """
        try:
            # 钳制到安全范围，防止异常大的设置值使 timedelta/日期运算
            # 抛 OverflowError 导致清理功能永久失效
            retention_days = min(max(int(retention_days), 0), 36500)
            cutoff = datetime.now() - timedelta(days=retention_days)
            async with self.pool.acquire() as conn:
                async with conn.cursor() as cur:
                    # 大表批量 DELETE 可能耗时较长，用 DDL 超时档兜底
                    await self._execute(
                        cur,
                        "DELETE FROM image_records WHERE timestamp < %s",
                        (cutoff,),
                        timeout=DDL_TIMEOUT_SECONDS,
                    )
                    return cur.rowcount
        except Exception as e:
            logger.error(f"[HistorySave] 清理图片记录失败: {e}")
            return -1
