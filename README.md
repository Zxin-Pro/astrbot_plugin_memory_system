# astrbot_plugin_memory_system

为 AstrBot 提供跨会话的**长期记忆系统**：自动记忆提取、相关记忆检索、上下文注入、历史压缩、LLM 记忆工具与中文管理命令。

## 功能特性

- **长期记忆存储**：SQLite 持久化（aiosqlite 异步驱动，缺依赖自动降级 sqlite3 + 线程池），多用户 / 多会话数据隔离，字段含类型、标签、重要度、访问统计等。
- **自动记忆提取**：每轮对话后调用 LLM 判断是否值得记忆，输出结构化 JSON（类型 / 内容 / 标签 / 重要度），失败静默不影响聊天。
- **相关记忆检索 + 上下文注入**：LLM 请求前按关键词重叠、子串命中、重要度、时效衰减、访问热度多因子打分，组装分层记忆包（用户画像 / 相关记忆 / 历史摘要）注入 system_prompt，带 token 预算裁剪。
- **上下文压缩**：对话历史超过 token 阈值时调用 LLM 生成摘要，存为 `summary` 记忆并替换旧历史，保留最近 N 条原文。
- **LLM 工具**：`recall_long_term_memory(query)` / `memorize_long_term_memory(content, type)` / `forget_long_term_memory(id)`，模型可自主调用，且只允许操作当前用户自己的记忆。
- **管理命令**：`/记忆 帮助|列表|搜索|添加|删除|清空|统计|导出|导入|压缩`。

## 安装

1. 将本插件放入 AstrBot 的 `data/plugins/astrbot_plugin_memory_system/`（或通过 WebUI「插件管理 → 上传安装」上传 zip）。
2. 依赖 `aiosqlite` 已在 `requirements.txt` 中声明，AstrBot 加载时会自动安装；未安装成功也不影响运行（自动降级）。
3. 在 WebUI「插件管理」中启用插件。

## 配置（WebUI 可见）

| 配置项 | 默认值 | 说明 |
|---|---|---|
| enable | true | 是否启用 |
| db_path | "" | 数据库路径，留空用插件数据目录 |
| auto_memorize | true | 对话后自动提取记忆 |
| auto_recall | true | 请求前自动检索注入 |
| auto_compress | true | 历史超阈值自动压缩 |
| max_inject_memories | 5 | 注入相关记忆最大条数 |
| max_inject_tokens | 600 | 记忆包 token 预算 |
| compress_threshold_tokens | 3000 | 触发压缩的 token 阈值 |
| keep_recent_messages | 8 | 压缩时保留的最近原文条数 |
| importance_threshold | 0.5 | 自动提取的重要度门槛 |
| embedding_enable | false | 向量检索（默认关闭） |
| embedding_provider_id | "" | embedding 提供商 ID |
| llm_provider_id | "" | 提取/压缩/工具所用提供商，留空用当前对话提供商 |
| admin_only_commands | false | 仅管理员可用 /记忆 命令 |
| candidate_pool | 300 | 检索候选池大小 |
| webui_enable | true | 是否启用可视化 WebUI 面板 |
| webui_host | 0.0.0.0 | WebUI 监听地址 |
| webui_port | 6198 | WebUI 监听端口 |
| webui_password | "" | WebUI 登录密码，留空免登录 |

## 命令说明

```
/记忆 帮助                 查看帮助
/记忆 列表 [页码]          分页查看记忆
/记忆 搜索 <关键词>        关键词搜索
/记忆 添加 <类型> <内容>   手动添加（user_fact/preference/project/relationship/event）
/记忆 删除 <ID>            删除单条
/记忆 清空 确认            清空本人全部记忆（需二次确认）
/记忆 统计                 记忆统计
/记忆 导出                 导出 JSON 文件
/记忆 导入 <文件>          导入 JSON 文件（需与命令同时发送文件）
/记忆 压缩                 立即压缩当前会话历史
```

## 目录结构

```
astrbot_plugin_memory_system/
├── main.py            # 插件入口：钩子 / 工具 / 命令编排
├── metadata.yaml
├── _conf_schema.json
├── requirements.txt
├── README.md
├── adapter.py         # 所有 AstrBot API 调用隔离层（含 TODO 标注）
├── pages/webui/       # 官方插件页（AstrBot 面板插件卡片上的 WebUI 按钮）
│   └── index.html
├── webui/
│   ├── page_api.py        # 插件页后端 API 逻辑层（可独立单测）
│   ├── server.py          # 独立 WebUI 服务（可选，aiohttp + 密码鉴权）
│   └── index.html         # 独立 WebUI 单页面板
├── core/
│   ├── database.py        # SQLite 持久化（参数化 SQL，aiosqlite 优先）
│   ├── memory_manager.py  # 业务层：去重、校验、格式化
│   ├── retriever.py       # 多因子打分检索
│   ├── extractor.py       # LLM 自动记忆提取
│   ├── injector.py        # 记忆包组装与预算裁剪
│   ├── compressor.py      # 上下文压缩
│   ├── prompts.py         # 内置提示词（人格 / 工具说明 / 提取 / 压缩）
│   └── utils.py           # token 估算、关键词提取、JSON 容错解析
└── tests/
    └── test_memory_manager.py  # 10 个基础测试（python3 tests/test_memory_manager.py）
```

## WebUI 可视化面板

**方式一（推荐）：AstrBot 面板插件页**（v4.28.0+ 自带机制，无需额外配置）
打开 AstrBot 面板 →「插件」→「长期记忆系统」→ 点击 **WebUI** 按钮即可进入面板。鉴权由面板统一处理，功能包括：统计卡片、按用户筛选的记忆列表（分页/删除/重要度可视化）、跨用户搜索、手动添加、清空指定用户或全部记忆、导出 JSON。

**方式二：独立 WebUI 服务**（可选，供面板外直接访问）
配置 `webui_enable: true` 后，插件会启动独立 HTTP 服务（默认 `http://<服务器IP>:6198`）：
- `webui_password` 设置密码后需登录（Bearer Token）；留空则免登录，**公网服务器务必设置密码**
- 公网部署建议 `webui_host` 保持 `0.0.0.0` + 强密码，或改 `127.0.0.1` 配合反向代理

## 测试

core 层不依赖 AstrBot 平台，可直接运行：

```bash
python3 tests/test_memory_manager.py
# 或
python3 -m pytest tests/ -v
```

## 需按版本确认的接口（adapter.py 中已做安全降级）

1. **File 消息组件下载**：`/记忆 导入` 用，已兼容 `path` / `file` / `get_file()` / URL 下载多种形态。
2. **EmbeddingProvider 方法名**：`get_embedding` / `text_to_embedding` / `embed` 依次尝试，确认后可开启 `embedding_enable`。
3. **conversation_manager 历史读写**：`/记忆 压缩` 手动压缩用，已兼容 `history` 为 JSON 字符串或 list 两种形态；若写回接口 `update_conversation` 签名不同，请按版本修正 `adapter.py`。

适配 AstrBot master（2026-09 验证）：`on_llm_request(event, req: ProviderRequest)`、`on_llm_response(event, resp: LLMResponse)`、`provider.text_chat(prompt, ..., system_prompt=...)`、`filter.llm_tool` docstring 参数解析、`event.is_admin()`。
