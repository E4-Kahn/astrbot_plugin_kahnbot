"""定时汇总（F4 的第二条写入路径）。

输入是 `message` 表里水位之后的增量流水，输出是几条 `{text, kind, confidence}`。
三条护栏：

- `is_bot = 1` 的话只当上下文，不参与提炼（否则她会把自己的话记成事实）；
- `confidence < 0.5` 丢弃；
- 单次最多 10 条，批内 md5 去重，跨批靠 `MemoryWriter` 的去重。

水位（`meta.last_summary_ts`）只在拿到合法 JSON 后前移：模型抽风时同一批下轮重来，
靠 md5 去重兜底，不会写重。
"""

from __future__ import annotations

import logging
import time

from .llm import extract_json

logger = logging.getLogger(__name__)

SUMMARY_SYSTEM = (
    "你是一个记忆整理助手。你会收到一段群聊/私聊记录，从中挑出**长期有用**的信息。\n"
    "只抽这些：偏好、计划、关系、承诺、关键日期、身份信息。\n"
    "不抽：寒暄、情绪、一次性提问、玩笑、没有主语无法独立成句的碎片。\n"
    "每条必须能独立读懂（把'他''那个'换成具体的人或事）。不许编造记录里没有的内容。\n"
    "只输出 JSON 数组，不要解释。格式：\n"
    '[{"text": "一句话事实", "kind": "plan|preference|relation|anniversary|fact", '
    '"source": "owner|third_party", "confidence": 0.0}]\n'
    "source 按原说话人判：主人说的 owner，其他人说的 third_party。"
    "生日、纪念日、周年这类用 anniversary，并在 text 里保留日期。没有可记的就输出 []。"
)

VALID_KINDS = {"plan", "preference", "relation", "anniversary", "fact"}
WATERMARK_KEY = "last_summary_ts"
MAX_ROWS = 400


def _line(row) -> str:
    who = "主人" if row["is_owner"] else "群友"
    when = time.strftime("%m-%d %H:%M", time.localtime(int(row["ts"])))
    text = " ".join(str(row["text"]).split())
    return f"[{when}] {who}({row['sender_id']})：{text}"


def build_prompt(rows, max_chars: int) -> str:
    """把流水拼成提示词；超预算时丢最早的，保留最近的上下文。"""
    lines = [_line(row) for row in rows]
    kept: list[str] = []
    budget = max(200, int(max_chars))
    for line in reversed(lines):
        if kept and sum(len(x) + 1 for x in kept) + len(line) > budget:
            break
        kept.append(line)
    kept.reverse()
    return "以下是最近的聊天记录，请提炼长期记忆：\n" + "\n".join(kept)


class Summarizer:
    def __init__(self, store, writer, llm, cfg) -> None:
        self.store = store
        self.writer = writer
        self.llm = llm
        self.cfg = cfg

    # ------------------------------------------------------------------ 调度

    def last_ts(self) -> int:
        raw = self.store.get_meta(WATERMARK_KEY)
        try:
            return int(raw or 0)
        except (TypeError, ValueError):
            return 0

    async def maybe_run(self, now: int | None = None) -> bool:
        """心跳 tick 里调用：到点才跑，没新消息不动水位。"""
        if not self.cfg.summary_enabled or self.llm is None:
            return False
        now = int(now if now is not None else time.time())
        last = self.last_ts()
        if last <= 0:
            # 第一次启动不回头啃整本历史，从当下开始计水位。
            self.store.set_meta(WATERMARK_KEY, str(now))
            return False
        if now - last < self.cfg.summary_interval_hours * 3600:
            return False
        stats = await self.run(now=now)
        return bool(stats.get("written"))

    # ------------------------------------------------------------------ 执行

    async def run(self, *, force: bool = False, now: int | None = None) -> dict:
        now = int(now if now is not None else time.time())
        if not self.cfg.summary_enabled:
            return {"rows": 0, "written": 0, "reason": "disabled"}
        last = self.last_ts()
        since = last if last > 0 else now - 12 * 3600
        rows = self.store.messages_since(since, limit=MAX_ROWS, include_bot=False)
        if not rows:
            return {"rows": 0, "written": 0, "reason": "no rows"}
        if self.llm is None:
            return {"rows": len(rows), "written": 0, "reason": "no llm"}

        prompt = build_prompt(rows, self.cfg.summary_max_chars)
        reply = await self.llm.text(prompt, system_prompt=SUMMARY_SYSTEM)
        data = extract_json(reply) if reply else None
        if not isinstance(data, list):
            logger.warning("汇总失败：模型没有返回合法 JSON 数组（水位不前移，下轮重试）")
            return {"rows": len(rows), "written": 0, "reason": "bad json"}

        has_owner = any(row["is_owner"] for row in rows)
        scope, umo = self._common_scope(rows)
        written = 0
        seen: set[str] = set()
        items = []
        for raw in data:
            item = self._clean_item(raw, has_owner)
            if item is None:
                continue
            key = " ".join(item["text"].split())
            if key in seen:
                continue
            seen.add(key)
            items.append(item)

        items.sort(key=lambda x: x["confidence"], reverse=True)
        for item in items[: self.cfg.summary_max_items]:
            owner = item["source"] == "owner"
            result = await self.writer.add(
                text=item["text"],
                source="owner" if owner else "observed",
                kind=item["kind"],
                scope=scope,
                speaker="主人" if owner else "群友",
                uid="",
                umo=umo,
                visibility="private" if owner else "public",
                near_duplicate="skip",
                now=now,
            )
            if result.reason == "added":
                written += 1

        # 只有拿到合法 JSON 才前移水位；抽到 0 条也算这一批处理完了。
        self.store.set_meta(WATERMARK_KEY, str(max(int(row["ts"]) for row in rows)))
        logger.info(
            "汇总完成：%s 条流水 → 候选 %s 条 → 写入 %s 条（%s）",
            len(rows),
            len(items),
            written,
            "force" if force else "auto",
        )
        return {"rows": len(rows), "items": len(items), "written": written, "reason": "ok"}

    # ------------------------------------------------------------------ 辅助

    def _common_scope(self, rows) -> tuple[str, str]:
        counts: dict[tuple[str, str], int] = {}
        for row in rows:
            group_id = row["group_id"]
            key = (f"群:{group_id}", row["umo"]) if group_id else ("私聊", row["umo"])
            counts[key] = counts.get(key, 0) + 1
        if not counts:
            return "私聊", ""
        (scope, umo), _ = max(counts.items(), key=lambda kv: kv[1])
        return scope, umo

    def _clean_item(self, raw, has_owner: bool) -> dict | None:
        if not isinstance(raw, dict):
            return None
        text = " ".join(str(raw.get("text") or "").split())
        if len(text) < 3:
            return None
        try:
            confidence = float(raw.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        if confidence < self.cfg.summary_min_confidence:
            return None
        kind = str(raw.get("kind") or "fact").strip().lower()
        if kind not in VALID_KINDS:
            kind = "fact"
        source = "owner" if str(raw.get("source") or "").strip().lower() == "owner" else "third_party"
        # 这一批里根本没有主人说话，就不许模型把群友的话标成 owner。
        if source == "owner" and not has_owner:
            source = "third_party"
        return {"text": text, "kind": kind, "source": source, "confidence": confidence}
