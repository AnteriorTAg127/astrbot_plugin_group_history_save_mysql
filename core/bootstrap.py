"""插件装配与生命周期（v0.8.x 自 main.py 迁出）。

承接 main.py 迁出的全部**非注册性**职责：

- **服务装配**：MySQL 管理器 / 本地配置管理器 / 图片清理器 / 总结·人物·统计
  三服务 / WebAPI / 消息保存器 / 补库器 的构建与依赖注入（原
  ``GroupHistoryPlugin.__init__`` 函数体）。
- **后台 MySQL 初始化重试循环**（原 ``_background_mysql_init``）。
- **卸载清理**（原 ``terminate`` 函数体：public_api 注销 + 取消后台任务 +
  LIFO 停机 + 关闭连接池/本地库）。

main.py 保留注册性内容（``@register`` 类装饰、``@filter.*`` 指令/事件装饰
与签名、``terminate`` 在类体中的直接定义），只做薄委托与本模块互操作。
"""

import asyncio

from astrbot.api import logger

from .backfill import ReloadBackfill
from .cleaner import ImageCleaner
from .commands import GroupCommands
from .db_config import ConfigManager
from .db_mysql import MySQLManager
from .profile.service import ProfileService
from .public_api import register_config_manager, register_mysql_manager
from .saver import MessageSaver
from .stats import StatsService
from .summary import SummaryService
from .webapi import WebAPI

# 后台 MySQL 初始化的最大连续失败次数：超过后放弃重试并停用存储功能，
# 避免数据库长期不可用时无限刷日志。恢复方式：修正配置后在插件管理重启插件。
MAX_INIT_ATTEMPTS = 5


class PluginBootstrap:
    """插件装配与生命周期管理：构建全部服务并接管后台初始化与卸载。

    main.py ``__init__`` 构造本类（传入插件自身 ``plugin``），随后把本类公开的
    各服务实例透出到自己同名属性（``self.mysql_mgr`` 等），保持既有 handler 与
    测试的访问路径不变。
    """

    def __init__(self, plugin, context, config=None) -> None:
        """构建全部服务（仅组装引用、构造无 I/O，register_* 注册即时生效）。

        Args:
            plugin: GroupHistoryPlugin 实例，注入给各服务作为 ``star``
                （供其 ``text_to_image`` / ``html_render`` 鸭子类型调用）。
            context: AstrBot Context。
            config: AstrBotConfig 或 None；None 时按空 dict 兜底用默认值。
        """
        # config 可能为 None（框架在 _conf_schema.json 缺失时以 config=None
        # 实例化插件），统一用空 dict 兜底，保证默认值生效
        cfg = config or {}
        self.plugin = plugin
        self.mysql_mgr = MySQLManager(
            host=cfg.get("mysql_host", "127.0.0.1"),
            port=cfg.get("mysql_port", 3306),
            user=cfg.get("mysql_user", "root"),
            password=cfg.get("mysql_password", ""),
            database=cfg.get("mysql_database", "astrbot_history"),
            pool_min_size=cfg.get("pool_min_size", 1),
            pool_max_size=cfg.get("pool_max_size", 10),
            pool_idle_timeout=cfg.get("pool_idle_timeout", 120),
            pool_timeout=cfg.get("pool_timeout", 30),
            pool_ping_cooldown=cfg.get("pool_ping_cooldown", 5),
        )
        # v0.7.0 对外公共 API 注册：构造 MySQLManager 后立即注册（构造无 I/O，
        # 注册即时生效；MySQL 未连接时对外查询自然失败并记查询日志）
        register_mysql_manager(self.mysql_mgr)

        # 初始化本地配置管理器
        self.config_mgr = ConfigManager()
        # v0.7.0 查询日志存储注册：查询日志写内置 SQLite（config.db），
        # 由 ConfigManager 管理；构造后立即注册（未注册时日志仅 warning 降级）
        register_config_manager(self.config_mgr)

        # 初始化图片清理器
        self.cleaner = ImageCleaner(self.mysql_mgr, self.config_mgr)

        # 总结服务（须在 WebAPI 之前构造，以便注入其存储实例）
        self.summary_service = SummaryService(
            context, self.config_mgr, self.mysql_mgr, plugin
        )
        # 人物分析服务（须在 WebAPI 之前构造，以便注入服务与其存储实例）
        self.profile_service = ProfileService(
            context, self.config_mgr, self.mysql_mgr, plugin
        )
        # 数据分析服务（须在 WebAPI 之前构造，以便注入服务实例；
        # 构造仅组装上游模块引用无 I/O，调度器在 MySQL 初始化成功后才 start）
        self.stats_service = StatsService(context, self.mysql_mgr, self.config_mgr, plugin)

        # Web API（注入总结/人物存储与渲染实例 + 统计服务）
        self.web_api = WebAPI(
            context,
            self.mysql_mgr,
            self.config_mgr,
            self.cleaner,
            summary_storage=self.summary_service.storage,
            summary_renderer=self.summary_service.renderer,
            profile_service=self.profile_service,
            profile_storage=self.profile_service.storage,
            profile_renderer=self.profile_service.renderer,
            stats_service=self.stats_service,
        )

        # 消息保存器（解析/缓冲/落库/补录全部在 core.saver）
        self.saver = MessageSaver(self.mysql_mgr, self.config_mgr, self.stats_service)
        # 按群消息触发的自动补库（注入 saver / stats_service 快照回填收尾链）
        self.backfill = ReloadBackfill(
            context,
            self.mysql_mgr,
            self.config_mgr,
            saver=self.saver,
            stats_service=self.stats_service,
        )
        # 群配置/管理指令执行器（history_* 与 /补库 指令体）
        self.commands = GroupCommands(
            self.mysql_mgr, self.config_mgr, self.cleaner, self.backfill
        )

        self._init_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # 生命周期：初始化 / 后台 MySQL 初始化 / 卸载
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """异步初始化：连接数据库、启动定时任务。

        MySQL 连接放入后台任务执行，避免数据库不可达时阻塞 AstrBot 启动。
        """
        # 初始化本地配置（aiosqlite 本地文件，极快，不会阻塞）
        config_ok = await self.config_mgr.initialize()
        if not config_ok:
            logger.error("[HistorySave] 本地配置初始化失败，插件功能受限")

        # MySQL 初始化放入后台，不阻塞框架启动
        self._init_task = asyncio.create_task(self._background_mysql_init())
        logger.info("[HistorySave] 插件已加载，MySQL 连接在后台初始化中")

    async def _background_mysql_init(self) -> None:
        """后台初始化 MySQL，失败时每 60 秒重试一次。

        连续失败 MAX_INIT_ATTEMPTS 次后放弃重试：关闭连接池、停用存储功能，
        避免数据库长期不可用时无限刷日志。每次尝试用 120 秒强制兜底。
        """
        retry_interval = 60
        # initialize() 内含 schema 迁移 DDL（大表 ADD INDEX 可能远超 10s），
        # 原 10s 超时会把 DDL 中途 cancel，导致连续 5 次失败后永久放弃存储；
        # 本任务为后台任务，放长超时不阻塞框架启动（F8）
        init_timeout = 120
        attempt = 0
        while not self.saver.is_initialized:
            attempt += 1
            try:
                mysql_ok = await asyncio.wait_for(
                    self.mysql_mgr.initialize(), timeout=init_timeout
                )
                if mysql_ok:
                    self.saver.set_initialized()
                    # 先补录初始化窗口内缓冲的消息，再启动定时清理
                    await self.saver.flush_pending()
                    await self.cleaner.start()
                    # 启动总结清理调度器（失败仅记日志，不阻断插件启动）
                    try:
                        await self.summary_service.start()
                    except Exception as e:
                        logger.error(f"[HistorySummary] 启动总结服务失败: {e}")
                    # 启动人物分析清理调度器（失败仅记日志，不阻断插件启动）
                    try:
                        await self.profile_service.start()
                    except Exception as e:
                        logger.error(f"[Profile] 启动人物分析服务失败: {e}")
                    # 启动数据分析调度器（失败仅记日志，不阻断插件启动）
                    try:
                        await self.stats_service.start()
                    except Exception as e:
                        logger.error(f"[Stats] 启动数据分析服务失败: {e}")
                    # v0.5.5 快照启动回填：MySQL 可用且 stats 服务已启动后发起后台
                    # 批量回填（服务层 create_task 自持句柄、terminate 自行取消，
                    # 幂等可重入）；失败仅记日志，不阻断插件加载
                    try:
                        await self.stats_service.startup_backfill()
                    except Exception as e:
                        logger.error(f"[HistorySave] 快照启动回填发起失败: {e}")
                    # v0.6.1 重载自动补库改为按群消息触发：插件重启后某群首条消息到达时，
                    # on_group_message → ReloadBackfill.maybe_trigger 触发该群补库（每群一次）
                    logger.info(
                        "[HistorySave] MySQL 连接成功，插件初始化完成，开始监听群消息"
                    )
                    return
                else:
                    logger.warning(
                        f"[HistorySave] MySQL 连接失败（第 {attempt} 次尝试），"
                        f"{retry_interval}s 后重试..."
                    )
            except asyncio.TimeoutError:
                logger.warning(
                    f"[HistorySave] MySQL 初始化超时（第 {attempt} 次尝试），"
                    f"{retry_interval}s 后重试..."
                )
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning(
                    f"[HistorySave] MySQL 初始化异常（第 {attempt} 次尝试）: {e}，"
                    f"{retry_interval}s 后重试..."
                )
            # 重试耗尽：放弃并停用存储功能（关闭连接池使后续操作快速失败）
            if attempt >= MAX_INIT_ATTEMPTS:
                # 明确记录被丢弃的缓冲消息条数，避免静默丢失无迹可查（F6）
                dropped = self.saver.mark_gave_up()
                logger.error(
                    f"[HistorySave] MySQL 连续 {attempt} 次连接失败，停止重试，"
                    f"消息存储功能已停用，同时丢弃启动窗口缓冲消息 {dropped} 条。"
                    f"请检查数据库配置与连通性，"
                    f"然后在插件管理中重启本插件以恢复。"
                )
                try:
                    await self.mysql_mgr.close()
                except Exception:
                    pass
                return
            # 等待后重试
            try:
                await asyncio.sleep(retry_interval)
            except asyncio.CancelledError:
                return

    async def shutdown(self) -> None:
        """插件卸载/停用时清理资源（原 main.terminate 函数体，LIFO 停机）。"""
        # v0.7.0 对外公共 API 先注销：插件开始终止后，调用方（含持有旧函数
        # 引用的插件）的对外查询立即抛 PublicAPIError，而非访问已关闭连接池
        # 静默失败（详见 PUBLIC_API.md FAQ）；MySQL 与查询日志存储一并注销
        register_mysql_manager(None)
        register_config_manager(None)
        # 取消后台初始化任务（若仍在重试中）
        if self._init_task and not self._init_task.done():
            self._init_task.cancel()
            try:
                await self._init_task
            except asyncio.CancelledError:
                pass
        # v0.6.0 停止重载自动补库任务（吞 CancelledError 不阻塞后续清理）
        try:
            await self.backfill.stop()
        except asyncio.CancelledError:
            pass
        # 停止数据分析调度器（LIFO：最后启动的最先停止；吞 CancelledError 不阻塞后续清理）
        try:
            await self.stats_service.stop()
        except asyncio.CancelledError:
            pass
        # 停止总结清理调度器（与 cleaner 停止并列，吞 CancelledError 不阻塞后续清理）
        try:
            await self.summary_service.stop()
        except asyncio.CancelledError:
            pass
        # 停止人物分析清理调度器（与总结停止并列，吞 CancelledError 不阻塞后续清理）
        try:
            await self.profile_service.stop()
        except asyncio.CancelledError:
            pass
        await self.cleaner.stop()
        await self.mysql_mgr.close()
        await self.config_mgr.close()
        logger.info("[HistorySave] 插件已安全停止")


__all__ = ["PluginBootstrap", "MAX_INIT_ATTEMPTS"]
