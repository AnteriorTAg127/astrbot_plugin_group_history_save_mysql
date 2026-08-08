"""MySQL 动态连接池管理（v0.6.0 起自 db_mysql.py 拆分）。

连接池支持自动扩容（按需创建连接至上限）和自动缩容（回收空闲连接至下限），
后台 reaper 定期巡检：回收空闲超时连接、健康检查（ping）替换失效连接。
本模块仅承载 DynamicPool 类，连接池行为常量定义于此（base.py 从本模块导入，
供全库经 core/db_mysql/__init__.py 再导出使用）。
"""

import asyncio
import time
from contextlib import asynccontextmanager

import aiomysql

from astrbot.api import logger

# 建连失败后的退避重试间隔（秒）：可取消的短 sleep，在 acquire_timeout
# 窗口内换回瞬时故障的重试机会（见 _get_connection create 模式）
CREATE_RETRY_BACKOFF_SECONDS = 1.0

# initialize() 复位前等待在途操作（_pending）归零的最长秒数：
# 超时记 warning 并强制复位（见 DynamicPool.initialize）
RESET_PENDING_WAIT_SECONDS = 20.0


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

        # 后台簿记任务集合（槽位回滚 / 取消清理）：asyncio.create_task 的
        # 返回值仅被事件循环弱引用，不保存强引用的话任务可能在运行前被 GC
        # 回收，导致簿记回滚丢失（F10e）
        self._background_tasks: set[asyncio.Task] = set()

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
        - 复位前先有界等待在途操作（_pending）归零（F10d）：强行清 0 会使在途操作
          完成时的 _pending-=1 变负值，size 少计进而超 max_size 扩容
        - 残留的 _free/_used 统一清理复位，防止历史泄漏槽位拖垮后续尝试
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

        async with self._not_empty:
            # F10d：不再强行把 _pending 清 0——先有界等待在途的校验/扩容操作
            # （占用 _pending 槽位者）自然归零。强行清 0 后，这些操作收尾时的
            # _pending-=1 会产生负值，使 size 少计、扩容超 max_size。
            # Condition.wait 等待期间释放锁，不阻塞在途操作进锁做簿记；其收尾
            # 路径（_finish_acquire / _schedule_rollback 等）都会 notify 唤醒本等待。
            if self._pending > 0:
                try:
                    await asyncio.wait_for(
                        self._not_empty.wait_for(lambda: self._pending <= 0),
                        timeout=RESET_PENDING_WAIT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        f"[DynamicPool] 等待在途操作归零超过 "
                        f"{RESET_PENDING_WAIT_SECONDS}s（_pending={self._pending}），"
                        f"强制复位，簿记可能短暂偏差"
                    )
                    self._pending = 0
            for conn, _, _ in self._free:
                self._record_destroyed(conn)
            self._free.clear()
            # 初始化重试意味着此前数据库不可用：在途连接已无意义，
            # 统一关闭并复位簿记（借用方的 shielded 归还逻辑对已关闭连接是幂等的）
            for conn in self._used:
                self._record_destroyed(conn)
            self._used.clear()

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

        替换失效连接时一律「先建新再销旧」（F10c）：若建连失败，
        调用方异常路径会把旧连接恰好销毁一次；先销旧再建新的顺序
        会在建连失败时被异常路径二次 _record_destroyed，破坏统计不变量。
        """
        if conn.closed:
            new_conn = await self._create_connection()
            self._record_destroyed(conn)
            return new_conn
        if conn_pinged > 0 and (time.monotonic() - conn_pinged) < self._ping_cooldown:
            return conn
        if not await self._is_alive(conn):
            new_conn = await self._create_connection()
            self._record_destroyed(conn)
            return new_conn
        return conn

    @asynccontextmanager
    async def acquire(self):
        """获取一个连接（上下文管理器）。

        如果无空闲连接且未达上限，自动创建新连接（扩容）。
        如果已达上限，等待直到有连接释放或超时。
        归还路径用 asyncio.shield 包裹：调用方任务被取消时，
        归还操作仍会在独立内部任务中执行完毕，连接不会无主泄漏。

        归还时的连接处置（F9）：
        - 借用体以 CancelledError / TimeoutError（查询执行超时）退出：
          被中断的查询可能仍有未读完的响应字节残留在连接上，协议状态
          不可信，销毁该连接而非放回空闲池，杜绝脏连接免检复用；
        - 普通 SQL 异常（语法错误、约束冲突等）：该语句的协议交互已完整
          结束，连接可安全复用，正常归还；
        - 正常退出：维持现状（归还并刷新 last_pinged）。
        """
        conn = await self._get_connection()
        # 连接「中毒」标记：本次借用经历了取消/超时，归还时必须销毁
        poisoned = False
        try:
            yield conn
        except (asyncio.CancelledError, asyncio.TimeoutError):
            # 仅取消与超时毒化连接；其余异常（含 pymysql 各类 SQL 错误）
            # 走正常归还路径
            poisoned = True
            raise
        finally:
            await asyncio.shield(self._release_connection(conn, poisoned=poisoned))

    def _track_background_task(self, task: asyncio.Task):
        """把后台簿记任务的强引用挂到实例集合，完成后自动 discard。

        asyncio.create_task 的返回值仅被事件循环弱引用，若不保存，
        任务可能在运行前被 GC 回收，簿记回滚随之丢失（F10e）。
        """
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

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
            self._track_background_task(asyncio.create_task(_rollback()))
        except RuntimeError:
            # 事件循环不可用（本插件调用路径下几乎不会发生）：降级为同步修正
            self._pending -= 1
            if close_conn is not None:
                self._record_destroyed(close_conn)

    def _schedule_snapshot_cleanup(
        self, conns: list[tuple[aiomysql.Connection, float, float]]
    ):
        """以独立任务清理健康检查中未处理完的快照连接（F10b）。

        快照连接已从 _free 移出并占用 _pending 校验槽位；取消穿出时
        当前任务已无法安全等待锁，槽位回滚交给独立任务完成。连接逐个
        经 _record_destroyed 关闭，维持 created - recycled = free + used
        + pending 不变量，杜绝 FD 泄漏与槽位永久占用。
        """
        if not conns:
            return

        async def _cleanup():
            try:
                async with self._not_empty:
                    self._pending -= len(conns)
                    self._not_empty.notify()
            except Exception as e:
                logger.error(f"[DynamicPool] 快照校验槽位回滚失败: {e}")
            for _conn, _, _ in conns:
                self._record_destroyed(_conn)

        try:
            self._track_background_task(asyncio.create_task(_cleanup()))
        except RuntimeError:
            # 事件循环不可用：降级为同步修正
            self._pending -= len(conns)
            for _conn, _, _ in conns:
                self._record_destroyed(_conn)

    async def _finish_acquire(self, conn: aiomysql.Connection) -> aiomysql.Connection:
        """校验/创建成功后把连接登记进 _used（取消安全）。"""
        try:
            async with self._not_empty:
                if self._closed:
                    raise RuntimeError("连接池已关闭")
                self._pending -= 1
                self._used.add(conn)
                # 唤醒 _pending 相关等待者（如 initialize 复位等待在途操作归零，F10d）
                self._not_empty.notify()
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
          （create 模式失败后先经可取消短退避再重试，F4；deadline 到由循环顶部抛 TimeoutError）
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
                    # 连接年龄超过 pool_recycle 时直接换新（复用已预留的槽位）。
                    # F10c：销毁旧连接后立即置空引用——若随后的建连失败，
                    # 异常路径不会把已销毁的旧连接再次 _record_destroyed
                    if (
                        self._pool_recycle
                        and (time.monotonic() - conn_ts) > self._pool_recycle
                    ):
                        self._record_destroyed(conn_to_check)
                        conn_to_check = None
                        conn = await self._create_connection()
                    else:
                        # _ensure_connection 内部「先建新再销旧」：即使重建失败，
                        # 本异常路径也只会把旧连接销毁一次，不会双计数
                        conn = await self._ensure_connection(conn_to_check, conn_pinged)
                        if conn is not conn_to_check:
                            # 原连接已在 _ensure_connection 内销毁，置空防双计数
                            conn_to_check = None
                except BaseException:
                    self._schedule_rollback(close_conn=conn_to_check)
                    raise
                return await self._finish_acquire(conn)

            # mode == "create"：锁外创建新连接
            try:
                conn = await self._create_connection()
            except asyncio.CancelledError:
                # 被取消：经独立任务回滚预留槽位后原样上抛（不做退避重试）
                self._schedule_rollback()
                raise
            except BaseException as e:
                # F4：建连失败不再立即 raise——回滚槽位后经可取消的短退避
                # 重新进入循环重试，让瞬时故障（网络抖动 / DB 短暂重启）在
                # acquire_timeout 窗口内有机会成功；deadline 到由循环顶部抛
                # TimeoutError。sleep 本身可取消：退避中被取消则 CancelledError
                # 直接上抛，槽位已交给回滚任务，不会泄漏。
                self._log_create_error(e)
                self._schedule_rollback()
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    await asyncio.sleep(min(CREATE_RETRY_BACKOFF_SECONDS, remaining))
                continue
            return await self._finish_acquire(conn)

    async def _release_connection(
        self, conn: aiomysql.Connection, poisoned: bool = False
    ):
        """归还连接到池中。

        poisoned=True 表示连接在本次借用中经历了取消/超时，协议状态
        不可信（可能残留未读完的响应字节），销毁而非放回空闲池（F9）。
        """
        async with self._not_empty:
            self._used.discard(conn)
            if self._closed or conn.closed or poisoned:
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

        取消安全（F10a/F10b）：快照连接已全部移出 _free 并占用 _pending
        校验槽位。ping 期间被取消不再吞掉 CancelledError，而是原样上抛
        让 reaper 及时退出；穿出循环前把所有未处理完的快照连接（含被取消
        时正在处理的那个）交给独立任务逐个关闭并回滚槽位，杜绝连接泄漏
        与 _pending 永久占用。
        """
        # 快照当前所有空闲连接并整体转入校验槽位；unfinished 记录尚未
        # 完成簿记收尾的连接，取消穿出时按它清理
        async with self._lock:
            if not self._free:
                return
            unfinished = list(self._free)
            self._free.clear()
            self._pending += len(unfinished)

        try:
            while unfinished:
                conn, ts, last_pinged = unfinished[0]
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
                except asyncio.CancelledError:
                    # F10a：取消不得吞掉——该连接协议状态不可信，连同后续
                    # 未处理连接一并由外层清理后 re-raise，让 reaper 及时退出
                    raise
                except BaseException:
                    # 其余异常：丢弃该连接并继续下一个（维持原语义）
                    self._schedule_rollback(close_conn=conn)
                    unfinished.pop(0)
                    continue

                # 锁内：存活则归还（保留原空闲时间戳，更新 ping 时间戳），
                # 失效则丢弃。块内无 await，取消只会发生在获取锁的瞬间，
                # 届时该连接仍在 unfinished 中，由外层统一清理
                async with self._lock:
                    self._pending -= 1
                    if alive and not self._closed:
                        self._free.append((conn, ts, time.monotonic()))
                    else:
                        self._record_destroyed(conn)
                unfinished.pop(0)
        except asyncio.CancelledError:
            # F10b：取消穿出快照循环——unfinished 中即所有校验槽位尚未
            # 回滚的连接，交独立任务关闭并回滚后再上抛
            self._schedule_snapshot_cleanup(unfinished)
            raise

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
            except asyncio.CancelledError:
                # 取消同样不得吞掉：回滚预留槽位后上抛（与 F10a 语义一致）
                self._schedule_rollback()
                raise
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

        # 等后台簿记任务（槽位回滚 / 取消清理，均为短任务）收尾，
        # 避免进程退出时遗留「task destroyed but pending」告警；
        # 此时不持锁，任务可正常进锁完成簿记
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)

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
