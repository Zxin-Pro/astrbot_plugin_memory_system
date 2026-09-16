"""上下文压缩器：历史超阈值时调用 LLM 生成摘要，保存为 summary 记忆并裁剪上下文。"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from .memory_manager import MemoryManager
from .prompts import COMPRESS_PROMPT
from .utils import estimate_tokens

logger = logging.getLogger("memory_system.compressor")

LLMCaller = Callable[[str, str], Awaitable[str]]


class ContextCompressor:
    """对话历史压缩。

    压缩策略：
    - 估算 contexts 的 token 总量，超过阈值时触发；
    - 保留最近 keep_recent_messages 条原文，更早的历史序列化后交给 LLM 摘要；
    - 摘要以 type=summary 写入长期记忆，并用一条 system 消息替换旧历史。
    """

    def __init__(
        self,
        manager: MemoryManager,
        llm_caller: LLMCaller,
        compress_threshold_tokens: int = 3000,
        keep_recent_messages: int = 8,
    ) -> None:
        self.manager = manager
        self._llm = llm_caller
        self._threshold = max(500, int(compress_threshold_tokens))
        self._keep = max(2, int(keep_recent_messages))

    # ----------------------------------------------------------------- helpers

    @staticmethod
    def _render_message(msg: dict[str, Any]) -> str:
        """把一条 OpenAI 格式消息渲染为纯文本行（兼容 content 为分片列表的情况）。"""
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if isinstance(content, list):
            texts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") in ("text", None):
                    texts.append(str(part.get("text", "")))
            content = "\n".join(t for t in texts if t)
        content = str(content).strip()
        if not content:
            return ""
        return f"[{role}] {content}"

    def estimate_context_tokens(self, contexts: list[dict[str, Any]]) -> int:
        return sum(
            estimate_tokens(self._render_message(m)) for m in contexts if isinstance(m, dict)
        )

    def should_compress(self, contexts: list[dict[str, Any]]) -> bool:
        if not contexts or len(contexts) <= self._keep:
            return False
        return self.estimate_context_tokens(contexts) > self._threshold

    # ---------------------------------------------------------------- compress

    async def compress(
        self,
        user_id: str,
        session_id: str,
        contexts: list[dict[str, Any]],
        force: bool = False,
    ) -> tuple[list[dict[str, Any]], str]:
        """压缩上下文。

        Returns:
            (新 contexts, 摘要文本)。未触发压缩时原样返回 (contexts, "")。
        """
        if not contexts or len(contexts) <= self._keep:
            return contexts, ""
        if not force and not self.should_compress(contexts):
            return contexts, ""
        try:
            history, tail = contexts[: -self._keep], contexts[-self._keep :]
            transcript = "\n".join(
                line for line in (self._render_message(m) for m in history) if line
            )
            if not transcript.strip():
                return contexts, ""
            summary = await self._llm(
                f"【对话历史】\n{transcript[:12000]}\n\n请生成摘要。",
                COMPRESS_PROMPT,
            )
            summary = (summary or "").strip()
            if not summary:
                return contexts, ""
            # 写入长期记忆
            await self.manager.memorize(
                user_id=user_id,
                content=summary,
                memory_type="summary",
                tags=["对话摘要"],
                importance=0.7,
                session_id=session_id,
                source="compressed",
            )
            # 用一条 system 摘要消息替换旧历史
            new_contexts: list[dict[str, Any]] = [
                {"role": "system", "content": f"（以下是更早对话的摘要）\n{summary}"},
                *tail,
            ]
            logger.info(
                "上下文已压缩: %d 条 -> %d 条 (user=%s)", len(contexts), len(new_contexts), user_id
            )
            return new_contexts, summary
        except Exception:
            logger.exception("上下文压缩失败（保留原历史）")
            return contexts, ""
