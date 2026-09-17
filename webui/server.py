"""WebUI 服务：为插件提供独立的可视化面板（aiohttp，AstrBot 自带依赖）。

- 端口 / 开关 / 密码均可在 WebUI 配置中调整；
- 密码为空时免登录（仅建议内网使用），设置了密码则需 Bearer Token；
- 所有数据库操作复用 core 层，接口与 /记忆 命令同源。
"""

from __future__ import annotations

from typing import Any

import hmac
import logging
import secrets

from aiohttp import web

try:  # 作为插件包内的相对导入
    from ..core.database import MemoryDatabase
    from ..core.memory_manager import MemoryManager
except ImportError:  # 独立测试时按顶层包导入
    from core.database import MemoryDatabase
    from core.memory_manager import MemoryManager

logger = logging.getLogger("memory_system.webui")

AUTH_exempt_paths = {"/", "/index.html", "/api/login"}


def _ok(data: Any = None, message: str = "ok") -> web.Response:
    return web.json_response({"status": "ok", "message": message, "data": data})


def _err(message: str, status: int = 400) -> web.Response:
    return web.json_response(
        {"status": "error", "message": message, "data": None}, status=status
    )


class MemoryWebUI:
    """长期记忆可视化面板服务。"""

    def __init__(
        self,
        db: MemoryDatabase,
        manager: MemoryManager,
        host: str = "0.0.0.0",
        port: int = 6198,
        password: str = "",
    ) -> None:
        self.db = db
        self.manager = manager
        self.host = host
        self.port = int(port)
        self.password = password
        self._token: str = secrets.token_hex(16)
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    # ------------------------------------------------------------------ middle

    def _authorized(self, request: web.Request) -> bool:
        if not self.password:  # 未设置密码 = 免登录
            return True
        if request.path in AUTH_exempt_paths:
            return True
        auth = request.headers.get("Authorization", "")
        return hmac.compare_digest(auth, f"Bearer {self._token}")

    async def _guard(self, request: web.Request, handler):
        if not self._authorized(request):
            return _err("未登录或令牌无效", 401)
        try:
            return await handler(request)
        except web.HTTPException:
            raise
        except Exception as e:  # 统一兜底，面板永不 500 裸奔
            logger.exception("[记忆系统] WebUI 接口异常: %s", request.path)
            return _err(f"服务器内部错误: {e}", 500)

    # -------------------------------------------------------------------- api

    async def _api_login(self, request: web.Request) -> web.Response:
        body = await request.json() if request.can_read_body else {}
        password = str(body.get("password", ""))
        if not self.password:
            return _ok({"token": self._token})
        if hmac.compare_digest(password, self.password):
            return _ok({"token": self._token})
        return _err("密码错误", 403)

    async def _api_stats(self, request: web.Request) -> web.Response:
        user_id = request.query.get("user_id") or None
        if user_id:
            return _ok(await self.manager.stats(user_id))
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
        return _ok(
            {
                "total": total,
                "users": users,
                "by_type": by_type,
                "avg_importance": round(importance_sum / total, 3) if total else 0.0,
                "total_accesses": total_accesses,
            }
        )

    async def _api_list(self, request: web.Request) -> web.Response:
        page = max(1, int(request.query.get("page", "1") or 1))
        page_size = min(100, max(1, int(request.query.get("page_size", "20") or 20)))
        user_id = request.query.get("user_id") or None
        if user_id:
            memories, total = await self.manager.list_page(user_id, page=page, page_size=page_size)
        else:
            rows = await self.db.export_all()
            rows.sort(key=lambda r: r.get("created_at", 0), reverse=True)
            total = len(rows)
            memories = rows[(page - 1) * page_size : page * page_size]
        return _ok({"memories": memories, "total": total, "page": page, "page_size": page_size})

    async def _api_search(self, request: web.Request) -> web.Response:
        keyword = (request.query.get("keyword") or "").strip()
        user_id = request.query.get("user_id") or None
        if not keyword:
            return _ok([])
        if user_id:
            result = await self.manager.search_keyword(user_id, keyword, limit=50)
        else:
            rows = await self.db.search_all_users(keyword, limit=50)
            result = rows
        return _ok(result)

    async def _api_add(self, request: web.Request) -> web.Response:
        body = await request.json()
        user_id = str(body.get("user_id", "")).strip()
        content = str(body.get("content", "")).strip()
        if not user_id or not content:
            return _err("user_id 和 content 不能为空")
        mid, created = await self.manager.memorize(
            user_id=user_id,
            content=content,
            memory_type=str(body.get("type", "user_fact")),
            tags=body.get("tags") or [],
            importance=float(body.get("importance", 0.8)),
            session_id=str(body.get("session_id", "")),
            source="manual",
        )
        return _ok({"id": mid}, "已更新" if not created else "已添加")

    async def _api_delete(self, request: web.Request) -> web.Response:
        body = await request.json()
        memory_id = int(body.get("id", 0))
        deleted = await self.manager.delete(memory_id)
        return _ok({"deleted": deleted}, "已删除" if deleted else "记忆不存在")

    async def _api_clear(self, request: web.Request) -> web.Response:
        body = await request.json()
        if not body.get("confirm"):
            return _err("缺少 confirm 确认")
        user_id = str(body.get("user_id", "")).strip()
        count = await self.manager.clear(user_id) if user_id else await self.manager.clear_all()
        return _ok({"deleted": count}, f"已清空 {count} 条")

    async def _api_export(self, request: web.Request) -> web.Response:
        user_id = request.query.get("user_id") or None
        records = await self.db.export_all(user_id)
        return web.json_response(records)

    async def _api_users(self, request: web.Request) -> web.Response:
        rows = await self.db.export_all()
        users: dict[str, int] = {}
        for r in rows:
            users[r["user_id"]] = users.get(r["user_id"], 0) + 1
        return _ok([{"user_id": u, "count": c} for u, c in sorted(users.items(), key=lambda x: -x[1])])

    # ----------------------------------------------------------------- static

    async def _index(self, request: web.Request) -> web.Response:
        import os

        static_dir = os.path.join(os.path.dirname(__file__))
        return web.FileResponse(os.path.join(static_dir, "index.html"))

    # ------------------------------------------------------------------- boot

    def _build_app(self) -> web.Application:
        app = web.Application(client_max_size=8 * 1024 * 1024)
        app.router.add_get("/", self._index)
        app.router.add_post("/api/login", lambda r: self._guard(r, self._api_login))
        app.router.add_get("/api/stats", lambda r: self._guard(r, self._api_stats))
        app.router.add_get("/api/list", lambda r: self._guard(r, self._api_list))
        app.router.add_get("/api/search", lambda r: self._guard(r, self._api_search))
        app.router.add_get("/api/users", lambda r: self._guard(r, self._api_users))
        app.router.add_get("/api/export", lambda r: self._guard(r, self._api_export))
        app.router.add_post("/api/add", lambda r: self._guard(r, self._api_add))
        app.router.add_post("/api/delete", lambda r: self._guard(r, self._api_delete))
        app.router.add_post("/api/clear", lambda r: self._guard(r, self._api_clear))
        return app

    async def start(self) -> None:
        self._runner = web.AppRunner(self._build_app(), access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()
        logger.info("[记忆系统] WebUI 已启动: http://%s:%d", self.host, self.port)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            self._site = None
            logger.info("[记忆系统] WebUI 已停止")
