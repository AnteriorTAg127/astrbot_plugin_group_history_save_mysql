"""Web API 人物分析端点（v0.6.0 包化拆分）。

ProfileMixin：人物分析设置 / LLM 提供商 / 可选群 / 发起分析 / 历史记录
等 v0.4.0+ 端点。
"""

import asyncio
import json
from pathlib import Path

from astrbot.api import logger
from astrbot.api.web import error_response, file_response, json_response, request
from astrbot.core.utils.io import save_temp_img

from ..db_config import ConfigManager
from .base import PROFILE_ANALYZE_TIMEOUT, _profile_result_to_dict


class ProfileMixin:
    """人物分析端点 Mixin（v0.6.0 拆分自 web_api.py）。"""

    async def api_profile_settings(self):
        """获取全部人物分析配置（附默认值与类型声明，供前端渲染表单）。

        settings 为逐项类型化后的完整 19 项；defaults/types 原样透出
        ``PROFILE_DEFAULTS`` / ``PROFILE_TYPES``（类型已是 "bool"/"int"/... 字符串）。
        """
        try:
            settings = await self.config_mgr.get_all_profile_settings()
        except Exception:
            logger.error("[Profile] 获取人物分析配置失败", exc_info=True)
            return error_response("获取人物分析配置失败", status_code=500)
        return json_response(
            {
                "settings": settings,
                "defaults": ConfigManager.PROFILE_DEFAULTS,
                "types": ConfigManager.PROFILE_TYPES,
            }
        )

    async def api_profile_settings_save(self):
        """批量保存人物分析配置。

        先校验后写入：所有传入键值全部通过 PROFILE_TYPES 声明类型校验后才
        交由 ``save_profile_settings`` 写入，任一非法整体 400、不写入任何项
        （沿用 api_summary_settings_save 范式）；``save_profile_settings``
        返回 False（校验未通过或数据库写入异常）→ 500。
        值归一化：bool→"true"/"false"；list/dict→JSON 序列化；其余→str()。
        """
        payload = await request.json(default={})
        incoming = payload.get("settings")
        if not isinstance(incoming, dict):
            return error_response("settings 必须为键值对象", status_code=400)

        # ① 未知键检查 + ② 值归一化为存储字符串
        normalized: dict[str, str] = {}
        for key, value in incoming.items():
            if key not in ConfigManager.PROFILE_TYPES:
                return error_response(f"未知的配置键: {key}", status_code=400)
            if isinstance(value, bool):
                str_value = "true" if value else "false"
            elif isinstance(value, (list, dict)):
                str_value = json.dumps(value, ensure_ascii=False)
            else:
                str_value = str(value)
            normalized[key] = str_value

        # ③ 逐个按声明类型校验，任一失败整体 400、不写入任何项
        #    （json.JSONDecodeError 为 ValueError 子类，已被覆盖）
        for key, str_value in normalized.items():
            try:
                ConfigManager._convert_profile_value(
                    str_value, ConfigManager.PROFILE_TYPES[key]
                )
            except (ValueError, TypeError) as e:
                return error_response(f"配置项 {key} 的值非法: {e}", status_code=400)

        # ④ 全部校验通过后写入
        try:
            ok = await self.config_mgr.save_profile_settings(normalized)
            if not ok:
                return error_response("保存设置失败（数据库写入异常）", status_code=500)
            settings = await self.config_mgr.get_all_profile_settings()
        except Exception:
            logger.error("[Profile] 保存人物分析配置失败", exc_info=True)
            return error_response("保存人物分析配置失败", status_code=500)

        return json_response({"saved": True, "settings": settings})

    async def api_profile_settings_reset(self):
        """恢复人物分析配置默认值（全部 19 项，``reset_profile_settings`` 无入参）。"""
        try:
            await self.config_mgr.reset_profile_settings()
            settings = await self.config_mgr.get_all_profile_settings()
        except Exception:
            logger.error("[Profile] 重置人物分析配置失败", exc_info=True)
            return error_response("重置人物分析配置失败", status_code=500)
        return json_response({"reset": True, "settings": settings})

    async def api_profile_providers(self):
        """获取可用 LLM（Chat Completion）提供商列表，供前端下拉选择。

        经 ``Context.get_all_providers()`` 获取（框架仅返回 CHAT_COMPLETION
        类型），从 provider_config 取 id/name。获取失败或无可用提供商时
        返回空列表并记 warning，不抛 500（与 summary providers 同源同范式）。
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
            logger.warning("[Profile] 获取 LLM 提供商列表失败", exc_info=True)
            providers = []
        return json_response({"providers": providers})

    async def api_profile_groups(self):
        """获取「发起分析」群下拉列表（模式感知，v0.5.6 修复 all_mode 下无群可选）。

        返回 ``{"groups": [{"group_id", "enabled", "count"}], "all_mode": bool}``：
        白名单 ∪ chat_history 有数据的群（去重、消息数降序），两种记录模式下
        下拉框都能列出可分析的群。profile_service 未注入时回落白名单语义。
        """
        if self.profile_service is not None:
            try:
                groups = await self.profile_service.resolve_launch_groups()
                all_mode = await self.profile_service.is_all_mode()
            except Exception:
                logger.error("[Profile] 获取群列表失败", exc_info=True)
                return error_response("获取群列表失败", status_code=500)
        else:
            try:
                groups = await self.config_mgr.get_groups()
            except Exception:
                logger.error("[Profile] 获取群列表失败", exc_info=True)
                return error_response("获取群列表失败", status_code=500)
            base = await self.config_mgr.get_all_settings()
            all_mode = base.get("all_mode", "false") == "true"
        return json_response({"groups": groups, "all_mode": all_mode})

    async def api_profile_analyze(self):
        """触发人物分析（跨群分析唯一入口）。

        body ``{sender_id, group_id}``：group_id 为空或 ``"all"`` → 全局分析
        （``event=None``，不经 OneBot）。服务端以 ``asyncio.wait_for`` 包裹
        ``run_analysis`` 做内部超时兜底（``PROFILE_ANALYZE_TIMEOUT``），超时
        返回 504 结构化错误。成功返回序列化的 ProfileResult（顶层 ``result``
        键避撞桥接解包）。``run_analysis`` 自身绝不抛异常，失败返回降级结果。
        """
        if self.profile_service is None:
            return error_response("人物分析模块尚未初始化", status_code=503)

        payload = await request.json(default={})
        sender_id = str(payload.get("sender_id") or "").strip()
        group_id = str(payload.get("group_id") or "").strip()
        if not sender_id:
            return error_response("缺少 sender_id 参数", status_code=400)
        if not sender_id.isdigit():
            return error_response("sender_id 必须为纯数字 QQ 号", status_code=400)

        try:
            result = await asyncio.wait_for(
                self.profile_service.run_analysis(sender_id, group_id, event=None),
                timeout=PROFILE_ANALYZE_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.error(
                f"[Profile] Web 触发分析超时（>{PROFILE_ANALYZE_TIMEOUT}s）："
                f"sender={sender_id} group={group_id or 'all'}"
            )
            return error_response(
                f"分析超时（超过 {PROFILE_ANALYZE_TIMEOUT} 秒），请稍后重试",
                status_code=504,
            )
        except Exception:
            logger.error("[Profile] Web 触发分析异常", exc_info=True)
            return error_response("分析失败，请稍后重试", status_code=500)

        return json_response({"result": _profile_result_to_dict(result)})

    async def api_profile_history(self):
        """分页列出历史人物分析记录。

        返回 ``{"total", "profiles", "page", "page_size"}``（顶层用 profiles
        专名避撞桥接解包）。分页参数显式转换并做边界归一（参考 summary history）。
        """
        if self.profile_storage is None:
            return error_response("人物分析模块尚未初始化", status_code=503)

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
            result = await self.profile_storage.list_profiles(page, page_size)
        except Exception:
            logger.error("[Profile] 获取历史分析列表失败", exc_info=True)
            return error_response("获取历史分析列表失败", status_code=500)
        return json_response(result)

    async def api_profile_history_detail(self):
        """读取单条历史人物分析详情。

        filename 为 list_profiles 返回的相对名（``<scope目录>/<文件名>``）；
        路径穿越白名单校验已在 ProfileStorage 层完成，非法或不存在统一
        返回 None → 404。
        """
        if self.profile_storage is None:
            return error_response("人物分析模块尚未初始化", status_code=503)

        filename = (request.query.get("filename") or "").strip()
        if not filename:
            return error_response("缺少 filename 参数", status_code=400)

        try:
            detail = await self.profile_storage.read(filename)
        except Exception:
            logger.error("[Profile] 读取分析详情失败", exc_info=True)
            return error_response("读取分析详情失败", status_code=500)
        if detail is None:
            return error_response("分析记录不存在", status_code=404)
        return json_response({"detail": detail})

    async def api_profile_history_export(self):
        """导出历史人物分析为图片（后端 T2I 流水线渲染，与聊天端图片同模板同配置）。

        filename 为 list_profiles 返回的相对名（``<scope目录>/<文件名>``）；
        渲染失败（模板缺失 / 两轮渲染全败等）统一 502，前端 toast 明确报错。
        """
        if self.profile_storage is None:
            return error_response("人物分析模块尚未初始化", status_code=503)

        filename = (request.query.get("filename") or "").strip()
        if not filename:
            return error_response("缺少 filename 参数", status_code=400)

        try:
            detail = await self.profile_storage.read(filename)
        except Exception:
            logger.error("[Profile] 导出前读取分析详情失败", exc_info=True)
            return error_response("读取分析详情失败", status_code=500)
        if detail is None:
            return error_response("分析记录不存在", status_code=404)

        renderer = self.profile_renderer
        if renderer is None:
            return error_response("人物分析模块尚未初始化", status_code=503)
        try:
            img_bytes = await renderer.render_from_dict(detail)
        except ValueError as e:
            logger.warning(f"[Profile] 导出图片渲染失败: {e}")
            return error_response("渲染失败，请稍后重试", status_code=502)
        except Exception:
            logger.error("[Profile] 导出图片渲染异常", exc_info=True)
            return error_response("渲染失败，请稍后重试", status_code=502)

        try:
            path = await asyncio.to_thread(save_temp_img, img_bytes)
        except Exception:
            logger.error("[Profile] 导出图片落盘失败", exc_info=True)
            return error_response("导出图片落盘失败", status_code=500)
        # filename 为 <scope目录>/<文件名> 相对名，用 stem 取纯文件名再拼 .png，
        # 避免下载名含路径分隔符与 .json.png 双后缀
        return file_response(path, filename=f"{Path(filename).stem}.png")

    async def api_profile_history_delete(self):
        """删除单条历史人物分析记录。

        filename 兼容 query 与 body 两种传参（DELETE 语义下前端多走 query）；
        路径穿越防护在 ProfileStorage 层完成，不存在/删除失败 → 404。
        """
        if self.profile_storage is None:
            return error_response("人物分析模块尚未初始化", status_code=503)

        filename = (request.query.get("filename") or "").strip()
        if not filename:
            payload = await request.json(default={})
            filename = str(payload.get("filename") or "").strip()
        if not filename:
            return error_response("缺少 filename 参数", status_code=400)

        try:
            deleted = await self.profile_storage.delete(filename)
        except Exception:
            logger.error("[Profile] 删除分析记录失败", exc_info=True)
            return error_response("删除分析记录失败", status_code=500)
        if not deleted:
            return error_response("分析记录不存在或删除失败", status_code=404)
        return json_response({"deleted": True})
