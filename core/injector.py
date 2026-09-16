"""上下文注入器：把检索到的记忆组装成"记忆包"写入 LLM 请求。"""

from __future__ import annotations

import logging
from typing import Any

from .database import MemoryDatabase
from .memory_manager import MemoryManager
from .prompts import memory_pack_template
from .retriever import MemoryRetriever
from .utils import estimate_tokens

logger = logging.getLogger("memory_system.injector")


def _truncate_to_tokens(text: str, token_budget: int) -> str:
    """按字符近似截断文本到 token 预算以内（1 token ≈ 1.6 字符，留安全余量）。"""
    if not text:
        return ""
    max_chars = max(20, int(token_budget * 1.4))
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n…（已截断）"


def _fit_section(section: str, token_budget: int) -> str:
    """把一个多行段落裁剪到 token 预算内：按行从末尾丢弃，不足一行时截断。"""
    if not section or token_budget <= 0:
        return ""
    if estimate_tokens(section) <= token_budget:
        return section
    lines = section.splitlines()
    while lines and estimate_tokens("\n".join(lines)) > token_budget:
        lines.pop()
    joined = "\n".join(lines)
    if estimate_tokens(joined) > token_budget:  # 单行超预算，硬截断
        joined = _truncate_to_tokens(joined, token_budget)
    return joined


class MemoryInjector:
    """在 LLM 请求前组装并注入分层记忆包：

    【当前用户消息】（由主流程负责，本模块不动）
    【用户画像】+【相关长期记忆】+【最新摘要】→ 追加到 system_prompt
    """

    def __init__(
        self,
        db: MemoryDatabase,
        manager: MemoryManager,
        retriever: MemoryRetriever,
        max_inject_memories: int = 5,
        max_inject_tokens: int = 600,
        profile_limit: int = 8,
    ) -> None:
        self.db = db
        self.manager = manager
        self.retriever = retriever
        self._max_memories = max(1, max_inject_memories)
        self._max_tokens = max(100, max_inject_tokens)
        self._profile_limit = max(0, profile_limit)

    async def build_pack(self, user_id: str, query: str) -> str:
        """构建记忆包文本；无内容时返回空字符串。

        预算分配（按 max_inject_tokens）：
        - 相关记忆 50%（不足则剩余让给画像/摘要）
        - 用户画像 30%
        - 最新摘要 20%
        各段超出预算时从末尾丢弃条目，最终仍超限则整包硬截断。
        """
        try:
            budget = self._max_tokens

            # 1) 用户画像
            profile_section = ""
            if self._profile_limit > 0:
                profile = await self.db.get_profile(user_id, limit=self._profile_limit)
                profile_section = self.manager.format_for_llm(profile)
            profile_budget = int(budget * 0.3)
            profile_section = _fit_section(profile_section, profile_budget)

            # 2) 相关记忆
            related_section = ""
            if query and query.strip():
                related = await self.retriever.retrieve(
                    user_id, query, top_k=self._max_memories
                )
                related_section = self.manager.format_for_llm(related)
            related_budget = int(budget * 0.5)
            related_section = _fit_section(related_section, related_budget)

            # 3) 最新摘要
            summary_section = ""
            summary = await self.db.get_latest_summary(user_id)
            if summary:
                summary_section = summary.get("content", "")
            used = estimate_tokens(profile_section) + estimate_tokens(related_section)
            summary_budget = max(0, budget - used)
            summary_section = _fit_section(summary_section, summary_budget)

            if not (profile_section or related_section or summary_section):
                return ""
            pack = memory_pack_template(profile_section, related_section, summary_section)
            if estimate_tokens(pack) > budget:  # 整包兜底硬截断
                pack = _truncate_to_tokens(pack, budget)
            return pack
        except Exception:
            logger.exception("构建记忆包失败（跳过注入）")
            return ""

    async def inject(self, system_prompt: str, user_id: str, query: str) -> str:
        """返回追加了记忆包与使用规则的 system_prompt。"""
        from .prompts import CORE_PERSONA_PROMPT, TOOL_USAGE_PROMPT

        parts: list[str] = [system_prompt or ""]
        pack = await self.build_pack(user_id, query)
        if pack:
            parts.append(pack)
        persona = CORE_PERSONA_PROMPT
        tool_hint = TOOL_USAGE_PROMPT
        merged = "\n\n".join(p for p in (persona, tool_hint, *parts[1:]) if p)
        return (parts[0] + "\n\n" + merged).strip() if parts[0] else merged
