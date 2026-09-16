"""相关记忆检索器：多因子打分排序（关键词重叠、子串命中、重要度、时效、访问热度）。

embedding 向量检索默认关闭；开启后若平台可用 embedding 提供商，
可在 adapter 层提供向量并传入 `attach_vectors` 增强排序（TODO 标注）。
"""

from __future__ import annotations

import logging
import math
import time
from typing import Any

from .database import MemoryDatabase
from .memory_manager import MemoryManager
from .utils import extract_keywords

logger = logging.getLogger("memory_system.retriever")


class MemoryRetriever:
    """根据当前消息检索最相关的长期记忆。"""

    def __init__(
        self,
        db: MemoryDatabase,
        manager: MemoryManager,
        candidate_pool: int = 300,
        recency_half_life_days: float = 14.0,
    ) -> None:
        self.db = db
        self.manager = manager
        self._candidate_pool = max(50, candidate_pool)
        self._half_life = max(0.5, recency_half_life_days) * 86400.0

    # ------------------------------------------------------------------ scoring

    @staticmethod
    def _mem_keywords(memory: dict[str, Any]) -> set[str]:
        content = memory.get("content", "") or ""
        keys = set(extract_keywords(content))
        for tag in memory.get("tags") or []:
            keys.update(extract_keywords(str(tag)))
        return keys

    def score(self, memory: dict[str, Any], query_keys: set[str]) -> float:
        """对单条记忆打分。分数范围约 0~1.2。"""
        if not query_keys:
            return 0.0
        mem_keys = self._mem_keywords(memory)
        if not mem_keys:
            return 0.0
        overlap = len(query_keys & mem_keys) / len(query_keys)
        content = memory.get("content", "") or ""
        # 子串直接命中给强加成
        substring_hit = any(len(k) >= 2 and k in content for k in query_keys)
        importance = float(memory.get("importance", 0.5))
        # 时效衰减（重要度高的记忆衰减更慢）
        age = max(0.0, time.time() - float(memory.get("created_at", 0)))
        decay = 0.5 ** (age / (self._half_life * max(0.25, importance)))
        recency_bonus = 0.15 * (1.0 - decay)
        access_bonus = min(float(memory.get("access_count", 0)), 10.0) * 0.005
        score = 0.6 * overlap + (0.4 if substring_hit else 0.0) * (
            0.5 + 0.5 * overlap
        )
        score += 0.35 * importance + recency_bonus + access_bonus
        return score

    # ----------------------------------------------------------------- retrieve

    async def retrieve(
        self,
        user_id: str,
        query: str,
        top_k: int = 5,
        min_importance: float = 0.0,
        min_score: float = 0.12,
    ) -> list[dict[str, Any]]:
        """检索与 query 最相关的 top_k 条记忆，并记录访问。"""
        top_k = max(1, top_k)
        query_keys = set(extract_keywords(query or ""))
        candidates = await self.db.recent(user_id, limit=self._candidate_pool)
        if not candidates:
            return []
        scored: list[tuple[float, dict[str, Any]]] = []
        for m in candidates:
            if float(m.get("importance", 0)) < min_importance:
                continue
            s = self.score(m, query_keys)
            if s >= min_score:
                scored.append((s, m))
        if not scored:
            return []
        scored.sort(key=lambda pair: pair[0], reverse=True)
        top = [m for _, m in scored[:top_k]]
        # 忽略访问记录失败（best effort）
        await self.manager.record_access([int(m["id"]) for m in top])
        return top

    # ----------------------------------------------------- embedding (optional)

    async def attach_vectors(
        self, memories: list[dict[str, Any]], query_vector: list[float]
    ) -> list[dict[str, Any]]:
        """（可选）给候选记忆附加向量相似度并混合排序。

        TODO: 需要确认所用 AstrBot 版本的 EmbeddingProvider 接口
        （如 `await provider.get_embedding(text)` 的确切方法名），
        确认后在 adapter.py 中实现向量获取并调用本方法。
        """
        return memories
