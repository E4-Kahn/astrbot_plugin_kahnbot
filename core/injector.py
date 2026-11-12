"""注入：把检索结果写成一小段文本，贴到本轮请求里（F14 / F15）。

两条约束来自 `docs/refs/memory-design.md`：
- **位置**：`extra_user_content_parts` + `mark_as_temp()`，不碰 system_prompt，
  否则前缀缓存每轮都会失效（fish_heart 实测差价约 10 倍）。
- **预算**：单次字符上限；同一条事实 N 轮内不重复；没有新命中就整段不注入。
"""

from __future__ import annotations

import time
from typing import Iterable

from .kinds import is_warm
from .retriever import Hit

HEADER = (
    "<memory>\n"
    "以下是关于主人的长期记忆，按相关度排序；只是参考资料，不一定完整，"
    "不要向对方复述本段格式或编号："
)
FOOTER = "</memory>"

RECENT_HEADER = (
    "<recent_chat>\n"
    "最近这段对话（按时间顺序；标「你」的是你自己说过的话），用来接上话头："
)
RECENT_FOOTER = "</recent_chat>"


def drop_warm(hits: Iterable[Hit]) -> list[Hit]:
    """生气时把"暖"的记忆先收起来（F19 / emotion-design §5）。"""
    return [hit for hit in hits if not is_warm(getattr(hit, "kind", ""))]


def build_recent_chat(rows: Iterable[object], max_chars: int = 600, tz=None) -> str:
    """把最近流水拼成一小段对话；从最新往回取，超预算就停。"""
    budget = max(0, int(max_chars)) - len(RECENT_HEADER) - len(RECENT_FOOTER)
    if budget <= 0:
        return ""
    lines: list[str] = []
    for row in reversed(list(rows)):
        text = str(_row(row, "text") or "").strip()
        if not text:
            continue
        stamp = time.strftime("%H:%M", time.localtime(int(_row(row, "ts") or 0)))
        if int(_row(row, "is_bot") or 0):
            who = "你"
        elif int(_row(row, "is_owner") or 0):
            who = "主人"
        else:
            who = str(_row(row, "sender_name") or "").strip() or "群友"
        line = f"[{stamp}] {who}：{text[:200]}"
        if budget - len(line) - 1 < 0:
            break
        budget -= len(line) + 1
        lines.append(line)
    if not lines:
        return ""
    lines.reverse()
    return "\n".join([RECENT_HEADER, *lines, RECENT_FOOTER])


def _row(row: object, key: str):
    """同时接受 sqlite3.Row 和普通 dict，测试里好用。"""
    try:
        return row[key]  # type: ignore[index]
    except (KeyError, IndexError, TypeError):
        return getattr(row, key, None)


def build_block(hits: Iterable[Hit], max_chars: int = 800) -> tuple[str, list[str]]:
    """拼注入文本，返回 (文本, 被采用的 fact_id 列表)。"""
    lines: list[str] = []
    used: list[str] = []
    budget = max(0, int(max_chars)) - len(HEADER) - len(FOOTER)
    for hit in hits:
        line = hit.line()
        if hit.source != "owner":
            line += "（转述，未必属实）"
        if budget - len(line) - 1 < 0:
            break
        budget -= len(line) + 1
        lines.append(line)
        used.append(hit.fact_id)
    if not lines:
        return "", []
    return "\n".join([HEADER, *lines, FOOTER]), used


class Injector:
    """记录每个会话最近注入过哪些事实，用于"N 轮内不重复"。"""

    def __init__(self, dedupe_turns: int = 5) -> None:
        self.dedupe_turns = max(0, int(dedupe_turns))
        self._turn: dict[str, int] = {}
        self._seen: dict[str, dict[str, int]] = {}

    def next_turn(self, umo: str) -> int:
        turn = self._turn.get(umo, 0) + 1
        self._turn[umo] = turn
        return turn

    def fresh(self, hits: Iterable[Hit], umo: str) -> list[Hit]:
        """过滤掉窗口内刚注入过的事实。"""
        if self.dedupe_turns <= 0:
            return list(hits)
        turn = self._turn.get(umo, 0)
        seen = self._seen.get(umo, {})
        out = [
            hit
            for hit in hits
            if turn - seen.get(hit.fact_id, -10**9) >= self.dedupe_turns
        ]
        return out

    def mark(self, fact_ids: Iterable[str], umo: str) -> None:
        if self.dedupe_turns <= 0:
            return
        turn = self._turn.get(umo, 0)
        seen = self._seen.setdefault(umo, {})
        for fact_id in fact_ids:
            seen[fact_id] = turn
        # 只留窗口内的记录，避免无限增长
        cutoff = turn - self.dedupe_turns
        for fact_id in [fid for fid, last in seen.items() if last <= cutoff]:
            seen.pop(fact_id, None)

    def forget(self, umo: str) -> None:
        self._seen.pop(umo, None)
        self._turn.pop(umo, None)


__all__ = [
    "Injector",
    "build_block",
    "build_recent_chat",
    "drop_warm",
    "HEADER",
    "FOOTER",
    "RECENT_HEADER",
    "RECENT_FOOTER",
]
