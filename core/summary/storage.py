"""总结 JSON 持久化存储（模块 G）。

将 :class:`SummaryResult` 以 JSON 文件形式落盘到 data 目录（base_dir 由
main.py 注入，通常为 ``StarTools.get_data_dir() / "summaries"``），并提供
列表 / 读取 / 过期清理能力。

目录结构::

    base_dir/<群号>/<unix时间戳>_<6位hex>.json

安全约束：群号仅允许纯数字（``^\\d+$``）、文件名仅允许
``^\\d+_[0-9a-f]{6}\\.json$``，任何 ``..`` / 目录分隔符均被拒绝，杜绝路径穿越。

文件 I/O 统一经 ``asyncio.to_thread`` 执行同步读写（单文件体量小，不引入
aiofiles 依赖）。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from datetime import datetime
from pathlib import Path

from astrbot.api import logger

from .models import SummaryResult

# 群号白名单字符校验：仅纯数字（防路径穿越）
_GROUP_ID_RE = re.compile(r"^\d+$")
# 总结文件名校验：<unix秒>_<6位小写hex>.json（防路径穿越，任何 ../ 与 / 均拒绝）
_FILENAME_RE = re.compile(r"^\d+_[0-9a-f]{6}\.json$")

# 落盘时间统一格式（字符串字典序 == 时间序，便于列表排序）
_TIME_FMT = "%Y-%m-%d %H:%M:%S"


class SummaryStorage:
    """总结 JSON 文件存储器（save / list / read / cleanup_expired）。"""

    def __init__(self, base_dir: Path):
        """初始化存储器。

        Args:
            base_dir: 总结文件根目录，由 main.py 注入
                （``StarTools.get_data_dir() / "summaries"``），本模块不负责获取
        """
        self.base_dir = Path(base_dir)

    # ========== 写入 ==========

    async def save(self, group_id: str, result: SummaryResult) -> Path:
        """将总结结果持久化为 JSON 文件。

        文件名形如 ``1751234567_a1b2c3.json``（unix 秒 + 6 位随机 hex，
        避免同一秒多次总结冲突）。

        Args:
            group_id: 群号（仅允许纯数字）
            result: 总结引擎完整产出

        Returns:
            Path: 写入的文件绝对路径

        Raises:
            ValueError: group_id 非纯数字
        """
        self._validate_group_id(group_id)
        group_dir = self.base_dir / str(group_id)
        filename = f"{int(time.time())}_{uuid.uuid4().hex[:6]}.json"
        path = group_dir / filename
        payload = self._build_payload(group_id, result)
        await asyncio.to_thread(self._write_file, group_dir, path, payload)
        logger.info(f"[HistorySummary] 总结已保存: {path}")
        return path

    @staticmethod
    def _validate_group_id(group_id: str) -> None:
        """校验群号仅含数字，非法则抛 ValueError（防路径穿越）。"""
        if not _GROUP_ID_RE.match(str(group_id)):
            raise ValueError(f"非法群号（仅允许纯数字）: {group_id!r}")

    @staticmethod
    def _build_payload(group_id: str, result: SummaryResult) -> dict:
        """将 SummaryResult 转为可 JSON 序列化的落盘结构。

        datetime 字段转 ``YYYY-MM-DD HH:MM:SS`` 字符串（None 保持 None），
        tuple 列表（top_senders / sections）转为二维 list。
        """
        stats = result.stats
        return {
            "group_id": str(group_id),
            "generated_at": datetime.now().strftime(_TIME_FMT),
            "scope_desc": result.scope_desc,
            "sources": dict(result.sources),
            "stats": {
                "total": stats.total,
                "participant_count": stats.participant_count,
                "time_start": (
                    stats.time_start.strftime(_TIME_FMT) if stats.time_start else None
                ),
                "time_end": (
                    stats.time_end.strftime(_TIME_FMT) if stats.time_end else None
                ),
                "top_senders": [
                    [sender_id, sender_name, count]
                    for sender_id, sender_name, count in stats.top_senders
                ],
                "truncated": stats.truncated,
            },
            "sections": [[title, content] for title, content in result.sections],
            "raw_llm_text": result.raw_llm_text,
            "provider_id": result.provider_id,
            "messages_used": result.messages_used,
        }

    @staticmethod
    def _write_file(group_dir: Path, path: Path, payload: dict) -> None:
        """同步写入：先建群目录再落盘（to_thread 内执行）。"""
        group_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # ========== 列表 ==========

    async def list_by_group(
        self,
        group_id: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict:
        """分页列出总结文件的元信息。

        Args:
            group_id: 群号；None 表示遍历所有群子目录
            page: 页码（从 1 开始，<1 归一为 1）
            page_size: 每页条数（<1 归一为 1）

        Returns:
            dict: ``{"total": int, "items": [{group_id, filename, generated_at,
            scope_desc, provider_id, messages_used}], "page": int,
            "page_size": int}``；items 按 generated_at **降序**（最新在前）
            排序后再分页

        Raises:
            ValueError: group_id 非 None 且非纯数字
        """
        if group_id is not None:
            self._validate_group_id(group_id)
        page = max(1, int(page))
        page_size = max(1, int(page_size))

        items = await asyncio.to_thread(self._scan_items, group_id)
        # generated_at 为定长 "YYYY-MM-DD HH:MM:SS"，字典序 == 时间序；
        # 缺失/异常文件 generated_at 为空串，降序时自然沉底
        items.sort(key=lambda it: it["generated_at"], reverse=True)

        total = len(items)
        start = (page - 1) * page_size
        return {
            "total": total,
            "items": items[start : start + page_size],
            "page": page,
            "page_size": page_size,
        }

    def _scan_items(self, group_id: str | None) -> list[dict]:
        """同步扫描 base_dir 收集文件元信息（to_thread 内执行）。

        单个文件读取/解析失败仅 logger.warning 跳过，不中断整体列表。
        """
        items: list[dict] = []
        if not self.base_dir.is_dir():
            return items

        if group_id is not None:
            group_dirs = [self.base_dir / str(group_id)]
        else:
            group_dirs = [
                p
                for p in self.base_dir.iterdir()
                if p.is_dir() and _GROUP_ID_RE.match(p.name)
            ]

        for gdir in group_dirs:
            if not gdir.is_dir():
                continue
            gid = gdir.name
            for f in gdir.glob("*.json"):
                if not _FILENAME_RE.match(f.name):
                    # 非本模块命名规则的文件（如外部放入的散文件）直接忽略
                    continue
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    if not isinstance(data, dict):
                        raise ValueError("JSON 顶层不是对象")
                    items.append(
                        {
                            "group_id": gid,
                            "filename": f.name,
                            "generated_at": str(data.get("generated_at") or ""),
                            "scope_desc": str(data.get("scope_desc") or ""),
                            "provider_id": str(data.get("provider_id") or ""),
                            "messages_used": int(data.get("messages_used") or 0),
                        }
                    )
                except Exception as e:
                    logger.warning(
                        f"[HistorySummary] 总结文件读取/解析失败，已跳过: {f} ({e})"
                    )
        return items

    # ========== 读取 ==========

    async def read(self, group_id: str, filename: str) -> dict | None:
        """读取单个总结文件的完整 JSON。

        路径穿越防护（双重正则白名单）：
        - group_id 必须匹配 ``^\\d+$``（拒绝 ``..``、``/``、``\\`` 等一切非数字）
        - filename 必须匹配 ``^\\d+_[0-9a-f]{6}\\.json$``
        非法输入直接拒绝（warning 日志）并返回 None，绝不拼接路径。

        Args:
            group_id: 群号
            filename: 文件名（含 .json 后缀）

        Returns:
            dict | None: 解析后的 JSON 字典；路径非法、文件不存在或
            解析失败均返回 None（解析失败记 warning，不存在静默）
        """
        if not _GROUP_ID_RE.match(str(group_id)):
            logger.warning(f"[HistorySummary] 拒绝非法群号访问: {group_id!r}")
            return None
        if not _FILENAME_RE.match(str(filename)):
            logger.warning(f"[HistorySummary] 拒绝非法文件名访问: {filename!r}")
            return None

        path = self.base_dir / str(group_id) / str(filename)
        return await asyncio.to_thread(self._read_file, path)

    @staticmethod
    def _read_file(path: Path) -> dict | None:
        """同步读取并解析 JSON（to_thread 内执行）。"""
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as e:
            logger.warning(f"[HistorySummary] 总结文件读取失败: {path} ({e})")
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            logger.warning(f"[HistorySummary] 总结文件解析失败: {path} ({e})")
            return None
        return data if isinstance(data, dict) else None

    # ========== 过期清理 ==========

    async def cleanup_expired(self, retention_days: int) -> int:
        """清理过期总结文件（按文件 mtime 判定）。

        遍历 ``base_dir/<群号>/*.json``，删除 mtime 早于 ``retention_days``
        天的文件；某群目录清空后一并移除。单个文件异常捕获后继续，
        结尾 logger.info 输出一行汇总。

        Args:
            retention_days: 保留天数

        Returns:
            int: 实际删除的文件数
        """
        return await asyncio.to_thread(self._cleanup_files, int(retention_days))

    def _cleanup_files(self, retention_days: int) -> int:
        """同步执行过期清理（to_thread 内执行）。"""
        if not self.base_dir.is_dir():
            logger.info(
                f"[HistorySummary] 过期总结清理完成：删除 0 个文件"
                f"（保留 {retention_days} 天，目录不存在）"
            )
            return 0

        cutoff = time.time() - retention_days * 86400
        deleted = 0
        for gdir in self.base_dir.iterdir():
            if not (gdir.is_dir() and _GROUP_ID_RE.match(gdir.name)):
                continue
            for f in gdir.glob("*.json"):
                try:
                    if f.stat().st_mtime < cutoff:
                        f.unlink()
                        deleted += 1
                except OSError as e:
                    logger.warning(
                        f"[HistorySummary] 删除过期总结失败，已跳过: {f} ({e})"
                    )
            # 群目录清空后一并移除（rmdir 仅能删空目录，天然安全）
            try:
                if not any(gdir.iterdir()):
                    gdir.rmdir()
            except OSError as e:
                logger.warning(f"[HistorySummary] 移除空群目录失败: {gdir} ({e})")

        logger.info(
            f"[HistorySummary] 过期总结清理完成：删除 {deleted} 个文件"
            f"（保留 {retention_days} 天）"
        )
        return deleted
