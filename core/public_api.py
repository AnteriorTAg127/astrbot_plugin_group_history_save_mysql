"""对外公共 API（v0.7.0 模块 B）。

供其他插件通过 PEP 420 namespace package 导入调用：:

    from data.plugins.astrbot_plugin_group_history_save_mysql.core.public_api import query_records

**导入无副作用**：本模块不 import main.py、不触发插件注册、不做任何 I/O；
仅在 register_mysql_manager() 被调用后持有 MySQLManager 内部引用（不对外导出）。
"""

import inspect
import time
from datetime import datetime
from typing import TYPE_CHECKING

from astrbot.api import logger

from .query_enrich import enrich_query_reply

if TYPE_CHECKING:
    from .db_config import ConfigManager
    from .db_mysql import MySQLManager


class PublicAPIError(Exception):
    """对外受控异常：本插件 MySQL 未初始化等调用方应自行捕获的场景。"""


# 模块级私有引用：main.py 在插件 __init__ 构造后调用 register_* 注入；
# 不对外导出，调用方拿不到连接池等内部对象
_mysql_mgr: "MySQLManager | None" = None
_config_mgr: "ConfigManager | None" = None


def register_mysql_manager(mgr: "MySQLManager | None") -> None:
    """注册或注销 MySQL 管理器（main.py 调用；构造无 I/O，注册即时生效）。

    传入 None 即注销（插件 terminate 时调用）：置空模块级引用后，对外
    调用将抛 PublicAPIError 而非静默失败——插件被禁用/重载期间，调用方
    持有的旧函数引用（顶层 from import）也能得到明确错误。
    """
    global _mysql_mgr
    _mysql_mgr = mgr


def register_config_manager(mgr: "ConfigManager | None") -> None:
    """注册或注销本地配置管理器（main.py 调用；查询日志存储于此）。

    传入 None 即注销（插件 terminate 时调用，语义同 register_mysql_manager）；
    未注册时查询日志写入仅 warning 降级（不影响查询结果）。
    """
    global _config_mgr
    _config_mgr = mgr


def _infer_caller() -> str:
    """从调用栈推断调用方标识（尽力而为）。

    向上找第一个模块路径不属于本插件包（astrbot_plugin_group_history_save_mysql）
    的调用方帧，取其模块名（frame.f_globals.get("__name__")）；以 data.plugins.
    开头的截取到插件包根（如 data.plugins.my_plugin.core.handler →
    data.plugins.my_plugin），其余形态原样保留；推断失败返回 "unknown"。
    """
    try:
        for frame_info in inspect.stack():
            frame = frame_info.frame
            module_name = frame.f_globals.get("__name__")
            if not module_name:
                continue
            # 跳过本插件包内的帧（本模块自身、main.py 及 core/ 各模块）
            if "astrbot_plugin_group_history_save_mysql" in module_name:
                continue
            parts = module_name.split(".")
            if len(parts) > 3 and parts[:2] == ["data", "plugins"]:
                return ".".join(parts[:3])
            return module_name
    except Exception:
        pass
    return "unknown"


async def _enrich_query_reply(mgr: "MySQLManager", records: list[dict]) -> None:
    """为查询结果批量补充回复目标消息（v0.8.2 R2：委托 core/query_enrich 共享实现）。

    与 core/webapi/query.py 的 _enrich_query_reply 共用同一份逻辑，杜绝口径
    分叉；本函数名保留做兼容。反查经 mgr.get_messages_by_ids 回调注入，
    共享模块本身不依赖 db 层。
    """
    await enrich_query_reply(records, mgr.get_messages_by_ids)


async def query_records(
    caller: str | None = None,
    group_id: str | None = None,
    sender_id: str | None = None,
    time_start: str | None = None,
    time_end: str | None = None,
    keyword: str | None = None,
    page: int = 1,
    page_size: int = 50,
    mgr: "MySQLManager | None" = None,
    log_mgr: "ConfigManager | None" = None,
) -> dict:
    """对外查询聊天记录（多条件分页），返回纯数据 {"total", "records"}。

    每次调用写一条查询日志（成功/失败都记；日志写失败仅 warning 不影响结果）。
    未初始化（register_mysql_manager 尚未调用）或时间参数格式非法时抛
    PublicAPIError；查询本身失败不向调用方抛底层异常：记失败日志后返回
    {"total": 0, "records": [], "_error": "..."}。

    **失败与无结果的区分**：查询执行失败时返回值含 ``_error`` 键（成功时
    无此键），调用方可用 ``"_error" in result`` 判断；仅返回 total=0 且无
    ``_error`` 表示确实无符合条件的数据。失败细节见查询日志。

    Args:
        caller: 调用方标识；缺省从调用栈自动推断（推断为尽力而为，
            建议显式传入以获得准确审计标识）
        group_id: 群号过滤（可选，字符串形式）
        sender_id: QQ 号过滤（可选，字符串形式）
        time_start: 开始时间（格式 YYYY-MM-DD HH:MM:SS，非法格式抛
            PublicAPIError）
        time_end: 结束时间（同上）
        keyword: 关键词，模糊匹配 content 与 sender_name
        page: 页码（<1 归 1）
        page_size: 每页条数（夹取 [1, 200]）
        mgr: MySQL 管理器显式注入（v0.8.2 R5，可选）；缺省 None 时回读模块
            全局 _mysql_mgr（register_mysql_manager 注册值），显式传入非 None
            则使用传入值——测试/高级调用方无需改动全局即可调用，老调用零变化
        log_mgr: 查询日志存储（ConfigManager）显式注入（可选）；缺省 None 时
            回读模块全局 _config_mgr，显式传入非 None 则使用传入值，语义同 mgr

    Returns:
        dict: {"total": int, "records": list[dict]}；查询失败时额外含
            "_error" 键。records 每条含 id/timestamp(str)/group_id/
            sender_id/sender_name/message_type/content/message_id/
            at_list/reply_id/reply_message

    Raises:
        PublicAPIError: 本插件 MySQL 尚未初始化完成，或时间参数格式非法
    """
    # v0.8.2 R5：显式注入优先，缺省回读模块全局（register_* 注册值）——
    # 只读全局一次（无写入），老调用与测试直接赋值全局的用法行为不变
    mgr = mgr if mgr is not None else _mysql_mgr
    if mgr is None:
        raise PublicAPIError("本插件 MySQL 尚未初始化完成，请稍后重试")

    # 查询日志存储（内置 SQLite config.db）：未注册时写入仅 warning 降级，
    # 不阻断查询（与日志写失败同一降级口径）
    log_mgr = log_mgr if log_mgr is not None else _config_mgr

    caller = (caller or _infer_caller())[:128]
    # 参数夹取：page < 1 归 1；page_size 夹取 [1, 200]；空串视同未提供
    if page < 1:
        page = 1
    page_size = max(1, min(page_size, 200))
    group_id = group_id or None
    sender_id = sender_id or None
    keyword = keyword or None

    # 时间参数格式预检：非法格式尽早抛 PublicAPIError（参数错误，不写查询
    # 日志），避免无效查询打到数据库——MySQL 对非法日期串的行为依赖
    # sql_mode，非严格模式下会静默转 0 值返回空结果，掩盖调用方参数错误
    for label, val in (("time_start", time_start), ("time_end", time_end)):
        if val:
            try:
                datetime.strptime(val, "%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError):
                raise PublicAPIError(
                    f"{label} 格式应为 YYYY-MM-DD HH:MM:SS，收到: {val!r}"
                )

    # 日志参数仅记录查询条件，不含 content 全文与任何密钥（PRD §2.3.6）；
    # keyword 截断 500 字符、caller 截断 128（对齐 query_log 表列宽），
    # 防止调用方用超长输入撑爆日志表
    params_log = {
        "group_id": group_id,
        "sender_id": sender_id,
        "time_start": time_start,
        "time_end": time_end,
        "keyword": (keyword or "")[:500] or None,
        "page": page,
        "page_size": page_size,
    }

    t0 = time.perf_counter()
    try:
        result = await mgr.query_messages(
            group_id=group_id,
            sender_id=sender_id,
            time_start=time_start,
            time_end=time_end,
            keyword=keyword,
            page=page,
            page_size=page_size,
            raise_on_error=True,
        )
        cost_ms = int((time.perf_counter() - t0) * 1000)
        records = result.get("records") or []
        await _enrich_query_reply(mgr, records)
        try:
            if log_mgr is not None:
                await log_mgr.insert_query_log(
                    caller=caller,
                    method="query_records",
                    params=params_log,
                    result_count=len(records),
                    success=True,
                    cost_ms=cost_ms,
                )
        except Exception as e:
            # 日志写失败不影响查询结果（模块 A 内部已吞异常，此处双保险）
            logger.warning(f"[HistorySave] 写入查询日志失败: {e}")
        return result
    except Exception as e:
        cost_ms = int((time.perf_counter() - t0) * 1000)
        try:
            if log_mgr is not None:
                await log_mgr.insert_query_log(
                    caller=caller,
                    method="query_records",
                    params=params_log,
                    result_count=0,
                    success=False,
                    error_msg=str(e)[:500],
                    cost_ms=cost_ms,
                )
        except Exception as log_e:
            logger.warning(f"[HistorySave] 写入查询日志失败: {log_e}")
        # 不向调用方抛底层异常：失败与无结果经 _error 键区分，
        # 失败细节见查询日志
        return {
            "total": 0,
            "records": [],
            "_error": "查询执行失败，详情见查询日志",
        }


# 单次 count_messages 批量查询的 sender_ids 上限（对齐数据层 IN 分块范式）
_COUNT_SENDERS_LIMIT = 500
# 查询日志中 sender_ids 的样本展示数（防超长参数撑爆日志表）
_COUNT_SENDERS_SAMPLE = 5


async def count_messages(
    caller: str | None = None,
    group_id: str | None = None,
    sender_ids: str | list[str] | None = None,
    time_start: str | None = None,
    time_end: str | None = None,
    mgr: "MySQLManager | None" = None,
    log_mgr: "ConfigManager | None" = None,
) -> dict:
    """对外统计文本消息数（群总计 + 批量人统计），返回纯数据 dict。

    实时聚合 MySQL chat_history（只统计文本消息）；group_id 单群、
    sender_ids 支持单人（str）或批量（list，≤500）。批量聚合为数据层
    单条 GROUP BY SQL（无 N+1 循环），适合高并发调用。

    **失败与无结果的区分**：查询执行失败时返回值含 ``_error`` 键，
    成功时无此键（与 query_records 同口径）。

    Args:
        caller: 调用方标识；缺省从调用栈自动推断（建议显式传入）
        group_id: 限定单群（可选）；与 sender_ids 至少提供其一
        sender_ids: 单人传字符串、批量传列表（≤500）；与 group_id
            至少提供其一
        time_start: 开始时间（格式 YYYY-MM-DD HH:MM:SS，非法抛
            PublicAPIError）
        time_end: 结束时间（同上）
        mgr: MySQL 管理器显式注入（v0.8.2 R5，可选）；缺省 None 时回读模块
            全局 _mysql_mgr，显式传入非 None 则使用传入值，语义同
            query_records.mgr
        log_mgr: 查询日志存储（ConfigManager）显式注入（可选）；缺省 None 时
            回读模块全局 _config_mgr，语义同 query_records.log_mgr

    Returns:
        dict: {
            "group_total": int,      # group_id 给定时该群文本消息总数
            "senders": {             # sender_ids 给定时每人计数
                sid: {"in_group": int, "total": int},
                # in_group：该人在群内发言数（group_id 也给定时才有）；
                # total：该人跨群总发言数
            },
        }；查询失败时额外含 "_error" 键

    Raises:
        PublicAPIError: 未初始化 / 时间格式非法 / 参数缺失（group_id 与
            sender_ids 都未提供）/ sender_ids 超 500
    """
    # v0.8.2 R5：显式注入优先，缺省回读模块全局（同 query_records）
    mgr = mgr if mgr is not None else _mysql_mgr
    if mgr is None:
        raise PublicAPIError("本插件 MySQL 尚未初始化完成，请稍后重试")

    # 查询日志存储（内置 SQLite config.db）：未注册时写入仅 warning 降级
    log_mgr = log_mgr if log_mgr is not None else _config_mgr
    caller = (caller or _infer_caller())[:128]

    # 时间参数格式预检（同 query_records，非法抛 PublicAPIError 不写日志）
    for label, val in (("time_start", time_start), ("time_end", time_end)):
        if val:
            try:
                datetime.strptime(val, "%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError):
                raise PublicAPIError(
                    f"{label} 格式应为 YYYY-MM-DD HH:MM:SS，收到: {val!r}"
                )

    # 参数归一：group_id 空串归 None；sender_ids 字符串转单元素列表、
    # 列表过滤空值、空归 None
    group_id = group_id or None
    if isinstance(sender_ids, str):
        sender_ids = [sender_ids] if sender_ids else None
    elif isinstance(sender_ids, (list, tuple)):
        sender_ids = [s for s in sender_ids if s] or None
    else:
        sender_ids = None

    # 参数存在性校验：避免全库无过滤 COUNT 全表扫描（高并发不友好）
    if group_id is None and not sender_ids:
        raise PublicAPIError("请至少提供 group_id 或 sender_ids 之一")
    if sender_ids and len(sender_ids) > _COUNT_SENDERS_LIMIT:
        raise PublicAPIError(
            f"sender_ids 单次最多 {_COUNT_SENDERS_LIMIT} 个，"
            f"当前 {len(sender_ids)} 个，请分批查询"
        )

    # 日志参数仅记录过滤条件与批量样本（不含任何密钥与消息内容）
    params_log = {
        "group_id": group_id,
        "sender_ids_count": len(sender_ids) if sender_ids else None,
        "sender_ids_sample": sender_ids[:_COUNT_SENDERS_SAMPLE] if sender_ids else None,
        "time_start": time_start,
        "time_end": time_end,
    }

    t0 = time.perf_counter()
    try:
        result = await mgr.count_messages(
            group_id=group_id,
            sender_ids=sender_ids,
            time_start=time_start,
            time_end=time_end,
        )
        cost_ms = int((time.perf_counter() - t0) * 1000)
        senders = result.get("senders") or {}
        try:
            if log_mgr is not None:
                await log_mgr.insert_query_log(
                    caller=caller,
                    method="count_messages",
                    params=params_log,
                    result_count=len(senders),
                    success=True,
                    cost_ms=cost_ms,
                )
        except Exception as e:
            logger.warning(f"[HistorySave] 写入查询日志失败: {e}")
        return result
    except Exception as e:
        cost_ms = int((time.perf_counter() - t0) * 1000)
        try:
            if log_mgr is not None:
                await log_mgr.insert_query_log(
                    caller=caller,
                    method="count_messages",
                    params=params_log,
                    result_count=0,
                    success=False,
                    error_msg=str(e)[:500],
                    cost_ms=cost_ms,
                )
        except Exception as log_e:
            logger.warning(f"[HistorySave] 写入查询日志失败: {log_e}")
        # 不向调用方抛底层异常：失败与无结果经 _error 键区分
        return {
            "group_total": 0,
            "senders": {},
            "_error": "查询执行失败，详情见查询日志",
        }


__all__ = ["query_records", "count_messages", "PublicAPIError"]
