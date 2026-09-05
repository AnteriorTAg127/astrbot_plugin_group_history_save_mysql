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
from pathlib import Path

from astrbot.api import logger
from astrbot.api.star import StarTools

from .backfill import ReloadBackfill
from .cleaner import ImageCleaner
from .commands import GroupCommands
from .db_config import ConfigManager
from .db_mysql import MySQLManager
from .db_sqlite import SQLiteManager
from .profile.service import ProfileService
from .public_api import register_config_manager, register_mysql_manager
from .saver import MessageSaver
from .stats import StatsService
from .summary import SummaryService
from .webapi import WebAPI

# 后台 MySQL 初始化的最大连续失败次数：超过后放弃重试并停用存储功能，
# 避免数据库长期不可用时无限刷日志。恢复方式：修正配置后在插件管理重启插件。
MAX_INIT_ATTEMPTS = 5

# v0.9.0 迁移器（core/sqlite_migrator.py 由同版本模块 C 提供）：仅 sqlite
# 后端需要；延迟导入避免与模块 C 的并行开发互相阻塞
try:
    from .sqlite_migrator import SQLiteMigrator
except ImportError:  # pragma: no cover - 模块 C 未就位时的降级
    SQLiteMigrator = None

# 存储后端启动锁文件（v0.9.0）。**不能用配置实体承载**：AstrBot
# AstrBotConfig.check_config_integrity 在插件加载时以 _conf_schema.json 派生
# dict 重建配置（astrbot/core/config/astrbot_config.py），schema 外的键会被
# 剥离回写——放 config 侧的锁标记从未生效（开发/v0.9.0/debug/debug_0.md
# PROD-BUG-02，复现脚本 _repro_prod_bug_02.py）。故锁落插件自有数据目录
# data/plugin_data/<plugin>/backend.lock（rules 规则 3：持久化在 data 下）；
# 解锁方式 = 删除该文件后重启插件。
BACKEND_LOCK_FILENAME = "backend.lock"
BACKEND_LOCK_HINT = (
    "data/plugin_data/astrbot_plugin_group_history_save_mysql/backend.lock"
)


def _read_backend_lock() -> str:
    """读启动锁文件内容（同步、幂等、绝不抛出）。

    Returns:
        str: "mysql" / "sqlite"；无锁文件或内容非法一律返回 ""（未锁定）
    """
    try:
        path = (
            Path(StarTools.get_data_dir("astrbot_plugin_group_history_save_mysql"))
            / BACKEND_LOCK_FILENAME
        )
        if not path.is_file():
            return ""
        value = path.read_text(encoding="utf-8").strip().lower()
        return value if value in ("mysql", "sqlite") else ""
    except Exception as e:
        logger.warning(f"[HistorySave] 读取存储后端锁文件失败（按未锁定处理）: {e}")
        return ""


def _write_backend_lock_file(backend: str) -> bool:
    """写启动锁文件（同步、全兜底）。

    Args:
        backend: "mysql" / "sqlite"

    Returns:
        bool: 是否写入成功
    """
    try:
        path = (
            Path(StarTools.get_data_dir("astrbot_plugin_group_history_save_mysql"))
            / BACKEND_LOCK_FILENAME
        )
        path.write_text(backend, encoding="utf-8")
        return True
    except Exception as e:
        logger.error(f"[HistorySave] 存储后端锁文件写入失败: {e}")
        return False


def _resolve_backend(cfg: dict, locked_raw: str) -> tuple[str, bool, bool, str]:
    """存储后端启动锁判定（v0.9.0，纯函数便于测试）。

    锁定语义：后端初始化成功后，插件把生效后端写入自有锁文件
    （backend.lock，**不放配置实体**——schema 外键会被框架剥离，见
    _read_backend_lock 注释与 debug_0.md PROD-BUG-02），此后配置页的
    storage_backend 改动一律不生效——危险选项防护，防止误切换导致
    「看不到另一侧历史」。解锁方式：停用插件、删除 backend.lock、重启。

    三重提醒②的日志由调用方（PluginBootstrap.__init__）打印，本函数只判定。

    Args:
        cfg: 插件配置 dict（读 storage_backend）
        locked_raw: 锁文件原始内容（合法值 mysql/sqlite，其余视为未锁定）

    Returns:
        tuple: (生效后端, lock_mismatch 是否配置与锁不一致被挡回,
        has_lock 是否已锁定, config_backend 配置原始合法值)
    """
    raw = str(cfg.get("storage_backend", "mysql") or "").strip().lower()
    config_backend = raw if raw in ("mysql", "sqlite") else "mysql"
    locked = str(locked_raw or "").strip().lower()
    if locked not in ("mysql", "sqlite"):
        locked = ""
    if not locked:
        # 未锁定：按配置选择，初始化成功后由调用方写锁
        return config_backend, False, False, config_backend
    if locked == config_backend:
        return locked, False, True, config_backend
    # 已锁定且配置被改动：仍按锁定后端启动，仅记录不一致供警告与横幅使用
    return locked, True, True, config_backend


def _parse_bool_cfg(value, default: bool = True) -> bool:
    """配置 bool 显式解析：兼容 bool 与 "true"/"false" 字符串，异常回退默认。"""
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in ("true", "1", "yes", "on"):
        return True
    if s in ("false", "0", "no", "off"):
        return False
    return default


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

        # ---- v0.9.0 存储后端启动锁判定（危险选项，三重提醒②）----
        # backend: 实际生效后端；lock_mismatch: 配置被改动但被锁挡回。
        # 锁读本地小文件（同步、极快），是唯一必要的构造期 I/O——锁判定必须
        # 先于后端构造，无法延后到 async initialize
        backend, self.lock_mismatch, self.locked, self.config_backend = (
            _resolve_backend(cfg, _read_backend_lock())
        )
        self.is_sqlite_backend = backend == "sqlite"
        if self.lock_mismatch:
            # 连发三条 ERROR：醒目且各自给出「现状 / 如何解锁 / 数据后果」
            logger.error(
                f"[HistorySave] ⚠ 检测到存储后端配置（storage_backend="
                f"{self.config_backend}）与已锁定后端（{backend}）不一致："
                f"本插件按启动锁继续使用 {backend}，运行中修改配置不生效。"
            )
            logger.error(
                "[HistorySave] ⚠ 这是一个危险选项。如确需切换：停用本插件，"
                f"删除 {BACKEND_LOCK_HINT} 文件，再重启插件。"
            )
            logger.error(
                "[HistorySave] ⚠ mysql / sqlite 两侧数据完全独立：切换后将"
                "看不到另一侧的历史记录，请先用 /导出聊天记录 完成迁移。"
                "请谨慎操作！"
            )
        # 未锁定时记下待写锁值（后端初始化成功后才落盘，避免锁到一个
        # 根本没连上的后端上）
        self._pending_lock_write = None if self.locked else backend

        if self.is_sqlite_backend:
            # SQLite 备用后端（history.db，无需安装 MySQL）：属性名保留
            # mysql_mgr——下游全部模块按鸭子类型使用同一套同名方法
            self.mysql_mgr = SQLiteManager(
                wal_mode=_parse_bool_cfg(cfg.get("sqlite_wal_mode", True), True),
                busy_timeout_ms=cfg.get("sqlite_busy_timeout_ms", 5000),
            )
        else:
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
        # v0.7.0 对外公共 API 注册：构造存储管理器后立即注册（构造无 I/O，
        # 注册即时生效；后端未连接时对外查询自然失败并记查询日志）
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
        # v0.9.0：SQLite 后端不构造本服务——数据分析依赖 MySQL 侧大量聚合
        # 查询（core/stats/repository.py 走 pool.acquire 直连 + MySQL 方言），
        # 明确不支持，Web/指令侧统一按「未初始化」提示不可用。
        self.stats_service = (
            None
            if self.is_sqlite_backend
            else StatsService(context, self.mysql_mgr, self.config_mgr, plugin)
        )
        # v0.9.0 一次性迁移器：仅 SQLite 模式需要（把 MySQL 历史导入本地）
        self.migrator = (
            SQLiteMigrator(self.mysql_mgr, cfg)
            if (self.is_sqlite_backend and SQLiteMigrator is not None)
            else None
        )

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
            # v0.9.0 存储信息 provider（WebAPI 的 storage/info 端点消费）
            storage_info_provider=self._storage_info,
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
        # 群配置/管理指令执行器（history_* 与 /补库 /导出聊天记录 指令体）
        self.commands = GroupCommands(
            self.mysql_mgr,
            self.config_mgr,
            self.cleaner,
            self.backfill,
            migrator=self.migrator,
        )

        self._init_task: asyncio.Task | None = None

    async def _storage_info(self) -> dict:
        """返回存储后端状态（v0.9.0，Web 面板顶部横幅数据源）。

        Returns:
            dict: {backend, locked, config_backend, lock_mismatch,
            history_db_path(仅 sqlite), stats_available}
        """
        return {
            "backend": "sqlite" if self.is_sqlite_backend else "mysql",
            "locked": self.locked,
            "config_backend": self.config_backend,
            "lock_mismatch": self.lock_mismatch,
            "history_db_path": (
                str(getattr(self.mysql_mgr, "db_path", ""))
                if self.is_sqlite_backend
                else ""
            ),
            "stats_available": not self.is_sqlite_backend,
        }

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
        # v0.9.0：双后端共用本重试循环，日志文案按生效后端替换
        backend_name = "SQLite" if self.is_sqlite_backend else "MySQL"
        attempt = 0
        while not self.saver.is_initialized:
            attempt += 1
            try:
                mysql_ok = await asyncio.wait_for(
                    self.mysql_mgr.initialize(), timeout=init_timeout
                )
                if mysql_ok:
                    # v0.9.0 启动锁落盘：后端初始化成功后才写 backend.lock，
                    # 危险选项自此锁定；写失败仅记日志不阻断
                    self._write_backend_lock()
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
                    # v0.9.0：SQLite 后端不启动数据分析（stats_service 为 None）
                    if self.stats_service is not None:
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
                        f"[HistorySave] {backend_name} "
                        + (
                            "存储初始化成功（history.db），插件初始化完成，开始监听群消息"
                            if self.is_sqlite_backend
                            else "连接成功，插件初始化完成，开始监听群消息"
                        )
                    )
                    return
                else:
                    logger.warning(
                        f"[HistorySave] {backend_name} 连接失败（第 {attempt} 次尝试），"
                        f"{retry_interval}s 后重试..."
                    )
            except asyncio.TimeoutError:
                logger.warning(
                    f"[HistorySave] {backend_name} 初始化超时（第 {attempt} 次尝试），"
                    f"{retry_interval}s 后重试..."
                )
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning(
                    f"[HistorySave] {backend_name} 初始化异常（第 {attempt} 次尝试）: {e}，"
                    f"{retry_interval}s 后重试..."
                )
            # 重试耗尽：放弃并停用存储功能（关闭连接池使后续操作快速失败）
            if attempt >= MAX_INIT_ATTEMPTS:
                # 明确记录被丢弃的缓冲消息条数，避免静默丢失无迹可查（F6）
                dropped = self.saver.mark_gave_up()
                logger.error(
                    f"[HistorySave] {backend_name} 连续 {attempt} 次连接失败，停止重试，"
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

    def _write_backend_lock(self) -> None:
        """把生效后端写入启动锁文件 backend.lock（v0.9.0 启动锁落盘）。

        仅在「未锁定 → 首次锁定」时写一次；已锁定的实例不重写。
        **落本地文件而非配置实体**：AstrBot 配置完整性检查会剥离
        schema 外的键（debug_0.md PROD-BUG-02），写进配置等于没写。
        写失败仅记日志不阻断本次运行（下次启动按配置重新判定）。
        """
        if not self._pending_lock_write:
            return
        backend = self._pending_lock_write
        if _write_backend_lock_file(backend):
            logger.info(
                f"[HistorySave] 存储后端已锁定为 {backend}"
                f"（{BACKEND_LOCK_HINT}，危险选项，勿随意更改；"
                f"解锁 = 停用插件并删除该文件）"
            )
            self._pending_lock_write = None
            self.locked = True

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
        # v0.9.0：SQLite 后端 stats_service 本就是 None，跳过
        if self.stats_service is not None:
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
