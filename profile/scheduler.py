"""每日过期人物分析清理定时任务（模块 I）。

后台任务生命周期与退避范式镜像 ``summary/scheduler.py`` 的
:class:`CleanupScheduler`（其又沿用主目录 ``cleaner.py`` 的 F4 模式）：

- ``start()``：``asyncio.create_task`` 创建循环任务（重复调用防护）
- ``_loop()``：启动先执行一次清理，随后每 24h 一次；单次失败按指数退避
  （60→120→300→600s，上限 600s）立即重试，成功后复位退避计数
- ``stop()``：cancel + await + 吞 ``CancelledError``，无任务时 no-op
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from astrbot.api import logger

from .storage import ProfileStorage

if TYPE_CHECKING:
    from ..db_config import ConfigManager

# 保留天数兜底值（配置缺失/非法时使用，与 db_config.PROFILE_DEFAULTS 一致）
DEFAULT_KEEP_DAYS = 30

# 清理循环间隔（秒）：每 24 小时执行一次
_CLEANUP_INTERVAL = 24 * 3600


class ProfileCleanupScheduler:
    """过期人物分析文件定时清理调度器（每 24h 一轮，含异常退避重试）。"""

    # 异常退避序列（秒）：与 cleaner.py ImageCleaner 的 F4 模式一致，上限 600s
    _BACKOFF_SEQUENCE = [60, 120, 300, 600]

    def __init__(self, storage: ProfileStorage, config_mgr: ConfigManager):
        """初始化调度器。

        Args:
            storage: 人物分析存储器（清理动作委托其 cleanup）
            config_mgr: 配置管理器（读取 profile_keep_days）
        """
        self.storage = storage
        self.config_mgr = config_mgr
        self._task: asyncio.Task | None = None
        self._fail_count = 0  # 连续失败次数（成功后复位）

    async def start(self) -> None:
        """启动每日清理任务。

        重复调用防护：已有任务未结束时直接返回，不再重复创建。
        """
        if self._task is not None and not self._task.done():
            logger.warning("[Profile] 清理调度器已在运行，忽略重复 start()")
            return
        self._fail_count = 0
        self._task = asyncio.create_task(self._loop())
        logger.info("[Profile] 人物分析清理定时任务已启动（间隔 24 小时）")

    async def stop(self) -> None:
        """停止清理任务：cancel + await + 吞 CancelledError；无任务时 no-op。"""
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        logger.info("[Profile] 人物分析清理定时任务已停止")

    async def _loop(self) -> None:
        """清理循环：启动先执行一次，随后每 24h 一次。

        单次清理失败按退避序列（60→120→300→600s 封顶）等待后立即重试，
        成功后复位退避计数并排定下一个 24h 周期；任意等待期间均可被
        stop() 的 cancel 打断。
        """
        while True:
            try:
                await self._cleanup_once()
                self._fail_count = 0
                await asyncio.sleep(_CLEANUP_INTERVAL)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                backoff = self._BACKOFF_SEQUENCE[
                    min(self._fail_count, len(self._BACKOFF_SEQUENCE) - 1)
                ]
                self._fail_count += 1
                logger.error(f"[Profile] 过期人物分析清理失败，{backoff}s 后重试: {e}")
                await asyncio.sleep(backoff)

    async def _cleanup_once(self) -> None:
        """执行一次过期清理。

        先读取 ``profile_keep_days``（get_profile_setting_typed，
        非法值兜底 30 天），再委托 storage.cleanup。

        Raises:
            Exception: 清理执行失败（由 _loop 按退避序列重试）
        """
        try:
            keep_days = int(
                await self.config_mgr.get_profile_setting_typed("profile_keep_days")
            )
            if keep_days <= 0:
                raise ValueError(f"保留天数非法: {keep_days}")
        except (TypeError, ValueError) as e:
            logger.warning(
                f"[Profile] profile_keep_days 配置非法，"
                f"回退默认 {DEFAULT_KEEP_DAYS} 天: {e}"
            )
            keep_days = DEFAULT_KEEP_DAYS

        await self.storage.cleanup(keep_days)
