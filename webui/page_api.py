"""插件页（Dashboard Plugin Page）后端 API 逻辑层。

与 AstrBot 解耦：main.py 中的薄封装负责从 astrbot.api.web 的 request 代理
取 query/body 并包装 {status, data} 响应；本模块只做业务处理，可独立单测。
"""

from __future__ import annotations

from typing import Any

try:  # 作为插件包内的相对导入
    from ..core.database import MemoryDatabase
    from ..core.memory_manager import MemoryManager
except ImportError:  # 独立测试时按顶层包导入
    from core.database import MemoryDatabase
    from core.memory_manager import MemoryManager


class PageAPI:
    """插件页面板的后端接口实现。"""

    def __init__(self, db: MemoryDatabase, manager: MemoryManager) -> None:
        self.db = db
        self.manager = manager

    # -------------------------------------------------------------------- GET

    async def stats(self, query: dict[str, str]) -> dict[str, Any]:
        user_id = query.get("user_id") or None
        if user_id:
            return await self.manager.stats(user_id)
        rows = await self.db.export_all()
        users = sorted({r["user_id"] for r in rows})
        by_type: dict[str, int] = {}
        total_accesses = 0
        importance_sum = 0.0
        for r in rows:
            by_type[r["type"]] = by_type.get(r["type"], 0) + 1
            total_accesses += int(r.get("access_count", 0))
            importance_sum += float(r.get("importance", 0.5))
        total = len(rows)
        return {
            "total": total,
            "users": users,
            "by_type": by_type,
            "avg_importance": round(importance_sum / total, 3) if total else 0.0,
            "total_accesses": total_accesses,
        }

    async def list_memories(self, query: dict[str, str]) -> dict[str, Any]:
        page = max(1, int(query.get("page") or 1))
        page_size = min(100, max(1, int(query.get("page_size") or 20)))
        user_id = query.get("user_id") or None
        if user_id:
            memories, total = await self.manager.list_page(
                user_id, page=page, page_size=page_size
            )
        else:
            rows = await self.db.export_all()
            rows.sort(key=lambda r: r.get("created_at", 0), reverse=True)
            total = len(rows)
            memories = rows[(page - 1) * page_size : page * page_size]
        return {
            "memories": memories,
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    async def search(self, query: dict[str, str]) -> list[dict[str, Any]]:
        keyword = (query.get("keyword") or "").strip()
        if not keyword:
            return []
        user_id = query.get("user_id") or None
        if user_id:
            return await self.manager.search_keyword(user_id, keyword, limit=50)
        return await self.db.search_all_users(keyword, limit=50)

    async def users(self) -> list[dict[str, Any]]:
        rows = await self.db.export_all()
        users: dict[str, int] = {}
        for r in rows:
            users[r["user_id"]] = users.get(r["user_id"], 0) + 1
        return [
            {"user_id": u, "count": c}
            for u, c in sorted(users.items(), key=lambda x: -x[1])
        ]

    async def export(self, query: dict[str, str]) -> list[dict[str, Any]]:
        return await self.db.export_all(query.get("user_id") or None)

    # ------------------------------------------------------------------- POST

    async def add(self, body: dict[str, Any]) -> dict[str, Any]:
        user_id = str(body.get("user_id", "")).strip()
        content = str(body.get("content", "")).strip()
        if not user_id or not content:
            raise ValueError("user_id 和 content 不能为空")
        mid, created = await self.manager.memorize(
            user_id=user_id,
            content=content,
            memory_type=str(body.get("type", "user_fact")),
            tags=body.get("tags") or [],
            importance=float(body.get("importance", 0.8)),
            session_id=str(body.get("session_id", "")),
            source="manual",
        )
        return {"id": mid}

    async def delete(self, body: dict[str, Any]) -> dict[str, Any]:
        memory_id = int(body.get("id", 0))
        deleted = await self.manager.delete(memory_id)
        return {"deleted": deleted}

    async def clear(self, body: dict[str, Any]) -> dict[str, Any]:
        if not body.get("confirm"):
            raise ValueError("缺少 confirm 确认")
        user_id = str(body.get("user_id", "")).strip()
        count = (
            await self.manager.clear(user_id)
            if user_id
            else await self.manager.clear_all()
        )
        return {"deleted": count}
