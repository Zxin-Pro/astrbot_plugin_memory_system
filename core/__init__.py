"""astrbot_plugin_memory_system 核心模块。

core 包内所有模块不依赖 AstrBot 平台 API，保持纯净（仅标准库 + aiosqlite），
所有与 AstrBot 平台交互的代码统一隔离在插件根目录的 adapter.py 中。
"""
