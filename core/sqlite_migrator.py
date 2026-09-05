"""MySQL → SQLite 一次性聊天记录迁移器（v0.9.0）。

把既有 MySQL 库（chat_history / image_records）中的历史数据一次性导入
本地 SQLite（history.db），供「storage_backend=sqlite」的部署把旧数据带过来。

设计要点（PRD v0.9.0 §4 F6）：
- **只读 MySQL、只写 SQLite**：迁移不改源库任何数据；读取侧自建独立
  aiomysql 连接池（与运行时后端解耦，sqlite 模式下 MySQLManager 根本不存在），
  用完即关。
- **幂等可重跑**：分页按 MySQL 自增 id 升序推进；写入复用 SQLiteManager
  ``insert_chat_message`` / ``insert_image_record``——chat_history 的
  message_id 唯一索引使重复消息自动跳过（图片表无唯一约束，按
  (group_id, image_url) 预查去重）。半途失败已导入的数据保留，重跑零重复。
- **防重入**：``asyncio.Lock``，迁移进行中重复触发直接回提示。
- **全量标记**：全量迁移成功后写 meta ``migrated_from_mysql`` = 完成时间；
  此后再次无参全量被拦截（提示改用 hours 增量补导），避免大体量重放。
- **进度可见**：每批一条 INFO 日志；返回汇总文案（消息/图片/跳过/耗时）。

风险披露：大表全量迁移耗时与数据量成正比（MySQL 端读取 + 本地逐批写入）；
迁移期间实时消息链路正常工作、互不影响；history.db 体积随导入增长，
磁盘空间请预留。
"""

import asyncio
import time
from datetime import datetime, timedelta

from astrbot.api import logger

# 每批读取/写入行数（与补库批大小同量级；过大占内存、过小拖慢进度）
_MIGRATE_BATCH_SIZE = 2000

# 全量迁移完成标记的 meta 键
_META_KEY = "migrated_from_mysql"

# 用法提示（hours 参数非法 / 缺参场景）
USAGE_TEXT = (
    "用法：/导出聊天记录 [小时数]\n"
    "不填小时数 = 全量导入（把 MySQL 全部聊天记录导入本地 SQLite）\n"
    "填 N = 仅导入最近 N 小时（如 /导出聊天记录 24）"
)


class SQLiteMigrator:
    """一次性迁移执行器：MySQL chat_history/image_records → 本地 history.db。"""

    def __init__(self, sqlite_mgr, config):
        """仅组装引用，不建立任何连接（懒建，migrate() 时才连 MySQL）。

        Args:
            sqlite_mgr: SQLiteManager 实例（bootstrap 注入，写入侧复用其
                insert_*/get_existing_*/get_meta/set_meta 公开方法）
            config: AstrBotConfig dict，读取 mysql_host/mysql_port/
                mysql_user/mysql_password/mysql_database 五项建 MySQL 连接
        """
        cfg = config or {}
        self.sqlite_mgr = sqlite_mgr
        self._mysql_kwargs = {
            "host": str(cfg.get("mysql_host", "127.0.0.1")),
            "port": int(cfg.get("mysql_port", 3306)),
            "user": str(cfg.get("mysql_user", "root")),
            "password": str(cfg.get("mysql_password", "")),
            "db": str(cfg.get("mysql_database", "astrbot_history")),
        }
        # 防重入锁：同一迁移器实例同时只允许一个 migrate()
        self._lock = asyncio.Lock()

    async def _create_mysql_pool(self):
        """建立迁移专用 MySQL 只读连接池（独立于运行时后端）。

        抽为方法便于离线自测替换（注入假池）。autocommit=True：只读查询
        无需事务；池容量 1-2，迁移串行跑，不抢运行时资源。
        """
        import aiomysql

        return await aiomysql.create_pool(
            minsize=1,
            maxsize=2,
            autocommit=True,
            **self._mysql_kwargs,
        )

    async def migrate(self, hours: str = "") -> str:
        """执行一次迁移，返回用户可见结果文案（绝不抛出）。

        Args:
            hours: 空串 = 全量；数字串 = 仅导入最近 N 小时（timestamp >=
                now - N 小时）。非法值回用法提示。

        Returns:
            str: 成功汇总 / 拦截提示 / 失败原因（含可重跑说明）
        """
        if self._lock.locked():
            return "迁移正在进行中，请稍后再试。"

        # ---- hours 校验 ----
        full_run = not str(hours or "").strip()
        window_start = None
        if not full_run:
            try:
                n = int(str(hours).strip())
            except (TypeError, ValueError):
                return f"小时数无效：{hours!r}\n{USAGE_TEXT}"
            if n <= 0:
                return f"小时数需为正整数。\n{USAGE_TEXT}"
            window_start = datetime.now() - timedelta(hours=n)

        async with self._lock:
            # ---- 全量防重标记 ----
            if full_run:
                try:
                    done_at = await self.sqlite_mgr.get_meta(_META_KEY)
                except Exception:
                    done_at = None
                if done_at:
                    return (
                        f"已完成过全量迁移（标记时间 {done_at}）。"
                        f"如需补导近期数据请指定小时数，如 /导出聊天记录 24。"
                    )

            started = time.monotonic()
            pool = None
            try:
                pool = await self._create_mysql_pool()
            except Exception as e:
                logger.error(f"[HistorySave] 聊天记录迁移：连接 MySQL 失败: {e}")
                return f"连接 MySQL 失败：{e}。请确认数据库可用后重试。"

            try:
                msg_ok, msg_skip = await self._migrate_chat_history(pool, window_start)
                img_ok, img_skip = await self._migrate_image_records(pool, window_start)
            except Exception as e:
                logger.error(f"[HistorySave] 聊天记录迁移失败: {e}", exc_info=True)
                return f"迁移失败：{e}（已导入数据保留，可重跑，幂等不会重复。）"
            finally:
                try:
                    pool.close()
                    await pool.wait_closed()
                except Exception:
                    pass

            elapsed = time.monotonic() - started
            if full_run:
                try:
                    await self.sqlite_mgr.set_meta(
                        _META_KEY,
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    )
                except Exception as e:
                    logger.warning(f"[HistorySave] 聊天记录迁移：完成标记写入失败: {e}")
            scope = "全量" if full_run else f"近 {hours.strip()} 小时"
            return (
                f"迁移完成（{scope}）：消息 {msg_ok} 条、图片 {img_ok} 条、"
                f"跳过重复 {msg_skip + img_skip} 条、耗时 {elapsed:.1f} 秒。"
            )

    # ------------------------------------------------------------------
    # 两张表的分页导入（各自独立游标推进）
    # ------------------------------------------------------------------

    async def _fetch_batch(self, pool, sql: str, params: tuple) -> list[tuple]:
        """执行一条只读 SELECT，返回行列表（每批独立借还是长循环，此处
        单批内用完即归还连接，避免长时间占用池连接）。"""
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, params)
                return list(await cur.fetchall())

    async def _migrate_chat_history(self, pool, window_start) -> tuple[int, int]:
        """分页导入 chat_history（id 升序，批 2000）。返回 (导入数, 跳过数)。"""
        cols = (
            "id, timestamp, group_id, sender_id, sender_name, "
            "message_type, content, message_id, at_list, reply_id"
        )
        last_id = 0
        inserted = skipped = 0
        while True:
            where = "id > %s"
            params: list = [last_id]
            if window_start is not None:
                where += " AND timestamp >= %s"
                params.append(window_start)
            rows = await self._fetch_batch(
                pool,
                f"SELECT {cols} FROM chat_history WHERE {where} "
                f"ORDER BY id LIMIT {_MIGRATE_BATCH_SIZE}",
                tuple(params),
            )
            if not rows:
                break
            last_id = int(rows[-1][0])

            # 重复统计：按群分组预查已存在 message_id（get_existing_message_ids
            # 契约恒带 WHERE group_id=?，空串跨群预查永不命中——debug_0.md
            # PROD-BUG-01 修复：逐群查询后合并；分块 ≤500 由其内部完成）。
            # 预查仅影响 skipped/inserted 计数口径：真正防重复落库由
            # chat_history.message_id 唯一索引 + insert 幂等兜底
            by_group_ids: dict[str, list[str]] = {}
            for r in rows:
                if r[7]:
                    by_group_ids.setdefault(str(r[2] or ""), []).append(str(r[7]))
            existing: set[str] = set()
            for gid, ids in by_group_ids.items():
                found = await self.sqlite_mgr.get_existing_message_ids(gid, ids)
                if found:
                    existing |= found

            for r in rows:
                _id, ts, group_id, sender_id, sender_name = r[0], r[1], r[2], r[3], r[4]
                message_type, content, message_id, at_list, reply_id = (
                    r[5],
                    r[6],
                    r[7],
                    r[8],
                    r[9],
                )
                mid = str(message_id or "")
                if mid and mid in existing:
                    skipped += 1
                    continue
                ok = await self.sqlite_mgr.insert_chat_message(
                    group_id=str(group_id or ""),
                    sender_id=str(sender_id or ""),
                    sender_name=str(sender_name or ""),
                    message_type=str(message_type or "text"),
                    content=str(content or ""),
                    message_id=mid,
                    at_list=str(at_list or ""),
                    reply_id=str(reply_id or ""),
                    timestamp=ts if isinstance(ts, datetime) else None,
                )
                if ok:
                    inserted += 1
            logger.info(
                f"[HistorySave] 聊天记录迁移进度：消息 已导 {inserted} 条 / "
                f"跳过 {skipped} 条（游标 id<{last_id}）"
            )
            if len(rows) < _MIGRATE_BATCH_SIZE:
                break
        return inserted, skipped

    async def _migrate_image_records(self, pool, window_start) -> tuple[int, int]:
        """分页导入 image_records（id 升序，批 2000）。返回 (导入数, 跳过数)。"""
        cols = "id, timestamp, group_id, sender_id, sender_name, image_url"
        last_id = 0
        inserted = skipped = 0
        while True:
            where = "id > %s"
            params: list = [last_id]
            if window_start is not None:
                where += " AND timestamp >= %s"
                params.append(window_start)
            rows = await self._fetch_batch(
                pool,
                f"SELECT {cols} FROM image_records WHERE {where} "
                f"ORDER BY id LIMIT {_MIGRATE_BATCH_SIZE}",
                tuple(params),
            )
            if not rows:
                break
            last_id = int(rows[-1][0])

            # 图片表无唯一约束：按群分组预查已存在 URL 做跳过统计
            by_group: dict[str, list[str]] = {}
            for r in rows:
                gid = str(r[2] or "")
                url = str(r[5] or "")
                if url:
                    by_group.setdefault(gid, []).append(url)
            existing_urls: dict[str, set[str]] = {}
            for gid, urls in by_group.items():
                existing_urls[gid] = (
                    await self.sqlite_mgr.get_existing_image_urls(gid, urls)
                ) or set()

            for r in rows:
                ts, group_id, sender_id, sender_name, image_url = (
                    r[1],
                    str(r[2] or ""),
                    str(r[3] or ""),
                    str(r[4] or ""),
                    str(r[5] or ""),
                )
                if image_url and image_url in existing_urls.get(group_id, set()):
                    skipped += 1
                    continue
                ok = await self.sqlite_mgr.insert_image_record(
                    group_id=group_id,
                    sender_id=sender_id,
                    image_url=image_url,
                    sender_name=sender_name,
                    timestamp=ts if isinstance(ts, datetime) else None,
                )
                if ok:
                    inserted += 1
            logger.info(
                f"[HistorySave] 聊天记录迁移进度：图片 已导 {inserted} 条 / "
                f"跳过 {skipped} 条（游标 id<{last_id}）"
            )
            if len(rows) < _MIGRATE_BATCH_SIZE:
                break
        return inserted, skipped


__all__ = ["SQLiteMigrator", "USAGE_TEXT"]
