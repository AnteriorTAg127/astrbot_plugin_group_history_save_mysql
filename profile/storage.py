"""人物分析结果 JSON 持久化存储（模块 I）。

将 :class:`ProfileResult` 以 JSON 文件形式落盘到 data 目录（base_dir 由
main.py 注入，通常为 ``StarTools.get_data_dir() / "profiles"``），并提供
保存 / 列表 / 读取 / 删除 / 过期清理能力。

目录结构（按 scope 分子目录）::

    base_dir/group_<群号>/<YYYYmmdd_HHMMSS>_<sender_id>[_n].json
    base_dir/all/<YYYYmmdd_HHMMSS>_<sender_id>[_n].json

文件名基于 ``result.created_at`` 确定性生成（不依赖随机源）；同一秒同一
目标重复保存时追加递增后缀 ``_2``、``_3`` …… 避免覆盖。

安全约束（双重防护，复用 summary storage 白名单范式）：
- 群号仅允许纯数字（``^\\d+$``）；scope 目录仅允许 ``group_\\d+`` 或 ``all``；
  文件名仅允许 ``^\\d{8}_\\d{6}_\\d+(_\\d+)?\\.json$``
- read / delete 入参为 list_profiles 返回的相对名（``<scope目录>/<文件名>``），
  先正则白名单拒绝一切 ``..`` / 多余分隔符，再经 ``resolve()`` +
  ``is_relative_to(base_dir)`` 二次校验，杜绝路径穿越

文件 I/O 统一经 ``asyncio.to_thread`` 执行同步读写（单文件体量小，不引入
aiofiles 依赖）。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime
from pathlib import Path

from astrbot.api import logger

from .models import ProfileResult

# scope 子目录白名单：group_<纯数字群号> 或 all（防路径穿越）
_SCOPE_DIR_RE = re.compile(r"^(?:group_\d+|all)$")
# 群号白名单：仅纯数字
_GROUP_ID_RE = re.compile(r"^\d+$")
# 文件名校验：<YYYYmmdd>_<HHMMSS>_<sender_id>[_<碰撞序号>].json
_FILENAME_RE = re.compile(r"^\d{8}_\d{6}_\d+(?:_\d+)?\.json$")
# list/read/delete 使用的相对名校验：<scope目录>/<文件名>，任何 ../ 与多余 / 均拒绝
_RELNAME_RE = re.compile(r"^(?:group_\d+|all)/\d{8}_\d{6}_\d+(?:_\d+)?\.json$")

# 落盘时间统一格式（字符串字典序 == 时间序，便于列表排序）
_TIME_FMT = "%Y-%m-%d %H:%M:%S"
# 文件名用紧凑时间格式
_COMPACT_FMT = "%Y%m%d_%H%M%S"


class ProfileStorage:
    """人物分析结果 JSON 文件存储器（save / list / read / delete / cleanup）。"""

    def __init__(self, base_dir: Path):
        """初始化存储器。

        Args:
            base_dir: 分析结果根目录，由 main.py 注入
                （``StarTools.get_data_dir() / "profiles"``），本模块不负责获取
        """
        self.base_dir = Path(base_dir)

    # ========== 写入 ==========

    async def save(self, result: ProfileResult) -> str:
        """将人物分析结果持久化为 JSON 文件。

        文件名基于 ``result.created_at`` 确定性生成，形如
        ``group_987654/20260802_103045_123456.json``；同一秒同一目标重复
        保存时追加递增后缀（``_2``、``_3`` ……）。``result.created_at``
        为空时填入当前时间。

        Args:
            result: 人物分析引擎完整产出

        Returns:
            str: 写入文件相对 base_dir 的相对名（``<scope目录>/<文件名>``，
            可直接回传 read / delete）；磁盘写入失败时返回空串 ``""``
            （仅记 warning，由上层决定降级策略）

        Raises:
            ValueError: scope 非法（非 "group"/"all"）或群号非纯数字
        """
        if not result.created_at:
            result.created_at = datetime.now().strftime(_TIME_FMT)

        scope_dir_name = self._scope_dir_name(result)
        scope_dir = self.base_dir / scope_dir_name
        payload = self._build_payload(result)
        try:
            filename = await asyncio.to_thread(
                self._write_file, scope_dir, result, payload
            )
        except OSError as e:
            logger.warning(f"[Profile] 分析结果保存失败: {scope_dir} ({e})")
            return ""
        relname = f"{scope_dir_name}/{filename}"
        logger.info(f"[Profile] 人物分析已保存: {self.base_dir / relname}")
        return relname

    @staticmethod
    def _scope_dir_name(result: ProfileResult) -> str:
        """由 target 推导 scope 子目录名，非法输入抛 ValueError（防路径穿越）。

        - scope == "all" → ``all``
        - scope == "group" 且群号为纯数字 → ``group_<群号>``
        """
        target = result.target
        if target.scope == "all":
            return "all"
        if target.scope == "group" and _GROUP_ID_RE.match(str(target.group_id or "")):
            return f"group_{target.group_id}"
        raise ValueError(
            f"非法分析范围（scope 须为 group/all，群号须纯数字）: "
            f"scope={target.scope!r}, group_id={target.group_id!r}"
        )

    @staticmethod
    def _compact_time(created_at: str) -> str:
        """将 created_at 字符串归一化为文件名用紧凑时间（YYYYmmdd_HHMMSS）。

        解析三级回退：``%Y-%m-%d %H:%M:%S`` → ``fromisoformat`` → 当前时间。
        """
        try:
            dt = datetime.strptime(str(created_at), _TIME_FMT)
        except (ValueError, TypeError):
            try:
                dt = datetime.fromisoformat(str(created_at))
            except (ValueError, TypeError):
                dt = datetime.now()
        return dt.strftime(_COMPACT_FMT)

    @staticmethod
    def _build_payload(result: ProfileResult) -> dict:
        """将 ProfileResult 全量转为可 JSON 序列化的落盘结构。

        datetime 字段转 ``YYYY-MM-DD HH:MM:SS`` 字符串（None 保持 None），
        tuple 列表（group_breakdown / top_partners / sections）转为二维 list。
        """
        stats = result.stats
        target = result.target
        return {
            "created_at": result.created_at,
            "scope_desc": result.scope_desc,
            "provider_id": result.provider_id,
            "messages_used": result.messages_used,
            "sources": dict(result.sources),
            "relation_context_complete": result.relation_context_complete,
            "target": {
                "sender_id": target.sender_id,
                "sender_name": target.sender_name,
                "scope": target.scope,
                "group_id": target.group_id,
            },
            "stats": {
                "total": stats.total,
                "group_count": stats.group_count,
                "group_breakdown": [
                    [gid, count] for gid, count in stats.group_breakdown
                ],
                "time_start": (
                    stats.time_start.strftime(_TIME_FMT) if stats.time_start else None
                ),
                "time_end": (
                    stats.time_end.strftime(_TIME_FMT) if stats.time_end else None
                ),
                "active_days": stats.active_days,
                "hour_dist": list(stats.hour_dist),
                "weekday_dist": list(stats.weekday_dist),
                "peak_hour": stats.peak_hour,
                "peak_weekday": stats.peak_weekday,
                "avg_length": stats.avg_length,
                "total_chars": stats.total_chars,
                "emoji_ratio": stats.emoji_ratio,
                "question_ratio": stats.question_ratio,
                "top_partners": [
                    [sender_id, name, count]
                    for sender_id, name, count in stats.top_partners
                ],
                "truncated": stats.truncated,
            },
            "sections": [[title, content] for title, content in result.sections],
            "raw_llm_text": result.raw_llm_text,
        }

    @staticmethod
    def _write_file(scope_dir: Path, result: ProfileResult, payload: dict) -> str:
        """同步写入：建目录 → 碰撞探测定文件名 → 落盘（to_thread 内执行）。

        Returns:
            str: 实际写入的文件名（不含目录）
        """
        scope_dir.mkdir(parents=True, exist_ok=True)
        # sender_id 仅保留数字，防文件名注入（QQ 号恒为数字，异常值归零兜底）
        sender_id = re.sub(r"\D", "", str(result.target.sender_id or "")) or "0"
        stem = f"{ProfileStorage._compact_time(result.created_at)}_{sender_id}"
        filename = f"{stem}.json"
        seq = 2
        while (scope_dir / filename).exists():
            filename = f"{stem}_{seq}.json"
            seq += 1
        (scope_dir / filename).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return filename

    # ========== 列表 ==========

    async def list_profiles(self, page: int = 1, page_size: int = 20) -> dict:
        """分页列出所有分析结果的摘要信息。

        Args:
            page: 页码（从 1 开始，<1 归一为 1）
            page_size: 每页条数（<1 归一为 1）

        Returns:
            dict: ``{"total": int, "profiles": [{filename, target_name,
            sender_id, scope, scope_desc, created_at, provider_id, total}],
            "page": int, "page_size": int}``；profiles 按 created_at
            **降序**（最新在前）排序后再分页
        """
        page = max(1, int(page))
        page_size = max(1, int(page_size))

        profiles = await asyncio.to_thread(self._scan_profiles)
        # created_at 为定长 "YYYY-MM-DD HH:MM:SS"，字典序 == 时间序；
        # 缺失/异常文件 created_at 为空串，降序时自然沉底
        profiles.sort(key=lambda it: it["created_at"], reverse=True)

        total = len(profiles)
        start = (page - 1) * page_size
        return {
            "total": total,
            "profiles": profiles[start : start + page_size],
            "page": page,
            "page_size": page_size,
        }

    def _scan_profiles(self) -> list[dict]:
        """同步扫描 base_dir 收集摘要（to_thread 内执行）。

        单个文件读取/解析失败仅 logger.warning 跳过，不中断整体列表；
        非本模块命名规则的文件/目录直接忽略。
        """
        items: list[dict] = []
        if not self.base_dir.is_dir():
            return items

        for sdir in self.base_dir.iterdir():
            if not (sdir.is_dir() and _SCOPE_DIR_RE.match(sdir.name)):
                continue
            scope_name = sdir.name
            for f in sdir.glob("*.json"):
                if not _FILENAME_RE.match(f.name):
                    continue
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    if not isinstance(data, dict):
                        raise ValueError("JSON 顶层不是对象")
                    target = data.get("target")
                    target = target if isinstance(target, dict) else {}
                    stats = data.get("stats")
                    stats = stats if isinstance(stats, dict) else {}
                    items.append(
                        {
                            "filename": f"{scope_name}/{f.name}",
                            "target_name": str(target.get("sender_name") or ""),
                            "sender_id": str(target.get("sender_id") or ""),
                            "scope": str(target.get("scope") or ""),
                            "scope_desc": str(data.get("scope_desc") or ""),
                            "created_at": str(data.get("created_at") or ""),
                            "provider_id": str(data.get("provider_id") or ""),
                            "total": int(stats.get("total") or 0),
                        }
                    )
                except Exception as e:
                    logger.warning(
                        f"[Profile] 分析结果文件读取/解析失败，已跳过: {f} ({e})"
                    )
        return items

    # ========== 读取 / 删除 ==========

    async def read(self, filename: str) -> dict | None:
        """读取单个分析结果文件的完整 JSON。

        路径穿越防护（正则白名单 + resolve 二次校验）：
        - filename 为 list_profiles 返回的相对名（``<scope目录>/<文件名>``），
          必须匹配 ``^(?:group_\\d+|all)/\\d{8}_\\d{6}_\\d+(_\\d+)?\\.json$``
        - resolve 后必须仍落在 base_dir 内（is_relative_to 校验）
        非法输入直接拒绝（warning 日志）并返回 None，绝不拼接路径。

        Args:
            filename: 相对名（含 scope 目录前缀与 .json 后缀）

        Returns:
            dict | None: 解析后的 JSON 字典；路径非法、文件不存在或
            解析失败均返回 None（解析失败记 warning，不存在静默）
        """
        path = self._resolve_inside_base(filename)
        if path is None:
            return None
        return await asyncio.to_thread(self._read_file, path)

    async def delete(self, filename: str) -> bool:
        """删除单个分析结果文件。

        路径穿越防护同 :meth:`read`；scope 目录清空后一并移除。

        Args:
            filename: 相对名（含 scope 目录前缀与 .json 后缀）

        Returns:
            bool: 成功删除返回 True；路径非法、文件不存在或删除失败返回 False
        """
        path = self._resolve_inside_base(filename)
        if path is None:
            return False
        deleted = await asyncio.to_thread(self._delete_file, path)
        if deleted:
            # 清空后的 scope 目录一并移除（rmdir 仅能删空目录，天然安全）
            scope_dir = path.parent
            try:
                if scope_dir.is_dir() and not any(scope_dir.iterdir()):
                    scope_dir.rmdir()
            except OSError as e:
                logger.warning(f"[Profile] 移除空 scope 目录失败: {scope_dir} ({e})")
        return deleted

    def _resolve_inside_base(self, filename: str) -> Path | None:
        """将相对名解析为 base_dir 内的绝对路径；非法返回 None（双重校验）。

        1) 正则白名单拒绝一切 ``..`` / 多余分隔符 / 非法 scope 目录；
        2) resolve() 后 is_relative_to(base_dir) 二次确认落在根目录内。
        """
        name = str(filename or "")
        if not _RELNAME_RE.match(name):
            logger.warning(f"[Profile] 拒绝非法文件名访问: {filename!r}")
            return None
        candidate = (self.base_dir / name).resolve()
        if not candidate.is_relative_to(self.base_dir.resolve()):
            logger.warning(f"[Profile] 拒绝越界路径访问: {filename!r}")
            return None
        return candidate

    @staticmethod
    def _read_file(path: Path) -> dict | None:
        """同步读取并解析 JSON（to_thread 内执行）。"""
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as e:
            logger.warning(f"[Profile] 分析结果文件读取失败: {path} ({e})")
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            logger.warning(f"[Profile] 分析结果文件解析失败: {path} ({e})")
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _delete_file(path: Path) -> bool:
        """同步删除单个文件（to_thread 内执行）。"""
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError as e:
            logger.warning(f"[Profile] 分析结果文件删除失败: {path} ({e})")
            return False

    # ========== 过期清理 ==========

    async def cleanup(self, keep_days: int) -> int:
        """清理过期分析结果文件（按文件 mtime 判定）。

        遍历 ``base_dir/<scope目录>/*.json``，删除 mtime 早于 ``keep_days``
        天的文件；scope 目录清空后一并移除。单个文件异常捕获后继续，
        结尾 logger.info 输出一行汇总。

        Args:
            keep_days: 保留天数

        Returns:
            int: 实际删除的文件数
        """
        return await asyncio.to_thread(self._cleanup_files, int(keep_days))

    def _cleanup_files(self, keep_days: int) -> int:
        """同步执行过期清理（to_thread 内执行）。"""
        if not self.base_dir.is_dir():
            logger.info(
                f"[Profile] 过期分析清理完成：删除 0 个文件"
                f"（保留 {keep_days} 天，目录不存在）"
            )
            return 0

        cutoff = time.time() - keep_days * 86400
        deleted = 0
        for sdir in self.base_dir.iterdir():
            if not (sdir.is_dir() and _SCOPE_DIR_RE.match(sdir.name)):
                continue
            for f in sdir.glob("*.json"):
                try:
                    if f.stat().st_mtime < cutoff:
                        f.unlink()
                        deleted += 1
                except OSError as e:
                    logger.warning(f"[Profile] 删除过期分析失败，已跳过: {f} ({e})")
            # scope 目录清空后一并移除（rmdir 仅能删空目录，天然安全）
            try:
                if not any(sdir.iterdir()):
                    sdir.rmdir()
            except OSError as e:
                logger.warning(f"[Profile] 移除空 scope 目录失败: {sdir} ({e})")

        logger.info(
            f"[Profile] 过期分析清理完成：删除 {deleted} 个文件（保留 {keep_days} 天）"
        )
        return deleted
