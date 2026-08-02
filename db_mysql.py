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
        ping_cooldown: int = 5,
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
        self._ping_cooldown = max(
            1, ping_cooldown
        )  # 最少 1 秒，防止设为 0 导致永不 ping

        # 连接存储: list of (connection, last_used_timestamp, last_pinged_timestamp)
        self._free: list[tuple[aiomysql.Connection, float, float]] = []
        self._used: set[aiomysql.Connection] = set()
        # 注意：asyncio 同步原语必须在事件循环内创建，延迟到 initialize()
        self._lock: asyncio.Lock | None = None
        self._not_empty: asyncio.Condition | None = None
        self._closed = False
        self._reaper_task: asyncio.Task | None = None

        # 统计
        self._total_created = 0
        self._total_recycled = 0
        self._pending = 0  # 正在创建但尚未入池的连接数（用于 max_size 预留）

        # 建连失败日志节流状态（60s 窗口 + 窗口内累计次数）
        self._create_err_log_ts = 0.0
        self._create_err_count = 0

    @property
    def size(self) -> int:
        """当前总连接数（空闲 + 使用中 + 创建中）。"""
        return len(self._free) + len(self._used) + self._pending

    @property
    def free_size(self) -> int:
        """空闲连接数。"""
        return len(self._free)

    @property
    def used_size(self) -> int:
        """使用中连接数。"""
        return len(self._used)

    def _record_destroyed(
        self, conn: aiomysql.Connection | None = None, count: int = 1
    ):
        """记录连接销毁（用于统计：created - recycled = free + used）。

        所有连接销毁路径（替换失效连接、pool_recycle 换新、reaper 回收、
        health check 丢弃、归还时发现已关闭、预探测临时连接关闭等）
        都须经此方法更新计数器，保证池子行踪可解释。
        """
        self._total_recycled += count
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    async def _create_connection(self) -> aiomysql.Connection:
        """创建一个新的数据库连接。

        不传 read_timeout/write_timeout：aiomysql 各版本支持不一致
        （0.2.x 的 connect() 不接受这两个参数，直接 TypeError）。
        防挂起改由两层版本无关的超时兜底：
        - connect_timeout=10：aiomysql 内部 TCP 连接超时
        - asyncio.wait_for(15s)：兜住 DNS 解析等 connect_timeout 覆盖不到的挂起
        """
        conn = await asyncio.wait_for(
            aiomysql.connect(
                host=self._host,
                port=self._port,
                user=self._user,
                password=self._password,
                db=self._db,
                charset="utf8mb4",
                autocommit=True,
                connect_timeout=10,
            ),
            timeout=15,
        )
        self._total_created += 1
        return conn

    def _log_create_error(self, exc: BaseException):
        """建连失败日志节流：60 秒窗口内最多一条 ERROR，附带累计次数。

        数据库不可用时 Web 后台轮询会每隔几秒触发一次扩容建连，
        逐条记日志会刷屏。首次失败立即输出，窗口内的后续次数
        累计后在下一条日志中一并汇报。
        """
        now = time.monotonic()
        self._create_err_count += 1
        if now - self._create_err_log_ts < 60:
            return
        self._create_err_log_ts = now
        count = self._create_err_count
        self._create_err_count = 0
        logger.error(
            f"[DynamicPool] 创建连接失败（近期累计 {count} 次，抑制重复日志）: {exc}"
        )

    async def initialize(self):
        """初始化连接池，启动 reaper，并尝试预创建 1 个连接。

        预创建使用 2 秒短超时：成功则放入空闲池，失败仅记录 warning，
        不会阻塞事件循环或 AstrBot 启动。后续连接按需创建。

        重试场景（被外部循环反复调用）下：
        - 同步原语只创建一次，避免替换正在被等待的 Lock/Condition 使旧等待者永久收不到 notify
        - 残留的 _free/_used/_pending 统一清理复位，防止历史泄漏槽位拖垮后续尝试
        - 先启动 reaper 再做预创建，避免预创建被取消时 reaper 缺失
        """
        # asyncio 同步原语只创建一次（Python 3.10+ 创建时不绑定事件循环，按需在等待时解析）
        if self._lock is None:
            self._lock = asyncio.Lock()
            self._not_empty = asyncio.Condition(self._lock)

        # 清理上一次初始化残留（重试场景）
        if self._reaper_task and not self._reaper_task.done():
            self._reaper_task.cancel()
            try:
                await self._reaper_task
            except asyncio.CancelledError:
                pass

        async with self._lock:
            for conn, _, _ in self._free:
                self._record_destroyed(conn)
            self._free.clear()
            # 初始化重试意味着此前数据库不可用：在途连接已无意义，
            # 统一关闭并复位簿记（借用方的 shielded 归还逻辑对已关闭连接是幂等的）
            for conn in self._used:
                self._record_destroyed(conn)
            self._used.clear()
            self._pending = 0

        # 先启动 reaper，防止后续步骤被取消时巡检缺失
        self._reaper_task = asyncio.create_task(self._reaper_loop())

        # 仅尝试预创建 1 个连接，2 秒超时，避免阻塞启动
        try:
            conn = await asyncio.wait_for(self._create_connection(), timeout=2)
            async with self._lock:
                if self._closed:
                    self._record_destroyed(conn)
                else:
                    self._free.append((conn, time.monotonic(), time.monotonic()))
        except asyncio.TimeoutError:
            logger.warning("[DynamicPool] 预创建连接超时 (2s)，数据库当前可能不可用")
        except Exception as e:
            logger.warning(f"[DynamicPool] 预创建连接失败: {e}")

        logger.info(
            f"[DynamicPool] 初始化完成 | min={self._min_size} "
            f"max={self._max_size} idle_timeout={self._idle_timeout}s "
            f"| 预创建 {len(self._free)} 个连接"
        )

    async def _is_alive(self, conn: aiomysql.Connection) -> bool:
        """检查连接是否存活（5 秒超时兜底，防止半开连接上 ping 长期挂起）。"""
        try:
            await asyncio.wait_for(conn.ping(reconnect=False), timeout=5)
            return True
        except Exception:
            return False

    async def _ensure_connection(
        self, conn: aiomysql.Connection, conn_pinged: float = 0.0
    ) -> aiomysql.Connection:
        """确保连接可用，失效则重建。

        conn_pinged 是连接上次通过 ping 验证的时间戳（monotonic 秒）。

        连接刚刚从 idle 取出（is_alive 已返回 True）时，
        _ping_cooldown 秒内 skip ping 直接复用；服务端 ssl_timeout
        默认 10 分钟（8.0.28+），保持连接的 SELECT 1 又会被
        _is_alive 吞掉 any 异常，所以通过 conn.ping 的 timeout=5s 来
        兜底半开连接检测，ping 频率由 cooldown 控制。
        """
        if conn.closed:
            self._record_destroyed(conn)
            return await self._create_connection()
        if (
            conn_pinged
            and conn_pinged > 0
            and (time.monotonic() - conn_pinged) < self._ping_cooldown
        ):
            return conn
        if not await self._is_alive(conn):
            self._record_destroyed(conn)
            return await self._create_connection()
        return conn

    @asynccontextmanager
    async def acquire(self):
        """获取一个连接（上下文管理器）。

        如果无空闲连接且未达上限，自动创建新连接（扩容）。
        如果已达上限，等待直到有连接释放或超时。
        归还路径用 asyncio.shield 包裹：调用方任务被取消时，
        归还操作仍会在独立内部任务中执行完毕，连接不会无主泄漏。
        """
        conn = await self._get_connection()
        try:
            yield conn
        finally:
            await asyncio.shield(self._release_connection(conn))

    def _schedule_rollback(self, close_conn: aiomysql.Connection | None = None):
        """以独立任务回滚一个 _pending 槽位（并可选关闭连接）。

        专用于异常/取消路径：独立任务不受当前任务取消的影响，
        即使当前任务无法安全地等待锁获取（CancelledError 会在锁等待点
        打断清理），簿记回滚也一定执行。
        """

        async def _rollback():
            try:
                async with self._not_empty:
                    self._pending -= 1
                    self._not_empty.notify()
            except Exception as e:
                logger.error(f"[DynamicPool] 槽位回滚失败: {e}")
            if close_conn is not None:
                self._record_destroyed(close_conn)

        try:
            asyncio.create_task(_rollback())
        except RuntimeError:
            # 事件循环不可用（本插件调用路径下几乎不会发生）：降级为同步修正
            self._pending -= 1
            if close_conn is not None:
                self._record_destroyed(close_conn)

    async def _finish_acquire(self, conn: aiomysql.Connection) -> aiomysql.Connection:
        """校验/创建成功后把连接登记进 _used（取消安全）。"""
        try:
            async with self._lock:
                if self._closed:
                    raise RuntimeError("连接池已关闭")
                self._pending -= 1
                self._used.add(conn)
            return conn
        except BaseException:
            # 池已关闭或在登记点被取消：关闭连接并回滚槽位后原样抛出
            self._schedule_rollback(close_conn=conn)
            raise

    async def _get_connection(self) -> aiomysql.Connection:
        """从池中获取一个可用连接。

        网络I/O（ping/connect）在锁外执行，避免高并发下连接池退化为串行；
        锁内仅做 _free.pop / _pending / _used.add 等O(1)簿记操作。

        正确性保证：
        - deadline 在每轮循环开头复检：建连反复失败也会按 acquire_timeout 超时，不会无限自旋
        - 校验中的连接计入 _pending（size 保持恒定）：既防止超量扩容，也保证 reaper
          校验与 acquire 不会并发使用同一连接
        - 所有可取消的 await 点都通过 _schedule_rollback 回滚簿记：CancelledError
          （BaseException）不会泄漏 _pending 槽位或把连接钉死在 _used
        - 等待超时后回到循环顶部复检 _free/deadline：与超时撞期的 notify() 不会丢失
        """
        deadline = time.monotonic() + self._acquire_timeout

        while True:
            if self._closed:
                raise RuntimeError("连接池已关闭")
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"[DynamicPool] 获取连接超时 ({self._acquire_timeout}s)，"
                    f"当前连接数: {self.size}/{self._max_size}"
                )

            mode: str | None = None
            conn_to_check: aiomysql.Connection | None = None
            conn_ts = 0.0
            conn_pinged = 0.0

            # ---- 锁内：快速簿记 ----
            async with self._not_empty:
                if self._free:
                    # 取一个空闲连接进入"校验中"状态（free-1 / pending+1，size 不变）
                    conn_to_check, conn_ts, conn_pinged = self._free.pop(0)
                    self._pending += 1
                    mode = "check"
                elif self.size < self._max_size:
                    # 预留扩容槽位（_pending 计入 size，防止其他协程同时扩容超限）
                    self._pending += 1
                    mode = "create"
                else:
                    # 已达上限，等待释放
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(
                            f"[DynamicPool] 获取连接超时 ({self._acquire_timeout}s)，"
                            f"当前连接数: {self.size}/{self._max_size}"
                        )
                    try:
                        await asyncio.wait_for(
                            self._not_empty.wait(), timeout=remaining
                        )
                    except asyncio.TimeoutError:
                        # 不立即抛错：回到循环顶部复检 _free 与 deadline，
                        # 避免与超时撞期的 notify() 被吞掉
                        pass
                    continue

            # ---- 锁外：网络 I/O（异常/取消时经独立任务回滚簿记） ----

            if mode == "check":
                try:
                    # 连接年龄超过 pool_recycle 时直接换新（复用已预留的槽位）
                    if (
                        self._pool_recycle
                        and (time.monotonic() - conn_ts) > self._pool_recycle
                    ):
                        self._record_destroyed(conn_to_check)
                        conn = await self._create_connection()
                    else:
                        conn = await self._ensure_connection(conn_to_check, conn_pinged)
                except BaseException:
                    self._schedule_rollback(close_conn=conn_to_check)
                    raise
                return await self._finish_acquire(conn)

            # mode == "create"：锁外创建新连接
            try:
                conn = await self._create_connection()
            except BaseException as e:
                self._log_create_error(e)
                self._schedule_rollback()
                raise
            return await self._finish_acquire(conn)

    async def _release_connection(self, conn: aiomysql.Connection):
        """归还连接到池中。"""
        async with self._not_empty:
            self._used.discard(conn)
            if self._closed or conn.closed:
                self._record_destroyed(conn)
            else:
                self._free.append((conn, time.monotonic(), time.monotonic()))
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
            # 只回收超出 min_size 的空闲连接（_min_size 至少为 1，列表非空已蕴含）
            while (
                len(self._free) > self._min_size
                and (now - self._free[0][1]) > self._idle_timeout
            ):
                conn, _, _ = self._free.pop(0)
                to_close.append(conn)

        for conn in to_close:
            self._record_destroyed(conn)

        if to_close:
            logger.info(
                f"[DynamicPool] 回收 {len(to_close)} 个空闲连接 | "
                f"当前: free={self.free_size} used={self.used_size}"
            )

    async def _health_check(self):
        """检查空闲连接健康状态，移除失效连接。

        采用"租约"模式：把待检查的连接从 _free 移入 _pending 校验槽，
        acquire 在此期间取不到它，杜绝 reaper 的 ping 与业务查询并发
        使用同一 aiomysql 连接造成的协议错乱；ping 仍在锁外执行，
        巡检不会阻塞 acquire。

        连接在 _ping_cooldown 秒内刚被验证过的直接跳过，减少不必要的
        MySQL 往返（reaper 30s 一次，cooldown 默认 5s，正常场景下
        大部分连接不必再 ping）。

        关键：遍历 _free 的**快照**，一轮即止。旧实现用 while True
        循环 pop+append 无限往复，冷却跳过使循环体变纯 CPU 路径后
        会陷入忙等死循环把 CPU 打到 100%。
        """
        # 快照当前所有空闲连接，一轮检查完毕即停
        async with self._lock:
            if not self._free:
                return
            snapshot = list(self._free)
            self._free.clear()
            self._pending += len(snapshot)

        for conn, ts, last_pinged in snapshot:
            # 锁外：ping 冷却内的连接跳过检查
            try:
                now = time.monotonic()
                if (
                    last_pinged > 0
                    and (now - last_pinged) < self._ping_cooldown
                    and not conn.closed
                ):
                    alive = True
                else:
                    alive = not conn.closed and await self._is_alive(conn)
            except BaseException:
                self._schedule_rollback(close_conn=conn)
                continue

            # 锁内：存活则归还（保留原空闲时间戳，更新 ping 时间戳），失效则丢弃
            async with self._lock:
                self._pending -= 1
                if alive and not self._closed:
                    self._free.append((conn, ts, time.monotonic()))
                else:
                    self._record_destroyed(conn)

        # 如果低于 min_size，经预留槽位补充连接（严格不超过 max_size）
        while True:
            async with self._lock:
                if self._closed or self.size >= self._min_size:
                    break
                if self.size >= self._max_size:
                    break
                self._pending += 1
            try:
                conn = await self._create_connection()
            except BaseException:
                self._schedule_rollback()
                break
            async with self._lock:
                self._pending -= 1
                if self._closed:
                    self._record_destroyed(conn)
                    break
                self._free.append((conn, time.monotonic(), time.monotonic()))

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
        """关闭连接池，释放所有连接。

        - 唤醒所有等待者，使其检测到 _closed 后立即失败（而非白等满 acquire_timeout）
        - 不强关 _used 中的连接：借用方的 shielded 归还逻辑会检测到 _closed 并关闭，
          避免把正在执行中的查询（如多语句清空操作）从中间打断
        """
        self._closed = True
        if self._reaper_task:
            self._reaper_task.cancel()
            try:
                await self._reaper_task
            except asyncio.CancelledError:
                pass

        # 如果 initialize() 未执行，_lock 为 None，直接清理即可
        if self._lock is None:
            for conn, _, _ in self._free:
                self._record_destroyed(conn)
            self._free.clear()
            return

        async with self._not_empty:
            for conn, _, _ in self._free:
                self._record_destroyed(conn)
            self._free.clear()
            # 唤醒全部等待协程：循环顶部复检 _closed 后会立即抛 RuntimeError
            self._not_empty.notify_all()

        if self._used:
            logger.info(
                f"[DynamicPool] 连接池已关闭（{len(self._used)} 个连接仍在使用中，"
                f"将由借用方归还时关闭）"
            )
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
                        at_list VARCHAR(512) DEFAULT '',
                        reply_id VARCHAR(64) DEFAULT '',
                        INDEX idx_group_time (group_id, timestamp),
                        INDEX idx_sender_time (sender_id, timestamp),
                        INDEX idx_group_sender_time (group_id, sender_id, timestamp),
                        INDEX idx_message_id (message_id)
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
        """将旧表结构迁移到当前版本。

        检测并迁移内容：
        - v0.2：chat_history / image_records 的 group_id、sender_id 列若不是字符串类型
          （varchar/char/text），MODIFY 为 VARCHAR(32) NOT NULL
          （MySQL 会自动把已有数字值转为字符串，不丢数据，列上索引自动重建）
        - v0.2：image_records 缺少 sender_name 列时自动 ADD COLUMN
        - v0.4.0：chat_history 缺少 at_list / reply_id 列时自动 ADD COLUMN
          （人物分析所需 @ 对象 / 回复目标标记；MySQL 无 ADD COLUMN IF NOT EXISTS
          语法，先查 INFORMATION_SCHEMA.COLUMNS 判定缺列再 ALTER，重复执行幂等）

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
                            await cur.execute(
                                "SELECT DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS"
                                " WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s",
                                ("chat_history", column),
                            )
                            row = await cur.fetchone()
                            if not row:
                                await cur.execute(
                                    f"ALTER TABLE chat_history ADD COLUMN {column} {column_def}"
                                )
                                logger.info(
                                    f"[HistorySave] 表结构迁移: chat_history 已新增 {column} 列"
                                )
                        except Exception as e:
                            logger.warning(
                                f"[HistorySave] 表结构迁移 chat_history.{column} 失败: {e}"
                            )

                    # v0.4.1：chat_history.message_id 补索引（查询/人物分析按 message_id
                    # 批量反查被回复消息，表增大后无索引会退化为全表扫描）。
                    # ADD INDEX 无 IF NOT EXISTS 语法，先查 INFORMATION_SCHEMA.STATISTICS
                    # 判定索引存在与否再 ALTER，幂等可重入；MySQL 8.0 ADD INDEX 为
                    # 在线 DDL（INPLACE），几百万行也不阻塞持续写入。
                    try:
                        await cur.execute(
                            "SELECT INDEX_NAME FROM INFORMATION_SCHEMA.STATISTICS"
                            " WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND INDEX_NAME = %s"
                            " LIMIT 1",
                            ("chat_history", "idx_message_id"),
                        )
                        row = await cur.fetchone()
                        if not row:
                            await cur.execute(
                                "ALTER TABLE chat_history ADD INDEX idx_message_id (message_id)"
                            )
                            logger.info(
                                "[HistorySave] 表结构迁移: chat_history 已新增 idx_message_id 索引"
                            )
                    except Exception as e:
                        logger.warning(
                            f"[HistorySave] 表结构迁移 chat_history.idx_message_id 失败: {e}"
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
        at_list: str = "",
        reply_id: str = "",
    ) -> bool:
        """插入一条聊天记录。

        Args:
            group_id: 群号（字符串形式）
            sender_id: 发送者 QQ 号（字符串形式）
            sender_name: 发送者昵称
            message_type: 消息类型（text/mixed，纯图片消息不入文本表）
            content: 文本内容
            message_id: 消息 ID
            at_list: 本条消息 @ 的 QQ 号列表，英文逗号分隔（如 "123,456"）；无则空串
            reply_id: 本条消息回复的目标消息 message_id；无则空串

        Returns:
            bool: 是否插入成功
        """
        try:
            # TEXT 列上限 65535 字节：超长内容截断并留标记，
            # 避免严格 sql_mode 下整条插入失败导致消息彻底丢失
            if content and len(content.encode("utf-8")) > 60000:
                content = (
                    content.encode("utf-8")[:60000].decode("utf-8", "ignore")
                    + "\n…[内容过长已截断]"
                )
            async with self.pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """INSERT INTO chat_history
                           (timestamp, group_id, sender_id, sender_name, message_type, content, message_id, at_list, reply_id)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                        (
                            datetime.now(),
                            group_id,
                            sender_id,
                            sender_name,
                            message_type,
                            content,
                            message_id,
                            at_list,
                            reply_id,
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
        # image_url 列为 VARCHAR(1024)：超长链接跳过入库并告警，
        # 避免严格模式下整条插入失败（且不影响同消息的文本记录）
        if len(image_url) > 1024:
            logger.warning(
                f"[HistorySave] 图片链接超长（{len(image_url)} 字符），已跳过入库: "
                f"{image_url[:80]}..."
            )
            return False
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
            # 钳制到安全范围，防止异常大的设置值使 timedelta/日期运算
            # 抛 OverflowError 导致清理功能永久失效
            retention_days = min(max(int(retention_days), 0), 36500)
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

        优先使用 TRUNCATE TABLE（DDL，自动复位自增 ID，性能远优于 DELETE）。
        若 MySQL 用户无 DROP 权限（TRUNCATE 需要），回退到 DELETE + ALTER 方式。

        Returns:
            dict: {"success": bool, "deleted_messages": int, "deleted_images": int, "truncated": bool}
        """
        result: dict = {
            "success": False,
            "deleted_messages": 0,
            "deleted_images": 0,
            "truncated": False,
        }
        try:
            async with self.pool.acquire() as conn:
                async with conn.cursor() as cur:
                    # 两表独立清空：一张表失败不影响另一张，
                    # 成功状态由各自的返回值汇总
                    messages_ok = await self._purge_table(
                        cur, "chat_history", result, "deleted_messages"
                    )
                    images_ok = await self._purge_table(
                        cur, "image_records", result, "deleted_images"
                    )
            result["success"] = messages_ok and images_ok
        except Exception as e:
            logger.error(f"[HistorySave] 清空所有数据失败: {e}")
        # 所有路径都输出审计日志（含部分失败情形）；
        # TRUNCATE 无 rowcount，计数取自操作前的 SELECT COUNT(*)
        if result["success"]:
            logger.warning(
                f"[HistorySave] 已清空所有数据"
                f"{' (TRUNCATE)' if result['truncated'] else ' (DELETE)'}: "
                f"聊天记录 {result['deleted_messages']} 条, "
                f"图片记录 {result['deleted_images']} 条"
            )
        else:
            logger.error(
                f"[HistorySave] 清空所有数据未完全成功: "
                f"聊天记录 {result['deleted_messages']} 条, "
                f"图片记录 {result['deleted_images']} 条"
            )
        return result

    async def _purge_table(self, cur, table: str, result: dict, count_key: str) -> bool:
        """清空单张表并把删除条数写入 result。

        TRUNCATE 是 DDL，没有 rowcount，因此操作前先 SELECT COUNT(*)
        取得条数用于审计日志。表名来自本类硬编码，无注入风险。

        Args:
            cur: 数据库游标
            table: 表名
            result: 用于写入删除条数的结果字典
            count_key: result 中删除条数的键名

        Returns:
            bool: 该表是否清空成功
        """
        try:
            await cur.execute(f"SELECT COUNT(*) FROM {table}")
            row = await cur.fetchone()
            pre_count = int(row[0]) if row else 0
        except Exception as e:
            logger.error(f"[HistorySave] 预统计 {table} 行数失败: {e}")
            return False

        if await self._truncate_table(cur, table):
            result["truncated"] = True
            result[count_key] = pre_count
            return True

        try:
            await cur.execute(f"DELETE FROM {table}")
            deleted = cur.rowcount
            # rowcount 异常（如 -1）时回退到预统计值，保证审计计数合理
            result[count_key] = deleted if deleted >= 0 else pre_count
            await self._reset_auto_increment(cur, table)
            return True
        except Exception as e:
            logger.error(f"[HistorySave] DELETE FROM {table} 失败: {e}")
            return False

    async def _truncate_table(self, cur, table: str) -> bool:
        """尝试用 TRUNCATE TABLE 清空表。

        TRUNCATE 是 DDL，自动复位自增 ID，性能远优于 DELETE。
        需要 DROP 权限，权限不足或其他异常时返回 False 供调用方回退到 DELETE。

        Args:
            cur: 数据库游标
            table: 表名（本类硬编码，无注入风险）

        Returns:
            bool: True 表示 TRUNCATE 成功，False 表示需回退到 DELETE
        """
        try:
            await cur.execute(f"TRUNCATE TABLE {table}")
            return True
        except Exception as e:
            logger.warning(
                f"[HistorySave] TRUNCATE {table} 失败（可能无 DROP 权限），"
                f"回退到 DELETE: {e}"
            )
            return False

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
                    await cur.execute(
                        "SELECT COUNT(*), "
                        "SUM(timestamp >= %s AND timestamp < %s) "
                        "FROM chat_history",
                        (today_start, tomorrow_start),
                    )
                    row = await cur.fetchone()
                    if row:
                        stats["total_messages"] = int(row[0] or 0)
                        stats["today_messages"] = int(row[1] or 0)

                    await cur.execute(
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
                    await cur.execute(
                        """SELECT DATE(timestamp) as date, COUNT(*) as count
                           FROM chat_history
                           WHERE timestamp >= %s
                           GROUP BY DATE(timestamp) ORDER BY date""",
                        (cutoff,),
                    )
                    msg_rows = await cur.fetchall()
                    msg_map = {str(row[0]): row[1] for row in msg_rows}

                    await cur.execute(
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
                    # at_list/reply_id 为 v0.4.0 新增列，迁移前的存量行可能为 NULL，
                    # 保持原样返回，下游按 .get 兜底
                    await cur.execute(
                        f"""SELECT id, timestamp, group_id, sender_id, sender_name,
                                   message_type, content, message_id, at_list, reply_id
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
            self._log_op_error("query_messages", "查询聊天记录", e)
        return result

    async def get_messages_by_ids(self, message_ids: list[str]) -> list[dict]:
        """按 message_id 批量精确查询 chat_history（关系分析反查被回复者用）。

        空入参或全为空串 → 直接返回 []；入参中的空串先过滤，
        IN 占位符按过滤后的列表动态生成（全参数化，无拼接注入风险）。

        Args:
            message_ids: 待查询的 message_id 列表

        Returns:
            list[dict]: 命中记录列表，每条含 timestamp(str)/group_id/sender_id/
                sender_name/message_type/content/message_id/at_list/reply_id；
                空入参或查询异常时返回 []
        """
        ids = [mid for mid in (message_ids or []) if mid]
        if not ids:
            return []
        try:
            placeholders = ",".join(["%s"] * len(ids))
            async with self.pool.acquire() as conn:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute(
                        f"""SELECT timestamp, group_id, sender_id, sender_name,
                                   message_type, content, message_id, at_list, reply_id
                            FROM chat_history
                            WHERE message_id IN ({placeholders})""",
                        ids,
                    )
                    rows = await cur.fetchall()
                    for row in rows:
                        row["timestamp"] = str(row["timestamp"])
                    return rows
        except Exception as e:
            self._log_op_error("get_messages_by_ids", "按ID批量查询聊天记录", e)
            return []

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
