"""`state.json` 的读写。

几十个 float，不值得单开一张表；用临时文件 + `os.replace` 原子替换，
避免进程被杀时留下半个 JSON。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def load_state(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def save_state(path: Path, state: dict[str, Any]) -> bool:
    tmp = path.with_suffix(".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False
