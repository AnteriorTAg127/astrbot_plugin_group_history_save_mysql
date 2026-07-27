"""MySQL 数据库操作层。

负责管理动态连接池、建表、索引创建，以及聊天记录和图片记录的增删查操作。
连接池支持自动扩容（按需创建连接至上限）和自动缩容（回收空闲连接至下限）。
"""

import asyncio
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import aiomysql

from astrbot.api import logger


class DynamicPool:
    """动态 MySQL 连接池。

    特性：
    - 按需创建连接，高并发时自动扩容至 max_size
    - 空闲连接超过 idle_timeout 后自动回收（保留 min_size 个）
    - 后台 reaper 任务定期巡检
    - 连接健康检查（ping），失效连接自动替换
    """

    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        db: str,
        min_size: int = 1,
        max_size: int = 10,
        idle_timeout: int = 120,
        acquire_timeout: int = 30,
        pool_recycle: int = 3600,
    ):
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._db = db
        self._min_size = max(1, min_size)
        self._max_size = max(self._min_size, max_size)
        self._idle_timeout = idle_timeout
        self._acquire_timeout = acquire_timeout
        self._pool_recycle = pool_recycle

        # 连接存储: list of (connection, last_used_timestamp)
        self._free: list[tuple[aiomysql.Connection, float]] = []
        self._used: set[aiomysql.Connection] = set()
        # 注意：asyncio 同步原语必须在事件循环内创建，延迟到 initialize()
        self._lock: asyncio.Lock | None = None
        self._not_empty: asyncio.Condition | None = None
        self._closed = False
        self._reaper_task: asyncio.Task | None = None

        # 统计
        self._total_created = 0
        self._total_recycled = 0

    @property
    def size(self) -> int:
        """当前总连接数（空闲 + 使用中）。"""
        return len(self._free) + len(self._used)

    @property
    def free_size(self) -> int:
        """空闲连接数。"""
        return len(self._free)

    @property
    def used_size(self) -> int:
        """使用中连接数。"""
        return len(self._used)

    async def _create_connection(self) -> aiomysql.Connection:
        """创建一个新的数据库连接。"""
        conn = await aiomysql.connect(
            host=self._host,
            port=self._port,
            user=self._user,
            password=self._password,
            db=self._db,
            charset="utf8mb4",
            autocommit=True,
            connect_timeout=10,
        )
        self._total_created += 1
        return conn

    async def initialize(self):
        """初始化连接池，预创建 min_size 个连接并启动 reaper。"""
        # 在事件循环内创建 asyncio 同步原语（避免插件重载时 loop 不一致）
        self._lock = asyncio.Lock()
        self._not_empty = asyncio.Condition(self._lock)

        for _ in range(self._min_size):
            try:
                conn = await self._create_connection()
                self._free.append((conn, time.monotonic()))
            except Exception as e:
                logger.warning(f"[DynamicPool] 预创建连接失败: {e}")
        self._reaper_task = asyncio.create_task(self._reaper_loop())
        logger.info(
            f"[DynamicPool] 初始化完成 | min={self._min_size} "
            f"max={self._max_size} idle_timeout={self._idle_timeout}s "
            f"| 已预创建 {len(self._free)} 个连接"
        )

    async def _is_alive(self, conn: aiomysql.Connection) -> bool:
        """检查连接是否存活。"""
        try:
            await conn.ping(reconnect=False)
            return True
        except Exception:
            return False

    async def _ensure_connection(
        self, conn: aiomysql.Connection
    ) -> aiomysql.Connection:
        """确保连接可用，失效则重建。"""
        if conn.closed:
            return await self._create_connection()
        if not await self._is_alive(conn):
            try:
                conn.close()
            except Exception:
                pass
            return await self._create_connection()
        return conn

    @asynccontextmanager
    async def acquire(self):
        """获取一个连接（上下文管理器）。

        如果无空闲连接且未达上限，自动创建新连接（扩容）。
        如果已达上限，等待直到有连接释放或超时。
        """
        conn = await self._get_connection()
        try:
            yield conn
        finally:
            await self._release_connection(conn)

    async def _get_connection(self) -> aiomysql.Connection:
        """从池中获取一个可用连接。"""
        if self._closed:
            raise RuntimeError("连接池已关闭")

        deadline = time.monotonic() + self._acquire_timeout

        async with self._not_empty:
            while True:
                # 尝试从空闲连接中取一个
                while self._free:
                    conn, _ = self._free.pop(0)
                    conn = await self._ensure_connection(conn)
                    self._used.add(conn)
                    return conn

                # 无空闲连接，尝试扩容
                if self.size < self._max_size:
                    try:
                        conn = await self._create_connection()
                        self._used.add(conn)
                        return conn
                    except Exception as e:
                        logger.error(f"[DynamicPool] 扩容创建连接失败: {e}")

                # 已达上限，等待释放
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"[DynamicPool] 获取连接超时 ({self._acquire_timeout}s)，"
                        f"当前连接数: {self.size}/{self._max_size}"
                    )
                try:
                    await asyncio.wait_for(self._not_empty.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    raise TimeoutError(
                        f"[DynamicPool] 获取连接超时 ({self._acquire_timeout}s)，"
                        f"当前连接数: {self.size}/{self._max_size}"
                    )

    async def _release_connection(self, conn: aiomysql.Connection):
        """归还连接到池中。"""
        async with self._not_empty:
            self._used.discard(conn)
            if self._closed or conn.closed:
                try:
                    conn.close()
                except Exception:
                    pass
            else:
                self._free.append((conn, time.monotonic()))
            self._not_empty.notify()

    async def _reaper_loop(self):
        """后台巡检任务：回收空闲超时连接、检查连接健康。"""
        while not self._closed:
            try:
                await asyncio.sleep(30)  # 每 30 秒巡检一次
                await self._reap_idle_connections()
                await self._health_check()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[DynamicPool] Reaper 异常: {e}")

    async def _reap_idle_connections(self):
        """回收超过 idle_timeout 的空闲连接（保留 min_size 个）。"""
        now = time.monotonic()
        to_close = []

        async with self._lock:
            # 只回收超出 min_size 的空闲连接
            while (
                len(self._free) > self._min_size
                and self._free
                and (now - self._free[0][1]) > self._idle_timeout
            ):
                conn, _ = self._free.pop(0)
                to_close.append(conn)

        for conn in to_close:
            try:
                conn.close()
            except Exception:
                pass
            self._total_recycled += 1

        if to_close:
            logger.info(
                f"[DynamicPool] 回收 {len(to_close)} 个空闲连接 | "
                f"当前: free={self.free_size} used={self.used_size}"
            )

    async def _health_check(self):
        """检查空闲连接健康状态，移除失效连接。"""
        to_remove = []
        async with self._lock:
            for conn, ts in self._free:
                if conn.closed or not await self._is_alive(conn):
                    to_remove.append((conn, ts))
            for item in to_remove:
                self._free.remove(item)

        for conn, _ in to_remove:
            try:
                conn.close()
            except Exception:
                pass

        # 如果低于 min_size，补充连接
        deficit = self._min_size - self.size
        if deficit > 0:
            for _ in range(deficit):
                try:
                    conn = await self._create_connection()
                    async with self._lock:
                        self._free.append((conn, time.monotonic()))
                except Exception:
                    break

    def get_pool_info(self) -> dict:
        """获取连接池状态信息。"""
        return {
            "min_size": self._min_size,
            "max_size": self._max_size,
            "current_size": self.size,
            "free": self.free_size,
            "used": self.used_size,
            "total_created": self._total_created,
            "total_recycled": self._total_recycled,
            "idle_timeout": self._idle_timeout,
        }

    async def close(self):
        """关闭连接池，释放所有连接。"""
        self._closed = True
        if self._reaper_task:
            self._reaper_task.cancel()
            try:
                await self._reaper_task
            except asyncio.CancelledError:
                pass

        # 如果 initialize() 未执行，_lock 为 None，直接清理即可
        if self._lock is None:
            for conn, _ in self._free:
                try:
                    conn.close()
                except Exception:
                    pass
            self._free.clear()
            return

        async with self._lock:
            for conn, _ in self._free:
                try:
                    conn.close()
                except Exception:
                    pass
            self._free.clear()

            for conn in self._used:
                try:
                    conn.close()
                except Exception:
                    pass
            self._used.clear()

        logger.info("[DynamicPool] 连接池已关闭")


class MySQLManager:
    """MySQL 数据库管理器，使用动态连接池管理所有数据库操作。"""

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
        )

    async def initialize(self) -> bool:
        """初始化连接池、创建表结构并迁移旧表结构。

        Returns:
            bool: 初始化是否成功
        """
        try:
            await self.pool.initialize()
            await self._create_tables()
            await self._migrate_schema()
            logger.info("[HistorySave] MySQL 动态连接池初始化成功")
            return True
        except Exception as e:
            logger.error(f"[HistorySave] MySQL 初始化失败: {e}")
            return False

    async def _create_tables(self):
        """创建聊天记录表和图片记录表（含索引）。"""
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                # 聊天记录表
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS chat_history (
                        id BIGINT AUTO_INCREMENT PRIMARY KEY,
                        timestamp DATETIME NOT NULL,
                        group_id VARCHAR(32) NOT NULL,
                        sender_id VARCHAR(32) NOT NULL,
                        sender_name VARCHAR(128) DEFAULT '',
                        message_type VARCHAR(16) DEFAULT 'text',
                        content TEXT,
                        message_id VARCHAR(64) DEFAULT '',
                        INDEX idx_group_time (group_id, timestamp),
                        INDEX idx_sender_time (sender_id, timestamp),
                        INDEX idx_group_sender_time (group_id, sender_id, timestamp)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """)
                # 图片记录表
                await cur.execute("""
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
                """)

    async def _migrate_schema(self):
        """将 v0.1 旧表结构迁移到 v0.2 新结构。

        检测并迁移内容：
        - chat_history / image_records 的 group_id、sender_id 列若不是字符串类型
          （varchar/char/text），MODIFY 为 VARCHAR(32) NOT NULL
          （MySQL 会自动把已有数字值转为字符串，不丢数据，列上索引自动重建）
        - image_records 缺少 sender_name 列时自动 ADD COLUMN

        每一步独立 try/except 包裹，失败仅记录 error 日志，
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
                            await cur.execute(
                                "SELECT DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS"
                                " WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s",
                                (table, column),
                            )
                            row = await cur.fetchone()
                            if row and str(row[0]).lower() not in (
                                "varchar",
                                "char",
                                "text",
                            ):
                                await cur.execute(
                                    f"ALTER TABLE {table} MODIFY COLUMN {column} VARCHAR(32) NOT NULL"
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
                        await cur.execute(
                            "SELECT DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS"
                            " WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s",
                            ("image_records", "sender_name"),
                        )
                        row = await cur.fetchone()
                        if not row:
                            await cur.execute(
                                "ALTER TABLE image_records ADD COLUMN sender_name VARCHAR(128) NOT NULL DEFAULT ''"
                            )
                            logger.info(
                                "[HistorySave] 表结构迁移: image_records 已新增 sender_name 列"
                            )
                    except Exception as e:
                        logger.error(
                            f"[HistorySave] 表结构迁移 image_records.sender_name 失败: {e}"
                        )
        except Exception as e:
            logger.error(f"[HistorySave] 表结构迁移失败（无法获取连接）: {e}")

    async def insert_chat_message(
        self,
        group_id: str,
        sender_id: str,
        sender_name: str,
        message_type: str,
        content: str,
        message_id: str,
    ) -> bool:
        """插入一条聊天记录。

        Args:
            group_id: 群号（字符串形式）
            sender_id: 发送者 QQ 号（字符串形式）
            sender_name: 发送者昵称
            message_type: 消息类型（text/image/mixed）
            content: 文本内容
            message_id: 消息 ID

        Returns:
            bool: 是否插入成功
        """
        try:
            async with self.pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """INSERT INTO chat_history
                           (timestamp, group_id, sender_id, sender_name, message_type, content, message_id)
                           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                        (
                            datetime.now(),
                            group_id,
                            sender_id,
                            sender_name,
                            message_type,
                            content,
                            message_id,
                        ),
                    )
            return True
        except Exception as e:
            logger.error(f"[HistorySave] 插入聊天记录失败: {e}")
            return False

    async def insert_image_record(
        self, group_id: str, sender_id: str, image_url: str, sender_name: str = ""
    ) -> bool:
        """插入一条图片记录。

        Args:
            group_id: 群号（字符串形式）
            sender_id: 发送者 QQ 号（字符串形式）
            image_url: 图片 URL
            sender_name: 发送者昵称（可选，默认空字符串）

        Returns:
            bool: 是否插入成功
        """
        try:
            async with self.pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """INSERT INTO image_records
                           (timestamp, group_id, sender_id, sender_name, image_url)
                           VALUES (%s, %s, %s, %s, %s)""",
                        (datetime.now(), group_id, sender_id, sender_name, image_url),
                    )
            return True
        except Exception as e:
            logger.error(f"[HistorySave] 插入图片记录失败: {e}")
            return False

    async def clean_old_images(self, retention_days: int) -> int:
        """清理指定天数之前的图片记录。

        Args:
            retention_days: 保留天数

        Returns:
            int: 删除的记录数，失败返回 -1
        """
        try:
            cutoff = datetime.now() - timedelta(days=retention_days)
            async with self.pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "DELETE FROM image_records WHERE timestamp < %s",
                        (cutoff,),
                    )
                    return cur.rowcount
        except Exception as e:
            logger.error(f"[HistorySave] 清理图片记录失败: {e}")
            return -1

    async def purge_all(self) -> dict:
        """清空所有聊天记录和图片记录（不可恢复），并复位自增 ID。

        用 DELETE 清空（保留删除条数供前端提示），随后对每张表执行
        ``ALTER TABLE ... AUTO_INCREMENT = 1`` 复位自增主键——因为 InnoDB 的
        DELETE 不会自动复位自增计数，不复位会导致清空后新数据 ID 从旧最大值继续。
        复位语句各自独立 try/except，失败仅记 warning，不影响清空本身。

        Returns:
            dict: {"success": bool, "deleted_messages": int, "deleted_images": int}
        """
        result = {"success": False, "deleted_messages": 0, "deleted_images": 0}
        try:
            async with self.pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("DELETE FROM chat_history")
                    result["deleted_messages"] = cur.rowcount
                    await self._reset_auto_increment(cur, "chat_history")
                    await cur.execute("DELETE FROM image_records")
                    result["deleted_images"] = cur.rowcount
                    await self._reset_auto_increment(cur, "image_records")
            result["success"] = True
            logger.warning(
                f"[HistorySave] 已清空所有数据: 聊天记录 {result['deleted_messages']} 条, "
                f"图片记录 {result['deleted_images']} 条"
            )
        except Exception as e:
            logger.error(f"[HistorySave] 清空所有数据失败: {e}")
        return result

    async def _reset_auto_increment(self, cur, table: str) -> None:
        """表清空后将自增 ID 复位到 1。

        DELETE 不会复位 InnoDB 自增计数，需显式 ALTER。表名来自本类硬编码，无注入风险。
        权限不足等异常仅记 warning，不向上抛（清空已成功，只是 ID 未复位）。
        """
        try:
            await cur.execute(f"ALTER TABLE {table} AUTO_INCREMENT = 1")
        except Exception as e:
            logger.warning(
                f"[HistorySave] 复位 {table} 自增 ID 失败（不影响清空）: {e}"
            )

    async def get_stats(self) -> dict:
        """获取统计信息（今日消息数、今日图片数、总消息数、总图片数）。

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
            today = datetime.now().strftime("%Y-%m-%d")
            async with self.pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT COUNT(*) FROM chat_history WHERE DATE(timestamp) = %s",
                        (today,),
                    )
                    row = await cur.fetchone()
                    stats["today_messages"] = row[0] if row else 0

                    await cur.execute(
                        "SELECT COUNT(*) FROM image_records WHERE DATE(timestamp) = %s",
                        (today,),
                    )
                    row = await cur.fetchone()
                    stats["today_images"] = row[0] if row else 0

                    await cur.execute("SELECT COUNT(*) FROM chat_history")
                    row = await cur.fetchone()
                    stats["total_messages"] = row[0] if row else 0

                    await cur.execute("SELECT COUNT(*) FROM image_records")
                    row = await cur.fetchone()
                    stats["total_images"] = row[0] if row else 0
        except Exception as e:
            logger.error(f"[HistorySave] 获取统计信息失败: {e}")
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
            async with self.pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """SELECT DATE(timestamp) as date, COUNT(*) as count
                           FROM chat_history
                           WHERE timestamp >= DATE_SUB(CURDATE(), INTERVAL %s DAY)
                           GROUP BY DATE(timestamp) ORDER BY date""",
                        (days,),
                    )
                    msg_rows = await cur.fetchall()
                    msg_map = {str(row[0]): row[1] for row in msg_rows}

                    await cur.execute(
                        """SELECT DATE(timestamp) as date, COUNT(*) as count
                           FROM image_records
                           WHERE timestamp >= DATE_SUB(CURDATE(), INTERVAL %s DAY)
                           GROUP BY DATE(timestamp) ORDER BY date""",
                        (days,),
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
            logger.error(f"[HistorySave] 获取每日统计失败: {e}")
        return result

    async def query_messages(
        self,
        group_id: str | None = None,
        sender_id: str | None = None,
        time_start: str | None = None,
        time_end: str | None = None,
        keyword: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> dict:
        """查询聊天记录（支持多条件过滤和分页）。

        Args:
            group_id: 群号过滤（可选，字符串形式）
            sender_id: QQ 号过滤（可选，字符串形式）
            time_start: 开始时间（格式 YYYY-MM-DD HH:MM:SS）
            time_end: 结束时间
            keyword: 关键词，模糊匹配 content 与 sender_name（可选）
            page: 页码（从 1 开始）
            page_size: 每页条数

        Returns:
            dict: {"total": int, "records": list[dict]}
        """
        result = {"total": 0, "records": []}
        try:
            conditions = []
            params = []

            if group_id:
                conditions.append("group_id = %s")
                params.append(group_id)
            if sender_id:
                conditions.append("sender_id = %s")
                params.append(sender_id)
            if time_start:
                conditions.append("timestamp >= %s")
                params.append(time_start)
            if time_end:
                conditions.append("timestamp <= %s")
                params.append(time_end)
            if keyword:
                # 转义 LIKE 通配符，避免用户输入的 % _ 干扰匹配
                kw = (
                    keyword.replace("\\", "\\\\")
                    .replace("%", "\\%")
                    .replace("_", "\\_")
                )
                like = f"%{kw}%"
                conditions.append("(content LIKE %s OR sender_name LIKE %s)")
                params.extend([like, like])

            where_clause = " AND ".join(conditions) if conditions else "1=1"
            offset = (page - 1) * page_size

            async with self.pool.acquire() as conn:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    # 查询总数
                    await cur.execute(
                        f"SELECT COUNT(*) as total FROM chat_history WHERE {where_clause}",
                        params,
                    )
                    row = await cur.fetchone()
                    result["total"] = row["total"] if row else 0

                    # 查询数据
                    await cur.execute(
                        f"""SELECT id, timestamp, group_id, sender_id, sender_name,
                                   message_type, content, message_id
                            FROM chat_history WHERE {where_clause}
                            ORDER BY timestamp DESC
                            LIMIT %s OFFSET %s""",
                        params + [page_size, offset],
                    )
                    rows = await cur.fetchall()
                    for row in rows:
                        row["timestamp"] = str(row["timestamp"])
                    result["records"] = rows
        except Exception as e:
            logger.error(f"[HistorySave] 查询聊天记录失败: {e}")
        return result

    async def ping(self) -> dict:
        """检测数据库连接状态。

        Returns:
            dict: {"connected": bool, "latency_ms": float, "pool": dict}
        """
        try:
            start = time.time()
            async with self.pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT 1")
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
