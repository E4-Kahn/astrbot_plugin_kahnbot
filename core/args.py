"""指令参数解析。

AstrBot 的 `CommandFilter` 用 `" ".join(params)` 处理 `GreedyStr`，因此正文可以带空格；
但"重要度 80"这种尾缀不能作为独立位置参数（会被当成正文的一部分或者类型转换失败），
所以统一在这里从整段文字末尾把它剥出来。
"""

from __future__ import annotations

import re

_IMPORTANCE = re.compile(
    r"(?:重要度|权重|importance)\s*[:：=]?\s*(\d{1,3})\s*$",
    re.IGNORECASE,
)


def split_importance(raw: str) -> tuple[str, int | None]:
    """把 "…正文… 重要度 80" 拆成 ("…正文…", 80)；没有尾缀时返回 (正文, None)。"""

    text = (raw or "").strip()
    if not text:
        return "", None
    match = _IMPORTANCE.search(text)
    if match is None:
        return text, None
    value = max(0, min(int(match.group(1)), 100))
    body = text[: match.start()].rstrip(" \t，,、;；")
    return body, value
