"""SQLite 备用存储后端（SQLiteManager，v0.9.0 引入）。

为聊天记录存储提供 SQLite 备用后端（history.db），与 MySQLManager
（core/db_mysql/ 包）**同名同签名同返回语义**：bootstrap 在
storage_backend=sqlite 时以本类替换 MySQLManager 构造 ``self.mysql_mgr``
（属性名不变），经鸭子类型透传给下游全部模块（saver/backfill/summary/
profile/webapi/public_api/cleaner/commands），对外调用零改动。

为使鸭子类型完整，本类除 PRD F3 列出的方法外，还提供下游无条件调用的
``get_stats`` / ``get_daily_stats``（core/commands.py 状态指令与
core/webapi/storage.py 状态/每日统计端点直接调用，缺失会 AttributeError），
语义逐字对齐 core/db_mysql/stats.py。

与 MySQL 侧的实现差异（对外语义已对齐）：

- 时间戳：MySQL 存 DATETIME（写入传 datetime 对象）；SQLite 存 Unix 秒
  （INTEGER），写入 ``int(dt.timestamp())``，读取 ``datetime.fromtimestamp``
  还原为 datetime；``query_messages`` / ``get_messages_by_ids`` 输出的
  timestamp 仍为 "YYYY-MM-DD HH:MM:SS" 字符串（与 MySQL str() 输出同构）；
- 时间过滤：MySQL 对非法时间串经隐式转换后比较为假（等效空结果）；SQLite
  侧显式解析（兼容 HH:MM:SS / HH:MM / 纯日期三种格式），解析失败按空结果
  处理（1=0 条件），避免退化为无时间过滤的全量查询；
- 大小写敏感：MySQL utf8mb4_unicode_ci 默认不区分大小写；SQLite 默认区分。
  keyword 的 LIKE 查询以 ``LOWER()`` 包裹对齐（ASCII 范围内等效）；
- message_id 唯一索引：MySQL 侧为普通索引（允许重复行）；SQLite 侧为
  UNIQUE（补库/迁移幂等依赖）。空 message_id 统一存 NULL（UNIQUE 对 NULL
  不去重，与 MySQL「多条无 ID 消息可共存」语义一致）；非空 message_id
  撞唯一索引时按「已入库」幂等返回 True（避免实时路径写入失败退缓冲后
  对同一条消息无限重试）。**迁移模块（SQLiteMigrator）写入侧需做同样的
  空值归一（空串 → NULL），否则历史无 ID 消息仅第一条能导入**；
- 清空（purge_all）：SQLite 无 TRUNCATE，用 DELETE FROM + 清空
  sqlite_sequence 复位自增 ID（truncated 恒 False，webapi 日志文案走
  DELETE 分支）；
- 超时兜底：MySQL 侧经 asyncio.wait_for 防网络挂起；SQLite 为本地文件
  无网络往返，锁等待由 PRAGMA busy_timeout 兜底，不再叠加 wait_for。

连接模型：aiosqlite 单连接 + ``asyncio.Lock`` 串行全部操作（SQLite 单写者
模型）；连接意外断开后经 ``_ensure_db`` 自愈重连（范式同
core/db_config/base.py，含 _connect_lock 双重检查）；cursor 统一
``async with`` 管理。

数据文件：data/plugin_data/astrbot_plugin_group_history_save_mysql/history.db
（经 StarTools.get_data_dir 取规范目录，与 config.db 分文件——聊天数据可能
很大，清理/备份/删除互不影响）。

公开方法清单（签名/返回语义与 MySQLManager 逐参一致）：

- 生命周期：initialize / close / ping
- 写入：insert_chat_message / insert_image_record
- 查询：query_messages / get_messages_by_ids / get_recent_messages /
  get_last_message_time / get_existing_message_ids / get_existing_image_urls /
  get_all_group_ids / count_messages / get_all_groups_summary
- 统计（下游无条件调用的鸭子类型面）：get_stats / get_daily_stats
- 维护：clean_old_images / purge_all
- meta 读写（迁移标记用，模块 C 依赖）：get_meta / set_meta
"""

import asyncio
import sqlite3
import time
from datetime import datetime, timedelta

import aiosqlite

from astrbot.api import logger
from astrbot.api.star import StarTools

PLUGIN_NAME = "astrbot_plugin_group_history_save_mysql"

# busy_timeout 默认值与合法范围（毫秒）：非法值回退默认、超范围夹取
_BUSY_TIMEOUT_DEFAULT_MS = 5000
_BUSY_TIMEOUT_MIN_MS = 0
_BUSY_TIMEOUT_MAX_MS = 60000

# 批量 IN 查询分块大小（对齐 MySQL 侧 get_existing_message_ids 范式）
_CHUNK_SIZE = 500


class SQLiteManager:
    """SQLite 聊天记录存储管理器（MySQLManager 的备用后端，鸭子类型兼容）。

    aiosqlite 单连接 + asyncio.Lock 串行全部操作（SQLite 单写者模型）；
    连接意外断开后经 _ensure_db 自愈重连；查询类操作失败经 _log_op_error
    节流记日志、写入类操作失败逐条记日志，均返回 False/空集/None/0
    （防御性契约与 MySQL 侧一致，绝不向上冒泡）。
    """

    # 后端类型标记：webapi/bootstrap 判断后端类型用（MySQLManager 无此属性）
    is_sqlite_backend = True

    def __init__(self, wal_mode: bool = True, busy_timeout_ms: int = 5000):
        """仅组装引用与参数解析（构造无 I/O），数据库文件路径在此确定。

        Args:
            wal_mode: 是否启用 WAL 日志模式（提升并发读写性能）；
                兼容字符串形式布尔配置（"true"/"1" 视为开）
            busy_timeout_ms: busy_timeout 毫秒数；int(str(x)) 显式转换，
                非法值回退 5000 并警告，合法值夹取到 0–60000
        """
        # 使用 StarTools.get_data_dir() 获取规范的数据目录（返回 Path，已自动
        # 创建），与 config.db 分文件：聊天数据可能很大，清理/备份/删除互不影响
        data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.db_path = str(data_dir / "history.db")
        self._db: aiosqlite.Connection | None = None
        # 串行全部操作的锁（SQLite 单写者模型）
        self._lock = asyncio.Lock()
        # 连接建立串行化：防止多个协程在断线后并发重连产生多余连接
        # （范式同 core/db_config/base.py）
        self._connect_lock = asyncio.Lock()
        # 查询类操作错误日志节流状态（与 MySQLManagerBase 同构）
        self._op_err_state: dict[str, tuple[float, int]] = {}

        # WAL 配置解析：兼容 bool / 字符串形式布尔（"true"/"1" 开，其余关）
        if isinstance(wal_mode, bool):
            self.wal_mode = wal_mode
        elif isinstance(wal_mode, str):
            self.wal_mode = wal_mode.strip().lower() in ("true", "1", "yes")
        else:
            self.wal_mode = bool(wal_mode)
        # 建连时 PRAGMA journal_mode 的实际生效结果（WAL 在部分文件系统上
        # 可能不被支持而静默回退 delete，供 webapi 状态卡展示 WAL 状态用）
        self.wal_active = False

        # busy_timeout 参数解析：int(str(x)) 显式转换 + try/except 兜底，
        # 非法值（浮点/布尔/非数字字符串等）回退默认值并警告；
        # 合法值夹取到 [0, 60000]
        try:
            timeout_ms = int(str(busy_timeout_ms))
        except (ValueError, TypeError):
            logger.warning(
                f"[HistorySave] sqlite_busy_timeout_ms 配置非法"
                f"（{busy_timeout_ms!r}），回退默认值 {_BUSY_TIMEOUT_DEFAULT_MS}"
            )
            timeout_ms = _BUSY_TIMEOUT_DEFAULT_MS
        else:
            clamped = min(max(timeout_ms, _BUSY_TIMEOUT_MIN_MS), _BUSY_TIMEOUT_MAX_MS)
            if clamped != timeout_ms:
                logger.warning(
                    f"[HistorySave] sqlite_busy_timeout_ms 超出范围（{timeout_ms}，"
                    f"应在 {_BUSY_TIMEOUT_MIN_MS}–{_BUSY_TIMEOUT_MAX_MS}），"
                    f"已夹取为 {clamped}"
                )
                timeout_ms = clamped
        self.busy_timeout_ms = timeout_ms

    # ------------------------------------------------------------------
    # 连接管理与建表
    # ------------------------------------------------------------------

    async def _ensure_db(self) -> None:
        """确保数据库连接可用，未连接则自动建连（幂等、自愈）。

        initialize() 失败或连接意外断开后，各公开方法经由此方法自愈重连，
        而不是带着 None 坏连接永久失败（范式同 core/db_config/base.py）。
        调用方须已持有 self._lock（本方法不重复加锁）。

        Raises:
            Exception: 连接或建表失败（由各公开方法自身的 except 兜底）
        """
        if self._db is not None:
            return
        async with self._connect_lock:
            # 双重检查：先抢到锁的协程可能已经完成建连
            if self._db is not None:
                return
            try:
                conn = await aiosqlite.connect(self.db_path)
                try:
                    # PRAGMA journal_mode=WAL（wal_mode 为 True 时）：WAL 读写
                    # 不互斥；部分文件系统不支持时 SQLite 静默回退，取回实际
                    # 模式记录到 wal_active 供状态展示
                    if self.wal_mode:
                        async with conn.execute("PRAGMA journal_mode=WAL") as cursor:
                            row = await cursor.fetchone()
                        self.wal_active = bool(row) and str(row[0]).lower() == "wal"
                    else:
                        self.wal_active = False
                    # PRAGMA busy_timeout：写锁竞争时的等待毫秒数
                    async with conn.execute(
                        f"PRAGMA busy_timeout={self.busy_timeout_ms}"
                    ):
                        pass
                    await self._create_tables(conn)
                    await conn.commit()
                except Exception:
                    # 清理半成品连接，保留下次调用的自愈能力
                    try:
                        await conn.close()
                    except Exception:
                        pass
                    raise
                self._db = conn
            except Exception:
                self._db = None
                raise

    async def _create_tables(self, conn: aiosqlite.Connection) -> None:
        """创建聊天记录表、图片记录表与 meta 表（含索引，全部幂等）。

        表名来自本类硬编码，无注入风险。字段与 MySQL 侧含义一致
        （SQLite 类型亲和，TEXT/INTEGER 亲和映射）。

        Args:
            conn: aiosqlite 连接（由 _ensure_db 传入，避免 self._db 未就绪）
        """
        # 聊天记录表（字段对齐 PRD F3：timestamp 存 Unix 秒 INTEGER，
        # raw_data/image_urls 为 SQLite 侧扩展列，MySQL 无对应列）
        async with conn.execute("""
            CREATE TABLE IF NOT EXISTS chat_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id TEXT,
                group_id TEXT,
                sender_id TEXT,
                sender_name TEXT,
                content TEXT,
                timestamp INTEGER,
                type TEXT,
                raw_data TEXT,
                image_urls TEXT,
                at_list TEXT,
                reply_id TEXT
            )
        """):
            pass
        async with conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_history_message_id"
            " ON chat_history (message_id)"
        ):
            pass
        async with conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_history_group_time"
            " ON chat_history (group_id, timestamp)"
        ):
            pass
        async with conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_history_sender"
            " ON chat_history (sender_id)"
        ):
            pass
        async with conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_history_timestamp"
            " ON chat_history (timestamp)"
        ):
            pass
        # 图片记录表
        async with conn.execute("""
            CREATE TABLE IF NOT EXISTS image_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id TEXT,
                group_id TEXT,
                sender_id TEXT,
                sender_name TEXT,
                image_url TEXT,
                timestamp INTEGER
            )
        """):
            pass
        async with conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_image_records_group_time"
            " ON image_records (group_id, timestamp)"
        ):
            pass
        # meta 表（迁移标记 migrated_from_mysql 等，模块 C 读写）
        async with conn.execute("""
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """):
            pass

    async def initialize(self) -> bool:
        """初始化数据库连接并创建表结构。

        Returns:
            bool: 初始化是否成功
        """
        try:
            async with self._lock:
                await self._ensure_db()
            if not self.wal_active and self.wal_mode:
                logger.warning(
                    "[HistorySave] SQLite WAL 模式未生效（当前文件系统可能"
                    "不支持，已回退默认日志模式），不影响功能"
                )
            logger.info("[HistorySave] SQLite 聊天记录数据库初始化成功")
            return True
        except Exception as e:
            logger.error(f"[HistorySave] SQLite 聊天记录数据库初始化失败: {e}")
            return False

    async def close(self):
        """关闭数据库连接（幂等；关闭后如再调用读写方法会自动重连）。"""
        async with self._connect_lock:
            if self._db is not None:
                try:
                    await self._db.close()
                except Exception as e:
                    logger.warning(f"[HistorySave] 关闭 SQLite 连接异常: {e}")
                # 置 None，使 _ensure_db 在需要时能够重新建连
                self._db = None
                logger.info("[HistorySave] SQLite 聊天记录数据库已关闭")

    async def ping(self) -> dict:
        """检测数据库连接状态（顶层键与 MySQL 版 ping 对齐）。

        Returns:
            dict: {"connected": bool, "latency_ms": float, "pool": dict,
                "db": str}。pool 为单连接占位信息（used/current_size/
                min_size/max_size 恒 1，core/commands.py 的 pool_info 取值
                路径按该结构取值不炸）；db 键给 history.db 文件路径
                （webapi 展示用）。失败静默返回 connected=False（与 MySQL
                版 ping 一致，不在 ping 内记日志）。
        """
        start = time.time()
        connected = True
        try:
            async with self._lock:
                await self._ensure_db()
                async with self._db.execute("SELECT 1") as cursor:
                    await cursor.fetchone()
        except Exception:
            connected = False
        if connected:
            latency = round((time.time() - start) * 1000, 2)
        else:
            latency = -1
        return {
            "connected": connected,
            "latency_ms": latency,
            "pool": {
                "used": 1,
                "current_size": 1,
                "min_size": 1,
                "max_size": 1,
            },
            "db": self.db_path,
        }

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

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
        timestamp: datetime | None = None,
    ) -> bool:
        """插入一条聊天记录。

        Args:
            group_id: 群号（字符串形式）
            sender_id: 发送者 QQ 号（字符串形式）
            sender_name: 发送者昵称
            message_type: 消息类型（text/mixed，纯图片消息不入文本表）
            content: 文本内容
            message_id: 消息 ID；空串存 NULL（UNIQUE 索引不去重 NULL，
                与 MySQL「多条无 ID 消息可共存」语义一致）
            at_list: 本条消息 @ 的 QQ 号列表，英文逗号分隔（如 "123,456"）；无则空串
            reply_id: 本条消息回复的目标消息 message_id；无则空串
            timestamp: 消息时间戳；None 取当前时间（实时消息现行为），
                补录路径传入消息原始到达时刻（F11，契约 K1）

        Returns:
            bool: 是否插入成功；message_id 已存在（唯一索引去重）按
                「已入库」幂等返回 True
        """
        try:
            # TEXT 列上限 65535 字节（MySQL 侧行为对齐）：超长内容截断并留
            # 标记，避免极端长内容造成存储异常导致消息彻底丢失
            if content and len(content.encode("utf-8")) > 60000:
                content = (
                    content.encode("utf-8")[:60000].decode("utf-8", "ignore")
                    + "\n…[内容过长已截断]"
                )
            # F11：补录路径透传消息到达时刻；实时路径缺省取当前时间。
            # timestamp 统一转 Unix 秒 INTEGER 存储（int 显式转换）
            if timestamp is None:
                timestamp = datetime.now()
            ts = self._to_epoch(timestamp)
            # 空 message_id 归一为 NULL：UNIQUE 索引对 NULL 不去重，
            # 保证多条无 ID 消息可共存（与 MySQL 侧行为一致）
            mid = message_id or None
            async with self._lock:
                await self._ensure_db()
                async with self._db.execute(
                    """INSERT INTO chat_history
                       (message_id, group_id, sender_id, sender_name, content,
                        timestamp, type, raw_data, image_urls, at_list, reply_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)""",
                    (
                        mid,
                        group_id,
                        sender_id,
                        sender_name,
                        content,
                        ts,
                        message_type,
                        at_list,
                        reply_id,
                    ),
                ):
                    pass
                await self._db.commit()
            return True
        except sqlite3.IntegrityError as e:
            # 唯一索引去重：message_id 已存在（实时与补库并发 / 写入失败
            # 退缓冲重试窗口的竞态兜底）。MySQL 侧普通索引会写入重复行，
            # 此处按「消息已入库」幂等成功处理——返回 True 让调用方
            # （saver 失败退缓冲重试链）不再对同一条消息无限重试
            logger.warning(f"[HistorySave] 聊天记录已存在，跳过重复插入: {e}")
            return True
        except Exception as e:
            logger.error(f"[HistorySave] 插入聊天记录失败: {e}")
            return False

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
        # 超长链接跳过入库并告警（与 MySQL VARCHAR(1024) 列宽行为对齐），
        # 避免极端长 URL 入库（且不影响同消息的文本记录）
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
            ts = self._to_epoch(timestamp)
            async with self._lock:
                await self._ensure_db()
                async with self._db.execute(
                    """INSERT INTO image_records
                       (message_id, group_id, sender_id, sender_name, image_url,
                        timestamp)
                       VALUES (NULL, ?, ?, ?, ?, ?)""",
                    (group_id, sender_id, sender_name, image_url, ts),
                ):
                    pass
                await self._db.commit()
            return True
        except Exception as e:
            logger.error(f"[HistorySave] 插入图片记录失败: {e}")
            return False

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    async def query_messages(
        self,
        group_id: str | None = None,
        sender_id: str | None = None,
        time_start: str | None = None,
        time_end: str | None = None,
        keyword: str | None = None,
        page: int = 1,
        page_size: int = 50,
        raise_on_error: bool = False,
    ) -> dict:
        """查询聊天记录（支持多条件过滤和分页）。

        Args:
            group_id: 群号过滤（可选，字符串形式）
            sender_id: QQ 号过滤（可选，字符串形式）
            time_start: 开始时间（格式 YYYY-MM-DD HH:MM:SS，闭区间）
            time_end: 结束时间（同上）
            keyword: 关键词，模糊匹配 content 与 sender_name（可选）；
                LIKE 通配符转义与大小写不敏感语义与 MySQL 侧一致
            page: 页码（从 1 开始）
            page_size: 每页条数
            raise_on_error: True 时查询异常直接向上抛出（v0.7.0 供对外
                公共 API 精确感知失败并写失败日志）；默认 False 行为
                不变（异常记日志后返回空结果）

        Returns:
            dict: {"total": int, "records": list[dict]}
        """
        result: dict = {"total": 0, "records": []}
        try:
            conditions = []
            params: list = []

            if group_id:
                conditions.append("group_id = ?")
                params.append(group_id)
            if sender_id:
                conditions.append("sender_id = ?")
                params.append(sender_id)
            if time_start:
                # 时间闭区间：字符串解析为 Unix 秒后与 INTEGER 列比较；
                # 非法格式按空结果处理（1=0），与 MySQL 隐式转换失败的
                # 实际行为（匹配不到任何行）等效，避免退化为全量查询
                start_ts = self._parse_time_to_ts(time_start)
                if start_ts is not None:
                    conditions.append("timestamp >= ?")
                    params.append(start_ts)
                else:
                    conditions.append("1=0")
            if time_end:
                end_ts = self._parse_time_to_ts(time_end)
                if end_ts is not None:
                    conditions.append("timestamp <= ?")
                    params.append(end_ts)
                else:
                    conditions.append("1=0")
            if keyword:
                # 转义 LIKE 通配符，避免用户输入的 % _ 干扰匹配
                kw = (
                    keyword.replace("\\", "\\\\")
                    .replace("%", "\\%")
                    .replace("_", "\\_")
                )
                like = f"%{kw}%"
                # SQLite LIKE 默认大小写敏感且无默认转义符（MySQL
                # utf8mb4_unicode_ci 默认不敏感且以反斜杠转义），LOWER()
                # 包裹对齐不区分大小写语义，ESCAPE '\' 使 kw 的反斜杠
                # 转义（\% \_ \\）与 MySQL 行为一致
                conditions.append(
                    "(LOWER(content) LIKE LOWER(?) ESCAPE '\\'"
                    " OR LOWER(sender_name) LIKE LOWER(?) ESCAPE '\\')"
                )
                params.extend([like, like])

            where_clause = " AND ".join(conditions) if conditions else "1=1"
            offset = (page - 1) * page_size

            async with self._lock:
                await self._ensure_db()
                # 查询总数
                async with self._db.execute(
                    f"SELECT COUNT(*) FROM chat_history WHERE {where_clause}",
                    params,
                ) as cursor:
                    row = await cursor.fetchone()
                result["total"] = int(row[0]) if row and row[0] is not None else 0

                # 查询数据（排序与 MySQL 侧一致：timestamp DESC；
                # at_list/reply_id 为 v0.4.0 新增列，存量行可能为 NULL，
                # 保持原样返回，下游按 .get 兜底）
                async with self._db.execute(
                    f"""SELECT id, timestamp, group_id, sender_id, sender_name,
                               type, content, message_id, at_list, reply_id
                        FROM chat_history WHERE {where_clause}
                        ORDER BY timestamp DESC
                        LIMIT ? OFFSET ?""",
                    params + [page_size, offset],
                ) as cursor:
                    rows = await cursor.fetchall()
            result["records"] = [self._row_to_message_dict(row) for row in rows]
        except Exception as e:
            if raise_on_error:
                # v0.7.0：对外公共 API 需要精确感知失败（写失败日志），
                # 由调用方负责捕获；内部模块调用默认 False 行为不变
                raise
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
            rows: list = []
            async with self._lock:
                await self._ensure_db()
                # IN 列表分块查询：每块 ≤ 500（对齐 MySQL 侧范式）
                for i in range(0, len(ids), _CHUNK_SIZE):
                    chunk = ids[i : i + _CHUNK_SIZE]
                    placeholders = ",".join(["?"] * len(chunk))
                    async with self._db.execute(
                        f"""SELECT timestamp, group_id, sender_id, sender_name,
                                   type, content, message_id, at_list, reply_id
                            FROM chat_history
                            WHERE message_id IN ({placeholders})""",
                        chunk,
                    ) as cursor:
                        rows.extend(await cursor.fetchall())
            return [self._row_to_message_dict(row, include_id=False) for row in rows]
        except Exception as e:
            self._log_op_error("get_messages_by_ids", "按ID批量查询聊天记录", e)
            return []

    async def get_existing_message_ids(
        self, group_id: str, message_ids: list[str]
    ) -> set[str]:
        """按群批量查询已存在的 message_id 集合（重载补库去重用）。

        仅返回命中 chat_history.message_id 非空的 id；message_ids 为空返回空集；
        IN 列表分块（每块 ≤500）避免超长 SQL；全部参数化绑定；
        异常记 error 日志后返回空集（让上层按"全不存在"处理，不阻断补库）。
        """
        ids = [mid for mid in (message_ids or []) if mid]
        if not ids:
            return set()
        existing: set[str] = set()
        try:
            async with self._lock:
                await self._ensure_db()
                # IN 列表分块查询：每块 ≤ 500，避免超长 SQL 与参数占位符过多
                for i in range(0, len(ids), _CHUNK_SIZE):
                    chunk = ids[i : i + _CHUNK_SIZE]
                    placeholders = ",".join(["?"] * len(chunk))
                    async with self._db.execute(
                        f"""SELECT message_id FROM chat_history
                            WHERE group_id = ? AND message_id IN ({placeholders})""",
                        [group_id] + chunk,
                    ) as cursor:
                        rows = await cursor.fetchall()
                    for row in rows:
                        if row and row[0]:
                            existing.add(str(row[0]))
            return existing
        except Exception as e:
            self._log_op_error("get_existing_message_ids", "查询群内已存在消息ID", e)
            return set()

    async def get_existing_image_urls(self, group_id: str, urls: list[str]) -> set[str]:
        """按群批量查询已存在的图片 URL 集合（v0.6.0 重载补库图片去重用）。

        过滤空串；IN 列表分块（每块 ≤500）避免超长 SQL；全部参数化绑定；
        异常记 error 日志后返回空集（让上层按"全不存在"处理，不阻断补库）。
        """
        items = [url for url in (urls or []) if url]
        if not items:
            return set()
        existing: set[str] = set()
        try:
            async with self._lock:
                await self._ensure_db()
                # IN 列表分块查询：每块 ≤ 500，避免超长 SQL 与参数占位符过多
                for i in range(0, len(items), _CHUNK_SIZE):
                    chunk = items[i : i + _CHUNK_SIZE]
                    placeholders = ",".join(["?"] * len(chunk))
                    async with self._db.execute(
                        f"""SELECT image_url FROM image_records
                            WHERE group_id = ? AND image_url IN ({placeholders})""",
                        [group_id] + chunk,
                    ) as cursor:
                        rows = await cursor.fetchall()
                    for row in rows:
                        if row and row[0]:
                            existing.add(str(row[0]))
            return existing
        except Exception as e:
            self._log_op_error("get_existing_image_urls", "查询群内已存在图片链接", e)
            return set()

    async def get_all_group_ids(self) -> list[str]:
        """全量群清单（v0.6.0）：chat_history 中实际有数据的群（group_id 去重）。

        all_mode 全局记录模式下白名单无意义（录制覆盖所有群），补库群清单
        改以「有数据的群」为准——无数据的群拉协议端也是空，跳过即可。

        Returns:
            list[str]: 字符串形式 group_id 列表；异常记日志返回 []（由调用方降级）
        """
        try:
            async with self._lock:
                await self._ensure_db()
                async with self._db.execute(
                    "SELECT DISTINCT group_id FROM chat_history"
                ) as cursor:
                    rows = await cursor.fetchall()
            return [str(row[0]) for row in rows if row and row[0] is not None]
        except Exception as e:
            self._log_op_error("get_all_group_ids", "查询有数据的群清单", e)
            return []

    async def get_recent_messages(self, group_id: str, limit: int) -> list[dict]:
        """按群取最近 limit 条已记录消息的 message_id 与 content（补库重叠边界检测用）。

        按 timestamp DESC（id DESC 兜底）取最新记录；content 供无 message_id
        的消息做内容比对兜底。补库路径会向表内插入原始时间戳早于实时记录的消息，
        自增 id 与 timestamp 不再单调同向，按 id DESC 取出的「最近 N 条」会漏掉
        真正最新的实时记录导致重叠检测失效；改以 timestamp 为第一排序键，与时间
        语义对齐（命中 idx_chat_history_group_time 索引，无额外代价）。
        limit <= 0 返回空列表；异常记 error 日志后返回空列表（让上层按
        「无重叠参照」处理，退化为窗口/轮数停止，不阻断补库）。
        """
        if limit <= 0:
            return []
        try:
            async with self._lock:
                await self._ensure_db()
                async with self._db.execute(
                    """SELECT message_id, content FROM chat_history
                       WHERE group_id = ?
                       ORDER BY timestamp DESC, id DESC LIMIT ?""",
                    (group_id, limit),
                ) as cursor:
                    rows = await cursor.fetchall()
            return [
                {
                    "message_id": str(row[0]) if row[0] is not None else "",
                    "content": str(row[1]) if row[1] is not None else "",
                }
                for row in rows
            ]
        except Exception as e:
            self._log_op_error("get_recent_messages", "查询最近消息重叠参照", e)
            return []

    async def get_last_message_time(self, group_id: str):
        """按群取最后一条已记录消息的时间（补库窗口起点用）。

        返回该群 chat_history 中最大的 `timestamp`（datetime）或 None（群无记录）；
        补库以此时间往前偏移固定重叠量作为窗口起点，保证把「最后一条已知记录之后」
        的缺口拉取补齐（比 v0.6.1 用 `last_terminate_time` 停机时间更贴近真实缺口）。
        异常记 error 日志后返回 None（让上层按「无记录」回退 `backfill_hours`
        窗口，不阻断补库）。

        Args:
            group_id: 群号（字符串形式）

        Returns:
            datetime | None: 该群最后一条已记录消息时间；无记录/异常返回 None
        """
        try:
            async with self._lock:
                await self._ensure_db()
                async with self._db.execute(
                    "SELECT MAX(timestamp) FROM chat_history WHERE group_id = ?",
                    (group_id,),
                ) as cursor:
                    row = await cursor.fetchone()
            if not row or row[0] is None:
                return None
            return datetime.fromtimestamp(int(row[0]))
        except Exception as e:
            self._log_op_error("get_last_message_time", "查询群最后消息时间", e)
            return None

    async def count_messages(
        self,
        group_id: str | None = None,
        sender_ids: list[str] | None = None,
        time_start: str | None = None,
        time_end: str | None = None,
        raise_on_error: bool = False,
    ) -> dict:
        """文本消息计数（v0.7.0 对外统计接口数据层，批量 + 高并发优化）。

        群总计（COUNT）+ 批量群内（GROUP BY sender_id）+ 批量跨群
        （GROUP BY sender_id）三组计数按需执行；批量聚合为**单条 SQL**
        （禁止 N+1 循环查询），IN 列表 ≤500 单块（对齐 get_existing_message_ids
        范式），全参数化绑定；时间条件同 query_messages 范式（闭区间、
        非法格式按空结果）。索引利用：群总计走 idx_chat_history_group_time、
        群内走 idx_chat_history_group_time（sender 行内裁剪）、
        跨群走 idx_chat_history_sender。

        与 MySQL 侧的异常契约差异（PRD F3 指定）：MySQL 版异常直接向上抛、
        参数校验在上游 public_api；本版增加 raise_on_error 参数（PRD 签名），
        参数缺失/非法与 SQL 执行失败默认（False）不抛，返回含 ``_error`` 键的
        结果 dict（语义与 v0.7.0 对外 API 的 _error 约定一致：成功时无此键）；
        raise_on_error=True 时 SQL 异常向上抛（由调用方统一捕获）。

        Args:
            group_id: 限定单群（可选，字符串形式）
            sender_ids: 批量 QQ 号（可选，≤500）
            time_start: 开始时间（格式 YYYY-MM-DD HH:MM:SS，闭区间）
            time_end: 结束时间（同上）
            raise_on_error: True 时 SQL 执行异常直接向上抛出；默认 False

        Returns:
            dict: {
                "group_total": int,      # group_id 给定时该群文本消息总数
                "senders": {             # sender_ids 给定时每人计数
                    sid: {"in_group": int, "total": int},
                    # in_group：该人在群内发言数（group_id 未给定时恒 0，
                    #   与 MySQL 版 group_id=NULL 比较为假的行为一致）；
                    # total：该人跨群总发言数
                },
            }；参数缺失/非法或执行失败时额外含 "_error" 键
        """
        out: dict = {}

        # 参数校验（与 public_api 上游校验同文案；数据层兜底防全表扫描）
        ids = [sid for sid in (sender_ids or []) if sid]
        if not group_id and not ids:
            out["_error"] = "请至少提供 group_id 或 sender_ids 之一"
            return out
        if len(ids) > _CHUNK_SIZE:
            out["_error"] = (
                f"sender_ids 单次最多 {_CHUNK_SIZE} 个，当前 {len(ids)} 个，请分批查询"
            )
            return out

        # 时间条件（公共 WHERE 片段，参数化；非法格式按空结果 1=0）
        time_conds = []
        time_params: list = []
        if time_start:
            start_ts = self._parse_time_to_ts(time_start)
            if start_ts is not None:
                time_conds.append("timestamp >= ?")
                time_params.append(start_ts)
            else:
                time_conds.append("1=0")
        if time_end:
            end_ts = self._parse_time_to_ts(time_end)
            if end_ts is not None:
                time_conds.append("timestamp <= ?")
                time_params.append(end_ts)
            else:
                time_conds.append("1=0")
        time_where = " AND ".join(time_conds)
        time_clause = f" AND {time_where}" if time_where else ""

        try:
            # 群总计：COUNT 走 idx_chat_history_group_time
            if group_id:
                async with self._lock:
                    await self._ensure_db()
                    async with self._db.execute(
                        f"SELECT COUNT(*) FROM chat_history"
                        f" WHERE group_id = ?{time_clause}",
                        [group_id] + time_params,
                    ) as cursor:
                        row = await cursor.fetchone()
                # 防御性 int 转换：不同驱动/游标类型可能返回 int/str
                out["group_total"] = int(row[0]) if row and row[0] is not None else 0

            # 批量计数：GROUP BY 单条 SQL，IN ≤500 单块
            if ids:
                async with self._lock:
                    await self._ensure_db()
                    in_placeholders = ",".join(["?"] * len(ids))
                    # 批量群内计数（group_id 未提供时 = NULL 比较为假 → 全 0，
                    # 与 MySQL 版行为一致）
                    async with self._db.execute(
                        f"SELECT sender_id, COUNT(*) FROM chat_history"
                        f" WHERE group_id = ? AND sender_id IN ({in_placeholders})"
                        f"{time_clause} GROUP BY sender_id",
                        [group_id] + ids + time_params,
                    ) as cursor:
                        in_rows = await cursor.fetchall()
                    # 批量跨群计数
                    async with self._db.execute(
                        f"SELECT sender_id, COUNT(*) FROM chat_history"
                        f" WHERE sender_id IN ({in_placeholders})"
                        f"{time_clause} GROUP BY sender_id",
                        ids + time_params,
                    ) as cursor:
                        total_rows = await cursor.fetchall()
                in_group_map = {
                    str(row[0]): int(row[1]) for row in in_rows if row[0] is not None
                }
                total_map = {
                    str(row[0]): int(row[1]) for row in total_rows if row[0] is not None
                }
                out["senders"] = {
                    sid: {
                        "in_group": in_group_map.get(sid, 0),
                        "total": total_map.get(sid, 0),
                    }
                    for sid in ids
                }
            return out
        except Exception as e:
            if raise_on_error:
                raise
            self._log_op_error("count_messages", "文本消息计数", e)
            out["_error"] = "查询执行失败，详情见查询日志"
            return out

    async def get_all_groups_summary(self) -> list[dict]:
        """全量群清单：chat_history 中实际有数据的群（v0.5.6 群下拉模式感知配套）。

        SQL 与返回结构照抄 core/profile/fetcher.py 的 get_all_groups_summary
        （417-433 行）：按 group_id 分组，COUNT(*) 计消息数、MAX(timestamp)
        记最近活跃时刻；排序 COUNT(*) DESC、同数 group_id ASC（输出确定性）。
        不限时间窗口、不截断（群数量级有限，走 idx_chat_history_group_time）。

        Returns:
            list[dict]: [{"group_id": str, "count": int,
                "last_active": datetime | None}]
        """
        try:
            async with self._lock:
                await self._ensure_db()
                async with self._db.execute(
                    "SELECT group_id, COUNT(*) AS cnt, MAX(timestamp) "
                    "FROM chat_history GROUP BY group_id "
                    "ORDER BY cnt DESC, group_id ASC"
                ) as cursor:
                    rows = await cursor.fetchall()
            return [
                {
                    "group_id": str(row[0]),
                    "count": int(row[1] or 0),
                    "last_active": (
                        datetime.fromtimestamp(int(row[2]))
                        if row[2] is not None
                        else None
                    ),
                }
                for row in rows
            ]
        except Exception as e:
            logger.error(f"[HistorySave] 查询有数据的群汇总失败: {e}")
            return []

    # ------------------------------------------------------------------
    # 统计（鸭子类型面：core/commands.py 与 core/webapi/storage.py 直接调用）
    # ------------------------------------------------------------------

    async def get_stats(self) -> dict:
        """获取统计信息（今日消息数、今日图片数、总消息数、总图片数）。

        语义逐字对齐 core/db_mysql/stats.py 的 get_stats："今日"区间在
        bot 侧按 datetime.now() 的当天零点计算，与写入侧时区语义保持一致。

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
            today_start_ts = self._to_epoch(today_start)
            tomorrow_start_ts = self._to_epoch(tomorrow_start)
            async with self._lock:
                await self._ensure_db()
                async with self._db.execute(
                    "SELECT COUNT(*), "
                    "SUM(timestamp >= ? AND timestamp < ?) "
                    "FROM chat_history",
                    (today_start_ts, tomorrow_start_ts),
                ) as cursor:
                    row = await cursor.fetchone()
                if row:
                    stats["total_messages"] = int(row[0] or 0)
                    stats["today_messages"] = int(row[1] or 0)

                async with self._db.execute(
                    "SELECT COUNT(*), "
                    "SUM(timestamp >= ? AND timestamp < ?) "
                    "FROM image_records",
                    (today_start_ts, tomorrow_start_ts),
                ) as cursor:
                    row = await cursor.fetchone()
                if row:
                    stats["total_images"] = int(row[0] or 0)
                    stats["today_images"] = int(row[1] or 0)
        except Exception as e:
            self._log_op_error("get_stats", "获取统计信息", e)
        return stats

    async def get_daily_stats(self, days: int = 7) -> list[dict]:
        """获取最近 N 天的每日统计。

        语义逐字对齐 core/db_mysql/stats.py 的 get_daily_stats：时间窗口在
        bot 侧计算（今天零点往前推 N 天至今），日期串 "YYYY-MM-DD" 经
        DATE(timestamp, 'unixepoch', 'localtime') 还原为本地日期（与写入侧
        int(datetime.timestamp()) 的本地时区语义一致）。

        Args:
            days: 查询天数

        Returns:
            list[dict]: 每日统计列表
        """
        result = []
        try:
            cutoff = datetime.now().replace(
                hour=0, minute=0, second=0, microsecond=0
            ) - timedelta(days=days)
            cutoff_ts = self._to_epoch(cutoff)
            async with self._lock:
                await self._ensure_db()
                async with self._db.execute(
                    """SELECT DATE(timestamp, 'unixepoch', 'localtime') as date,
                              COUNT(*) as count
                       FROM chat_history
                       WHERE timestamp >= ?
                       GROUP BY date ORDER BY date""",
                    (cutoff_ts,),
                ) as cursor:
                    msg_rows = await cursor.fetchall()
                msg_map = {str(row[0]): row[1] for row in msg_rows}

                async with self._db.execute(
                    """SELECT DATE(timestamp, 'unixepoch', 'localtime') as date,
                              COUNT(*) as count
                       FROM image_records
                       WHERE timestamp >= ?
                       GROUP BY date ORDER BY date""",
                    (cutoff_ts,),
                ) as cursor:
                    img_rows = await cursor.fetchall()
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

    # ------------------------------------------------------------------
    # 维护
    # ------------------------------------------------------------------

    async def clean_old_images(self, retention_days: int) -> int:
        """清理指定天数之前的图片记录。

        Args:
            retention_days: 保留天数

        Returns:
            int: 删除的记录数，失败返回 -1
        """
        try:
            # 钳制到安全范围，防止异常大的设置值使 timedelta/日期运算
            # 抛 OverflowError 导致清理功能永久失效（与 MySQL 侧一致）
            retention_days = min(max(int(retention_days), 0), 36500)
            cutoff = datetime.now() - timedelta(days=retention_days)
            cutoff_ts = self._to_epoch(cutoff)
            async with self._lock:
                await self._ensure_db()
                async with self._db.execute(
                    "DELETE FROM image_records WHERE timestamp < ?",
                    (cutoff_ts,),
                ) as cursor:
                    deleted = cursor.rowcount
                await self._db.commit()
            return deleted if deleted >= 0 else 0
        except Exception as e:
            logger.error(f"[HistorySave] 清理图片记录失败: {e}")
            return -1

    async def purge_all(self) -> dict:
        """清空所有聊天记录和图片记录（不可恢复），并复位自增 ID。

        SQLite 无 TRUNCATE：DELETE FROM 清空两表 + 清空 sqlite_sequence
        复位自增 ID（MySQL 侧 TRUNCATE 的对等实现）；truncated 恒 False
        （webapi 端点仅用于日志文案选择，False 即走 DELETE 文案分支）。

        Returns:
            dict: {"success": bool, "deleted_messages": int, "deleted_images": int,
                "truncated": bool}
        """
        result: dict = {
            "success": False,
            "deleted_messages": 0,
            "deleted_images": 0,
            "truncated": False,
        }
        try:
            async with self._lock:
                await self._ensure_db()
                # 两表独立清空：一张表失败不影响另一张，
                # 成功状态由各自的返回值汇总
                messages_ok = await self._purge_table(
                    self._db, "chat_history", result, "deleted_messages"
                )
                images_ok = await self._purge_table(
                    self._db, "image_records", result, "deleted_images"
                )
                # 复位自增 ID（DELETE 不会清 sqlite_sequence；两表从未插入
                # 过数据时该表不存在，跳过即可）
                if messages_ok and images_ok:
                    try:
                        async with self._db.execute("DELETE FROM sqlite_sequence"):
                            pass
                    except sqlite3.OperationalError:
                        pass
                await self._db.commit()
            result["success"] = messages_ok and images_ok
        except Exception as e:
            logger.error(f"[HistorySave] 清空所有数据失败: {e}")
        # 所有路径都输出审计日志（含部分失败情形），与 MySQL 侧同构
        if result["success"]:
            logger.warning(
                f"[HistorySave] 已清空所有数据 (DELETE): "
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

    async def _purge_table(
        self, conn: aiosqlite.Connection, table: str, result: dict, count_key: str
    ) -> bool:
        """清空单张表并把删除条数写入 result。

        表名来自本类硬编码，无注入风险。先 SELECT COUNT(*) 取预统计值
        （大表审计计数用），DELETE 后 rowcount 异常（如 -1）时回退预统计值
        （与 MySQL 侧 _purge_table 的审计计数语义一致）。

        Args:
            conn: 数据库连接
            table: 表名
            result: 用于写入删除条数的结果字典
            count_key: result 中删除条数的键名

        Returns:
            bool: 该表是否清空成功
        """
        try:
            async with conn.execute(f"SELECT COUNT(*) FROM {table}") as cursor:
                row = await cursor.fetchone()
            pre_count = int(row[0]) if row and row[0] is not None else 0
        except Exception as e:
            logger.error(f"[HistorySave] 预统计 {table} 行数失败: {e}")
            return False

        try:
            async with conn.execute(f"DELETE FROM {table}") as cursor:
                deleted = cursor.rowcount
            # rowcount 异常（如 -1）时回退到预统计值，保证审计计数合理
            result[count_key] = deleted if deleted >= 0 else pre_count
            return True
        except Exception as e:
            logger.error(f"[HistorySave] DELETE FROM {table} 失败: {e}")
            return False

    # ------------------------------------------------------------------
    # meta 表读写（迁移标记用，模块 C 依赖这两个方法名）
    # ------------------------------------------------------------------

    async def get_meta(self, key: str) -> str | None:
        """读取 meta 表指定键的值。

        Args:
            key: meta 键名（如 "migrated_from_mysql"）

        Returns:
            str | None: 键值；键不存在或查询异常返回 None
        """
        try:
            async with self._lock:
                await self._ensure_db()
                async with self._db.execute(
                    "SELECT value FROM meta WHERE key = ?", (key,)
                ) as cursor:
                    row = await cursor.fetchone()
            if not row or row[0] is None:
                return None
            return str(row[0])
        except Exception as e:
            logger.error(f"[HistorySave] 读取 meta 键 {key!r} 失败: {e}")
            return None

    async def set_meta(self, key: str, value: str) -> bool:
        """写入（UPSERT）meta 表指定键的值。

        Args:
            key: meta 键名（如 "migrated_from_mysql"）
            value: 键值

        Returns:
            bool: 是否写入成功
        """
        try:
            async with self._lock:
                await self._ensure_db()
                await self._db.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                    (key, value),
                )
                await self._db.commit()
            return True
        except Exception as e:
            logger.error(f"[HistorySave] 写入 meta 键 {key!r} 失败: {e}")
            return False

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _log_op_error(self, key: str, action: str, exc: Exception):
        """查询类操作的错误日志节流（60s 窗口，按 key 独立累计）。

        与 MySQLManagerBase._log_op_error 同构：数据库不可用/连接关闭后，
        Web 后台轮询会高频触发查询操作失败，逐条记录会刷屏。首次失败
        立即输出 ERROR，窗口内的后续次数累计后在下一条件日志中一并汇报。
        写入类操作（insert/clean/purge）低频且每次失败都意味着真实数据
        丢失，不走此节流。
        """
        now = time.monotonic()
        last, count = self._op_err_state.get(key, (0.0, 0))
        count += 1
        if now - last < 60:
            self._op_err_state[key] = (last, count)
            return
        self._op_err_state[key] = (now, 0)
        logger.error(f"[HistorySave] {action}失败（近期累计 {count} 次）: {exc}")

    @staticmethod
    def _to_epoch(dt: datetime) -> int:
        """把 naive 本地 datetime 转为 Unix 秒（Windows 预 1970 安全）。

        优先 ``datetime.timestamp()``（正确处理 DST）；Windows 的
        mktime 不支持 1970 前的本地时间（超长保留期钳制到 36500 天后的
        极早 cutoff 会触发 OSError [Errno 22]），此时退回纯算术 epoch
        （忽略 DST，误差 ≤1h，对清理/过滤窗口无实质影响）。

        Args:
            dt: naive 本地时间

        Returns:
            int: Unix 秒时间戳
        """
        try:
            return int(dt.timestamp())
        except (OSError, OverflowError, ValueError):
            return int((dt - datetime(1970, 1, 1)).total_seconds())

    @staticmethod
    def _parse_time_to_ts(value: str) -> int | None:
        """把时间字符串解析为 Unix 秒时间戳（本地时区，与写入侧一致）。

        兼容 "YYYY-MM-DD HH:MM:SS" / "YYYY-MM-DD HH:MM" / "YYYY-MM-DD"
        三种格式（纯日期按零点起算）。解析失败返回 None，由调用方按
        「非法时间参数 → 空结果」处理（与 MySQL 隐式转换失败行为等效）。

        Args:
            value: 时间字符串

        Returns:
            int | None: Unix 秒时间戳；解析失败返回 None
        """
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return SQLiteManager._to_epoch(datetime.strptime(value, fmt))
            except (ValueError, TypeError):
                continue
        logger.warning(
            f"[HistorySave] 时间参数格式非法（应为 YYYY-MM-DD HH:MM:SS）：{value!r}"
        )
        return None

    @staticmethod
    def _row_to_message_dict(row, include_id: bool = True) -> dict:
        """把 chat_history 查询行组装为与 MySQL 侧同构的 dict。

        键名与键序对齐 MySQL 侧 query_messages（含 id）/
        get_messages_by_ids（不含 id）的 DictCursor 行：
        id/timestamp/group_id/sender_id/sender_name/message_type/content/
        message_id/at_list/reply_id。

        - timestamp：Unix 秒 → "YYYY-MM-DD HH:MM:SS" 字符串（与 MySQL
          datetime 经 str() 输出同格式，下游 strptime/fromisoformat 兼容）；
        - type 列映射为 MySQL 侧的 message_type 键；
        - at_list/reply_id 为 v0.4.0 新增列语义，存量行可能为 NULL，
          保持原样返回，下游按 .get 兜底。

        Args:
            row: aiosqlite 查询行（元组，列序与 SELECT 列表一致）
            include_id: 是否包含 id 键（query_messages 为 True，
                get_messages_by_ids 为 False，与 MySQL 侧 SELECT 列表对齐）

        Returns:
            dict: 与 MySQL 侧同构的记录字典
        """
        if include_id:
            (
                row_id,
                ts,
                group_id,
                sender_id,
                sender_name,
                msg_type,
                content,
                message_id,
                at_list,
                reply_id,
            ) = row
        else:
            row_id = None
            (
                ts,
                group_id,
                sender_id,
                sender_name,
                msg_type,
                content,
                message_id,
                at_list,
                reply_id,
            ) = row
        record: dict = {}
        if include_id:
            record["id"] = int(row_id) if row_id is not None else None
        record["timestamp"] = (
            datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
            if ts is not None
            else None
        )
        record["group_id"] = group_id
        record["sender_id"] = sender_id
        record["sender_name"] = sender_name
        record["message_type"] = msg_type
        record["content"] = content
        record["message_id"] = message_id
        record["at_list"] = at_list
        record["reply_id"] = reply_id
        return record
