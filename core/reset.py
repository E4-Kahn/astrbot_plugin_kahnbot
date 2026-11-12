"""整机数据重置：流水、记忆、提醒排期、情绪状态全清；插件配置不动。

开发测试时可以先备份一份（`kahnbot.db` 用 sqlite 在线备份、`state.json` 与
`memory/` 原样拷贝），备份完再清。正式部署把 `reset_backup_dir` 留空就只清不备。
"""

from __future__ import annotations

import shutil
import sqlite3
import time
from pathlib import Path

from .memoryfiles import MemoryFiles
from .store import Store


def _stamp(now: float | None = None) -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.localtime(now or time.time()))


def snapshot(
    store: Store | None,
    files: MemoryFiles,
    state_path: str | Path,
    backup_root: str | Path,
    *,
    now: float | None = None,
) -> Path:
    """把当前数据拷一份到 `backup_root/kahnbot-<时间>/`，返回目录。"""
    target = Path(backup_root) / f"kahnbot-{_stamp(now)}"
    target.mkdir(parents=True, exist_ok=True)
    if store is not None and store.conn is not None:
        # 用 sqlite 自己的备份接口：服务还在跑也能拿到一致的快照。
        dest = sqlite3.connect(target / "kahnbot.db")
        try:
            store.conn.backup(dest)
        finally:
            dest.close()
    state = Path(state_path)
    if state.exists():
        shutil.copy2(state, target / "state.json")
    memory = state.parent / "memory"
    if memory.is_dir():
        shutil.copytree(memory, target / "memory", dirs_exist_ok=True)
    return target


def reset_all(
    store: Store | None,
    files: MemoryFiles,
    state_path: str | Path,
    *,
    backup_root: str | Path | None = None,
    now: float | None = None,
) -> dict:
    """清空业务数据。返回 `{backup, messages, facts}`。

    备份失败会抛出异常：宁可这次不重置，也不要用户以为备份了其实没有。
    """
    backup = ""
    if backup_root:
        backup = str(snapshot(store, files, state_path, backup_root, now=now))

    counts = {"messages": 0, "facts": 0}
    if store is not None:
        counts = store.reset()
    files.reset()

    state = Path(state_path)
    if state.exists():
        state.unlink()

    return {"backup": backup, "messages": counts.get("messages", 0), "facts": counts.get("facts", 0)}
