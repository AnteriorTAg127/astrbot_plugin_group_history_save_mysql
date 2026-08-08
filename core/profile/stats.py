"""确定性统计引擎（v0.4.0 人物分析）。

对目标用户的消息集合做**纯规则统计**，产出 :class:`ProfileStats` 供图表渲染
（决策 9：小时/星期分布柱图）与 LLM 叙述（结构化输入，如「23 点发言占比 40%」）
共同消费。

设计约束：
- **无 AI、无 I/O、无副作用**：纯 CPU 计算，同输入恒同输出，最易测；
- 口头禅/高频词**不做本地分词**（不引 jieba），交由 LLM 从样本归纳；
- 统计基于**全量**目标消息（长度预算截断只影响喂 LLM 的素材，不影响统计）；
- 契约见 ``开发/v0.4.0/分工.md``「共享接口契约 → 数据模型」ProfileStats 字段。
"""

from __future__ import annotations

from .models import ProfileMessage, ProfileStats

# ---------------------------------------------------------------------------
# emoji 检测（Unicode 区段范围判定）
# ---------------------------------------------------------------------------
#
# 覆盖的常见表情区段：
#   0x1F300–0x1FAFF  杂项符号与象形文字 / 表情符号 / 交通地图 / 补充符号（含肤色修饰 0x1F3FB–0x1F3FF）
#   0x1F000–0x1F02F  麻将牌
#   0x1F0A0–0x1F0FF  扑克牌
#   0x1F1E6–0x1F1FF  区域指示符（组合成国旗 emoji）
#   0x2600–0x27BF    杂项符号（☀☔❤…）+ 装饰符号（✂✈✨…）
#   0x2190–0x21FF    箭头（←→↔…，常被当表情用）
#   0x2B00–0x2BFF    杂项符号与箭头（⭐⬆…）
#   0xFE0F           变体选择符-16（将前一字符强制为 emoji 呈现，如 keycap 序列）
#
# 局限（有意取舍，够用即可）：
#   - 零宽连接符 ZWJ（0x200D）组合序列不单独识别，但其组成字符多已落在上述区段内；
#   - 纯文本常用符号如 ©®™、纯 ASCII 颜文字 :-) 不计入；
#   - 箭头区段会把少数纯排版箭头误判为 emoji，属可接受噪声（粗略占比指标）。
EMOJI_RANGES: tuple[tuple[int, int], ...] = (
    (0x1F000, 0x1F02F),
    (0x1F0A0, 0x1F0FF),
    (0x1F1E6, 0x1F1FF),
    (0x1F300, 0x1FAFF),
    (0x2190, 0x21FF),
    (0x2600, 0x27BF),
    (0x2B00, 0x2BFF),
)
EMOJI_SINGLETONS: frozenset[int] = frozenset({0xFE0F})


def contains_emoji(text: str) -> bool:
    """判断文本是否含至少一个 emoji 字符（任一码位命中上述区段即算）。"""
    for ch in text:
        cp = ord(ch)
        if cp in EMOJI_SINGLETONS:
            return True
        for lo, hi in EMOJI_RANGES:
            if lo <= cp <= hi:
                return True
    return False


# ---------------------------------------------------------------------------
# 统计构建器
# ---------------------------------------------------------------------------


class ProfileStatsBuilder:
    """确定性统计构建器（无状态，可复用单例）。"""

    def build(
        self,
        target_messages: list[ProfileMessage],
        partners: list[tuple[str, str, int]],
        truncated: bool = False,
    ) -> ProfileStats:
        """对目标消息列表计算全部统计项，返回 ProfileStats。

        Args:
            target_messages: 目标用户的消息（fetcher 已去重，通常时间升序；
                本方法不依赖顺序，time_start/time_end 取 min/max 保底）。
            partners: 互动对象排行 ``[(sender_id, name, count)]``（fetcher 已排序，直接透传）。
            truncated: 是否因长度预算截断（透传，仅用于展示标注，不影响统计）。

        Returns:
            ProfileStats：空消息列表安全兜底（total=0、各分布全 0、peak=0、
            ratio=0.0、time_start/time_end=None）。
        """
        total = len(target_messages)

        # ---- 群分布：按 group_id 聚合，条数降序（同数按 group_id 升序稳定排序） ----
        group_counts: dict[str, int] = {}
        for msg in target_messages:
            group_counts[msg.group_id] = group_counts.get(msg.group_id, 0) + 1
        group_breakdown = sorted(group_counts.items(), key=lambda kv: (-kv[1], kv[0]))

        # ---- 时间跨度：min/max（不假设入参有序） ----
        if target_messages:
            time_start = min(msg.timestamp for msg in target_messages)
            time_end = max(msg.timestamp for msg in target_messages)
        else:
            time_start = time_end = None

        # ---- 时间分布 / 活跃天数 / 长度 / emoji / 问号（单次遍历） ----
        hour_dist = [0] * 24
        weekday_dist = [0] * 7
        active_dates: set = set()
        total_chars = 0
        emoji_count = 0
        question_count = 0
        for msg in target_messages:
            ts = msg.timestamp
            hour_dist[ts.hour] += 1
            weekday_dist[ts.weekday()] += 1  # datetime.weekday(): Mon=0 … Sun=6
            active_dates.add(ts.date())
            total_chars += len(msg.content)
            if contains_emoji(msg.content):
                emoji_count += 1
            if msg.content.strip().endswith(("?", "？")):
                question_count += 1

        return ProfileStats(
            total=total,
            group_count=len(group_counts),
            group_breakdown=group_breakdown,
            time_start=time_start,
            time_end=time_end,
            active_days=len(active_dates),
            hour_dist=hour_dist,
            weekday_dist=weekday_dist,
            # max 取首个最大值索引；空集/全 0 时自然得 0
            peak_hour=max(range(24), key=hour_dist.__getitem__),
            peak_weekday=max(range(7), key=weekday_dist.__getitem__),
            avg_length=(total_chars / total) if total else 0.0,
            total_chars=total_chars,
            emoji_ratio=(emoji_count / total) if total else 0.0,
            question_ratio=(question_count / total) if total else 0.0,
            top_partners=partners,
            truncated=truncated,
        )
