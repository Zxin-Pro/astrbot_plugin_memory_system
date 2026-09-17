"""astrbot_plugin_memory_system —— AstrBot 长期记忆系统插件。

功能：
- 长期记忆存储（SQLite，多用户/多会话隔离）
- 对话后自动记忆提取（LLM 结构化提取）
- LLM 请求前相关记忆检索 + 记忆包注入
- 对话历史超过阈值时自动压缩为摘要
- 三个 LLM 工具：recall / memorize / forget
- 中文管理命令：/记忆 帮助|列表|搜索|添加|删除|清空|统计|导出|导入|压缩

架构：main.py 只做事件编排，业务逻辑在 core/（不依赖 AstrBot API），
平台交互全部隔离在 adapter.py。
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import MessageChain, filter
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import File, Plain
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, register

from .adapter import AstrBotAdapter, sanitize_filename
from .core.compressor import ContextCompressor
from .core.database import MemoryDatabase
from .core.extractor import MemoryExtractor
from .core.injector import MemoryInjector
from .core.memory_manager import MemoryManager
from .core.retriever import MemoryRetriever
from .webui.page_api import PageAPI
from .webui.server import MemoryWebUI

try:  # 插件页桥接（AstrBot v4.28+ 提供官方 Plugin Pages 机制）
    from astrbot.api.web import error_response, json_response, request

    _WEB_API_AVAILABLE = True
except ImportError:  # 老版本降级：仅保留独立 WebUI
    _WEB_API_AVAILABLE = False

AUTHOR = "Zxin-Pro"
PLUGIN_NAME = "astrbot_plugin_memory_system"


@register(
    name=PLUGIN_NAME,
    author=AUTHOR,
    desc="为机器人提供长期记忆：自动提取、相关检索、上下文注入、历史压缩与 LLM 记忆工具",
    version="1.1.0",
    repo="https://github.com/Zxin-Pro/astrbot_plugin_memory_system",
)
class MemorySystemPlugin(Star):
    """长期记忆系统插件主类。"""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.context = context
        self.config = config
        self.adapter = AstrBotAdapter(context, dict(config))
        self.db: MemoryDatabase | None = None
        self.manager: MemoryManager | None = None
        self.retriever: MemoryRetriever | None = None
        self.extractor: MemoryExtractor | None = None
        self.injector: MemoryInjector | None = None
        self.compressor: ContextCompressor | None = None
        self.webui: MemoryWebUI | None = None
        self.page_api: PageAPI | None = None
        self._bg_tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ 生命周期

    async def initialize(self) -> None:
        """插件加载时初始化组件（AstrBot 会自动调用本方法）。"""
        db_path = str(self.config.get("db_path", "") or "").strip()
        if not db_path:
            db_path = os.path.join(
                self.adapter.get_data_dir(), "memory_system.db"
            )
        self.db = MemoryDatabase(db_path)
        await self.db.initialize()
        self.manager = MemoryManager(self.db)
        self.retriever = MemoryRetriever(
            self.db,
            self.manager,
            candidate_pool=int(self.config.get("candidate_pool", 300)),
        )
        llm_caller = self.adapter.make_llm_caller()
        self.extractor = MemoryExtractor(
            self.manager,
            llm_caller,
            importance_threshold=float(self.config.get("importance_threshold", 0.5)),
        )
        self.injector = MemoryInjector(
            self.db,
            self.manager,
            self.retriever,
            max_inject_memories=int(self.config.get("max_inject_memories", 5)),
            max_inject_tokens=int(self.config.get("max_inject_tokens", 600)),
        )
        self.compressor = ContextCompressor(
            self.manager,
            llm_caller,
            compress_threshold_tokens=int(
                self.config.get("compress_threshold_tokens", 3000)
            ),
            keep_recent_messages=int(self.config.get("keep_recent_messages", 8)),
        )
        logger.info("[记忆系统] 初始化完成，数据库: %s", db_path)

        self.page_api = PageAPI(self.db, self.manager)
        if _WEB_API_AVAILABLE:
            self._register_page_apis()
            logger.info(
                "[记忆系统] 插件页已注册，请在 AstrBot 面板「插件 → 长期记忆系统」中打开 WebUI"
            )
        elif self.config.get("webui_enable", False):
            logger.warning("[记忆系统] 当前 AstrBot 版本不支持插件页 Web API，回退到独立 WebUI")

        # 独立 WebUI（可选的外部访问入口，默认关闭；面板内按钮走官方插件页）
        if self.config.get("webui_enable", False):
            self.webui = MemoryWebUI(
                self.db,
                self.manager,
                host=str(self.config.get("webui_host", "0.0.0.0")),
                port=int(self.config.get("webui_port", 6198)),
                password=str(self.config.get("webui_password", "") or ""),
            )
            try:
                await self.webui.start()
            except OSError as e:
                logger.error("[记忆系统] 独立 WebUI 启动失败（端口占用？）: %s", e)
                self.webui = None

    # --------------------------------------------------------- 插件页 Web API

    def _register_page_apis(self) -> None:
        """注册 Dashboard 插件页使用的 Web API（路由必须带插件名前缀）。"""
        reg = self.context.register_web_api
        prefix = f"/{PLUGIN_NAME}"
        reg(f"{prefix}/stats", self._page_stats, ["GET"], "记忆统计")
        reg(f"{prefix}/list", self._page_list, ["GET"], "记忆列表")
        reg(f"{prefix}/search", self._page_search, ["GET"], "搜索记忆")
        reg(f"{prefix}/users", self._page_users, ["GET"], "用户列表")
        reg(f"{prefix}/export", self._page_export, ["GET"], "导出记忆")
        reg(f"{prefix}/add", self._page_add, ["POST"], "添加记忆")
        reg(f"{prefix}/delete", self._page_delete, ["POST"], "删除记忆")
        reg(f"{prefix}/clear", self._page_clear, ["POST"], "清空记忆")

    @staticmethod
    def _query_dict() -> dict[str, str]:
        return {k: request.query.get(k) or "" for k in request.query.keys()}

    @staticmethod
    def _ok(data: Any) -> Any:
        return json_response({"status": "ok", "data": data})

    @staticmethod
    def _handle_error(e: Exception) -> Any:
        if isinstance(e, ValueError):
            return error_response(str(e))
        logger.exception("[记忆系统] 插件页接口异常")
        return error_response(f"服务器内部错误: {e}", 500)

    async def _page_stats(self):
        try:
            return self._ok(await self.page_api.stats(self._query_dict()))
        except Exception as e:
            return self._handle_error(e)

    async def _page_list(self):
        try:
            return self._ok(await self.page_api.list_memories(self._query_dict()))
        except Exception as e:
            return self._handle_error(e)

    async def _page_search(self):
        try:
            return self._ok(await self.page_api.search(self._query_dict()))
        except Exception as e:
            return self._handle_error(e)

    async def _page_users(self):
        try:
            return self._ok(await self.page_api.users())
        except Exception as e:
            return self._handle_error(e)

    async def _page_export(self):
        try:
            return self._ok(await self.page_api.export(self._query_dict()))
        except Exception as e:
            return self._handle_error(e)

    async def _page_add(self):
        try:
            body = await request.json(default={}) or {}
            return self._ok(await self.page_api.add(body))
        except Exception as e:
            return self._handle_error(e)

    async def _page_delete(self):
        try:
            body = await request.json(default={}) or {}
            return self._ok(await self.page_api.delete(body))
        except Exception as e:
            return self._handle_error(e)

    async def _page_clear(self):
        try:
            body = await request.json(default={}) or {}
            return self._ok(await self.page_api.clear(body))
        except Exception as e:
            return self._handle_error(e)

    async def terminate(self) -> None:
        """插件卸载时释放资源。"""
        for task in list(self._bg_tasks):
            task.cancel()
        self._bg_tasks.clear()
        if self.webui is not None:
            try:
                await self.webui.stop()
            except Exception:
                logger.exception("[记忆系统] WebUI 停止失败")
        if self.db is not None:
            await self.db.close()
        logger.info("[记忆系统] 已卸载")

    # ------------------------------------------------------------------ 内部工具

    def _enabled(self) -> bool:
        return bool(self.config.get("enable", True))

    def _spawn(self, coro) -> None:
        """创建后台任务并兜底记录异常，防止任务被 GC 或静默失败。"""
        task = asyncio.get_running_loop().create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        task.add_done_callback(
            lambda t: t.exception() and logger.error("[记忆系统] 后台任务异常: %r", t.exception())
        )

    # --------------------------------------------------------------- LLM 请求钩子

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        """LLM 请求前：压缩历史 + 注入记忆包。"""
        if not self._enabled() or self.injector is None:
            return
        try:
            user_id = self.adapter.get_user_id(event)
            session_id = self.adapter.get_session_id(event)
            self.adapter.bind_umo(session_id)
            query = str(getattr(req, "prompt", "") or event.message_str or "")
            if not query:
                return

            # 1) 上下文自动压缩
            contexts = list(getattr(req, "contexts", None) or [])
            if contexts and self.config.get("auto_compress", True):
                new_contexts, _summary = await self.compressor.compress(
                    user_id, session_id, contexts, force=False
                )
                if len(new_contexts) != len(contexts):
                    req.contexts = new_contexts

            # 2) 记忆包注入（追加到 system_prompt 末尾，不覆盖人格设定）
            if self.config.get("auto_recall", True):
                pack = await self.injector.build_pack(user_id, query)
                if pack:
                    req.system_prompt = f"{req.system_prompt}\n\n{pack}".strip()
                    logger.debug("[记忆系统] 已注入记忆包 (%d chars)", len(pack))
        except Exception:
            logger.exception("[记忆系统] on_llm_request 处理失败（不影响聊天）")

    # --------------------------------------------------------------- LLM 响应钩子

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse) -> None:
        """LLM 响应后：自动记忆提取（后台任务，不阻塞回复发送）。"""
        if not self._enabled() or self.extractor is None:
            return
        if not self.config.get("auto_memorize", True):
            return
        try:
            assistant_text = str(getattr(resp, "completion_text", "") or "").strip()
            user_text = str(event.message_str or "").strip()
            if not assistant_text or not user_text:
                return
            user_id = self.adapter.get_user_id(event)
            session_id = self.adapter.get_session_id(event)
            self._spawn(
                self.extractor.extract_and_store(user_id, session_id, user_text, assistant_text)
            )
        except Exception:
            logger.exception("[记忆系统] on_llm_response 处理失败（不影响聊天）")

    # ------------------------------------------------------------------ LLM 工具

    @filter.llm_tool(name="recall_long_term_memory")
    async def tool_recall(self, event: AstrMessageEvent, query: str) -> str:
        """检索长期记忆。当用户提及过去的事情，或你需要回忆相关背景时调用。

        Args:
            query(string): 要检索的记忆关键词或描述
        """
        if not self._enabled() or self.retriever is None:
            return "记忆系统未启用"
        try:
            user_id = self.adapter.get_user_id(event)
            memories = await self.retriever.retrieve(
                user_id, query, top_k=int(self.config.get("max_inject_memories", 5))
            )
            if not memories:
                return "（没有找到与该主题相关的长期记忆）"
            return self.manager.format_for_llm(memories)
        except Exception as e:
            logger.exception("[记忆系统] recall 工具失败")
            return f"记忆检索失败: {e}"

    @filter.llm_tool(name="memorize_long_term_memory")
    async def tool_memorize(self, event: AstrMessageEvent, content: str, type: str = "user_fact") -> str:
        """写入一条长期记忆。当用户透露值得长期保存的稳定信息时调用。

        Args:
            content(string): 要记住的内容，一句自包含的陈述句
            type(string): 记忆类型，可选 user_fact / preference / project / relationship / event
        """
        if not self._enabled() or self.manager is None:
            return "记忆系统未启用"
        try:
            user_id = self.adapter.get_user_id(event)
            session_id = self.adapter.get_session_id(event)
            mid, created = await self.manager.memorize(
                user_id=user_id,
                content=content,
                memory_type=type,
                tags=[],
                importance=0.6,
                session_id=session_id,
                source="auto",
            )
            return f"已{'写入' if created else '更新'}长期记忆 #{mid}: {content[:50]}"
        except Exception as e:
            logger.exception("[记忆系统] memorize 工具失败")
            return f"记忆写入失败: {e}"

    @filter.llm_tool(name="forget_long_term_memory")
    async def tool_forget(self, event: AstrMessageEvent, id: str) -> str:
        """删除一条长期记忆。当用户明确要求忘记某事，或记忆已确认过期时调用。

        Args:
            id(string): 要删除的记忆 ID（数字）
        """
        if not self._enabled() or self.manager is None:
            return "记忆系统未启用"
        try:
            try:
                memory_id = int(id)
            except (TypeError, ValueError):
                return "记忆 ID 必须是数字"
            user_id = self.adapter.get_user_id(event)
            memory = await self.manager.get(memory_id)
            if memory is None or memory.get("user_id") != user_id:
                return f"记忆 #{memory_id} 不存在或不属于当前用户"
            await self.manager.delete(memory_id)
            return f"已删除长期记忆 #{memory_id}: {memory.get('content', '')[:50]}"
        except Exception as e:
            logger.exception("[记忆系统] forget 工具失败")
            return f"记忆删除失败: {e}"

    # ------------------------------------------------------------------ 管理命令

    @filter.command("记忆")
    async def memory_command(self, event: AstrMessageEvent) -> None:
        """/记忆 管理命令总入口（子命令手动分发，稳定可靠）。"""
        if not self._enabled() or self.manager is None:
            yield event.plain_result("[记忆系统] 未启用")
            return
        # 权限控制（可配置）
        if self.config.get("admin_only_commands", False) and not self.adapter.is_admin(event):
            return  # 非管理员静默忽略
        user_id = self.adapter.get_user_id(event)
        args = self._parse_args(event)
        action = args[0] if args else "帮助"

        handlers = {
            "帮助": self._cmd_help,
            "列表": self._cmd_list,
            "搜索": self._cmd_search,
            "添加": self._cmd_add,
            "删除": self._cmd_delete,
            "清空": self._cmd_clear,
            "统计": self._cmd_stats,
            "导出": self._cmd_export,
            "导入": self._cmd_import,
            "压缩": self._cmd_compress,
        }
        handler = handlers.get(action)
        if handler is None:
            yield event.plain_result(f"未知子命令「{action}」，发送 /记忆 帮助 查看用法")
            return
        async for result in handler(event, user_id, args[1:]):
            yield result

    # ------------------------------------------------------------ 子命令实现

    @staticmethod
    def _parse_args(event: AstrMessageEvent) -> list[str]:
        """从原始消息中解析出 /记忆 之后的参数列表。"""
        text = str(event.message_str or "").strip()
        tokens = text.split()
        for i, t in enumerate(tokens):
            if t.lstrip("/") in ("记忆",):
                return tokens[i + 1 :]
        return tokens[1:] if tokens else []

    async def _cmd_help(self, event, user_id, args):
        yield event.plain_result(
            "【长期记忆系统】\n"
            "/记忆 列表 [页码] —— 查看记忆列表\n"
            "/记忆 搜索 <关键词> —— 搜索记忆\n"
            "/记忆 添加 <类型> <内容> —— 手动添加记忆（类型: user_fact/preference/project/relationship/event）\n"
            "/记忆 删除 <ID> —— 删除记忆\n"
            "/记忆 清空 确认 —— 清空你的全部记忆\n"
            "/记忆 统计 —— 查看记忆统计\n"
            "/记忆 导出 —— 导出记忆为 JSON 文件\n"
            "/记忆 导入 <文件> —— 导入记忆 JSON 文件\n"
            "/记忆 压缩 —— 立即压缩当前会话历史"
        )

    async def _cmd_list(self, event, user_id, args):
        page = 1
        if args:
            try:
                page = max(1, int(args[0]))
            except ValueError:
                page = 1
        memories, total = await self.manager.list_page(user_id, page=page, page_size=10)
        pages = max(1, -(-total // 10))
        header = f"共 {total} 条记忆，第 {page}/{pages} 页\n"
        yield event.plain_result(header + self.manager.format_for_human(memories))

    async def _cmd_search(self, event, user_id, args):
        if not args:
            yield event.plain_result("用法: /记忆 搜索 <关键词>")
            return
        keyword = " ".join(args)
        memories = await self.manager.search_keyword(user_id, keyword, limit=20)
        yield event.plain_result(
            f"搜索「{keyword}」结果（{len(memories)} 条）:\n"
            + self.manager.format_for_human(memories)
        )

    async def _cmd_add(self, event, user_id, args):
        if len(args) < 2:
            yield event.plain_result(
                "用法: /记忆 添加 <类型> <内容>\n类型: user_fact/preference/project/relationship/event"
            )
            return
        memory_type = args[0].lower()
        content = " ".join(args[1:])
        mid, created = await self.manager.memorize(
            user_id=user_id,
            content=content,
            memory_type=memory_type,
            tags=[],
            importance=0.8,
            session_id=self.adapter.get_session_id(event),
            source="manual",
        )
        verb = "已添加" if created else "已更新（原内容重复）"
        yield event.plain_result(f"{verb}记忆 #{mid}")

    async def _cmd_delete(self, event, user_id, args):
        if not args or not args[0].lstrip("#").isdigit():
            yield event.plain_result("用法: /记忆 删除 <ID>")
            return
        memory_id = int(args[0].lstrip("#"))
        memory = await self.manager.get(memory_id)
        if memory is None or memory.get("user_id") != user_id:
            yield event.plain_result(f"记忆 #{memory_id} 不存在或不属于你")
            return
        await self.manager.delete(memory_id)
        yield event.plain_result(f"已删除记忆 #{memory_id}: {memory.get('content', '')[:50]}")

    async def _cmd_clear(self, event, user_id, args):
        if not args or args[0] != "确认":
            yield event.plain_result("⚠️ 将清空你的全部长期记忆且不可恢复。确认请发送: /记忆 清空 确认")
            return
        count = await self.manager.clear(user_id)
        yield event.plain_result(f"已清空 {count} 条记忆")

    async def _cmd_stats(self, event, user_id, args):
        stats = await self.manager.stats(user_id)
        by_type = "\n".join(
            f"  {t}: {c}" for t, c in sorted(stats["by_type"].items(), key=lambda x: -x[1])
        )
        yield event.plain_result(
            f"【记忆统计】\n总数: {stats['total']}\n按类型:\n{by_type or '  （无）'}\n"
            f"平均重要度: {stats['avg_importance']}\n累计被访问: {stats['total_accesses']} 次"
        )

    async def _cmd_export(self, event, user_id, args):
        records = await self.db.export_all(user_id)
        path = os.path.join(
            self.adapter.get_data_dir(),
            f"memory_export_{sanitize_filename(user_id)}.json",
        )
        with open(path, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)
        yield event.chain_result(
            MessageChain(
                [
                    Plain(f"已导出 {len(records)} 条记忆"),
                    File(name=os.path.basename(path), file=path),
                ]
            )
        )

    async def _cmd_import(self, event, user_id, args):
        path = await self.adapter.file_component_to_path(event)
        if path is None:
            yield event.plain_result("请同时发送一个 JSON 记忆文件（先导出时生成的文件）")
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                records = json.load(f)
            if not isinstance(records, list):
                raise ValueError("文件格式应为记忆数组")
            count = await self.db.import_memories(records)
            yield event.plain_result(f"成功导入 {count} 条记忆")
        except (ValueError, json.JSONDecodeError, OSError) as e:
            yield event.plain_result(f"导入失败: {e}")

    async def _cmd_compress(self, event, user_id, args):
        umo = self.adapter.get_session_id(event)
        history = await self.adapter.get_conversation_history(umo)
        if not history:
            yield event.plain_result("没有可压缩的对话历史（或当前版本不支持读取历史）")
            return
        new_history, summary = await self.compressor.compress(
            user_id, umo, history, force=True
        )
        if not summary:
            yield event.plain_result("历史较短或压缩失败，未生成摘要")
            return
        saved = await self.adapter.save_conversation_history(umo, new_history)
        tip = "已写回会话" if saved else "（摘要已存为记忆；写回会话历史失败，见日志）"
        yield event.plain_result(f"压缩完成，{tip}\n摘要:\n{summary[:500]}")
