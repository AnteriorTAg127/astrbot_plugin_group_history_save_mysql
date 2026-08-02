"""消息捕获辅助函数（v0.4.0 人物分析 · 存储增强）。

从 AstrBot 消息链 / OneBot v11 原始事件中提取「@对象」与「回复目标」，供
main.py 在 ``on_group_message`` 入库时随文本一并透传给 ``insert_chat_message``
（chat_history 的 at_list / reply_id 两列），作为人物关系分析的数据基础。

两个函数均为纯函数、全程防御式：任何字段缺失 / 类型异常 / 结构不符都安全降级
（返回 ``[]`` 或 ``""``），绝不向上抛异常，绝不阻断消息存储。

- :func:`extract_at_targets`：遍历消息链提取所有 ``Comp.At`` 的目标 QQ（去重保序）
- :func:`extract_reply_id`：提取回复目标 message_id（消息链优先，raw_message 回退）
"""

from __future__ import annotations

import astrbot.api.message_components as Comp
from astrbot.api import logger


def extract_at_targets(message_chain) -> list[str]:
    """从消息链提取所有 ``Comp.At`` 组件的目标 QQ，去重保序。

    来源：AstrBot ``Comp.At`` 组件的 ``qq`` 字段（``int | str``；见
    ``astrbot/core/message/components.py``，``toDict()`` 输出
    ``{"type": "at", "data": {"qq": str(qq)}}``）。其中 ``AtAll``（@全体成员）
    为 ``At`` 的子类，``qq`` 取值恒为 ``"all"``，并非真实用户，予以剔除。

    归一化规则：

    - 逐个组件取 ``qq``，``str(qq).strip()`` 转字符串；
    - 丢弃空项与 ``"all"``（大小写不敏感，即 @全体成员）；
    - 去重但保留首次出现顺序；
    - 遍历中任一组件取值异常（如缺少 qq 字段）→ 记 debug 并跳过该组件继续；
    - 整体异常或 ``message_chain`` 为 ``None`` → 返回 ``[]``。

    Args:
        message_chain: ``event.get_messages()`` 返回的消息组件列表（可为 None）。

    Returns:
        list[str]: 被 @ 的 QQ 号字符串列表（去重保序）；无则 ``[]``。
    """
    targets: list[str] = []
    try:
        for component in message_chain or []:
            try:
                if not isinstance(component, Comp.At):
                    continue
                value = str(getattr(component, "qq", "") or "").strip()
                if not value or value.lower() == "all":
                    continue
                if value not in targets:
                    targets.append(value)
            except Exception as e:
                logger.debug(f"[Profile] 解析 At 组件失败，已跳过: {e}")
                continue
    except Exception as e:
        logger.debug(f"[Profile] 遍历消息链提取 @ 对象失败: {e}")
        return []
    return targets


def extract_reply_id(event) -> str:
    """提取本条消息回复的目标 message_id。

    两级来源（取到非空值即返回）：

    1. **消息链回复组件**：遍历 ``event.get_messages()``，找 ``Comp.Reply`` 组件
       取其 ``id`` 字段（``str | int``，即「所引用的消息 ID」；见
       ``astrbot/core/message/components.py``，``toDict()`` 输出
       ``{"type": "reply", "data": {"id": str(id)}}``）。
    2. **OneBot v11 原始事件回退**：读 ``event.message_obj.raw_message``
       （AstrBotMessage 保留的 OneBot 原始事件 dict）中的 ``message`` 段数组，
       找 ``type == "reply"`` 段的 ``data["id"]``。

    全程防御式 getattr：event 为 None、无 get_messages、无 message_obj、
    raw_message 非 dict、段结构异常、id 为空等任何情形均安全降级为 ``""``，
    绝不抛异常、绝不阻断消息存储。

    Args:
        event: AstrBot 消息事件（AstrMessageEvent）。

    Returns:
        str: 回复目标 message_id（字符串）；取不到返回 ``""``。
    """
    # 1) 优先消息链中的 Reply 组件
    try:
        chain = event.get_messages() if event is not None else []
        for component in chain or []:
            try:
                if not isinstance(component, Comp.Reply):
                    continue
                value = str(getattr(component, "id", "") or "").strip()
                if value:
                    return value
            except Exception as e:
                logger.debug(f"[Profile] 解析 Reply 组件失败，已跳过: {e}")
                continue
    except Exception as e:
        logger.debug(f"[Profile] 遍历消息链提取回复对象失败: {e}")

    # 2) 回退 OneBot v11 原始事件 raw_message 的 reply 段 data.id
    try:
        message_obj = getattr(event, "message_obj", None)
        raw = getattr(message_obj, "raw_message", None)
        segments = raw.get("message") if isinstance(raw, dict) else None
        if isinstance(segments, list):
            for seg in segments:
                try:
                    if not isinstance(seg, dict) or seg.get("type") != "reply":
                        continue
                    data = seg.get("data") or {}
                    if not isinstance(data, dict):
                        continue
                    value = str(data.get("id") or "").strip()
                    if value:
                        return value
                except Exception as e:
                    logger.debug(f"[Profile] 解析 reply 消息段失败: {e}")
                    continue
    except Exception as e:
        logger.debug(f"[Profile] raw_message 回退提取回复对象失败: {e}")

    return ""
