"""记忆管理器：数据库之上的业务层（去重、校验、格式化）。"""

from __future__ import annotations

import logging
from typing import Any

from .database import VALID_TYPES, MemoryDatabase
from .utils import clamp

logger = logging.getLogger("memory_system.manager")

TYPE_LABELS = {
    "user_fact": "用户事实",
    "preference": "偏好",
    "project": "项目",
    "relationship": "关系",
    "event": "事件",
    "summary": "摘要",
}


class MemoryManager:
    """对上层提供记忆增删查改的业务接口。"""

    def __init__(self, db: MemoryDatabase) -> None:
        self.db = db

    async def memorize(
        self,
        user_id: str,
        content: str,
        memory_type: str = "user_fact",
        tags: list[str] | str | None = None,
        importance: float = 0.5,
        session_id: str = "",
        source: str = "auto",
    ) -> tuple[int, bool]:
        """写入一条记忆（内容去重、类型与重要度校验）。

        Returns:
            (memory_id, created)
        """
        content = (content or "").strip()
        if not content:
            raise ValueError("记忆内容不能为空")
        if memory_type not in VALID_TYPES:
            memory_type = "user_fact"
        importance = clamp(float(importance), 0.0, 1.0)
        return await self.db.add_memory(
            user_id=user_id,
            content=content[:2000],
            memory_type=memory_type,
            tags=tags,
            importance=importance,
            session_id=session_id,
            source=source if source in ("auto", "manual", "compressed") else "auto",
        )

    async def delete(self, memory_id: int) -> bool:
        return await self.db.delete_memory(int(memory_id))

    async def get(self, memory_id: int) -> dict[str, Any] | None:
        return await self.db.get(int(memory_id))

    async def list_page(
        self,
        user_id: str,
        page: int = 1,
        page_size: int = 10,
        session_id: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        return await self.db.list_memories(user_id, session_id, page, page_size)

    async def search_keyword(
        self, user_id: str, keyword: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        return await self.db.search(user_id, keyword=keyword, limit=limit)

    async def stats(self, user_id: str) -> dict[str, Any]:
        return await self.db.stats(user_id)

    async def clear(self, user_id: str, session_id: str | None = None) -> int:
        return await self.db.clear(user_id, session_id)

    async def record_access(self, memory_ids: list[int]) -> None:
        """批量记录访问，用于排序权重。"""
        for mid in memory_ids:
            await self.db.touch(mid)

    @staticmethod
    def format_for_llm(memories: list[dict[str, Any]]) -> str:
        """把记忆列表格式化为给 LLM 看的紧凑文本。"""
        if not memories:
            return ""
        lines: list[str] = []
        for m in memories:
            tags = ",".join(m.get("tags") or [])
            label = TYPE_LABELS.get(m.get("type", ""), m.get("type", ""))
            tag_part = f" | 标签: {tags}" if tags else ""
            lines.append(
                f"[#{m['id']} | {label} | 重要度 {m.get('importance', 0.5):.2f}] "
                f"{m.get('content', '')}{tag_part}"
            )
        return "\n".join(lines)

    @staticmethod
    def format_for_human(memories: list[dict[str, Any]]) -> str:
        """把记忆列表格式化为给用户看的文本。"""
        if not memories:
            return "（没有记忆）"
        lines: list[str] = []
        for m in memories:
            label = TYPE_LABELS.get(m.get("type", ""), m.get("type", ""))
            lines.append(
                f"#{m['id']} [{label}] {m.get('content', '')} "
                f"(重要度 {m.get('importance', 0.5):.2f})"
            )
        return "\n".join(lines)
