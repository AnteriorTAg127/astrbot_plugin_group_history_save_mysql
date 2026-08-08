"""MySQL 数据库操作层（基础部分）。

负责连接池（DynamicPool）组装、统一 SQL 执行入口、建表与索引创建、
表结构迁移与必需列校验等核心初始化；模块常量集中定义于此。
（v0.6.0 起，由根目录 db_mysql.py 拆分为 core/db_mysql/ 包。）
"""

import asyncio
import time

from astrbot.api import logger

# 单条 SQL 执行超时兜底（秒）：aiomysql 无可靠的客户端读写超时，
# 业务查询统一经 asyncio.wait_for 兜底；超时后连接视为协议状态不可信，
# 由归还路径销毁（连接上可能残留未读完的响应字节）
QUERY_TIMEOUT_SECONDS = 30.0

# 建表/迁移/清空等维护性 DDL 的执行超时（秒）：大表 ADD INDEX / DELETE
# 可能耗时数分钟，不能用业务查询超时兜住，否则 DDL 被中途砍断后反复重试
# 始终无法完成（与外层初始化超时的放宽相配合）
DDL_TIMEOUT_SECONDS = 300.0

# 建连失败后的退避重试间隔（秒）：可取消的短 sleep，在 acquire_timeout
# 窗口内换回瞬时故障的重试机会（见 _get_connection create 模式）
CREATE_RETRY_BACKOFF_SECONDS = 1.0

# initialize() 复位前等待在途操作（_pending）归零的最长秒数：
# 超时记 warning 并强制复位（见 DynamicPool.initialize）
RESET_PENDING_WAIT_SECONDS = 20.0

# DynamicPool 定义于 pool.py，其依赖的模块常量已在上方定义；
# 后置导入化解 base ↔ pool 的循环依赖（包 __init__ 先导入 base 再导入 pool）
from .pool import DynamicPool  # noqa: E402


class MySQLManagerBase:
    """MySQL 数据库管理器（基础部分），使用动态连接池管理所有数据库操作。

    连接池组装、统一执行入口、建表/迁移/必需列校验与 ping/close 等核心
    逻辑集中于此；子功能（聊天记录/图片/统计/维护）由各 Mixin 提供，
    最终经 core/db_mysql/__init__.py 多继承组装为 MySQLManager。
    """

    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        database: str,
        pool_min_size: int = 1,
        pool_max_size: int = 10,
        pool_idle_timeout: int = 120,
        pool_timeout: int = 30,
        pool_ping_cooldown: int = 5,
    ):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.database = database
        self.pool = DynamicPool(
            host=host,
            port=port,
            user=user,
            password=password,
            db=database,
            min_size=pool_min_size,
            max_size=pool_max_size,
            idle_timeout=pool_idle_timeout,
            acquire_timeout=pool_timeout,
            ping_cooldown=pool_ping_cooldown,
        )
        # 查询类操作错误日志节流状态：key -> (上次输出时间, 窗口内累计次数)
        self._op_err_state: dict[str, tuple[float, int]] = {}

    def _log_op_error(self, key: str, action: str, exc: Exception):
        """查询类操作的错误日志节流（60s 窗口，按 key 独立累计）。

        数据库不可用/连接池关闭后，Web 后台轮询会高频触发这些操作失败，
        逐条记录会刷屏。首次失败立即输出 ERROR，窗口内的后续次数累计后
        在下一条日志中一并汇报。写入类操作（insert/clean/purge）低频且
        每次失败都意味着真实数据丢失，不走此节流。
        """
        now = time.monotonic()
        last, count = self._op_err_state.get(key, (0.0, 0))
        count += 1
        if now - last < 60:
            self._op_err_state[key] = (last, count)
            return
        self._op_err_state[key] = (now, 0)
        logger.error(f"[HistorySave] {action}失败（近期累计 {count} 次）: {exc}")

    async def initialize(self) -> bool:
        """初始化连接池、创建表结构并迁移旧表结构。

        连接池已尝试预创建 1 个连接；若预创建失败，再用 2 秒短超时探测一次。
        探测失败立即返回 False，避免长时间阻塞。

        Returns:
            bool: 初始化是否成功
        """
        try:
            await self.pool.initialize()
            # 预创建成功则跳过探测；否则 2 秒短超时探测
            if self.pool.free_size == 0 and not await self._probe_connection(timeout=2):
                logger.warning("[HistorySave] MySQL 连接探测失败，数据库当前不可用")
                return False
            await self._create_tables()
            await self._migrate_schema()
            # F7/K2：迁移后校验 INSERT/SELECT 硬依赖的必需列确实存在；
            # 缺失（如 ALTER 权限不足）时显式返回 False，走既有重试/放弃流程
            if not await self._verify_required_columns():
                return False
            logger.info("[HistorySave] MySQL 动态连接池初始化成功")
            return True
        except Exception as e:
            logger.error(f"[HistorySave] MySQL 初始化失败: {e}")
            return False

    async def _probe_connection(self, timeout: int = 3) -> bool:
        """尝试在指定超时内创建一个临时连接，验证 MySQL 是否可达。

        这个临时连接不在池子的 _free/_used 中，用完即销毁。
        通过 _record_destroyed 更新计数器，保证 created - recycled = pool size。

        Args:
            timeout: 最大等待秒数

        Returns:
            bool: 是否连接成功
        """
        try:
            conn = await asyncio.wait_for(
                self.pool._create_connection(), timeout=timeout
            )
            try:
                await conn.ping(reconnect=False)
                return True
            finally:
                self.pool._record_destroyed(conn)
        except asyncio.TimeoutError:
            logger.warning(f"[HistorySave] MySQL 连接探测超时 ({timeout}s)")
            return False
        except Exception as e:
            logger.warning(f"[HistorySave] MySQL 连接探测失败: {e}")
            return False

    async def _execute(
        self,
        cur,
        sql: str,
        params: tuple | list | None = None,
        timeout: float | None = None,
    ):
        """统一 SQL 执行入口：execute 外包 asyncio.wait_for 超时兜底（F9）。

        aiomysql 无可靠的客户端读写超时（各版本行为不一），查询长时间挂起
        会占死连接、拖垮连接池，故统一以 wait_for 兜底：
        - 超时抛 TimeoutError，经借用体上抛穿过 acquire——acquire 将
          CancelledError/TimeoutError 判定为「连接协议状态不可信」，归还时
          销毁该连接而非放回池（连接上可能残留未读完的响应字节）；
        - 普通 SQL 错误（语法/约束等）说明该语句协议交互已完整结束，
          连接归还与复用不受影响；
        - 默认（缓冲）游标下 fetch 只是读取 execute 已取回的结果，
          网络等待集中在 execute，兜底只需包这一层。

        Args:
            cur: 数据库游标
            sql: SQL 语句
            params: SQL 参数
            timeout: 超时秒数，缺省 QUERY_TIMEOUT_SECONDS；
                建表/迁移/清空等长耗时 DDL 应显式传 DDL_TIMEOUT_SECONDS
        """
        return await asyncio.wait_for(
            cur.execute(sql, params),
            timeout=QUERY_TIMEOUT_SECONDS if timeout is None else timeout,
        )

    async def _create_tables(self):
        """创建聊天记录表和图片记录表（含索引）。"""
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                # 聊天记录表
                await self._execute(
                    cur,
                    """
                    CREATE TABLE IF NOT EXISTS chat_history (
                        id BIGINT AUTO_INCREMENT PRIMARY KEY,
                        timestamp DATETIME NOT NULL,
                        group_id VARCHAR(32) NOT NULL,
                        sender_id VARCHAR(32) NOT NULL,
                        sender_name VARCHAR(128) DEFAULT '',
                        message_type VARCHAR(16) DEFAULT 'text',
                        content TEXT,
                        message_id VARCHAR(64) DEFAULT '',
                        at_list VARCHAR(512) DEFAULT '',
                        reply_id VARCHAR(64) DEFAULT '',
                        INDEX idx_group_time (group_id, timestamp),
                        INDEX idx_sender_time (sender_id, timestamp),
                        INDEX idx_group_sender_time (group_id, sender_id, timestamp),
                        INDEX idx_message_id (message_id),
                        INDEX idx_timestamp (timestamp)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """,
                    timeout=DDL_TIMEOUT_SECONDS,
                )
                # 图片记录表
                await self._execute(
                    cur,
                    """
                    CREATE TABLE IF NOT EXISTS image_records (
                        id BIGINT AUTO_INCREMENT PRIMARY KEY,
                        timestamp DATETIME NOT NULL,
                        group_id VARCHAR(32) NOT NULL,
                        sender_id VARCHAR(32) NOT NULL,
                        sender_name VARCHAR(128) NOT NULL DEFAULT '',
                        image_url VARCHAR(1024) NOT NULL,
                        INDEX idx_img_time (timestamp),
                        INDEX idx_img_group (group_id)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """,
                    timeout=DDL_TIMEOUT_SECONDS,
                )

    async def _migrate_schema(self):
        """将旧表结构迁移到当前版本。

        检测并迁移内容：
        - v0.2：chat_history / image_records 的 group_id、sender_id 列若不是字符串类型
          （varchar/char/text），MODIFY 为 VARCHAR(32) NOT NULL
          （MySQL 会自动把已有数字值转为字符串，不丢数据，列上索引自动重建）
        - v0.2：image_records 缺少 sender_name 列时自动 ADD COLUMN
        - v0.4.0：chat_history 缺少 at_list / reply_id 列时自动 ADD COLUMN
          （人物分析所需 @ 对象 / 回复目标标记；MySQL 无 ADD COLUMN IF NOT EXISTS
          语法，先查 INFORMATION_SCHEMA.COLUMNS 判定缺列再 ALTER，重复执行幂等）
        - v0.4.1/v0.5.2：chat_history 必需索引幂等补建（idx_group_time /
          idx_sender_time / idx_group_sender_time / idx_message_id / idx_timestamp；
          旧表由早期版本建出时 CREATE TABLE IF NOT EXISTS 为 no-op，复合索引缺失
          导致按群查询全表扫描，先查 INFORMATION_SCHEMA.STATISTICS 再 ALTER）

        每一步独立 try/except 包裹，失败仅记录日志（warning/error），
        绝不向上抛异常、不阻断插件启动。
        """
        try:
            async with self.pool.acquire() as conn:
                async with conn.cursor() as cur:
                    # 需要检测为字符串类型的列: (表名, 列名)
                    varchar_columns = [
                        ("chat_history", "group_id"),
                        ("chat_history", "sender_id"),
                        ("image_records", "group_id"),
                        ("image_records", "sender_id"),
                    ]
                    for table, column in varchar_columns:
                        try:
                            await self._execute(
                                cur,
                                "SELECT DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS"
                                " WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s",
                                (table, column),
                                timeout=DDL_TIMEOUT_SECONDS,
                            )
                            row = await cur.fetchone()
                            if row and str(row[0]).lower() not in (
                                "varchar",
                                "char",
                                "text",
                            ):
                                await self._execute(
                                    cur,
                                    f"ALTER TABLE {table} MODIFY COLUMN {column} VARCHAR(32) NOT NULL",
                                    timeout=DDL_TIMEOUT_SECONDS,
                                )
                                logger.info(
                                    f"[HistorySave] 表结构迁移: {table}.{column} 已改为 VARCHAR(32)"
                                )
                        except Exception as e:
                            logger.error(
                                f"[HistorySave] 表结构迁移 {table}.{column} 失败: {e}"
                            )

                    # 检测 image_records 是否缺少 sender_name 列
                    try:
                        await self._execute(
                            cur,
                            "SELECT DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS"
                            " WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s",
                            ("image_records", "sender_name"),
                            timeout=DDL_TIMEOUT_SECONDS,
                        )
                        row = await cur.fetchone()
                        if not row:
                            await self._execute(
                                cur,
                                "ALTER TABLE image_records ADD COLUMN sender_name VARCHAR(128) NOT NULL DEFAULT ''",
                                timeout=DDL_TIMEOUT_SECONDS,
                            )
                            logger.info(
                                "[HistorySave] 表结构迁移: image_records 已新增 sender_name 列"
                            )
                    except Exception as e:
                        logger.error(
                            f"[HistorySave] 表结构迁移 image_records.sender_name 失败: {e}"
                        )

                    # v0.4.0：chat_history 新增 at_list / reply_id 两列（人物分析
                    # 关系上下文数据源）。ADD COLUMN 无 IF NOT EXISTS 语法，
                    # 先查 INFORMATION_SCHEMA.COLUMNS 判定缺列再 ALTER，幂等可重入。
                    # 存量行该两列为 NULL，下游按 .get 兜底，summary 消费 content 不受影响。
                    new_columns = [
                        ("at_list", "VARCHAR(512) DEFAULT ''"),
                        ("reply_id", "VARCHAR(64) DEFAULT ''"),
                    ]
                    for column, column_def in new_columns:
                        try:
                            await self._execute(
                                cur,
                                "SELECT DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS"
                                " WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s",
                                ("chat_history", column),
                                timeout=DDL_TIMEOUT_SECONDS,
                            )
                            row = await cur.fetchone()
                            if not row:
                                await self._execute(
                                    cur,
                                    f"ALTER TABLE chat_history ADD COLUMN {column} {column_def}",
                                    timeout=DDL_TIMEOUT_SECONDS,
                                )
                                logger.info(
                                    f"[HistorySave] 表结构迁移: chat_history 已新增 {column} 列"
                                )
                        except Exception as e:
                            logger.warning(
                                f"[HistorySave] 表结构迁移 chat_history.{column} 失败: {e}"
                            )

                    # v0.5.2：必需索引统一幂等补建。复合索引只存在于
                    # CREATE TABLE IF NOT EXISTS 的 DDL 中，早期版本已建出的旧表
                    # 该 DDL 为 no-op，索引永久缺失——按群查询（数据分析/查询页
                    # 的 WHERE group_id = … AND timestamp …、全表 GROUP BY）全部
                    # 退化为全表扫描，大表上吃满 CPU。ADD INDEX 无 IF NOT EXISTS
                    # 语法，先查 INFORMATION_SCHEMA.STATISTICS 判定索引存在与否
                    # 再 ALTER，幂等可重入；MySQL 8.0 ADD INDEX 为在线 DDL
                    # （INPLACE），几百万行也不阻塞持续写入。
                    required_indexes = [
                        ("chat_history", "idx_group_time", "group_id, timestamp"),
                        ("chat_history", "idx_sender_time", "sender_id, timestamp"),
                        (
                            "chat_history",
                            "idx_group_sender_time",
                            "group_id, sender_id, timestamp",
                        ),
                        ("chat_history", "idx_message_id", "message_id"),
                        # 时间窗聚合（概览每日趋势/群排行/快照）无群条件时
                        # 走 timestamp 前缀范围扫描，避免全表扫描
                        ("chat_history", "idx_timestamp", "timestamp"),
                    ]
                    for table, index_name, columns in required_indexes:
                        try:
                            await self._execute(
                                cur,
                                "SELECT INDEX_NAME FROM INFORMATION_SCHEMA.STATISTICS"
                                " WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND INDEX_NAME = %s"
                                " LIMIT 1",
                                (table, index_name),
                                timeout=DDL_TIMEOUT_SECONDS,
                            )
                            row = await cur.fetchone()
                            if not row:
                                await self._execute(
                                    cur,
                                    f"ALTER TABLE {table} ADD INDEX {index_name} ({columns})",
                                    timeout=DDL_TIMEOUT_SECONDS,
                                )
                                logger.info(
                                    f"[HistorySave] 表结构迁移: {table} 已新增 {index_name} 索引"
                                )
                        except Exception as e:
                            logger.warning(
                                f"[HistorySave] 表结构迁移 {table}.{index_name} 失败: {e}"
                            )
        except Exception as e:
            logger.error(f"[HistorySave] 表结构迁移失败（无法获取连接）: {e}")

    async def _verify_required_columns(self) -> bool:
        """校验 chat_history 必需列（at_list / reply_id）确实存在（F7）。

        这两列是 INSERT/SELECT 的硬依赖：迁移 ADD COLUMN 失败（常见原因：
        MySQL 账号无 ALTER 权限）后若照常启动，后续所有读写都会报错。
        缺失时记 ERROR 并返回 False，让 initialize() 走既有的重试/放弃流程。

        探测自身容错：取连接或查询异常同样返回 False（按缺失处理），
        探测 SQL 不会再抛出异常影响初始化链路。

        Returns:
            bool: 必需列全部存在返回 True
        """
        # 与 _migrate_schema 中的列定义保持一致，供授权提示引用
        required_columns = {
            "at_list": "VARCHAR(512) DEFAULT ''",
            "reply_id": "VARCHAR(64) DEFAULT ''",
        }
        try:
            async with self.pool.acquire() as conn:
                async with conn.cursor() as cur:
                    for column, column_def in required_columns.items():
                        await self._execute(
                            cur,
                            "SELECT DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS"
                            " WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s",
                            ("chat_history", column),
                        )
                        row = await cur.fetchone()
                        if not row:
                            logger.error(
                                f"[HistorySave] chat_history 缺少必需列 {column}，"
                                f"消息入库与查询将全部失败。请检查 MySQL 账号是否具备 "
                                f"ALTER TABLE 权限，授权后手动执行: "
                                f"ALTER TABLE chat_history ADD COLUMN {column} {column_def}，"
                                f"然后重启重试"
                            )
                            return False
            return True
        except Exception as e:
            logger.error(
                f"[HistorySave] 校验 chat_history 必需列失败（按缺失处理）: {e}"
            )
            return False

    async def ping(self) -> dict:
        """检测数据库连接状态。

        Returns:
            dict: {"connected": bool, "latency_ms": float, "pool": dict}
        """
        try:
            start = time.time()
            async with self.pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await self._execute(cur, "SELECT 1")
            latency = (time.time() - start) * 1000
            return {
                "connected": True,
                "latency_ms": round(latency, 2),
                "pool": self.pool.get_pool_info(),
            }
        except Exception:
            return {
                "connected": False,
                "latency_ms": -1,
                "pool": self.pool.get_pool_info(),
            }

    async def close(self):
        """关闭连接池。"""
        await self.pool.close()
        logger.info("[HistorySave] MySQL 动态连接池已关闭")
