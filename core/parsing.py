"""消息解析纯函数（v0.6.0 自 main.py 迁出 + 新增补库解析）。

承接 main.py 迁出的消息解析纯函数（纯搬移，零行为变化）：

- :func:`extract_image_urls`：提取图片的原始 http(s) 链接（实时监听入库用）
- :func:`stats_fallback_text`：T2I 渲染失败时的降级纯文本摘要（/群统计 指令用）

v0.6.0 新增：

- :func:`parse_onebot_raw_message`：解析 OneBot ``get_group_msg_history`` 返回的
  单条原始消息（重载自动补库用，文本 + 图片 URL 双内容入库）。

v0.8.1 迁入：

- :func:`resolve_group_id`：解析目标群号（管理员指令参数群号或当前群）。

v0.8.2 迁入（R3 @/回复解析下沉，自 core/profile/capture.py 逐行搬入）：

- :func:`extract_at_targets`：遍历消息链提取所有 ``Comp.At`` 的目标 QQ（去重保序）
- :func:`extract_reply_id`：提取回复目标 message_id（消息链优先，raw_message 回退）

  两函数均为纯函数、全程防御式（任何异常安全降级，绝不向上抛出）；
  ``core/profile/capture.py`` 保留 re-export 兼容，老导入路径不断。
"""

from datetime import datetime

import astrbot.api.message_components as Comp
from astrbot.api import logger


def extract_image_urls(message_obj, message_chain) -> list[str]:
    """提取图片的原始 http(s) 链接。

    优先从 OneBot 原始事件(message_obj.raw_message)的图片消息段提取 QQ 下发的
    原始链接;raw_message 不可用或提取不到时,回退到遍历消息链中的 Image 组件。
    本地文件路径一律不返回(新版 AstrBot 预处理会把组件 url 改写为本地临时路径)。

    Args:
        message_obj: event.message_obj(AstrBotMessage,其 raw_message 为 OneBot 原始事件)
        message_chain: event.get_messages() 返回的消息组件列表

    Returns:
        list[str]: 以 http 开头的图片链接列表
    """
    urls: list[str] = []

    # 优先从 OneBot 原始事件的消息段数组提取(QQ 下发的原始链接)
    raw = getattr(message_obj, "raw_message", None)
    segments = raw.get("message") if isinstance(raw, dict) else None
    if isinstance(segments, list):
        for seg in segments:
            try:
                if not isinstance(seg, dict) or seg.get("type") != "image":
                    continue
                data = seg.get("data") or {}
                if not isinstance(data, dict):
                    continue
                # 优先取 data.url;部分实现(如 Lagrange)把链接放在 data.file
                url = data.get("url")
                if isinstance(url, str) and url.startswith(("http://", "https://")):
                    urls.append(url)
                    continue
                file = data.get("file")
                if isinstance(file, str) and file.startswith(("http://", "https://")):
                    urls.append(file)
            except Exception as e:
                logger.debug(f"[HistorySave] 解析图片消息段失败: {e}")
                continue

    if urls:
        return urls

    # 回退:遍历消息链中的 Image 组件(本地临时路径一律不收集)
    try:
        for component in message_chain or []:
            if not isinstance(component, Comp.Image):
                continue
            value = component.url or component.file
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                urls.append(value)
    except Exception as e:
        logger.debug(f"[HistorySave] 遍历消息链提取图片链接失败: {e}")

    return urls


def stats_fallback_text(data, label: str) -> str:
    """T2I 渲染失败时的降级纯文本摘要（单行）。

    内容：时间范围 label + 总消息数 + 活跃成员数 + Top3 发言人（「昵称: N条」，
    昵称缺失时回退 QQ 号）。供 ``/群统计`` 指令渲染失败兜底使用。

    Args:
        data: ``StatsData``（或其鸭子同构对象），需含 total_messages /
            active_senders / sender_ranking（取前三条）。
        label: 时间范围展示文案（如「今日」/「近7天」）。
    """
    tops = []
    for item in (getattr(data, "sender_ranking", None) or [])[:3]:
        name = getattr(item, "sender_name", "") or getattr(item, "sender_id", "")
        tops.append(f"{name}: {getattr(item, 'count', 0)}条")
    top_text = "、".join(tops) if tops else "暂无发言数据"
    return (
        f"【{label}】总消息数：{getattr(data, 'total_messages', 0)}，"
        f"活跃成员：{getattr(data, 'active_senders', 0)}，"
        f"Top3 发言人：{top_text}"
    )


def parse_onebot_raw_message(raw: dict, group_id: str) -> dict | None:
    """解析 OneBot get_group_msg_history 返回的单条原始消息（v0.6.0 重载补库用）。

    Args:
        raw: 消息对象，形如 {"time": <unix秒>, "message_id": ...,
            "sender": {"user_id":..., "nickname":...},
            "message": [{"type":"text","data":{"text":"..."}},
                        {"type":"image","data":{"url":"https://..."}}]}
        group_id: 所属群号（字符串）

    Returns:
        dict | None: 含 timestamp(datetime)/sender_id(str)/sender_name(str)/
        text(str)/image_urls(list[str])/message_id(str)/at_list(str)/reply_id(str)；
        无文本且无图片 URL、或结构异常返回 None（记 debug 日志，不抛异常）。
    """
    try:
        message_id = str(raw.get("message_id") or "")
        # 防御式读取 time 键（F8）：缺失/非数值记 warning 带 message_id 并跳过，
        # 不再被外层兜底静默吞掉。OSError 用于 Windows 平台 fromtimestamp 对
        # 越界时间戳（负数或远未来）的兜底行为（与补库窗口计算同一防越界口径）
        time_raw = raw.get("time")
        try:
            timestamp = datetime.fromtimestamp(int(time_raw))
        except (TypeError, ValueError, OverflowError, OSError):
            logger.warning(
                "[HistorySave] 解析 OneBot 原始消息缺 time 或非法（群 %s, message_id=%s），已跳过",
                group_id,
                message_id,
            )
            return None
        sender = raw.get("sender") or {}
        sender_id = str(sender.get("user_id") or "")
        sender_name = str(sender.get("nickname") or "")

        segments = raw.get("message")
        text_parts: list[str] = []
        image_urls: list[str] = []
        at_targets: list[str] = []
        reply_id = ""
        if isinstance(segments, list):
            for seg in segments:
                if not isinstance(seg, dict):
                    continue
                data = seg.get("data") or {}
                if not isinstance(data, dict):
                    continue
                seg_type = seg.get("type")
                if seg_type == "text":
                    # 仅拼接文本段；strip 后以 "\n" 拼接（与实时路径
                    # core/saver.py 的 "\n".join 口径一致，避免两条路径落库内容分叉）
                    text = str(data.get("text") or "").strip()
                    if text:
                        text_parts.append(text)
                elif seg_type == "image":
                    # 同 extract_image_urls 判定：仅收集 http(s) 原始链接，
                    # 本地文件路径（data.file 为本地路径）一律不收集
                    url = data.get("url")
                    if isinstance(url, str) and url.startswith(("http://", "https://")):
                        image_urls.append(url)
                        continue
                    file = data.get("file")
                    if isinstance(file, str) and file.startswith(
                        ("http://", "https://")
                    ):
                        image_urls.append(file)
                elif seg_type == "at":
                    # 仅收集数字字符串 QQ（"all" 等伪目标剔除）
                    qq = str(data.get("qq") or "")
                    if qq.isdigit():
                        at_targets.append(qq)
                elif seg_type == "reply":
                    reply_id = str(data.get("id") or "")

        text = "\n".join(text_parts)
        at_list = ",".join(at_targets)
        if not text and not image_urls:
            return None
        return {
            "timestamp": timestamp,
            "sender_id": sender_id,
            "sender_name": sender_name,
            "text": text,
            "image_urls": image_urls,
            "message_id": message_id,
            "at_list": at_list,
            "reply_id": reply_id,
        }
    except Exception as e:
        logger.debug(f"[HistorySave] 解析 OneBot 原始消息失败（群 {group_id}）: {e}")
        return None


def resolve_group_id(event, group_id_str: str) -> int | None:
    """解析目标群号：优先使用参数，否则使用当前群。

    Args:
        event: AstrMessageEvent，参数缺省时取当前群号。
        group_id_str: 指令参数传入的群号字符串（可能为空）。

    Returns:
        int | None: 解析成功的群号；参数非法或当前群号不可用时返回 None。
    """
    if group_id_str:
        try:
            return int(group_id_str)
        except ValueError:
            return None
    # 尝试获取当前群号
    try:
        gid = event.get_group_id()
        if gid:
            return int(gid)
    except (ValueError, TypeError):
        pass
    return None


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
