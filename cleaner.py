"""图片清理定时任务。

负责每天凌晨自动清理过期的图片记录，也支持手动触发清理。
"""

import asyncio
from datetime import datetime

from astrbot.api import logger

from .db_config import ConfigManager
from .db_mysql import MySQLManager


class ImageCleaner:
    """图片记录定时清理器。"""

    def __init__(self, mysql_mgr: MySQLManager, config_mgr: ConfigManager):
        self.mysql_mgr = mysql_mgr
        self.config_mgr = config_mgr
        self._task: asyncio.Task | None = None
        self._running = False

    async def start(self):
        """启动定时清理任务。"""
        self._running = True
        self._task = asyncio.create_task(self._cleanup_loop())
        logger.info("[HistorySave] 图片清理定时任务已启动")

    async def stop(self):
        """停止定时清理任务。"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("[HistorySave] 图片清理定时任务已停止")

    async def _cleanup_loop(self):
        """定时清理循环：每天凌晨 3:00 执行。"""
        from datetime import timedelta

        while self._running:
            try:
                now = datetime.now()
                # 计算距离下一个凌晨 3:00 的秒数
                target = now.replace(hour=3, minute=0, second=0, microsecond=0)
                if target <= now:
                    # 今天的 3:00 已过，等到明天
                    target += timedelta(days=1)

                wait_seconds = (target - now).total_seconds()
                await asyncio.sleep(wait_seconds)

                if not self._running:
                    break

                # 执行清理
                await self._do_cleanup()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[HistorySave] 清理任务异常: {e}")
                # 出错后等 60 秒重试
                await asyncio.sleep(60)

    async def _do_cleanup(self):
        """执行一次清理操作。"""
        try:
            retention_days = int(
                await self.config_mgr.get_setting("image_retention_days", "3")
            )
        except (ValueError, TypeError):
            retention_days = 3

        deleted = await self.mysql_mgr.clean_old_images(retention_days)
        if deleted >= 0:
            logger.info(
                f"[HistorySave] 自动清理完成：删除了 {deleted} 条过期图片记录"
                f"（保留 {retention_days} 天）"
            )
        else:
            logger.warning("[HistorySave] 自动清理执行失败")

    async def manual_clean(self, days: int | None = None) -> int:
        """手动触发清理。

        Args:
            days: 清理多少天前的数据，None 则使用配置值

        Returns:
            int: 删除的记录数，失败返回 -1
        """
        if days is None:
            try:
                days = int(
                    await self.config_mgr.get_setting("image_retention_days", "3")
                )
            except (ValueError, TypeError):
                days = 3

        deleted = await self.mysql_mgr.clean_old_images(days)
        if deleted >= 0:
            logger.info(
                f"[HistorySave] 手动清理完成：删除了 {deleted} 条图片记录（{days} 天前）"
            )
        return deleted
