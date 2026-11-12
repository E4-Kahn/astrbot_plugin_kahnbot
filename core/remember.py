"""「记住…」的三层判定。

F4 定的三层：唤醒层（私聊常驻 / 群里被叫到）→ 句式层（触发词在句首或前 6 字内，
且不是疑问 / 应答 / 转述）→ 回执层（写入后回一句"好，我记下了"）。

刻意不做形态闸门、也不让 LLM 判断"这句是不是要我记"：群聊里误判的代价是
把她随口一句话写进长期记忆，比漏记更难收拾。句式层宁可窄一点。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .when import parse_due

# 长词排前面："帮我记住"要命中"记住"，但"记住"和"记一下"谁先出现都能剥干净。
TRIGGERS: tuple[str, ...] = (
    "别忘了",
    "不要忘了",
    "提醒一下",
    "提醒我",
    "帮我记住",
    "帮我记一下",
    "帮我记",
    "记一下",
    "记下来",
    "记住",
    "记着",
    "记下",
)

_REMIND_TRIGGERS = ("别忘了", "不要忘了", "提醒一下", "提醒我")

# 应答：不是要她记，是在汇报"我记住了"。
_RESPONSE_PREFIX = ("我记住", "我记着", "记住了", "记着呢", "你记住", "全记住", "都记住")
_RESPONSE_SUFFIX = ("了没", "没有", "了吗")

# 转述：别人说的话不等于主人要交代的事。
_RELAY_PREFIX = ("他说", "她说", "文章说", "听说", "有人说", "群里说", "书里说", "微博说", "网上说")

_QUESTION_SUFFIX = ("吗", "么", "没", "?", "？")
_TRAILING = " \t\r\n，,。.！!？?、：:；;~～…"

_PREFIX_CUT = " \t\r\n，,。.！!？?、：:；;~～…"


@dataclass(frozen=True, slots=True)
class RememberIntent:
    """判定结果：要记的正文、解析出的到期时间（可能没有）、命中的触发词。"""

    text: str
    due_ts: int | None
    trigger: str
    kind: str = "remember"

    @property
    def is_remind(self) -> bool:
        return self.kind == "remind"


def strip_wake_prefix(text: str, wake_names: tuple[str, ...] = ()) -> str:
    """去掉开头的"鱼宝，"/"@鱼宝"这类叫名字的前缀，留正文。"""
    cleaned = text.strip()
    # 有些平台的消息正文会带 [At:qq] / @昵称，先扒掉。
    cleaned = re.sub(r"^(\[At:[^\]]+\]|@\S{1,20})\s*", "", cleaned)
    for _ in range(3):
        before = cleaned
        cleaned = cleaned.lstrip(_PREFIX_CUT)
        for name in wake_names:
            if name and cleaned.startswith(name):
                cleaned = cleaned[len(name):]
        if cleaned == before:
            break
    return cleaned.lstrip(_PREFIX_CUT)


def _looks_question(text: str) -> bool:
    stripped = text.rstrip(_TRAILING)
    if "？" in text or "?" in text:
        return True
    return stripped.endswith(_QUESTION_SUFFIX)


def _looks_response(text: str) -> bool:
    stripped = text.rstrip(_TRAILING)
    if stripped.startswith(_RESPONSE_PREFIX):
        return True
    return stripped.endswith(_RESPONSE_SUFFIX)


def _looks_relay(text: str) -> bool:
    stripped = text.lstrip(_PREFIX_CUT)
    return stripped.startswith(_RELAY_PREFIX)


def detect(
    text: str,
    *,
    woken: bool,
    private: bool = False,
    wake_names: tuple[str, ...] = (),
    now: float | None = None,
    tz=None,
) -> RememberIntent | None:
    """判断这句话是不是"记住…"。`woken` 为 False 时直接不认（唤醒层）。"""
    if not woken or not text:
        return None
    cleaned = strip_wake_prefix(text, wake_names)
    if not cleaned:
        return None
    if private:
        # 私聊本身就是常驻唤醒；这个参数只用于可读性，判定逻辑相同。
        pass
    if _looks_question(cleaned) or _looks_response(cleaned) or _looks_relay(cleaned):
        return None

    hit_at = -1
    trigger = ""
    for word in TRIGGERS:
        idx = cleaned.find(word)
        if idx == -1:
            continue
        if hit_at == -1 or idx < hit_at:
            hit_at, trigger = idx, word
        elif idx == hit_at and len(word) > len(trigger):
            trigger = word
    if hit_at == -1 or hit_at > 5:
        return None

    body = cleaned[hit_at + len(trigger):].lstrip(_PREFIX_CUT)
    body = body.rstrip(_TRAILING)
    if not body:
        return None
    # "记住我"不是一件事；太短的多半是语气，不是内容。
    if len(body) < 2:
        return None

    due = parse_due(cleaned, now=now, tz=tz)
    kind = "remind" if trigger in _REMIND_TRIGGERS else "remember"
    return RememberIntent(text=body, due_ts=due, trigger=trigger, kind=kind)
