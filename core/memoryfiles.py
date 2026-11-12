"""三份 markdown：facts.md / observed.md / schedule.md。

`kahnbot.db` 是索引，md 是可读、可手改的权威副本（memory-design §5.1）。两条硬规矩：

- 只用 uid 认人（昵称会变）。
- 行尾标记顺序固定：〔重要度 N〕〔已被取代 by #id〕〔已归档〕〔#fact_id〕，
  这样人肉看、正则读都稳。
"""

from __future__ import annotations

import os
import re
from datetime import datetime, tzinfo
from pathlib import Path

LAYER_FILES = {"owner": "facts.md", "observed": "observed.md"}
LAYER_TITLES = {"owner": "长期记忆（owner 层）", "observed": "观察记录（observed 层）"}

FACTS_HEADER = (
    "# 长期记忆（owner 层）\n\n"
    "主人亲口交代的事，可以拿来提醒。索引以 `kahnbot.db` 为准，这个文件可以直接看、直接改。\n"
)
OBSERVED_HEADER = (
    "# 观察记录（observed 层）\n\n"
    "群里听来的事，属于转述、未必属实，永远不主动提醒。\n"
)
SCHEDULE_HEADER = (
    "# 提醒排期\n\n"
    "每行一次提醒；到点发完就删行。格式：`- YYYY-MM-DD HH:MM · [scope] · #fact_id · 要说的话`\n"
)

_ENTRY_RE = re.compile(
    r"^- \*\*(?P<date>\d{4}-\d{2}-\d{2})\*\* · \[(?P<scope>[^\]]*)\|"
    r"(?P<speaker>[^|\]]*)\|uid:(?P<uid>[^|\]]*)\|(?P<layer>[a-z]+)\] "
    r"(?P<text>.*?)(?P<marks>(?:〔[^〕]*〕)*)\s*$"
)
_IMPORTANCE_RE = re.compile(r"〔重要度\s*(\d{1,3})〕")
_SUPERSEDED_RE = re.compile(r"〔已被取代 by #([0-9a-zA-Z]+)〕")
_ARCHIVED_RE = re.compile(r"〔已归档〕")
_FACT_ID_RE = re.compile(r"〔#([0-9a-zA-Z]+)〕")
_SCHEDULE_RE = re.compile(
    r"^- (?P<when>\d{4}-\d{2}-\d{2} \d{2}:\d{2}) · \[(?P<scope>[^\]]*)\] · #(?P<fact_id>[0-9a-zA-Z]+) · (?P<phrase>.*)$"
)


def entry_line(entry: dict) -> str:
    """把一条记忆渲染成 md 行。"""
    marks = []
    if entry.get("importance") is not None:
        marks.append(f"〔重要度 {int(entry['importance'])}〕")
    if entry.get("superseded_by"):
        marks.append(f"〔已被取代 by #{entry['superseded_by']}〕")
    if entry.get("archived"):
        marks.append("〔已归档〕")
    if entry.get("fact_id"):
        marks.append(f"〔#{entry['fact_id']}〕")
    text = " ".join(str(entry.get("text") or "").split())
    scope = entry.get("scope") or "私聊"
    speaker = entry.get("speaker") or "主人"
    uid = entry.get("uid") or ""
    layer = entry.get("layer") or "owner"
    date = entry.get("date") or ""
    return f"- **{date}** · [{scope}|{speaker}|uid:{uid}|{layer}] {text} {''.join(marks)}".rstrip()


def parse_line(line: str) -> dict | None:
    """解析一行 md 条目；不是条目返回 None。"""
    m = _ENTRY_RE.match(line.strip())
    if not m:
        return None
    marks = m.group("marks") or ""
    importance = _IMPORTANCE_RE.search(marks)
    superseded = _SUPERSEDED_RE.search(marks)
    fact_id = _FACT_ID_RE.search(marks)
    return {
        "date": m.group("date"),
        "scope": m.group("scope"),
        "speaker": m.group("speaker"),
        "uid": m.group("uid"),
        "layer": m.group("layer"),
        "text": m.group("text").strip(),
        "importance": int(importance.group(1)) if importance else None,
        "superseded_by": superseded.group(1) if superseded else "",
        "archived": bool(_ARCHIVED_RE.search(marks)),
        "fact_id": fact_id.group(1) if fact_id else "",
    }


class MemoryFiles:
    """读写 `data/memory/` 下的三份 md。"""

    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self.memory_dir = self.data_dir / "memory"

    def path(self, layer: str) -> Path:
        return self.memory_dir / LAYER_FILES[layer]

    def schedule_path(self) -> Path:
        return self.memory_dir / "schedule.md"

    def ensure_files(self) -> None:
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        for layer, header in (("owner", FACTS_HEADER), ("observed", OBSERVED_HEADER)):
            target = self.path(layer)
            if not target.exists():
                self._write(target, header)
        if not self.schedule_path().exists():
            self._write(self.schedule_path(), SCHEDULE_HEADER)

    # ---- 读写 -------------------------------------------------------------

    def _write(self, path: Path, text: str) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)

    def read(self, layer: str) -> str:
        try:
            return self.path(layer).read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    def load(self, layer: str) -> list[dict]:
        entries = []
        for line in self.read(layer).splitlines():
            entry = parse_line(line)
            if entry:
                entries.append(entry)
        return entries

    def append(self, layer: str, entry: dict) -> None:
        self.ensure_files()
        path = self.path(layer)
        text = self.read(layer)
        if text and not text.endswith("\n"):
            text += "\n"
        text += entry_line(entry) + "\n"
        self._write(path, text)

    def update(self, layer: str, fact_id: str, **changes) -> bool:
        """按 fact_id 改一行；返回是否找到。"""
        if not fact_id:
            return False
        lines = self.read(layer).splitlines()
        changed = False
        for i, line in enumerate(lines):
            entry = parse_line(line)
            if not entry or entry["fact_id"] != fact_id:
                continue
            entry.update({k: v for k, v in changes.items() if v is not None})
            lines[i] = entry_line(entry)
            changed = True
        if changed:
            self._write(self.path(layer), "\n".join(lines) + "\n")
        return changed

    def remove(self, layer: str, fact_id: str) -> bool:
        if not fact_id:
            return False
        lines = self.read(layer).splitlines()
        kept = [line for line in lines if (parse_line(line) or {}).get("fact_id") != fact_id]
        if len(kept) == len(lines):
            return False
        self._write(self.path(layer), "\n".join(kept) + "\n")
        return True

    def rewrite(self, layer: str, entries: list[dict]) -> None:
        header = FACTS_HEADER if layer == "owner" else OBSERVED_HEADER
        body = "\n".join(entry_line(entry) for entry in entries)
        self._write(self.path(layer), header + "\n" + body + ("\n" if body else ""))

    def reset(self) -> None:
        """清空三份 md，只留表头（整机重置用）。"""
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self._write(self.path("owner"), FACTS_HEADER + "\n")
        self._write(self.path("observed"), OBSERVED_HEADER + "\n")
        self._write(self.schedule_path(), SCHEDULE_HEADER + "\n")

    # ---- schedule.md -----------------------------------------------------

    def _schedule_lines(self) -> list[str]:
        try:
            return self.schedule_path().read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []

    def scheduled(self) -> list[dict]:
        items = []
        for line in self._schedule_lines():
            m = _SCHEDULE_RE.match(line.strip())
            if m:
                items.append(
                    {
                        "when": m.group("when"),
                        "scope": m.group("scope"),
                        "fact_id": m.group("fact_id"),
                        "phrase": m.group("phrase").strip(),
                    }
                )
        return items

    def add_schedule(self, due_ts: int, scope: str, fact_id: str, phrase: str, tz: tzinfo | None = None) -> None:
        self.ensure_files()
        when = datetime.fromtimestamp(due_ts, tz).strftime("%Y-%m-%d %H:%M")
        # 一条事实只留一行排期：改时间/改文案都是整行替换。
        lines = [line for line in self._schedule_lines() if f"· #{fact_id} ·" not in line]
        line = f"- {when} · [{scope}] · #{fact_id} · {' '.join(phrase.split())}"
        self._write(self.schedule_path(), "\n".join(lines + [line]) + "\n")

    def remove_schedule(self, fact_id: str) -> bool:
        lines = self._schedule_lines()
        kept = [line for line in lines if f"· #{fact_id} ·" not in line]
        if len(kept) == len(lines):
            return False
        self._write(self.schedule_path(), "\n".join(kept) + "\n")
        return True

    def schedule_phrase(self, fact_id: str) -> str:
        for item in self.scheduled():
            if item["fact_id"] == fact_id:
                return item["phrase"]
        return ""

    def schedule_line_for(self, fact_id: str) -> dict | None:
        for item in self.scheduled():
            if item["fact_id"] == fact_id:
                return item
        return None
