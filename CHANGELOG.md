# 更新日志

所有重要变更都会记录在本文件中。

## v1.2.0 (2026-09-17)

### 新增
- 接入 AstrBot 官方 Plugin Pages 机制：面板「插件 → 长期记忆系统」卡片出现 **WebUI 按钮**，点击直接进入可视化面板
- 面板功能：统计卡片（总数/用户数/平均重要度/访问次数/类型分布）、按用户筛选的记忆列表（分页/删除/重要度进度条）、跨用户搜索、手动添加记忆、清空指定用户或全部记忆（二次确认）、导出 JSON
- 插件页 Web API：stats / list / search / users / export / add / delete / clear，响应统一 `{status, data}` 信封

### 变更
- 插件显示名改为中文 **「长期记忆系统」**（⚠️ 插件名变更会导致旧配置文件失效，需重新配置；数据库不受影响）
- 插件页路由同时注册显示名与英文 ID 双前缀，兼容不同面板版本的取值方式
- 独立 WebUI 服务（端口 6198）降级为可选项 `webui_enable`，默认关闭

### 修复
- `webui/server.py` 在顶层包导入时相对导入报错（try/except 双导入）

## v1.1.0 (2026-09-16)

### 新增
- 可视化 WebUI 面板 v1：独立 aiohttp HTTP 服务（默认 6198 端口），支持密码鉴权（Bearer Token，hmac 常量时间比较）
- `webui/` 子包：server.py（REST API + 登录）+ index.html（暗色单页 SPA）
- 新增配置项：webui_enable / webui_host / webui_port / webui_password
- 新增数据库方法：`clear_all()`（跨用户清空）、`search_all_users()`（跨用户搜索）

## v1.0.0 (2026-09-16)

首个正式版本。

### 新增
- **长期记忆存储**：SQLite 持久化（aiosqlite 异步优先，缺依赖自动降级 sqlite3 + 线程池），多用户/多会话隔离，内容去重
- **自动记忆提取**：每轮对话后 LLM 结构化提取（JSON 容错解析、重要度门槛过滤），失败静默不影响聊天
- **相关记忆检索 + 上下文注入**：多因子打分（关键词重叠/子串命中/重要度/时效半衰/访问热度），记忆包按 30%/50%/20% token 预算分层注入
- **上下文压缩**：历史超 token 阈值时 LLM 摘要，存为 summary 记忆并替换旧历史
- **LLM 工具**：recall_long_term_memory / memorize_long_term_memory / forget_long_term_memory（带用户隔离）
- **管理命令**：/记忆 帮助|列表|搜索|添加|删除|清空|统计|导出|导入|压缩
- **配置项**：enable / db_path / auto_memorize / auto_recall / auto_compress / max_inject_memories / max_inject_tokens / compress_threshold_tokens / importance_threshold / embedding_enable / llm_provider_id / admin_only_commands 等
