"""人物分析编排层（Module J）：指令/Web 双入口 + 校验链 + 全流程串联 + 错误兜底。

承接 main.py（Module M）薄 handler 委托的 ``/人物分析`` 聊天指令，以及 WebAPI
（Module K）``POST /profile/analyze`` 的跨群触发，串联上游模块：

- fetcher（Module D）目标消息 + 关系上下文获取（MySQL 主 / OneBot 补）
- stats（Module E）确定性统计（时间分布 / 长度 / 互动排行，无 AI）
- analyzer（Module F）LLM 叙述生成（独立 provider 降级链，绝不抛异常）
- storage + scheduler（Module I）JSON 持久化与每日过期清理
- formatter（Module H）消息链渲染（forward / image / text，内部降级绝不向上抛）

校验链（仅聊天指令入口，严格按序，见 PRD §3.7）：

1. 总开关 ``profile_enabled``（关 → 「人物分析功能未开启」）
2. 权限 ``profile_permission``（``admin`` → 运行时 ``event.is_admin()`` 判定，
   非管理员 → 「该指令仅管理员可用」；``all`` → 放行；取不到角色安全按非管理员）
3. 群环境（``event.get_group_id()`` 非空；非群 → 「请在群内使用」）
4. 限流**检查**（用户 / 群双冷却，内存 dict + ``time.monotonic()``；只检查不盖章）
5. 目标解析（@ 优先，剔除 ``all`` 与 bot 自身 → 纯数字 QQ号 → 皆无用法提示）
6. 限流**盖章**（目标解析通过、真正执行前才写时间戳，避免无效指令白耗冷却），
   盖章后立即发**触发反馈**（``profile_feedback_mode``：reaction 贴 👍 失败降级
   文字 ``profile_feedback_text`` / text 文字 / none 关闭）

``run_analysis`` 为指令与 Web 共用核心：scope 推导 → fetch → stats → analyze →
注入 ``sources`` / ``scope_desc`` / ``relation_context_complete`` / ``created_at``
→ storage.save（失败仅记日志不阻断）；**全流程 try/except 兜底绝不冒泡**，任何
异常记 ``[Profile]`` error 并返回降级 :class:`ProfileResult`。

框架 API 核实记录（AstrBot 主项目 ``astrbot/core/platform/astr_message_event.py``）：

- ``event.get_group_id() -> str``，非群组消息返回空字符串
- ``event.get_sender_id() -> str``（取 ``message_obj.sender.user_id``）
- ``event.get_self_id() -> str``（机器人自身 ID，用于 @ 目标剔除 bot）
- ``event.get_messages() -> list``（消息链，供 :func:`extract_at_targets`）
- ``event.is_admin() -> bool``（等价 ``self.role == "admin"``；框架经
  ``waking_check`` 阶段对超管/管理员置 ``role = "admin"``，无独立 superuser 值）
- ``event.plain_result(text) -> MessageEventResult``，**继承自** ``MessageChain``，
  故可直接传给 ``async def send(self, message: MessageChain)``；全程 ``await send``，
  **绝不 yield**（保证可脱离事件钩子调用）
- ``StarTools.get_data_dir(plugin_name) -> Path``，导入路径与 summary 一致

契约见 开发/v0.4.0/分工.md「共享接口契约 / Module J」，不得私改。
"""

from __future__ import annotations

import asyncio
import math
import re
import time
from datetime import datetime
from typing import TYPE_CHECKING

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import StarTools

from .analyzer import ProfileAnalyzer
from .capture import extract_at_targets
from .fetcher import ProfileFetcher
from .formatter import ProfileFormatter
from .models import ProfileResult, ProfileTarget
from .scheduler import ProfileCleanupScheduler
from .stats import ProfileStatsBuilder
from .storage import ProfileStorage
from .t2i_render import ProfileT2IRenderer

if TYPE_CHECKING:
    from astrbot.api.star import Context, Star

    from ..db_config import ConfigManager
    from ..db_mysql import MySQLManager

# 插件名（与 db_config.py / summary service 一致），供 StarTools.get_data_dir 定位数据目录
PLUGIN_NAME = "astrbot_plugin_group_history_save_mysql"

# 纯数字 QQ号 正则（目标解析的第二路；@ 优先于参数）
_QQ_RE = re.compile(r"^\d+$")

# 落盘/展示统一时间格式（与 storage 一致）
_TIME_FMT = "%Y-%m-%d %H:%M:%S"

# ---------- 提示性文案（统一经 plain_result + await send 发送） ----------
_MSG_DISABLED = "人物分析功能未开启"
_MSG_NOT_GROUP = "请在群内使用"
_MSG_NOT_ADMIN = "该指令仅管理员可用"
_MSG_TOO_FREQUENT = "操作太频繁，请 {remain} 秒后再试"
_MSG_USAGE = "请 @ 要分析的人，或输入 QQ号，如 /人物分析 12345678"
_MSG_NO_MATERIAL = "该范围内没有可分析的消息"
_MSG_ANALYSIS_FAILED = "人物分析生成失败，请稍后重试"

# 冷却配置的最终兜底值（配置层已含回退，此处仅防御异常类型）
_DEFAULT_USER_COOLDOWN = 60
_DEFAULT_GROUP_COOLDOWN = 30

# ---------- 触发反馈（指令生效后即时确认，见 ProfileService._send_feedback） ----------
_DEFAULT_FEEDBACK_TEXT = "正在生成人物画像，请稍候…"
_REACTION_ACTION = "set_msg_emoji_like"
# 协议端要求参数名为 emoji_id；值取 👍 的 Unicode 码点（十进制字符串）
_REACTION_EMOJI_ID = "128077"


class ProfileService:
    """人物分析编排服务：指令/Web 双入口、校验链、全流程串联与错误兜底。

    Attributes:
        storage: 人物分析持久化器。公开以便 main.py（Module M）复用同一实例注入
            WebAPI（Module K）；base_dir 为
            ``StarTools.get_data_dir(PLUGIN_NAME) / "profiles"``。
    """

    def __init__(
        self,
        context: Context,
        config_mgr: ConfigManager,
        mysql_mgr: MySQLManager,
        star: Star,
    ) -> None:
        """构造服务并在内部组装上游模块与清理调度器。"""
        self._context = context
        self._config_mgr = config_mgr
        self._star = star
        # 上游模块实例（构造签名以各模块实际交付版本为准）
        self._fetcher = ProfileFetcher(mysql_mgr, config_mgr)
        self._stats_builder = ProfileStatsBuilder()
        self._analyzer = ProfileAnalyzer(context, config_mgr)
        renderer = ProfileT2IRenderer(star, config_mgr)
        self.renderer = renderer
        self._formatter = ProfileFormatter(star, config_mgr, renderer)
        self.storage = ProfileStorage(StarTools.get_data_dir(PLUGIN_NAME) / "profiles")
        self._scheduler = ProfileCleanupScheduler(self.storage, config_mgr)
        # 限流时间戳表（monotonic 时钟，仅内存不持久化）：
        # 检查与盖章分离——_precheck 只检查；_stamp_cooldown 在目标解析通过后盖章
        self._user_last: dict[str, float] = {}
        self._group_last: dict[str, float] = {}

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """启动过期分析清理调度器（失败仅记日志，不阻断插件启动）。"""
        try:
            await self._scheduler.start()
        except Exception:
            logger.error("[Profile] 启动清理调度器失败", exc_info=True)

    async def stop(self) -> None:
        """停止清理调度器：cancel + await，吞 CancelledError。"""
        try:
            await self._scheduler.stop()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.warning("[Profile] 停止清理调度器异常", exc_info=True)

    # ------------------------------------------------------------------
    # 聊天指令入口（main.py 薄 handler 委托于此，仅当前群）
    # ------------------------------------------------------------------

    async def handle_command(self, event: AstrMessageEvent, arg: str) -> None:
        """``/人物分析``（别名 ``/人物画像``、``/分析TA``）：分析当前群内指定用户。

        校验链（总开关 → 权限 → 群环境 → 限流检查 → 目标解析 → 限流盖章）通过后
        执行 ``run_analysis`` → ``formatter.render`` → ``await event.send``。
        异常分级兜底，绝不冒泡到 main.py handler 之外。
        """
        try:
            prechecked = await self._precheck(event)
            if prechecked is None:
                return
            group_id, sender_id = prechecked

            target_id = self._resolve_target(event, arg)
            if not target_id:
                await self._reply(event, _MSG_USAGE)
                return

            self._stamp_cooldown(sender_id, group_id)
            await self._send_feedback(event)

            result = await self.run_analysis(target_id, group_id, event)

            # 无素材短路（避免渲染空报告）；LLM 整链耗尽兜底文案
            if result.stats.total == 0:
                await self._reply(event, _MSG_NO_MATERIAL)
                return
            if not result.provider_id:
                await self._reply(event, _MSG_ANALYSIS_FAILED)
                return

            output_mode = await self._output_mode()
            chain = await self._formatter.render(result, output_mode)
            await event.send(chain)
        except Exception:
            # 最外层兜底：任何异常不得冒泡到 main.py handler 之外
            logger.error("[Profile] 人物分析指令处理异常", exc_info=True)
            await self._reply(event, _MSG_ANALYSIS_FAILED)

    # ------------------------------------------------------------------
    # 核心流程（指令 / Web 共用）
    # ------------------------------------------------------------------

    async def run_analysis(
        self, sender_id: str, group_id: str, event: AstrMessageEvent | None = None
    ) -> ProfileResult:
        """对单个目标执行完整人物分析，指令与 Web ``POST /profile/analyze`` 共用。

        Args:
            sender_id: 目标用户 QQ。
            group_id: 群号；**空串或 ``"all"`` → scope=all（全局，跨所有已保存群）**，
                否则 scope=group（单群）。Web 全局触发时 ``event`` 传 None，fetcher
                不做任何 OneBot 补齐（由其契约保证）。
            event: 当前消息事件（指令场景）；Web 场景为 None。

        Returns:
            ProfileResult：已注入 ``sources`` / ``scope_desc`` /
            ``relation_context_complete`` / ``created_at``。**绝不抛异常**：任何
            环节失败记 ``[Profile]`` error 并返回降级结果（provider_id 为空、
            sections 为「分析失败」单段），由调用方据此兜底。
        """
        scope, target_group_id = self._derive_scope(group_id)
        target = ProfileTarget(
            sender_id=str(sender_id or ""),
            sender_name="",
            scope=scope,
            group_id=target_group_id,
        )
        stats = None  # 异常兜底复用：analyze 前已算出的统计在降级时保留（total>0 → 上层报「生成失败」而非「无素材」）
        try:
            output_mode = await self._output_mode()

            # fetch（绝不抛）→ 从最近一条消息回填昵称
            outcome = await self._fetcher.fetch(target, event)
            target_messages = outcome.target_messages
            if target_messages:
                target.sender_name = target_messages[-1].sender_name or ""

            # stats（全量，无 AI）
            stats = self._stats_builder.build(target_messages, outcome.partners)

            if target_messages:
                result = await self._analyzer.analyze(
                    event,
                    target,
                    stats,
                    target_messages,
                    outcome.context_messages,
                    output_mode,
                )
            else:
                # 无素材短路：不调 LLM，产出降级结果（provider_id 为空）
                result = self._degraded_result(target, stats, scope, target_group_id)

            # 注入 fetcher/编排层元数据（analyzer 返回时这几项为默认值）
            result.sources = outcome.sources
            result.relation_context_complete = outcome.relation_context_complete
            result.scope_desc = self._scope_desc(scope, target_group_id, stats)
            result.created_at = datetime.now().strftime(_TIME_FMT)

            # 持久化：ValueError（scope/群号非法）/ 空串（磁盘错误）均仅记日志不阻断
            try:
                relname = await self.storage.save(result)
                if not relname:
                    logger.warning("[Profile] 分析结果持久化返回空（磁盘错误），不阻断")
            except ValueError:
                logger.warning(
                    "[Profile] 分析结果持久化参数非法（scope/群号），不阻断",
                    exc_info=True,
                )
            except Exception:
                logger.warning("[Profile] 分析结果持久化失败，不阻断", exc_info=True)

            logger.info(
                "[Profile] 分析完成 | scope=%s 目标=%s 拉取=%d 数据源=%s"
                " 关系对象=%d provider=%s 渲染模式=%s",
                scope,
                target.sender_id,
                stats.total,
                result.sources,
                len(outcome.partners),
                result.provider_id or "(失败)",
                output_mode,
            )
            return result
        except Exception:
            logger.error("[Profile] run_analysis 异常，降级返回失败结果", exc_info=True)
            return self._degraded_result(target, stats, scope, target_group_id)

    # ------------------------------------------------------------------
    # 校验链（步骤 1-4，仅聊天指令入口）
    # ------------------------------------------------------------------

    async def _precheck(self, event: AstrMessageEvent) -> tuple[str, str] | None:
        """执行校验链步骤 1-4（总开关 → 权限 → 群环境 → 限流检查）。

        全部通过返回 ``(group_id, sender_id)``，否则发送对应提示并返回 None。
        限流**只检查不盖章**，盖章在目标解析通过后由 _stamp_cooldown 完成。
        """
        # 1. 总开关（typed 读取已含类型回退，非 True 一律视为未启用）
        if not await self._config_mgr.get_profile_setting_typed("profile_enabled"):
            await self._reply(event, _MSG_DISABLED)
            return None

        # 2. 权限（admin 仅管理员 / all 所有人；未知值安全按 admin 处理）
        permission = (
            (await self._config_mgr.get_profile_setting("profile_permission"))
            .strip()
            .lower()
        )
        if permission != "all" and not self._is_admin(event):
            await self._reply(event, _MSG_NOT_ADMIN)
            return None

        # 3. 群环境（get_group_id 对非群消息返回空串）
        group_id = event.get_group_id()
        if not group_id:
            await self._reply(event, _MSG_NOT_GROUP)
            return None

        # 4. 限流检查（只检查，不盖章）
        # sender_id 兜底：get_sender_id 在 user_id 非 str 时返回空串，
        # 退化用会话 ID 作冷却键，保证限流维度不失效
        sender_id = event.get_sender_id() or f"session:{event.get_session_id()}"
        if not await self._check_cooldown(event, sender_id, group_id):
            return None
        return group_id, sender_id

    @staticmethod
    def _is_admin(event: AstrMessageEvent) -> bool:
        """运行时管理员判定：``event.is_admin()``（等价 role == "admin"）。

        框架经 waking_check 阶段对超管/管理员置 ``role = "admin"``，无独立
        superuser 值，故 is_admin 已覆盖「管理员/超管」。取不到安全按非管理员。
        """
        try:
            return bool(event.is_admin())
        except Exception:
            return False

    async def _check_cooldown(
        self, event: AstrMessageEvent, sender_id: str, group_id: str
    ) -> bool:
        """用户/群双冷却检查：任一仍在冷却 → 提示剩余秒数（向上取整）并返回 False。"""
        now = time.monotonic()
        user_cd = await self._int_setting(
            "profile_user_cooldown", _DEFAULT_USER_COOLDOWN
        )
        last = self._user_last.get(sender_id)
        if last is not None and now - last < user_cd:
            remain = math.ceil(user_cd - (now - last))
            await self._reply(event, _MSG_TOO_FREQUENT.format(remain=remain))
            return False
        group_cd = await self._int_setting(
            "profile_group_cooldown", _DEFAULT_GROUP_COOLDOWN
        )
        last = self._group_last.get(group_id)
        if last is not None and now - last < group_cd:
            remain = math.ceil(group_cd - (now - last))
            await self._reply(event, _MSG_TOO_FREQUENT.format(remain=remain))
            return False
        return True

    def _stamp_cooldown(self, sender_id: str, group_id: str) -> None:
        """盖限流时间戳（用户 + 群各记当前 monotonic）。

        仅在目标解析通过、真正开始执行前调用，避免非法参数等无效指令白耗冷却。
        """
        now = time.monotonic()
        self._user_last[sender_id] = now
        self._group_last[group_id] = now

    # ------------------------------------------------------------------
    # 目标解析（步骤 5）：@ 优先 → 纯数字 QQ号 → 皆无返回空串
    # ------------------------------------------------------------------

    def _resolve_target(self, event: AstrMessageEvent, arg: str) -> str:
        """解析分析目标 QQ：优先消息链 ``Comp.At``，其次纯数字参数。

        - @ 路：经 :func:`extract_at_targets` 提取（已剔除 ``"all"``、去重保序），
          再剔除 bot 自身（``event.get_self_id()``），取首个；
        - 参数路：``arg`` 去空白后为纯数字（``^\\d+$``）即视为 QQ号；
        - 两者皆无 → 返回空串（调用方发用法提示）。
        """
        # 1) @ 优先（extract_at_targets 已剔除 "all" 与空项、去重保序）
        try:
            chain = event.get_messages() or []
        except Exception:
            chain = []
        at_targets = extract_at_targets(chain)
        try:
            self_id = str(event.get_self_id() or "")
        except Exception:
            self_id = ""
        if self_id:
            at_targets = [qq for qq in at_targets if qq != self_id]
        if at_targets:
            return at_targets[0]

        # 2) 纯数字 QQ号
        stripped = (arg or "").strip()
        if _QQ_RE.match(stripped):
            return stripped

        # 3) 皆无
        return ""

    # ------------------------------------------------------------------
    # 触发反馈（限流盖章后、fetch/LLM 前的即时确认）
    # ------------------------------------------------------------------

    async def _send_feedback(self, event: AstrMessageEvent) -> None:
        """指令生效后即时反馈：reaction 贴表情（失败降级文字）/ text 文字 / none 关闭。

        模式取 ``profile_feedback_mode``（strip/lower 归一）；未知值（配置污染）
        记 warning 且不发消息。任何异常仅 warning，绝不冒泡、绝不阻断主流程。
        """
        try:
            mode = (
                (await self._config_mgr.get_profile_setting("profile_feedback_mode"))
                .strip()
                .lower()
            )
            if mode == "none":
                return
            if mode == "text":
                await self._reply(event, await self._feedback_text())
                return
            if mode == "reaction":
                if await self._react_to_trigger(event):
                    return
                # 贴表情失败 → 降级文字提示，保证用户始终收到确认
                await self._reply(event, await self._feedback_text())
                return
            logger.warning(
                "[Profile] 未知的 profile_feedback_mode: %r，跳过触发反馈", mode
            )
        except Exception:
            logger.warning("[Profile] 触发反馈失败，不影响主流程", exc_info=True)

    async def _feedback_text(self) -> str:
        """反馈文案：读 ``profile_feedback_text``；用户清空（空白）回退内置默认。"""
        text = (
            await self._config_mgr.get_profile_setting("profile_feedback_text")
        ).strip()
        return text or _DEFAULT_FEEDBACK_TEXT

    async def _react_to_trigger(self, event: AstrMessageEvent) -> bool:
        """经 OneBot 扩展 action 在触发消息上贴 👍（``set_msg_emoji_like``）。

        成功返回 True；任何失败返回 False（warning 日志），由调用方降级文字。
        client 获取方式参照 summary service 的防御式写法（getattr 取 bot、
        hasattr 检查 api）。
        """
        message_obj = getattr(event, "message_obj", None)
        message_id = getattr(message_obj, "message_id", None)
        if not message_id:
            logger.warning(
                "[Profile] 触发消息缺少 message_id，贴表情不可用，降级文字提示"
            )
            return False
        client = getattr(event, "bot", None)
        if client is None or not hasattr(client, "api"):
            logger.warning(
                "[Profile] 贴表情失败：取不到协议端 client"
                "（event.bot 为空或非 aiocqhttp 平台），降级文字提示"
            )
            return False
        try:
            message_id_int = int(str(message_id))
        except (TypeError, ValueError):
            logger.warning(
                "[Profile] 贴表情失败：message_id 无法转为整数（%r），降级文字提示",
                message_id,
            )
            return False
        try:
            await client.api.call_action(
                _REACTION_ACTION,
                message_id=message_id_int,
                emoji_id=_REACTION_EMOJI_ID,
            )
        except Exception:
            logger.warning(
                "[Profile] 贴表情失败（协议端可能不支持 %s），降级文字提示",
                _REACTION_ACTION,
                exc_info=True,
            )
            return False
        return True

    # ------------------------------------------------------------------
    # Web 群下拉（模式感知，v0.5.6 修复 all_mode 下只能选「全局」）
    # ------------------------------------------------------------------

    async def is_all_mode(self) -> bool:
        """读取全局记录模式开关（all_mode，口径同 stats 服务）。

        Returns:
            bool: 配置异常时按 False（白名单模式）处理，保守不扩大列表源。
        """
        try:
            settings = await self._config_mgr.get_all_settings()
            return settings.get("all_mode", "false") == "true"
        except Exception as e:
            logger.warning(f"[Profile] 读取 all_mode 失败（按白名单模式处理）: {e}")
            return False

    async def resolve_launch_groups(self) -> list[dict]:
        """Web「发起分析」群下拉：白名单 ∪ 有数据的群（去重，消息数降序）。

        白名单模式：白名单群 + 残留历史数据群；all_mode：即有数据的群
        （白名单为空）。保证两种记录模式下下拉框都能列出可分析的群
        （修复 all_mode 下只剩「全局」的问题，范式同
        ``stats.service.resolve_dropdown_groups``）。

        Returns:
            list[dict]: [{"group_id": str, "enabled": bool, "count": int | None}]；
            enabled 为白名单启用态（不在白名单记 True，all_mode 无白名单概念）；
            count 为历史消息总数（仅白名单且无数据的群为 None）。
        """
        try:
            whitelist = await self._config_mgr.get_groups()
        except Exception as e:
            logger.warning(f"[Profile] 读取白名单失败（下拉仅含有数据的群）: {e}")
            whitelist = []
        try:
            data_groups = await self._fetcher.get_all_groups_summary()
        except Exception as e:
            logger.warning(f"[Profile] 查询有数据的群失败（下拉仅含白名单）: {e}")
            data_groups = []
        merged: dict[str, dict] = {}
        for entry in whitelist or []:
            gid = str(entry.get("group_id", "") or "").strip()
            if gid:
                merged[gid] = {
                    "group_id": gid,
                    "enabled": bool(entry.get("enabled")),
                    "count": None,
                }
        for entry in data_groups or []:
            gid = entry["group_id"]
            if gid in merged:
                merged[gid]["count"] = entry["count"]
            else:
                merged[gid] = {
                    "group_id": gid,
                    "enabled": True,
                    "count": entry["count"],
                }
        return sorted(
            merged.values(),
            key=lambda item: (-(item["count"] or 0), item["group_id"]),
        )

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    @staticmethod
    def _derive_scope(group_id: str) -> tuple[str, str]:
        """由 group_id 推导 ``(scope, target_group_id)``。

        空串或 ``"all"``（大小写不敏感）→ ``("all", "")``；否则 ``("group", gid)``。
        """
        gid = str(group_id or "").strip()
        if not gid or gid.lower() == "all":
            return "all", ""
        return "group", gid

    @staticmethod
    def _scope_desc(scope: str, group_id: str, stats) -> str:
        """范围描述：单群「群 {group_id}」/ 全局「全部已保存群（N 个）」。"""
        if scope == "group":
            return f"群 {group_id}"
        group_count = getattr(stats, "group_count", 0) if stats is not None else 0
        return f"全部已保存群（{group_count} 个）"

    async def _output_mode(self) -> str:
        """输出模式：读 ``profile_output_mode``（strip，空白回退 forward）。"""
        return (
            await self._config_mgr.get_profile_setting("profile_output_mode")
        ).strip() or "forward"

    def _degraded_result(
        self,
        target: ProfileTarget,
        stats,
        scope: str,
        group_id: str,
    ) -> ProfileResult:
        """构造降级 :class:`ProfileResult`（「分析失败」单段 + provider_id 空）。

        stats 为 None 时用空消息集现算一份安全空统计。
        """
        if stats is None:
            stats = self._stats_builder.build([], [])
        return ProfileResult(
            target=target,
            stats=stats,
            sections=[("分析失败", _MSG_ANALYSIS_FAILED)],
            raw_llm_text="",
            provider_id="",
            messages_used=0,
            sources={},
            relation_context_complete=False,
            scope_desc=self._scope_desc(scope, group_id, stats),
            created_at=datetime.now().strftime(_TIME_FMT),
        )

    async def _reply(self, event: AstrMessageEvent, text: str) -> None:
        """发送提示性消息：plain_result + await send（绝不 yield）。

        ``MessageEventResult`` 是 ``MessageChain`` 子类，可直接传给 ``event.send``。
        发送失败仅 warning，避免错误分支二次崩溃。
        """
        try:
            await event.send(event.plain_result(text))
        except Exception:
            logger.warning("[Profile] 发送提示消息失败: %s", text, exc_info=True)

    async def _int_setting(self, key: str, default: int) -> int:
        """读取 int 配置（get_profile_setting_typed 已含回退，此处最终防御异常类型）。"""
        value = await self._config_mgr.get_profile_setting_typed(key)
        if isinstance(value, bool) or not isinstance(value, int):
            return default
        return value
