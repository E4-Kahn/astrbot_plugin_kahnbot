"""KahnBot 的业务实现。

`main.py` 只负责注册 handler 与串联；具体逻辑放在本包内，按职责分文件：

config.py    读配置、默认值收敛
store.py     SQLite（流水 / 记忆索引 / 配额 / meta）
recorder.py  catch-all 记录 + 噪音过滤
"""
