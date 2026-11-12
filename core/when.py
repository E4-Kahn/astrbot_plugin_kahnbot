"""时间短语 → 本地时间戳。

只服务一件事：主人说"下周三体检""明晚八点提醒我"，把这句话里的时间抠出来，
变成 `fact_index.due_ts`。抠不出来就返回 None，绝不猜——提醒错了比不提醒更烦人。

支持的范围（够用即可，不做自然语言全解析）：

- 今天 / 明天 / 后天 / 大后天，今晚 / 明早 / 明晚
- 下周X / 下下周X / 这周X / 周X / 星期X / 礼拜X（不带限定词时取"最近的那个"）
- N 天后 / N 周后 / N 个月后（阿拉伯数字或中文数字）
- N 分钟后 / N 小时后 / 半小时后（相对现在，用于"过会儿提醒我"）
- M 月 D 日 / 号，下个月 D 号
- 时间：凌晨 / 早上 / 上午 / 中午 / 下午 / 傍晚 / 晚上 + N 点（半 / 一刻 / N 分），或 14:30

约定：

- 只给日期不给时间 → 09:00；只给"晚上"这类时段 → 该时段的默认钟点（见 `PERIOD_HOUR`）。
- 只给时间不给日期 → 今天，但已经过了就顺延到明天。
- 算出来在过去的日期（"昨天""3 月 1 日"且已过）→ None。
- 秒一律归零，提醒不需要秒级精度。
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, tzinfo

# 全角数字与冒号：微信群聊里很常见，先归一化再解析。
_FULLWIDTH = str.maketrans("０１２３４５６７８９：．", "0123456789:.")

# 时段 → 没写具体钟点时的默认小时。
PERIOD_HOUR: dict[str, int] = {
    "凌晨": 5,
    "早上": 8,
    "早晨": 8,
    "上午": 9,
    "中午": 12,
    "下午": 14,
    "傍晚": 18,
    "晚上": 20,
    "夜里": 22,
    "夜间": 22,
}

# 带日期的时段词：既是"哪一天"也是"几点"。
_COMPOSITE = {
    "今晚": (0, "晚上"),
    "今早": (0, "早上"),
    "明晚": (1, "晚上"),
    "明早": (1, "早上"),
}

_DAY_WORDS = {
    "大后天": 3,
    "后天": 2,
    "明天": 1,
    "明日": 1,
    "明儿": 1,
    "今天": 0,
    "今日": 0,
}

# 长词在前，避免"后天"吃掉"大后天"。
_DAY_RE = re.compile("|".join(sorted(_DAY_WORDS, key=len, reverse=True)))

_WEEKDAY_RE = re.compile(r"(下下|下|这|本)?\s*(?:周|星期|礼拜)\s*([一二三四五六日天七1-7])")
_WEEKDAY_INDEX = {
    "一": 0,
    "二": 1,
    "三": 2,
    "四": 3,
    "五": 4,
    "六": 5,
    "日": 6,
    "天": 6,
    "七": 6,
    "1": 0,
    "2": 1,
    "3": 2,
    "4": 3,
    "5": 4,
    "6": 5,
    "7": 6,
}

_REL_MINUTES_RE = re.compile(r"(半|\d{1,3}|[一二三四五六七八九十]{1,3})\s*个?\s*分钟\s*(?:之)?后")
_REL_HOURS_RE = re.compile(r"(半|\d{1,2}|[一二三四五六七八九十]{1,3})\s*个?\s*(?:小时|钟头)\s*(?:之)?后")
_REL_DAYS_RE = re.compile(r"(\d{1,2}|[一二三四五六七八九十]{1,3})\s*个?\s*(天|日)\s*(?:之)?后")
_REL_WEEKS_RE = re.compile(r"(\d{1,2}|[一二三四五六七八九十]{1,3})\s*个?\s*(?:周|星期|礼拜)\s*(?:之)?后")
_REL_MONTHS_RE = re.compile(r"(\d{1,2}|[一二三四五六七八九十]{1,3})\s*个?\s*月\s*(?:之)?后")

_MONTH_DAY_RE = re.compile(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")
_NEXT_MONTH_DAY_RE = re.compile(r"下个?月\s*(\d{1,2})\s*[日号]")

_CLOCK_RE = re.compile(
    r"(凌晨|早上|早晨|上午|中午|下午|傍晚|晚上|夜里|夜间|晚)\s*"
    r"(\d{1,2})\s*(?:点|时)\s*(?:(半|一刻|三刻|\d{1,2}\s*分))?"
)
_CLOCK_NO_PERIOD_RE = re.compile(r"(?<![\d:：])(\d{1,2})\s*(?:点|时)\s*(?:(半|一刻|三刻|\d{1,2}\s*分))?")
_COLON_RE = re.compile(r"(?<!\d)(\d{1,2})\s*[:：]\s*(\d{1,2})(?!\d)")
_PERIOD_ONLY_RE = re.compile(r"凌晨|早上|早晨|上午|中午|下午|傍晚|晚上|夜里|夜间")

_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
           "十一": 11, "十二": 12, "十三": 13, "十四": 14, "十五": 15, "二十": 20, "三十": 30}


def _to_int(raw: str) -> int | None:
    raw = raw.strip()
    if not raw:
        return None
    if raw.isdigit():
        return int(raw)
    return _CN_NUM.get(raw)


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        nxt = date(year + 1, 1, 1)
    else:
        nxt = date(year, month + 1, 1)
    return (nxt - date(year, month, 1)).days


def _add_months(base: date, months: int) -> date:
    month_index = base.month - 1 + months
    year = base.year + month_index // 12
    month = month_index % 12 + 1
    day = min(base.day, _days_in_month(year, month))
    return date(year, month, day)


def _clock(text: str) -> tuple[int, int] | None:
    """抠出钟点，返回 (hour, minute)；抠不出来返回 None。"""
    m = _CLOCK_RE.search(text)
    if m:
        period, hour_raw, minute_raw = m.group(1), int(m.group(2)), m.group(3)
        hour = hour_raw
        if period in ("下午", "傍晚", "晚上", "晚", "夜里", "夜间") and hour < 12:
            hour += 12
        elif period == "中午" and hour < 6:
            hour += 12
        elif period == "凌晨" and hour == 12:
            hour = 0
        minute = _minute(minute_raw)
        return hour, minute

    m = _COLON_RE.search(text)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute

    m = _CLOCK_NO_PERIOD_RE.search(text)
    if m:
        hour = int(m.group(1))
        if 0 <= hour <= 23:
            return hour, _minute(m.group(2))
    return None


def _minute(raw: str | None) -> int:
    if not raw:
        return 0
    raw = raw.strip()
    if raw == "半":
        return 30
    if raw == "一刻":
        return 15
    if raw == "三刻":
        return 45
    raw = raw.replace("分", "").strip()
    try:
        value = int(raw)
    except ValueError:
        return 0
    return value if 0 <= value <= 59 else 0


def parse_due(text: str, *, now: float | None = None, tz: tzinfo | None = None) -> int | None:
    """把一句话里的时间抠成时间戳；抠不出来或已经过期返回 None。"""
    if not text:
        return None
    text = text.translate(_FULLWIDTH)
    now_dt = datetime.fromtimestamp(now, tz) if now is not None else datetime.now(tz)
    today = now_dt.date()

    base: date | None = None
    weekday_loose = False  # 不带"这/下"的"周X"，今天过了可以顺延到下周

    # 0) N 分钟后 / N 小时后：相对现在，不按整点算（"10 分钟后提醒我关火"）
    soon = _relative_soon(text, now_dt)
    if soon is not None:
        return soon

    # 1) 今天 / 明天 / 大后天 / 今晚 / 明早 …
    composite = next((w for w in _COMPOSITE if w in text), None)
    if composite:
        offset, _period = _COMPOSITE[composite]
        base = today + timedelta(days=offset)
    else:
        m = _DAY_RE.search(text)
        if m:
            base = today + timedelta(days=_DAY_WORDS[m.group(0)])

    # 2) N 天后 / N 周后 / N 个月后
    if base is None:
        m = _REL_DAYS_RE.search(text)
        if m:
            n = _to_int(m.group(1))
            if n:
                base = today + timedelta(days=n)
    if base is None:
        m = _REL_WEEKS_RE.search(text)
        if m:
            n = _to_int(m.group(1))
            if n:
                base = today + timedelta(days=7 * n)
    if base is None:
        m = _REL_MONTHS_RE.search(text)
        if m:
            n = _to_int(m.group(1))
            if n:
                base = _add_months(today, n)

    # 3) 5 月 20 日 / 5 月 20 号
    if base is None:
        m = _MONTH_DAY_RE.search(text)
        if m:
            month, day = int(m.group(1)), int(m.group(2))
            base = _safe_date(today.year, month, day)
            if base is not None and base < today:
                base = _safe_date(today.year + 1, month, day)

    # 4) 下个月 5 号
    if base is None:
        m = _NEXT_MONTH_DAY_RE.search(text)
        if m:
            day = int(m.group(1))
            nxt = _add_months(today.replace(day=1), 1)
            if 1 <= day <= _days_in_month(nxt.year, nxt.month):
                base = nxt.replace(day=day)

    # 5) 周X / 下周X / 这周X
    if base is None:
        m = _WEEKDAY_RE.search(text)
        if m:
            qualifier = m.group(1) or ""
            target = _WEEKDAY_INDEX.get(m.group(2))
            if target is not None:
                if qualifier == "下下":
                    ahead = (target - today.weekday()) % 7 + 7
                elif qualifier == "下":
                    ahead = (target - today.weekday()) % 7
                    if ahead == 0:
                        ahead = 7
                elif qualifier in ("这", "本"):
                    ahead = (target - today.weekday()) % 7
                else:
                    ahead = (target - today.weekday()) % 7
                    weekday_loose = True
                base = today + timedelta(days=ahead)

    clock = _clock(text)

    # 时段词兜底：只写"晚上"不写几点。
    period = None
    if composite:
        period = _COMPOSITE[composite][1]
    if period is None:
        m = _PERIOD_ONLY_RE.search(text)
        if m:
            period = m.group(0)

    if base is None and clock is None and period is None:
        return None

    if clock is not None:
        hour, minute = clock
    else:
        hour = PERIOD_HOUR.get(period, 9) if period else 9
        minute = 0

    if base is None:
        # 只有钟点：今天没到就今天，过了就明天。
        base = today
        candidate = datetime.combine(base, datetime.min.time(), tzinfo=tz).replace(hour=hour, minute=minute)
        if candidate <= now_dt:
            base = today + timedelta(days=1)

    try:
        due = datetime.combine(base, datetime.min.time(), tzinfo=tz).replace(hour=hour, minute=minute)
    except ValueError:
        return None

    if due <= now_dt:
        if weekday_loose:
            due += timedelta(days=7)
        else:
            return None

    return int(due.timestamp())


def _relative_soon(text: str, now_dt: datetime) -> int | None:
    """N 分钟/N 小时后 → 时间戳；不是这种说法返回 None（交给后面的日期解析）。"""
    for regex, unit in ((_REL_MINUTES_RE, "minutes"), (_REL_HOURS_RE, "hours")):
        m = regex.search(text)
        if not m:
            continue
        raw = m.group(1)
        if raw == "半":
            delta = timedelta(minutes=30)
        else:
            n = _to_int(raw)
            if not n or n <= 0:
                return None
            delta = timedelta(minutes=n) if unit == "minutes" else timedelta(hours=n)
        mark = now_dt + delta
        return int(mark.replace(second=0, microsecond=0).timestamp())
    return None


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def format_due(ts: int, tz: tzinfo | None = None) -> str:
    """给指令输出用：2026-11-18 09:00。"""
    return datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d %H:%M")
