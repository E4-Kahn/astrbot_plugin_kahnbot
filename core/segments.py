"""把一条回复拆成几条，并按打字节奏算间隔。

为什么不用 AstrBot 原生的分段回复：它只按标点机械切、没有条数上限、
全局对所有回复生效，而且间隔固定 1.5~3.5 秒，不看字数。这里的目标是
"像人一样想发几条发几条"——条数有上限，短回复不拆，代码和长内容不拆，
间隔按字数估打字时间。
"""

from __future__ import annotations

import re

#: 总长低于这个数就不拆——"嗯""好呀""知道啦"发一条最自然
MIN_CHARS = 8

#: 总长超过这个数也不拆：多半是长解释、清单或代码，拆开反而难读
MAX_CHARS = 300

#: 一句话短于这个长度时，并到下一句里，避免蹦出"好。"这种半个字的气泡
MIN_BUBBLE = 4

#: 每条气泡的基准间隔与每字增量（秒），对齐参考项目"约每秒 9 字"
BASE_DELAY = 0.7
PER_CHAR_DELAY = 0.09
MAX_DELAY = 3.5
JITTER = 0.4

_FENCE = "```"
_ENDERS = "。！？!?~…；;"
_SPLIT_RE = re.compile(rf"([^{_ENDERS}]*[{_ENDERS}]+|[^{_ENDERS}]+$)")


def split_bubbles(
    text: str,
    *,
    max_bubbles: int = 3,
    min_chars: int = MIN_CHARS,
    max_chars: int = MAX_CHARS,
) -> list[str]:
    """把一段回复拆成 1~max_bubbles 条。拆不动就原样返回一条。"""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) < min_chars or len(text) > max_chars or _FENCE in text:
        return [text]

    bubbles: list[str] = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        line_pieces = [p.strip() for p in _SPLIT_RE.findall(line) if p.strip()]
        if not line_pieces:
            continue
        merged: list[str] = []
        for piece in line_pieces:
            # 同一行里太短的句子并到下一句；换行是模型自己选的断点，不合并
            if merged and len(merged[-1]) < MIN_BUBBLE:
                merged[-1] += piece
            else:
                merged.append(piece)
        bubbles.extend(merged)

    # 超出的并进最后一条，不丢字
    if len(bubbles) > max_bubbles:
        head = bubbles[: max_bubbles - 1]
        tail = "".join(bubbles[max_bubbles - 1 :])
        bubbles = head + [tail]

    return [b for b in bubbles if b] or [text]


def bubble_delay(text: str, *, jitter: float = 0.0) -> float:
    """按字数估"打这条要多久"；jitter 传 0~JITTER 的随机量，测试时传 0。"""
    base = BASE_DELAY + PER_CHAR_DELAY * len(text or "")
    return max(BASE_DELAY, min(MAX_DELAY, base)) + max(0.0, min(JITTER, jitter))


__all__ = ["split_bubbles", "bubble_delay", "MIN_CHARS", "MAX_CHARS", "MAX_DELAY"]
