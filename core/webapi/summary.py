"""Web API 总结端点（v0.6.0 包化拆分）。

SummaryMixin：总结设置 / LLM 提供商 / 忽略名单 / 历史总结 等 v0.3+ 端点。
"""

import asyncio
import json
from pathlib import Path

from astrbot.api import logger
from astrbot.api.web import error_response, file_response, json_response, request
from astrbot.core.utils.io import save_temp_img

from ..db_config import ConfigManager


class SummaryMixin:
    """总结端点 Mixin（v0.6.0 拆分自 web_api.py）。"""

    async def api_summary_settings(self):
        """获取全部总结配置（附默认值与类型声明，供前端渲染表单与说明）。"""
        settings = await self.config_mgr.get_all_summary_settings()
        # 类型对象序列化为类型名（如 "bool"/"int"/"float"/"list"/"str"）
        types = {key: t.__name__ for key, t in ConfigManager.SUMMARY_TYPES.items()}
        return json_response(
            {
                "settings": settings,
                "defaults": ConfigManager.SUMMARY_DEFAULTS,
                "types": types,
            }
        )

    async def api_summary_settings_save(self):
        """批量保存总结配置。

        先校验后写入：所有传入键值全部通过 SUMMARY_TYPES 声明类型校验后才
        逐项写入，避免靠后字段非法时靠前字段已被写入的部分生效问题
        （沿用 api_save_settings 模式）。

        值归一化：bool→"true"/"false"；list/dict→JSON 序列化；其余→str()。
        """
        payload = await request.json(default={})
        incoming = payload.get("settings")
        if not isinstance(incoming, dict):
            return error_response("settings 必须为键值对象", status_code=400)

        # ① 未知键检查 + ② 值归一化为存储字符串
        normalized: dict[str, str] = {}
        for key, value in incoming.items():
            if key not in ConfigManager.SUMMARY_TYPES:
                return error_response(f"未知的配置键: {key}", status_code=400)
            if isinstance(value, bool):
                str_value = "true" if value else "false"
            elif isinstance(value, (list, dict)):
                str_value = json.dumps(value, ensure_ascii=False)
            else:
                str_value = str(value)
            normalized[key] = str_value

        # ③ 逐个按声明类型校验，任一失败整体 400、不写入任何项
        for key, str_value in normalized.items():
            try:
                ConfigManager._convert_summary_value(
                    str_value, ConfigManager.SUMMARY_TYPES[key]
                )
            except (ValueError, TypeError) as e:
                return error_response(f"配置项 {key} 的值非法: {e}", status_code=400)

        # ④ 全部校验通过后批量写入
        try:
            for key, str_value in normalized.items():
                ok = await self.config_mgr.set_summary_setting(key, str_value)
                if not ok:
                    return error_response("保存总结配置失败", status_code=500)
            settings = await self.config_mgr.get_all_summary_settings()
        except Exception:
            logger.error("[HistorySummary] 保存总结配置失败", exc_info=True)
            return error_response("保存总结配置失败", status_code=500)

        return json_response({"saved": True, "settings": settings})

    async def api_summary_settings_reset(self):
        """恢复总结配置默认值。

        keys 字段缺失或为 null → 全部重置（传 None）；
        提供列表（含空列表）→ 原样透传（空列表 = 不重置任何项）。
        """
        payload = await request.json(default={})
        keys = payload.get("keys")
        if keys is not None and not isinstance(keys, list):
            return error_response("keys 必须为配置键列表", status_code=400)

        try:
            settings = await self.config_mgr.reset_summary_settings(keys)
        except Exception:
            logger.error("[HistorySummary] 重置总结配置失败", exc_info=True)
            return error_response("重置总结配置失败", status_code=500)
        return json_response({"reset": True, "settings": settings})

    async def api_summary_providers(self):
        """获取可用 LLM（Chat Completion）提供商列表，供前端下拉选择。

        经 ``Context.get_all_providers()`` 获取（框架仅返回 CHAT_COMPLETION
        类型），从 provider_config 取 id/name。获取失败或无可用提供商时
        返回空列表并记 warning，不抛 500。
        """
        providers: list[dict] = []
        try:
            for prov in self.context.get_all_providers():
                cfg = getattr(prov, "provider_config", None) or {}
                prov_id = str(cfg.get("id") or "").strip()
                if not prov_id:
                    continue
                prov_name = str(cfg.get("name") or "").strip() or prov_id
                providers.append({"id": prov_id, "name": prov_name})
        except Exception:
            logger.warning("[HistorySummary] 获取 LLM 提供商列表失败", exc_info=True)
            providers = []
        return json_response({"providers": providers})

    async def api_summary_ignore_groups(self):
        """获取存在总结忽略记录的群列表。"""
        try:
            groups = await self.config_mgr.list_ignore_groups()
        except Exception:
            logger.error("[HistorySummary] 获取忽略群列表失败", exc_info=True)
            return error_response("获取忽略群列表失败", status_code=500)
        return json_response({"groups": groups})

    async def api_summary_ignore_list(self):
        """获取指定群的总结忽略名单。"""
        group_id = (request.query.get("group_id") or "").strip()
        if not group_id:
            return error_response("缺少 group_id 参数", status_code=400)
        if not group_id.isdigit():
            return error_response("group_id 必须为纯数字", status_code=400)

        try:
            senders = await self.config_mgr.get_ignore_senders(group_id)
        except Exception:
            logger.error("[HistorySummary] 获取忽略名单失败", exc_info=True)
            return error_response("获取忽略名单失败", status_code=500)
        return json_response({"group_id": group_id, "senders": senders})

    async def api_summary_ignore_add(self):
        """将成员加入群的总结忽略名单。"""
        payload = await request.json(default={})
        group_id = str(payload.get("group_id") or "").strip()
        sender_id = str(payload.get("sender_id") or "").strip()
        if not group_id or not sender_id:
            return error_response("缺少 group_id 或 sender_id 参数", status_code=400)
        if not group_id.isdigit():
            return error_response("group_id 必须为纯数字", status_code=400)

        try:
            added = await self.config_mgr.add_ignore_sender(group_id, sender_id)
        except Exception:
            logger.error("[HistorySummary] 添加忽略成员失败", exc_info=True)
            return error_response("添加忽略成员失败", status_code=500)
        if not added:
            return error_response("该发送者已在忽略名单中", status_code=409)
        return json_response({"added": True})

    async def api_summary_ignore_remove(self):
        """将成员移出群的总结忽略名单。"""
        payload = await request.json(default={})
        group_id = str(payload.get("group_id") or "").strip()
        sender_id = str(payload.get("sender_id") or "").strip()
        if not group_id or not sender_id:
            return error_response("缺少 group_id 或 sender_id 参数", status_code=400)
        if not group_id.isdigit():
            return error_response("group_id 必须为纯数字", status_code=400)

        try:
            removed = await self.config_mgr.remove_ignore_sender(group_id, sender_id)
        except Exception:
            logger.error("[HistorySummary] 移除忽略成员失败", exc_info=True)
            return error_response("移除忽略成员失败", status_code=500)
        if not removed:
            return error_response("该发送者不在忽略名单中", status_code=404)
        return json_response({"removed": True})

    async def api_summary_history(self):
        """分页列出历史总结记录（group_id 可选，缺省为全部群）。"""
        if self.summary_storage is None:
            return error_response("总结模块尚未初始化", status_code=503)

        # 群号为文本字段，空字符串视同未提供（None = 全部群）
        group_id = (request.query.get("group_id") or "").strip() or None
        # 显式转换，避免框架 type=int 行为不一致（参考 api_query 风格）
        page_str = request.query.get("page") or "1"
        try:
            page = int(page_str)
        except (ValueError, TypeError):
            page = 1
        page_size_str = request.query.get("page_size") or "20"
        try:
            page_size = int(page_size_str)
        except (ValueError, TypeError):
            page_size = 20

        # 边界归一
        if page < 1:
            page = 1
        if page_size < 1:
            page_size = 20
        if page_size > 200:
            page_size = 200

        try:
            result = await self.summary_storage.list_by_group(group_id, page, page_size)
        except ValueError as e:
            # group_id 非纯数字等入参错误
            return error_response(str(e), status_code=400)
        except Exception:
            logger.error("[HistorySummary] 获取历史总结列表失败", exc_info=True)
            return error_response("获取历史总结列表失败", status_code=500)
        return json_response(result)

    async def api_summary_history_detail(self):
        """读取单条历史总结详情。

        群号与文件名的白名单校验（防路径穿越）已在 SummaryStorage 层完成，
        非法或不存在统一返回 None → 404。
        """
        if self.summary_storage is None:
            return error_response("总结模块尚未初始化", status_code=503)

        group_id = (request.query.get("group_id") or "").strip()
        filename = (request.query.get("filename") or "").strip()
        if not group_id or not filename:
            return error_response("缺少 group_id 或 filename 参数", status_code=400)

        try:
            detail = await self.summary_storage.read(group_id, filename)
        except Exception:
            logger.error("[HistorySummary] 读取总结详情失败", exc_info=True)
            return error_response("读取总结详情失败", status_code=500)
        if detail is None:
            return error_response("总结记录不存在", status_code=404)
        return json_response({"detail": detail})

    async def api_summary_history_export(self):
        """导出历史总结为图片（后端 T2I 流水线渲染，与聊天端图片同模板同配置）。

        T2I 渲染耗时可能达数十秒，由前端 bridge.download 触发浏览器下载；
        渲染失败（模板缺失 / 两轮渲染全败等）统一 502，前端 toast 明确报错。
        """
        if self.summary_storage is None:
            return error_response("总结模块尚未初始化", status_code=503)

        group_id = (request.query.get("group_id") or "").strip()
        filename = (request.query.get("filename") or "").strip()
        if not group_id or not filename:
            return error_response("缺少 group_id 或 filename 参数", status_code=400)

        try:
            detail = await self.summary_storage.read(group_id, filename)
        except Exception:
            logger.error("[HistorySummary] 导出前读取总结详情失败", exc_info=True)
            return error_response("读取总结详情失败", status_code=500)
        if detail is None:
            return error_response("总结记录不存在", status_code=404)

        renderer = self.summary_renderer
        if renderer is None:
            return error_response("总结模块尚未初始化", status_code=503)
        try:
            img_bytes = await renderer.render_from_dict(detail)
        except ValueError as e:
            logger.warning(f"[HistorySummary] 导出图片渲染失败: {e}")
            return error_response("渲染失败，请稍后重试", status_code=502)
        except Exception:
            logger.error("[HistorySummary] 导出图片渲染异常", exc_info=True)
            return error_response("渲染失败，请稍后重试", status_code=502)

        try:
            path = await asyncio.to_thread(save_temp_img, img_bytes)
        except Exception:
            logger.error("[HistorySummary] 导出图片落盘失败", exc_info=True)
            return error_response("导出图片落盘失败", status_code=500)
        # filename 可能带目录前缀（如 group_123/xxx.json），用 stem 取纯文件名再拼
        # .png，避免下载名含路径分隔符与 .json.png 双后缀
        return file_response(path, filename=f"{Path(filename).stem}.png")
