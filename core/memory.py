"""记忆写入的一处出口。

四条路径最后都落到这里：`/kahn memory add`、群聊/私聊里识别到的"记住…"、
定时汇总、以及将来的其它入口。它负责：

- md5 精确去重（同一条事实不写两次）；
- 余弦 > 0.92 的近似处理：显式写入算"取代"，汇总算"重复跳过"；
- 双写（md + fact_index），md 是权威；
- 重要度、到期时间、归档、删除。

md 写失败不回滚数据库：索引可以重建，丢一条 md 行比丢一条记忆轻。
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass

from .embeddings import cosine, pack_vector, unpack_vector
from .llm import score_importance

logger = logging.getLogger(__name__)

NEAR_DUPLICATE = 0.92
DEFAULT_IMPORTANCE = 50
ARCHIVE_DAYS = 30
ARCHIVE_IMPORTANCE = 60


@dataclass(frozen=True, slots=True)
class WriteResult:
    fact_id: str = ""
    reason: str = "added"  # added / duplicate / near_duplicate / skipped
    superseded: str = ""


def fact_hash(text: str) -> str:
    return hashlib.md5(" ".join(text.split()).encode("utf-8")).hexdigest()


def layer_of(source: str) -> str:
    return "owner" if source == "owner" else "observed"


class MemoryWriter:
    def __init__(self, store, files, embedder, cfg, llm=None) -> None:
        self.store = store
        self.files = files
        self.embedder = embedder
        self.cfg = cfg
        self.llm = llm

    # ------------------------------------------------------------------ 写入

    async def add(
        self,
        *,
        text: str,
        source: str = "owner",
        kind: str = "fact",
        importance: int | None = None,
        due_ts: int | None = None,
        scope: str = "私聊",
        speaker: str = "主人",
        uid: str = "",
        umo: str = "",
        visibility: str = "private",
        near_duplicate: str = "supersede",
        now: int | None = None,
    ) -> WriteResult:
        text = " ".join((text or "").split())
        if len(text) < 2:
            return WriteResult(reason="skipped")
        now = int(now if now is not None else time.time())
        layer = layer_of(source)
        digest = fact_hash(text)

        existing = self.store.fact_by_hash(digest)
        if existing is not None:
            if near_duplicate == "skip":
                self.store.touch_facts([existing["fact_id"]], now)
                return WriteResult(existing["fact_id"], "duplicate")
            return WriteResult(existing["fact_id"], "duplicate")

        vec = await self._embed(text)
        replaced = ""
        if vec is not None:
            near = self._nearest(vec, kind)
            if near is not None:
                if near_duplicate == "skip":
                    self.store.touch_facts([near["fact_id"]], now)
                    return WriteResult(near["fact_id"], "near_duplicate")
                replaced = near["fact_id"]

        fact_id = digest[:12]
        if self.store.get_fact(fact_id) is not None:
            fact_id = digest[:8] + hashlib.md5(text.encode() + b"#").hexdigest()[:4]

        # 只有 owner 层、且开启提醒时才留 due_ts；observed 永不提醒（F21）。
        due = int(due_ts) if (due_ts and layer == "owner" and self.cfg.reminder_enabled) else None
        importance_value = DEFAULT_IMPORTANCE if importance is None else max(0, min(100, int(importance)))

        row_id = self.store.add_fact(
            fact_id=fact_id,
            text=text,
            hash_=digest,
            scope=scope,
            source=source,
            visibility=visibility,
            kind=kind,
            importance=importance_value,
            created_ts=now,
            embedding=pack_vector(vec) if vec is not None else None,
            due_ts=due,
            umo=umo,
        )
        if row_id is None:
            return WriteResult(reason="duplicate")

        self._append_md(
            layer,
            fact_id=fact_id,
            text=text,
            scope=scope,
            speaker=speaker,
            uid=uid,
            importance=importance_value,
            date=time.strftime("%Y-%m-%d", time.localtime(now)),
        )

        if replaced:
            self._replace_old(replaced, fact_id)
        return WriteResult(fact_id, "added", replaced)

    def _append_md(self, layer: str, **entry) -> None:
        try:
            self.files.append(layer, {"layer": layer, **entry})
        except Exception:  # noqa: BLE001 - md 丢了还能改，别让写入整条失败
            logger.exception("写 md 失败（%s）", layer)

    def _replace_old(self, old_id: str, new_id: str) -> None:
        old = self.store.get_fact(old_id)
        if old is None:
            return
        self.store.set_superseded(old_id, new_id)
        old_layer = layer_of(old["source"])
        try:
            self.files.update(old_layer, old_id, superseded_by=new_id)
            self.files.remove_schedule(old_id)
        except Exception:  # noqa: BLE001
            logger.exception("标记旧条目失败（%s）", old_id)
        if old["due_ts"]:
            self.store.clear_due(old_id)

    async def _embed(self, text: str) -> list[float] | None:
        if self.embedder is None:
            return None
        try:
            vec = await self.embedder.embed(text)
        except Exception:  # noqa: BLE001 - 向量拿不到就退化为纯字面
            logger.debug("向量化失败：%s", text[:20])
            return None
        return vec or None

    def _nearest(self, vec: list[float], kind: str):
        best = None
        best_score = NEAR_DUPLICATE
        for row in self.store.facts():
            if row["kind"] != kind or not row["embedding"]:
                continue
            try:
                other = unpack_vector(row["embedding"])
            except Exception:  # noqa: BLE001
                continue
            score = cosine(vec, other)
            if score > best_score:
                best, best_score = row, score
        return best

    # ------------------------------------------------------------------ 维护

    async def rescore(self, fact_id: str) -> int | None:
        """后台补打分：写入时没拿到分数（或手动加的）用它补。失败保持原值。"""
        if self.llm is None or not self.cfg.score_importance:
            return None
        row = self.store.get_fact(fact_id)
        if row is None:
            return None
        value = await score_importance(self.llm, row["text"])
        if value is None:
            return None
        self.store.set_importance(fact_id, value)
        try:
            self.files.update(layer_of(row["source"]), fact_id, importance=value)
        except Exception:  # noqa: BLE001
            logger.exception("更新 md 重要度失败（%s）", fact_id)
        return value

    async def maintain(self, now: int | None = None) -> int:
        """F7 归档：30 天没被提起、重要度 < 60、没有待提醒的，标已归档。"""
        now = int(now if now is not None else time.time())
        rows = self.store.archivable(now, days=ARCHIVE_DAYS, max_importance=ARCHIVE_IMPORTANCE)
        count = 0
        for row in rows:
            self.store.set_archived(row["fact_id"], True)
            try:
                self.files.update(layer_of(row["source"]), row["fact_id"], archived=True)
            except Exception:  # noqa: BLE001
                logger.exception("标记归档失败（%s）", row["fact_id"])
            count += 1
        if count:
            logger.info("归档了 %s 条长期没提起的记忆", count)
        return count

    async def drop(self, fact_id: str) -> bool:
        row = self.store.get_fact(fact_id)
        if row is None:
            return False
        self.store.delete_fact(fact_id)
        try:
            self.files.remove(layer_of(row["source"]), fact_id)
            self.files.remove_schedule(fact_id)
        except Exception:  # noqa: BLE001
            logger.exception("删除 md 行失败（%s）", fact_id)
        return True


__all__ = ["MemoryWriter", "WriteResult", "fact_hash", "layer_of"]
