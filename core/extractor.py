"""自动记忆提取器：对话结束后调用 LLM 判断并提取值得长期记忆的信息。"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from .memory_manager import MemoryManager
from .prompts import EXTRACT_SYSTEM_PROMPT
from .utils import clamp, parse_tags, safe_json_loads

logger = logging.getLogger("memory_system.extractor")

LLMCaller = Callable[[str, str], Awaitable[str]]
"""签名: async (prompt: str, system_prompt: str) -> str（由 adapter 提供）"""


class MemoryExtractor:
    """调用 LLM 判断对话片段是否值得长期记忆，并结构化写入。"""

    def __init__(
        self,
        manager: MemoryManager,
        llm_caller: LLMCaller,
        min_text_length: int = 6,
        max_memories_per_turn: int = 3,
        importance_threshold: float = 0.5,
    ) -> None:
        self.manager = manager
        self._llm = llm_caller
        self._min_len = min_text_length
        self._max_items = max(1, max_memories_per_turn)
        self._importance_threshold = clamp(float(importance_threshold), 0.0, 1.0)

    def _build_user_prompt(self, user_text: str, assistant_text: str) -> str:
        return (
            "【对话片段】\n"
            f"用户: {user_text[:1500]}\n"
            f"助手: {assistant_text[:1500]}\n"
            "请判断并输出 JSON。"
        )

    def _parse_result(self, raw: str) -> list[dict[str, Any]]:
        """把 LLM 输出解析为合法记忆条目列表（容错：dict / list / 多余包裹）。"""
        data = safe_json_loads(raw)
        if data is None:
            return []
        if isinstance(data, dict):
            if data.get("should_memorize") is False:
                return []
            data = [data]
        if not isinstance(data, list):
            return []
        items: list[dict[str, Any]] = []
        for item in data[: self._max_items]:
            if not isinstance(item, dict):
                continue
            if item.get("should_memorize") is False:
                continue
            content = str(item.get("content", "")).strip()
            if not content:
                continue
            try:
                importance = clamp(float(item.get("importance", 0.5)), 0.0, 1.0)
            except (TypeError, ValueError):
                importance = 0.5
            if importance < self._importance_threshold:
                continue
            items.append(
                {
                    "type": str(item.get("type", "user_fact")).strip(),
                    "content": content,
                    "tags": parse_tags(item.get("tags")),
                    "importance": importance,
                }
            )
        return items

    async def extract_and_store(
        self,
        user_id: str,
        session_id: str,
        user_text: str,
        assistant_text: str,
    ) -> list[dict[str, Any]]:
        """对一轮对话执行提取，把结果写入数据库。

        任何异常都被捕获并记录日志，绝不影响正常聊天。
        Returns:
            成功写入的记忆条目列表。
        """
        try:
            if not user_text or len(user_text.strip()) < self._min_len:
                return []
            raw = await self._llm(
                self._build_user_prompt(user_text, assistant_text),
                EXTRACT_SYSTEM_PROMPT,
            )
            items = self._parse_result(raw)
            stored: list[dict[str, Any]] = []
            for item in items:
                mid, created = await self.manager.memorize(
                    user_id=user_id,
                    content=item["content"],
                    memory_type=item["type"],
                    tags=item["tags"],
                    importance=item["importance"],
                    session_id=session_id,
                    source="auto",
                )
                if created:
                    stored.append({**item, "id": mid})
            if stored:
                logger.info(
                    "自动记忆 %d 条 (user=%s): %s",
                    len(stored),
                    user_id,
                    [i["content"][:30] for i in stored],
                )
            return stored
        except Exception:
            logger.exception("自动记忆提取失败（不影响聊天）")
            return []
