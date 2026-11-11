"""检索：字面 + 向量混合排序（`docs/refs/memory-design.md` 6.2）。

```
score = (0.6 * 余弦 + 0.4 * 字面) * (0.7 + 0.3 * 重要度/100) * 时间衰减 * 0.6(observed)
```

- 字面通道是兜底：人名、地名、数字向量容易糊，且 provider 不可用时只剩它。
- 重要度只影响排序；归档与否看 `importance < 60 且 30 天未命中`（F7）。
- 过滤：`archived=0`、`superseded_by IS NULL`、私聊事实只在主人上下文里可见（F9）。
"""

from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Iterable, Sequence

from .embeddings import EmbeddingClient, cosine, unpack_vector

CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
ALNUM_RUN = re.compile(r"[a-zA-Z0-9]+")

DECAY_DAYS = 30.0
OBSERVED_WEIGHT = 0.6


def tokens(text: str) -> set[str]:
    """中文按 bigram、英文数字按词切；查询与条目用同一套切法。"""
    lowered = (text or "").lower()
    out: set[str] = set()
    for run in CJK_RUN.findall(lowered):
        if len(run) == 1:
            out.add(run)
        for i in range(len(run) - 1):
            out.add(run[i : i + 2])
    for word in ALNUM_RUN.findall(lowered):
        if len(word) >= 2:
            out.add(word)
    return out


def literal_score(query_tokens: Iterable[str], text: str) -> float:
    query = set(query_tokens)
    if not query:
        return 0.0
    body = tokens(text)
    if not body:
        return 0.0
    return len(query & body) / len(query)


def decay_factor(row: sqlite3.Row, now: int) -> float:
    """`0.5 ** (距上次被提起天数 / 30)`；没被提起过就从创建时间算。"""
    basis = row["last_hit_ts"] or row["created_ts"] or now
    days = max(0.0, (now - float(basis)) / 86400.0)
    return 0.5 ** (days / DECAY_DAYS)


@dataclass(frozen=True, slots=True)
class Hit:
    fact_id: str
    text: str
    score: float
    source: str
    importance: int
    created_ts: int
    kind: str

    def line(self) -> str:
        when = time.strftime("%Y-%m-%d", time.localtime(int(self.created_ts or 0)))
        return f"- [{when}] {self.text}"


class Retriever:
    """把 store 里的事实排个序。同步 + 一个 async 入口。"""

    def __init__(self, store, embedder: EmbeddingClient | None = None) -> None:
        self.store = store
        self.embedder = embedder

    # ------------------------------------------------------------------ 打分

    @staticmethod
    def score_row(
        row: sqlite3.Row,
        *,
        query_tokens: set[str],
        query_vec: Sequence[float] | None,
        now: int,
    ) -> float:
        text = str(row["text"] or "")
        literal = literal_score(query_tokens, text)
        if query_vec:
            vector = unpack_vector(row["embedding"])
            cos = cosine(query_vec, vector) if vector else 0.0
            base = 0.6 * cos + 0.4 * literal
        else:
            base = literal
        if base <= 0.0:
            return 0.0
        importance = float(row["importance"] if row["importance"] is not None else 50)
        factor = 0.7 + 0.3 * max(0.0, min(100.0, importance)) / 100.0
        score = base * factor * decay_factor(row, now)
        if str(row["source"] or "owner") != "owner":
            score *= OBSERVED_WEIGHT
        return score

    # ------------------------------------------------------------------ 检索

    async def search(
        self,
        query: str,
        *,
        owner_context: bool,
        top_k: int = 5,
        min_score: float = 0.25,
    ) -> list[Hit]:
        query = (query or "").strip()
        if not query or self.store is None:
            return []
        query_tokens = tokens(query)
        query_vec = None
        if self.embedder is not None and self.embedder.resolve() is not None:
            query_vec = await self.embedder.embed(query)

        now = int(time.time())
        rows = self.store.facts()
        scored: list[tuple[float, sqlite3.Row]] = []
        for row in rows:
            visibility = str(row["visibility"] or "private")
            if visibility != "public" and not owner_context:
                continue
            score = self.score_row(
                row, query_tokens=query_tokens, query_vec=query_vec, now=now
            )
            if score > 0.0:
                scored.append((score, row))

        scored.sort(key=lambda item: item[0], reverse=True)
        hits: list[Hit] = []
        for score, row in scored[: max(1, int(top_k))]:
            if score < min_score:
                continue
            hits.append(
                Hit(
                    fact_id=str(row["fact_id"]),
                    text=str(row["text"]),
                    score=round(score, 4),
                    source=str(row["source"] or "owner"),
                    importance=int(row["importance"] or 50),
                    created_ts=int(row["created_ts"] or 0),
                    kind=str(row["kind"] or "fact"),
                )
            )
        if hits:
            try:
                self.store.touch_facts([hit.fact_id for hit in hits], now)
            except Exception:
                pass
        return hits

    # ------------------------------------------------------------------ 向量补齐

    async def ensure_embeddings(self, limit: int = 200) -> int:
        """给还没有向量的条目补算（导入历史、换 provider 后调用）。"""
        if self.embedder is None or self.embedder.resolve() is None or self.store is None:
            return 0
        done = 0
        for row in self.store.facts(include_archived=True):
            if done >= limit:
                break
            if row["embedding"]:
                continue
            vec = await self.embedder.embed(str(row["text"] or ""))
            if vec is None:
                continue
            from .embeddings import pack_vector

            self.store.set_fact_embedding(str(row["fact_id"]), pack_vector(vec))
            done += 1
        return done


__all__ = ["Hit", "Retriever", "literal_score", "tokens", "decay_factor"]
