"""AstrBot 平台适配层（隔离所有 AstrBot API 调用）。

本文件是唯一允许 import astrbot 的地方（main.py 除外）。
如果 AstrBot 版本升级导致接口变动，只需修改本文件。

已对照 AstrBot master (2026-09) 验证的接口：
- astrbot.api.star: Context / Star / register / StarTools
- astrbot.api.event: filter / AstrMessageEvent / MessageChain
- astrbot.api.event.filter: on_llm_request / on_llm_response / command / llm_tool / PermissionType
- astrbot.api.provider: ProviderRequest / LLMResponse
- provider.text_chat(prompt, session_id, image_urls, audio_urls, func_tool, contexts, system_prompt, ...) -> LLMResponse
- context.get_using_provider(umo) / context.get_provider_by_id(provider_id)
- event.get_sender_id() / event.unified_msg_origin / event.is_admin / event.message_str

TODO(需按版本确认的接口，均有安全降级):
1. File 消息组件的 get_file() 下载方法 —— 已用 try 多种属性名兼容。
2. EmbeddingProvider 的向量化方法名 —— embedding 功能默认关闭。
3. conversation_manager 的 history JSON 字段结构 —— /记忆 压缩 手动压缩用。
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Callable

logger = logging.getLogger("memory_system.adapter")

try:  # AstrBot 平台导入（隔离在此，核心模块不依赖）
    from astrbot.api import logger as astrbot_logger  # noqa: F401
    from astrbot.api.event import AstrMessageEvent, MessageChain
    from astrbot.api.star import Context, StarTools
    from astrbot.api.provider import LLMResponse, ProviderRequest  # noqa: F401

    _ASTRBOT_AVAILABLE = True
except ImportError:  # 允许在非 AstrBot 环境下做单元测试
    _ASTRBOT_AVAILABLE = False
    AstrMessageEvent = Any  # type: ignore
    MessageChain = Any  # type: ignore
    Context = Any  # type: ignore
    StarTools = Any  # type: ignore
    LLMResponse = Any  # type: ignore
    ProviderRequest = Any  # type: ignore


class AstrBotAdapter:
    """封装所有与 AstrBot 平台的交互，供 core 层调用。"""

    def __init__(self, context: Context, config: dict[str, Any]) -> None:
        self.context = context
        self.config = config

    # ------------------------------------------------------------------ context

    def get_provider(self) -> Any | None:
        """获取用于辅助 LLM 调用的对话提供商。

        优先使用配置中指定的 llm_provider_id，否则回退到当前使用的提供商。
        """
        provider_id = str(self.config.get("llm_provider_id", "") or "").strip()
        try:
            if provider_id:
                provider = self.context.get_provider_by_id(provider_id)
                if provider is not None:
                    return provider
                logger.warning("配置的 llm_provider_id=%s 未找到，回退到当前提供商", provider_id)
            umo = getattr(self, "_current_umo", None)
            return self.context.get_using_provider(umo) if umo else self.context.get_using_provider()
        except Exception:
            logger.exception("获取 Provider 失败")
            return None

    def make_llm_caller(self) -> Callable[[str, str], Any]:
        """返回 core 层使用的 LLM 调用闭包: async (prompt, system_prompt) -> str。"""

        async def _call(prompt: str, system_prompt: str) -> str:
            provider = self.get_provider()
            if provider is None:
                raise RuntimeError("没有可用的 LLM Provider")
            resp: LLMResponse = await provider.text_chat(
                prompt=prompt,
                session_id=None,
                contexts=[],
                image_urls=[],
                system_prompt=system_prompt or "",
            )
            text = getattr(resp, "completion_text", "") or ""
            if not text and getattr(resp, "result_chain", None) is not None:
                try:  # 兜底：从 MessageChain 提取纯文本
                    text = "".join(
                        getattr(c, "text", "") if hasattr(c, "text") else str(c)
                        for c in resp.result_chain.chain
                    )
                except Exception:
                    text = ""
            return text

        return _call

    # ------------------------------------------------------------------- event

    @staticmethod
    def get_user_id(event: AstrMessageEvent) -> str:
        """用户唯一标识（sender_id），用于记忆隔离。"""
        try:
            return str(event.get_sender_id() or "unknown")
        except Exception:
            return "unknown"

    @staticmethod
    def get_session_id(event: AstrMessageEvent) -> str:
        """会话标识（unified_msg_origin），用于多会话隔离。"""
        try:
            return str(event.unified_msg_origin or "")
        except Exception:
            return ""

    def bind_umo(self, umo: str) -> None:
        """记录当前 umo，供会话隔离的提供商选择使用。"""
        self._current_umo = umo

    @staticmethod
    def is_admin(event: AstrMessageEvent) -> bool:
        """判断事件发送者是否为管理员。"""
        try:
            return bool(event.is_admin())
        except Exception:
            return False

    def get_data_dir(self) -> str:
        """插件数据目录（用于导出文件等）。"""
        try:
            return str(StarTools.get_data_dir("astrbot_plugin_memory_system"))
        except Exception:
            fallback = os.path.join(
                "data", "plugin_data", "astrbot_plugin_memory_system"
            )
            os.makedirs(fallback, exist_ok=True)
            return fallback

    # -------------------------------------------------------------- file utils

    @staticmethod
    async def file_component_to_path(event: AstrMessageEvent) -> str | None:
        """从事件消息中提取 File 组件并转为本地路径（/记忆 导入 用）。"""
        if not _ASTRBOT_AVAILABLE:
            return None
        try:
            from astrbot.api.message_components import File

            for comp in event.get_messages():
                if isinstance(comp, File):
                    for attr in ("path", "file"):
                        val = getattr(comp, attr, None)
                        if isinstance(val, str) and os.path.exists(val):
                            return val
                    get_file = getattr(comp, "get_file", None)
                    if callable(get_file):
                        path = await get_file()
                        if isinstance(path, str) and os.path.exists(path):
                            return path
                    # URL 下载兜底
                    url = getattr(comp, "url", None)
                    if isinstance(url, str) and url.startswith("http"):
                        import aiohttp  # type: ignore

                        dest = os.path.join(
                            os.path.expanduser("~"), f"mem_import_{int(__import__('time').time())}.json"
                        )
                        async with aiohttp.ClientSession() as sess:
                            async with sess.get(url) as r:
                                if r.status == 200:
                                    with open(dest, "wb") as f:
                                        f.write(await r.read())
                                    return dest
        except Exception:
            logger.exception("提取导入文件失败")
        return None

    # --------------------------------------------------------------- embedding

    async def get_embedding(self, text: str) -> list[float] | None:
        """（可选）文本向量化。

        TODO: EmbeddingProvider 的方法名在不同版本有差异
        （get_embedding / text_to_embedding / embed），确认后启用。
        默认配置 embedding_enable=false，不调用本方法。
        """
        if not self.config.get("embedding_enable", False):
            return None
        provider_id = str(self.config.get("embedding_provider_id", "") or "").strip()
        try:
            provider = (
                self.context.get_provider_by_id(provider_id)
                if provider_id
                else None
            )
            if provider is None:
                return None
            for method_name in ("get_embedding", "text_to_embedding", "embed"):
                method = getattr(provider, method_name, None)
                if callable(method):
                    result = await method(text)
                    if isinstance(result, (list, tuple)):
                        return [float(x) for x in result]
        except Exception:
            logger.debug("embedding 获取失败", exc_info=True)
        return None

    # ---------------------------------------------------- conversation (manual)

    async def get_conversation_history(self, umo: str) -> list[dict[str, Any]] | None:
        """读取当前会话的对话历史（/记忆 压缩 手动压缩用）。

        TODO: Conversation.history 在不同版本为 JSON 字符串或 list，
        已做双兼容；若结构不符请按实际版本修正。
        """
        try:
            conv_mgr = getattr(self.context, "conversation_manager", None)
            if conv_mgr is None:
                return None
            conv = await conv_mgr.get_conversation(umo)
            if conv is None:
                return None
            history = getattr(conv, "history", None)
            if isinstance(history, str) and history.strip():
                import json

                parsed = json.loads(history)
                return parsed if isinstance(parsed, list) else None
            if isinstance(history, list):
                return history
        except Exception:
            logger.debug("读取对话历史失败", exc_info=True)
        return None

    async def save_conversation_history(
        self, umo: str, history: list[dict[str, Any]]
    ) -> bool:
        """把压缩后的历史写回会话。TODO: 同上，按版本确认。"""
        try:
            conv_mgr = getattr(self.context, "conversation_manager", None)
            if conv_mgr is None:
                return False
            import json

            await conv_mgr.update_conversation(
                umo, history=json.dumps(history, ensure_ascii=False)
            )
            return True
        except Exception:
            logger.debug("写回对话历史失败", exc_info=True)
            return False


def sanitize_filename(name: str) -> str:
    """过滤文件名中的非法字符。"""
    return re.sub(r"[^\w\u4e00-\u9fff.-]", "_", name)[:64] or "export"
