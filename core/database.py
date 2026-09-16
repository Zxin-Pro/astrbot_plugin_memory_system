"""SQLite 持久化层（aiosqlite 优先，缺依赖时自动降级为 sqlite3 + 线程池）。

设计要点：
- 所有 SQL 均使用参数化查询，杜绝 SQL 注入；
- 单连接 + asyncio.Lock 串行化写入，SQLite 单写场景足够安全；
- 行数据统一转为 dict 返回，与驱动解耦；
- 多用户/多会话通过 user_id / session_id 字段隔离，索引加速。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from typing import Any, Iterable

from .utils import now_ts, parse_tags

logger = logging.getLogger("memory_system.database")

VALID_TYPES = (
    "user_fact",
    "preference",
    "project",
    "relationship",
    "event",
    "summary",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       TEXT NOT NULL,
    session_id    TEXT NOT NULL DEFAULT '',
    content       TEXT NOT NULL,
    type          TEXT NOT NULL DEFAULT 'user_fact',
    tags          TEXT NOT NULL DEFAULT '[]',
    importance    REAL NOT NULL DEFAULT 0.5,
    source        TEXT NOT NULL DEFAULT 'auto',
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL,
    last_accessed REAL NOT NULL,
    access_count  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_mem_user ON memories(user_id);
CREATE INDEX IF NOT EXISTS idx_mem_session ON memories(session_id);
CREATE INDEX IF NOT EXISTS idx_mem_user_type ON memories(user_id, type);
CREATE INDEX IF NOT EXISTS idx_mem_user_time ON memories(user_id, created_at DESC);
"""


class _CursorShim:
    """用 sqlite3 模拟 aiosqlite Cursor 的最小接口。"""

    __slots__ = ("_cur",)

    def __init__(self, cur: sqlite3.Cursor) -> None:
        self._cur = cur

    @property
    def lastrowid(self) -> int | None:
        return self._cur.lastrowid

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount

    async def fetchall(self) -> list[Any]:
        return self._cur.fetchall()

    async def fetchone(self) -> Any | None:
        return self._cur.fetchone()


class _ConnShim:
    """用 sqlite3 + asyncio.to_thread 模拟 aiosqlite Connection 的最小接口。

    仅当环境缺少 aiosqlite 时使用（requirements.txt 已声明 aiosqlite，
    正常情况下不会走到这里）。
    """

    def __init__(self, path: str) -> None:
        self._conn: sqlite3.Connection = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = asyncio.Lock()

    async def _run(self, fn, *args: Any) -> Any:
        async with self._lock:
            return await asyncio.to_thread(fn, *args)

    async def execute(self, sql: str, params: Iterable[Any] = ()) -> _CursorShim:
        cur = await self._run(self._conn.execute, sql, tuple(params))
        return _CursorShim(cur)

    async def commit(self) -> None:
        await self._run(self._conn.commit)

    async def close(self) -> None:
        await self._run(self._conn.close)


class MemoryDatabase:
    """长期记忆数据库封装。所有方法均为异步、非阻塞。"""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn: Any | None = None
        self._lock = asyncio.Lock()
        self._driver = "aiosqlite"

    # ------------------------------------------------------------------ setup

    async def initialize(self) -> None:
        """打开连接、建表建索引。可重复调用（幂等）。"""
        if self._conn is not None:
            return
        directory = os.path.dirname(self._db_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        try:
            import aiosqlite  # type: ignore

            self._conn = await aiosqlite.connect(self._db_path)
            self._conn.row_factory = sqlite3.Row  # 行数据支持按列名访问
            self._driver = "aiosqlite"
        except ImportError:
            logger.warning("aiosqlite 未安装，降级使用 sqlite3 + 线程池")
            self._conn = _ConnShim(self._db_path)
            self._driver = "sqlite3-thread"
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._conn.executescript(_SCHEMA)
        await self._conn.commit()
        logger.info("记忆数据库已就绪 (%s): %s", self._driver, self._db_path)

    async def close(self) -> None:
        if self._conn is not None:
            try:
                await self._conn.close()
            finally:
                self._conn = None

    # ----------------------------------------------------------------- helpers

    @staticmethod
    def _to_dict(row: Any) -> dict[str, Any] | None:
        if row is None:
            return None
        data = dict(row)
        data["tags"] = parse_tags(data.get("tags", "[]"))
        return data

    async def _fetchall(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        cur = await self._conn.execute(sql, params)
        rows = await cur.fetchall()
        return [d for d in (self._to_dict(r) for r in rows) if d]

    async def _fetchone(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        cur = await self._conn.execute(sql, params)
        return self._to_dict(await cur.fetchone())

    # --------------------------------------------------------------- write ops

    async def add_memory(
        self,
        user_id: str,
        content: str,
        memory_type: str = "user_fact",
        tags: list[str] | str | None = None,
        importance: float = 0.5,
        session_id: str = "",
        source: str = "auto",
    ) -> tuple[int, bool]:
        """新增记忆；同一用户下内容完全重复时改为更新旧记录。

        Returns:
            (memory_id, created) —— created=False 表示命中重复、已更新。
        """
        memory_type = memory_type if memory_type in VALID_TYPES else "user_fact"
        tag_list = parse_tags(tags)
        now = now_ts()
        async with self._lock:
            cur = await self._conn.execute(
                "SELECT id FROM memories WHERE user_id = ? AND content = ? LIMIT 1",
                (user_id, content),
            )
            existing = await cur.fetchone()
            if existing is not None:
                mid = existing["id"] if isinstance(existing, sqlite3.Row) else existing[0]
                await self._conn.execute(
                    "UPDATE memories SET tags = ?, importance = ?, updated_at = ?, "
                    "session_id = ?, source = ? WHERE id = ?",
                    (
                        json.dumps(tag_list, ensure_ascii=False),
                        float(importance),
                        now,
                        session_id,
                        source,
                        mid,
                    ),
                )
                await self._conn.commit()
                return int(mid), False
            cur = await self._conn.execute(
                "INSERT INTO memories (user_id, session_id, content, type, tags, "
                "importance, source, created_at, updated_at, last_accessed, access_count) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
                (
                    user_id,
                    session_id,
                    content,
                    memory_type,
                    json.dumps(tag_list, ensure_ascii=False),
                    float(importance),
                    source,
                    now,
                    now,
                    now,
                ),
            )
            await self._conn.commit()
            return int(cur.lastrowid), True

    async def touch(self, memory_id: int) -> None:
        """记录一次访问（access_count + 1，刷新 last_accessed）。"""
        try:
            await self._conn.execute(
                "UPDATE memories SET access_count = access_count + 1, "
                "last_accessed = ? WHERE id = ?",
                (now_ts(), memory_id),
            )
            await self._conn.commit()
        except Exception:  # 访问统计失败不影响主流程
            logger.debug("touch 记忆 %s 失败", memory_id, exc_info=True)

    async def update_importance(self, memory_id: int, importance: float) -> bool:
        cur = await self._conn.execute(
            "UPDATE memories SET importance = ?, updated_at = ? WHERE id = ?",
            (float(importance), now_ts(), memory_id),
        )
        await self._conn.commit()
        return cur.rowcount > 0

    async def delete_memory(self, memory_id: int) -> bool:
        """删除单条记忆，返回是否实际删除。"""
        async with self._lock:
            cur = await self._conn.execute(
                "DELETE FROM memories WHERE id = ?", (memory_id,)
            )
            await self._conn.commit()
            return cur.rowcount > 0

    async def clear(self, user_id: str, session_id: str | None = None) -> int:
        """清空某用户（可限定会话）的所有记忆，返回删除条数。"""
        async with self._lock:
            if session_id:
                cur = await self._conn.execute(
                    "DELETE FROM memories WHERE user_id = ? AND session_id = ?",
                    (user_id, session_id),
                )
            else:
                cur = await self._conn.execute(
                    "DELETE FROM memories WHERE user_id = ?", (user_id,)
                )
            await self._conn.commit()
            return cur.rowcount

    async def import_memories(self, records: list[dict[str, Any]]) -> int:
        """批量导入记忆（导出文件格式），返回导入条数。"""
        count = 0
        for rec in records:
            try:
                await self.add_memory(
                    user_id=str(rec.get("user_id", "")),
                    content=str(rec.get("content", "")),
                    memory_type=str(rec.get("type", "user_fact")),
                    tags=parse_tags(rec.get("tags")),
                    importance=float(rec.get("importance", 0.5)),
                    session_id=str(rec.get("session_id", "")),
                    source="manual",
                )
                count += 1
            except (TypeError, ValueError):
                logger.warning("导入时跳过非法记录: %r", rec)
        return count

    # ---------------------------------------------------------------- read ops

    async def get(self, memory_id: int) -> dict[str, Any] | None:
        return await self._fetchone("SELECT * FROM memories WHERE id = ?", (memory_id,))

    async def find_exact(self, user_id: str, content: str) -> dict[str, Any] | None:
        return await self._fetchone(
            "SELECT * FROM memories WHERE user_id = ? AND content = ? LIMIT 1",
            (user_id, content),
        )

    async def list_memories(
        self,
        user_id: str,
        session_id: str | None = None,
        page: int = 1,
        page_size: int = 10,
    ) -> tuple[list[dict[str, Any]], int]:
        """分页列出记忆，返回 (记录, 总数)。"""
        page = max(1, page)
        where = "user_id = ?"
        params: list[Any] = [user_id]
        if session_id:
            where += " AND session_id = ?"
            params.append(session_id)
        total_row = await self._fetchone(
            f"SELECT COUNT(*) AS c FROM memories WHERE {where}", tuple(params)
        )
        total = int(total_row["c"]) if total_row else 0
        rows = await self._fetchall(
            f"SELECT * FROM memories WHERE {where} "
            "ORDER BY created_at DESC LIMIT ? OFFSET ?",
            tuple(params + [page_size, (page - 1) * page_size]),
        )
        return rows, total

    async def search(
        self,
        user_id: str,
        keyword: str = "",
        memory_type: str | None = None,
        tags: list[str] | None = None,
        min_importance: float = 0.0,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """按条件检索记忆（SQL 层过滤，供 retriever 粗筛与命令搜索使用）。"""
        where = "user_id = ? AND importance >= ?"
        params: list[Any] = [user_id, float(min_importance)]
        if keyword:
            where += " AND content LIKE ?"
            params.append(f"%{keyword}%")
        if memory_type:
            where += " AND type = ?"
            params.append(memory_type)
        if tags:
            for tag in tags:
                where += " AND tags LIKE ?"
                params.append(f'%"{tag}"%')
        params.append(int(limit))
        return await self._fetchall(
            f"SELECT * FROM memories WHERE {where} "
            "ORDER BY importance DESC, created_at DESC LIMIT ?",
            tuple(params),
        )

    async def recent(self, user_id: str, limit: int = 300) -> list[dict[str, Any]]:
        """取最近 N 条记忆（供检索器打分粗筛）。"""
        return await self._fetchall(
            "SELECT * FROM memories WHERE user_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (user_id, int(limit)),
        )

    async def get_profile(self, user_id: str, limit: int = 8) -> list[dict[str, Any]]:
        """取用户画像：重要度最高的 user_fact / preference。"""
        return await self._fetchall(
            "SELECT * FROM memories WHERE user_id = ? AND type IN ('user_fact', 'preference') "
            "ORDER BY importance DESC, updated_at DESC LIMIT ?",
            (user_id, int(limit)),
        )

    async def get_latest_summary(self, user_id: str) -> dict[str, Any] | None:
        return await self._fetchone(
            "SELECT * FROM memories WHERE user_id = ? AND type = 'summary' "
            "ORDER BY created_at DESC LIMIT 1",
            (user_id,),
        )

    async def stats(self, user_id: str) -> dict[str, Any]:
        """统计某用户的记忆数据。"""
        rows = await self._fetchall(
            "SELECT type, COUNT(*) AS c FROM memories WHERE user_id = ? GROUP BY type",
            (user_id,),
        )
        by_type = {r["type"]: r["c"] for r in rows}
        extra = await self._fetchone(
            "SELECT COUNT(*) AS total, IFNULL(AVG(importance), 0) AS avg_importance, "
            "IFNULL(SUM(access_count), 0) AS accesses FROM memories WHERE user_id = ?",
            (user_id,),
        )
        return {
            "total": int(extra["total"]) if extra else 0,
            "by_type": by_type,
            "avg_importance": round(float(extra["avg_importance"]), 3) if extra else 0.0,
            "total_accesses": int(extra["accesses"]) if extra else 0,
        }

    async def export_all(self, user_id: str | None = None) -> list[dict[str, Any]]:
        """导出全部（或指定用户的）记忆为可导入的记录列表。"""
        if user_id:
            return await self._fetchall(
                "SELECT * FROM memories WHERE user_id = ? ORDER BY created_at",
                (user_id,),
            )
        return await self._fetchall("SELECT * FROM memories ORDER BY created_at")
