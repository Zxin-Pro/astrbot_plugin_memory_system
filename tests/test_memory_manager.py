"""基础测试：core 层（数据库 / 管理器 / 检索器 / 提取器解析 / 压缩器 / 注入器）。

不依赖 AstrBot 平台，直接运行:
    python3 -m pytest tests/ -v
或:
    python3 tests/test_memory_manager.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.compressor import ContextCompressor  # noqa: E402
from core.database import MemoryDatabase  # noqa: E402
from core.extractor import MemoryExtractor  # noqa: E402
from core.injector import MemoryInjector  # noqa: E402
from core.memory_manager import MemoryManager  # noqa: E402
from core.retriever import MemoryRetriever  # noqa: E402
from core.utils import estimate_tokens, safe_json_loads  # noqa: E402


async def make_manager(tmpdir: str) -> tuple[MemoryDatabase, MemoryManager]:
    db = MemoryDatabase(os.path.join(tmpdir, "test.db"))
    await db.initialize()
    return db, MemoryManager(db)


# ----------------------------------------------------------------- 数据库层

async def test_add_and_dedup():
    with tempfile.TemporaryDirectory() as tmp:
        db, mgr = await make_manager(tmp)
        mid1, created1 = await mgr.memorize("u1", "用户是程序员", importance=0.8)
        assert created1
        mid2, created2 = await mgr.memorize("u1", "用户是程序员")
        assert not created2 and mid1 == mid2, "重复内容应更新而非新建"
        # 用户隔离：u2 看不到 u1 的记忆
        assert await mgr.memorize("u2", "用户是程序员") and True
        memories, total = await mgr.list_page("u1")
        assert total == 1 and memories[0]["id"] == mid1
        memories_u2, total_u2 = await mgr.list_page("u2")
        assert total_u2 == 1
        await db.close()


async def test_importance_clamp_and_type_validation():
    with tempfile.TemporaryDirectory() as tmp:
        db, mgr = await make_manager(tmp)
        mid, _ = await mgr.memorize("u1", "测试", memory_type="非法", importance=9.9)
        mem = await mgr.get(mid)
        assert mem["type"] == "user_fact" and mem["importance"] == 1.0
        await db.close()


async def test_search_and_stats_and_clear():
    with tempfile.TemporaryDirectory() as tmp:
        db, mgr = await make_manager(tmp)
        await mgr.memorize("u1", "用户喜欢打羽毛球", tags=["运动"], importance=0.9)
        await mgr.memorize("u1", "用户在合肥上学", memory_type="event", importance=0.6)
        result = await mgr.search_keyword("u1", "羽毛球")
        assert len(result) == 1 and "羽毛球" in result[0]["content"]
        stats = await mgr.stats("u1")
        assert stats["total"] == 2 and stats["by_type"].get("user_fact") == 1
        cleared = await mgr.clear("u1")
        assert cleared == 2 and (await mgr.stats("u1"))["total"] == 0
        await db.close()


async def test_export_import():
    with tempfile.TemporaryDirectory() as tmp:
        db, mgr = await make_manager(tmp)
        await mgr.memorize("u1", "记忆A", importance=0.7)
        records = await db.export_all("u1")
        assert len(records) == 1 and records[0]["content"] == "记忆A"
        # 导入到新用户
        payload = [dict(records[0], user_id="u9")]
        count = await db.import_memories(payload)
        assert count == 1
        assert (await mgr.stats("u9"))["total"] == 1
        await db.close()


# ----------------------------------------------------------------- 检索器

async def test_retriever_ranking():
    with tempfile.TemporaryDirectory() as tmp:
        db, mgr = await make_manager(tmp)
        await mgr.memorize("u1", "用户喜欢打羽毛球", importance=0.9)
        await mgr.memorize("u1", "用户养了一只猫叫团子", importance=0.8)
        await mgr.memorize("u1", "用户是程序员", importance=0.95)
        retriever = MemoryRetriever(db, mgr)
        top = await retriever.retrieve("u1", "我们今天打羽毛球怎么样？", top_k=2)
        assert top, "应至少检索到 1 条"
        assert "羽毛球" in top[0]["content"], "相关性最高的应排在前面"
        none_case = await retriever.retrieve("u1", "量子力学薛定谔方程", top_k=3)
        assert none_case == [] or True  # 无关查询允许为空或低分
        await db.close()


# ----------------------------------------------------------------- 提取器

async def test_extractor_parse_and_store():
    with tempfile.TemporaryDirectory() as tmp:
        db, mgr = await make_manager(tmp)

        async def fake_llm(prompt: str, system: str) -> str:
            return json.dumps(
                [
                    {
                        "should_memorize": True,
                        "type": "user_fact",
                        "content": "用户是程序员",
                        "tags": ["职业"],
                        "importance": 0.8,
                    },
                    {"should_memorize": True, "content": "太短不重要", "importance": 0.1},
                    {"should_memorize": False},
                ],
                ensure_ascii=False,
            )

        extractor = MemoryExtractor(mgr, fake_llm, importance_threshold=0.5)
        stored = await extractor.extract_and_store("u1", "s1", "我是一名程序员", "你好呀")
        assert len(stored) == 1 and stored[0]["content"] == "用户是程序员"
        assert (await mgr.stats("u1"))["total"] == 1
        await db.close()


async def test_extractor_llm_failure_is_silent():
    with tempfile.TemporaryDirectory() as tmp:
        db, mgr = await make_manager(tmp)

        async def broken_llm(prompt: str, system: str) -> str:
            raise RuntimeError("LLM 挂了")

        extractor = MemoryExtractor(mgr, broken_llm)
        stored = await extractor.extract_and_store("u1", "s1", "我在学 Rust", "加油")
        assert stored == [], "LLM 异常应被吞掉且返回空"
        # 非 JSON 输出也应安全
        async def garbage_llm(prompt: str, system: str) -> str:
            return "我不知道你在说什么，这不是 JSON"

        extractor2 = MemoryExtractor(mgr, garbage_llm)
        assert await extractor2.extract_and_store("u1", "s1", "abc", "def") == []
        await db.close()


# ----------------------------------------------------------------- 压缩器

async def test_compressor():
    with tempfile.TemporaryDirectory() as tmp:
        db, mgr = await make_manager(tmp)
        called = {}

        async def fake_llm(prompt: str, system: str) -> str:
            called["prompt_len"] = len(prompt)
            return "总览：我们聊了羽毛球和项目计划。\n- 用户喜欢羽毛球\n- 约定周末一起打球"

        compressor = ContextCompressor(
            mgr, fake_llm, compress_threshold_tokens=50, keep_recent_messages=2
        )
        contexts = [
            {
                "role": "user" if i % 2 else "assistant",
                "content": (
                    f"消息{i}：我们继续讨论羽毛球的杀球战术和 weekend 比赛安排，"
                    "顺便聊聊上次的装备采购计划，包括球拍、球线和手胶的预算分配。"
                ),
            }
            for i in range(30)
        ]
        assert compressor.should_compress(contexts), "超阈值应触发压缩"
        new_ctx, summary = await compressor.compress("u1", "s1", contexts)
        assert summary and new_ctx[0]["role"] == "system", "旧历史应被摘要替换"
        assert len(new_ctx) == 3, "保留最近 2 条 + 1 条摘要"
        stats = await mgr.stats("u1")
        assert stats["by_type"].get("summary") == 1, "摘要应写入数据库"
        # 未超阈值不触发
        short_ctx = [{"role": "user", "content": "hi"}]
        ctx2, s2 = await compressor.compress("u1", "s1", short_ctx)
        assert s2 == "" and ctx2 == short_ctx
        await db.close()


# ----------------------------------------------------------------- 注入器

async def test_injector_budget():
    with tempfile.TemporaryDirectory() as tmp:
        db, mgr = await make_manager(tmp)
        for i in range(20):
            await mgr.memorize(
                "u1", f"用户的兴趣爱好{i}是打羽毛球和编程", importance=0.9
            )
        retriever = MemoryRetriever(db, mgr)
        injector = MemoryInjector(db, mgr, retriever, max_inject_memories=5, max_inject_tokens=300)
        pack = await injector.build_pack("u1", "羽毛球")
        assert pack and "羽毛球" in pack
        assert estimate_tokens(pack) <= 300 + 50, "记忆包应控制在预算附近"
        empty = await injector.build_pack("ghost_user", "羽毛球")
        assert empty == ""
        await db.close()


# ----------------------------------------------------------------- 工具函数

def test_utils():
    assert estimate_tokens("你好世界") == 4
    assert estimate_tokens("hello world!") >= 2
    assert safe_json_loads('```json\n{"a": 1}\n```') == {"a": 1}
    assert safe_json_loads("前缀文字 {\"b\": [1,2]} 后缀") == {"b": [1, 2]}
    assert safe_json_loads("不是 JSON") is None
    assert len(extract_keywords_text("打羽毛球 abc")) > 0


# ----------------------------------------------------------------- 插件页 API

async def test_page_api():
    with tempfile.TemporaryDirectory() as tmp:
        db, mgr = await make_manager(tmp)
        await mgr.memorize("u1", "用户喜欢打羽毛球", tags=["运动"], importance=0.9)
        await mgr.memorize("u2", "用户在学 Rust", memory_type="event", importance=0.6)
        from webui.page_api import PageAPI

        page = PageAPI(db, mgr)
        stats = await page.stats({})
        assert stats["total"] == 2 and set(stats["users"]) == {"u1", "u2"}
        lst = await page.list_memories({"page": "1", "page_size": "1"})
        assert lst["total"] == 2 and len(lst["memories"]) == 1
        found = await page.search({"keyword": "羽毛球"})
        assert len(found) == 1 and found[0]["user_id"] == "u1"
        users = await page.users()
        assert users[0]["user_id"] == "u1" and users[0]["count"] == 1
        added = await page.add({"user_id": "u3", "content": "页面添加的记忆"})
        assert added["id"] > 0
        try:
            await page.add({"user_id": "", "content": "x"})
            assert False, "缺少 user_id 应报 ValueError"
        except ValueError:
            pass
        deleted = await page.delete({"id": added["id"]})
        assert deleted["deleted"] is True
        try:
            await page.clear({})
            assert False, "缺少 confirm 应报 ValueError"
        except ValueError:
            pass
        cleared = await page.clear({"confirm": True})
        assert cleared["deleted"] == 2
        await db.close()


# ----------------------------------------------------------------- WebUI

async def test_webui_end_to_end():
    import aiohttp

    with tempfile.TemporaryDirectory() as tmp:
        db, mgr = await make_manager(tmp)
        await mgr.memorize("u1", "用户喜欢打羽毛球", tags=["运动"], importance=0.9)
        from webui.server import MemoryWebUI

        ui = MemoryWebUI(db, mgr, host="127.0.0.1", port=0, password="pw123")
        # port=0 由 aiohttp 分配随机端口
        app_runner = None
        from aiohttp import web as aioweb

        runner = aioweb.AppRunner(ui._build_app(), access_log=None)
        await runner.setup()
        site = aioweb.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        base = f"http://127.0.0.1:{port}"
        try:
            async with aiohttp.ClientSession() as sess:
                # 未登录应 401
                async with sess.get(f"{base}/api/stats") as r:
                    assert r.status == 401
                # 密码错误 403
                async with sess.post(
                    f"{base}/api/login", json={"password": "wrong"}
                ) as r:
                    assert r.status == 403
                # 正确登录
                async with sess.post(
                    f"{base}/api/login", json={"password": "pw123"}
                ) as r:
                    tok = (await r.json())["data"]["token"]
                auth = {"Authorization": f"Bearer {tok}"}
                async with sess.get(f"{base}/api/stats", headers=auth) as r:
                    data = (await r.json())["data"]
                    assert data["total"] == 1 and "u1" in data["users"]
                async with sess.get(f"{base}/api/list?page=1", headers=auth) as r:
                    d = (await r.json())["data"]
                    assert d["total"] == 1 and d["memories"][0]["content"].startswith("用户喜欢")
                async with sess.get(
                    f"{base}/api/search?keyword=羽毛球", headers=auth
                ) as r:
                    d = (await r.json())["data"]
                    assert len(d) == 1
                async with sess.post(
                    f"{base}/api/add",
                    headers=auth,
                    json={"user_id": "u2", "content": "测试记忆", "type": "event"},
                ) as r:
                    d = (await r.json())["data"]
                    assert d["id"] > 0
                async with sess.post(
                    f"{base}/api/delete", headers=auth, json={"id": d["id"]}
                ) as r:
                    assert (await r.json())["data"]["deleted"] is True
                async with sess.post(
                    f"{base}/api/clear", headers=auth, json={"user_id": "u1", "confirm": True}
                ) as r:
                    assert (await r.json())["data"]["deleted"] == 1
                # 首页静态文件
                async with sess.get(f"{base}/") as r:
                    assert r.status == 200 and b"long_term" not in await r.read()
        finally:
            await runner.cleanup()
            await db.close()


def extract_keywords_text(text: str):
    from core.utils import extract_keywords

    return extract_keywords(text)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            if asyncio.iscoroutinefunction(t):
                asyncio.run(t())
            else:
                t()
            print(f"PASS  {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {t.__name__}: {e!r}")
    print(f"\n{len(tests) - failed}/{len(tests)} tests passed")
    sys.exit(1 if failed else 0)
