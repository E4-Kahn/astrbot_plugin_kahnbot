"""情绪状态机（M3）。

纯本地数值模拟，不调用任何模型；心跳在外面驱动 `drift()`，
`main.py` 只负责把"主人说了什么"翻译成一次 `on_owner_text()`。

设计依据：`docs/refs/emotion-design.md` §2–§6、§9。
"""

from __future__ import annotations

import time
from typing import Any

DRIVES = (
    "attachment",
    "curiosity",
    "reflection",
    "duty",
    "social",
    "fatigue",
    "intimacy",
    "stress",
    "joy",
)

# 静息值：她一个人的时候会慢慢回到这里
BASE: dict[str, float] = {
    "attachment": 0.50,
    "curiosity": 0.50,
    "reflection": 0.40,
    "duty": 0.50,
    "social": 0.50,
    "fatigue": 0.20,
    "intimacy": 0.40,
    "stress": 0.20,
    "joy": 0.50,
}

# 每个 tick 向 BASE 回归的比例（越大越快回到静息）
RATES: dict[str, float] = {
    "attachment": 0.04,
    "curiosity": 0.12,
    "reflection": 0.08,
    "duty": 0.10,
    "social": 0.10,
    "fatigue": 0.20,
    "intimacy": 0.03,
    "stress": 0.18,
    "joy": 0.15,
}

# blame / expectation 不在 DRIVES 里，单独漂移
BLAME_RATE = 0.08
EXPECTATION_BASE = 0.30
EXPECTATION_RATE = 0.10

# 五档阈值（§3）。avoidant 不在这里判定——它由 M6 的"躲"逻辑进入。
AGGRIEVED_AT = 25.0
ANGRY_AT = 45.0
AFRAID_AT = 70.0
STRESS_AFRAID_AT = 0.80

# 主人情绪事件（§6）。顺序有意义：先道歉，再责骂，最后夸奖。
APOLOGY_WORDS = ("对不起", "抱歉", "我错了", "别生气", "消消气", "原谅", "不是故意", "抱抱", "乖")
SCOLD_WORDS = ("滚", "烦", "闭嘴", "讨厌你", "笨", "傻", "别说话", "不想理你", "走开")
PRAISE_WORDS = ("谢谢", "谢啦", "感谢", "真棒", "厉害", "喜欢你", "爱你", "辛苦", "么么")

MAX_LOG = 20


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def default_state(now: int | None = None) -> dict[str, Any]:
    """全新的一份情绪状态。"""
    ts = int(now if now is not None else time.time())
    return {
        "drives": dict(BASE),
        "blame": 0.0,
        "blame_target": None,
        "expectation": EXPECTATION_BASE,
        "mode": "daily",
        "last_tick_ts": ts,
        "last_owner_ts": 0,
        "last_proactive_ts": 0,
        "proactive_count_today": 0,
        "unanswered": [],
        "log": [],
    }


def coerce_state(raw: Any, now: int | None = None) -> dict[str, Any]:
    """把 state.json 读出来的东西补全成合法状态（旧版本缺字段也能跑）。"""
    state = default_state(now)
    if not isinstance(raw, dict):
        return state

    drives = raw.get("drives")
    if isinstance(drives, dict):
        for name in DRIVES:
            try:
                state["drives"][name] = _clamp(float(drives.get(name, BASE[name])))
            except (TypeError, ValueError):
                state["drives"][name] = BASE[name]

    for key in ("blame", "expectation"):
        try:
            value = float(raw.get(key, state[key]))
        except (TypeError, ValueError):
            value = state[key]
        state[key] = _clamp(value, 0.0, 100.0 if key == "blame" else 1.0)

    mode = raw.get("mode")
    if mode in {"daily", "aggrieved", "angry", "afraid", "avoidant"}:
        state["mode"] = mode

    for key in ("last_tick_ts", "last_owner_ts", "last_proactive_ts"):
        try:
            state[key] = int(raw.get(key, state[key]))
        except (TypeError, ValueError):
            pass

    try:
        state["proactive_count_today"] = int(raw.get("proactive_count_today", 0))
    except (TypeError, ValueError):
        pass

    unanswered = raw.get("unanswered")
    if isinstance(unanswered, list):
        state["unanswered"] = [item for item in unanswered if isinstance(item, dict)][-20:]

    log = raw.get("log")
    if isinstance(log, list):
        state["log"] = [item for item in log if isinstance(item, dict)][-MAX_LOG:]

    target = raw.get("blame_target")
    if isinstance(target, str) and target:
        state["blame_target"] = target

    return state


class Emotion:
    """一个会漂移的情绪状态。所有方法都是同步的、纯本地的。"""

    def __init__(self, state: dict[str, Any] | None = None, now: int | None = None) -> None:
        self.state = coerce_state(state, now)
        self.state["mode"] = self.mode

    # ---------------------------------------------------------------- 读

    @property
    def drives(self) -> dict[str, float]:
        return self.state["drives"]

    @property
    def blame(self) -> float:
        return float(self.state["blame"])

    @property
    def mode(self) -> str:
        """由 blame / stress 推导档位；avoidant 由上层显式设置，这里不产生。"""
        if self.state.get("mode") == "avoidant":
            return "avoidant"
        if self.blame > AFRAID_AT or self.drives["stress"] >= STRESS_AFRAID_AT:
            return "afraid"
        if self.blame >= ANGRY_AT:
            return "angry"
        if self.blame >= AGGRIEVED_AT:
            return "aggrieved"
        return "daily"

    def mood_label(self) -> str:
        return {
            "daily": "日常",
            "aggrieved": "委屈",
            "angry": "生气",
            "afraid": "害怕",
            "avoidant": "回避",
        }.get(self.mode, self.mode)

    # ---------------------------------------------------------------- 漂移

    def drift(self, missed: int) -> None:
        """按错过的 tick 数，用闭式解一次性算完（不循环）。"""
        n = max(0, min(96, int(missed)))
        if n == 0:
            return
        for name in DRIVES:
            value = self.drives[name]
            rate = RATES[name]
            self.drives[name] = _clamp(BASE[name] + (value - BASE[name]) * (1 - rate) ** n)
        self.state["blame"] = max(0.0, self.blame * (1 - BLAME_RATE) ** n)
        self.state["expectation"] = _clamp(
            EXPECTATION_BASE + (self.state["expectation"] - EXPECTATION_BASE) * (1 - EXPECTATION_RATE) ** n
        )
        if self.state["blame"] < 0.5:
            self.state["blame"] = 0.0
            self.state["blame_target"] = None
        self.state["mode"] = self.mode

    # ---------------------------------------------------------------- 情绪事件

    def on_owner_seen(self, now: int | None = None) -> None:
        self.state["last_owner_ts"] = int(now if now is not None else time.time())

    def on_owner_text(self, text: str, now: int | None = None) -> str | None:
        """主人说话时调用；返回触发的情绪事件名（没有就 None）。"""
        self.on_owner_seen(now)
        value = (text or "").strip()
        if not value:
            return None

        if _hit(value, APOLOGY_WORDS):
            self._bump(joy=0.05)
            self.state["blame"] = max(0.0, self.blame - 30)
            return self._record("道歉/哄", now)
        if _hit(value, SCOLD_WORDS):
            self._bump(joy=-0.05)
            self.state["blame"] = _clamp(self.blame + 15, 0.0, 100.0)
            return self._record("责骂", now)
        if _hit(value, PRAISE_WORDS):
            self._bump(joy=0.10, social=0.05)
            self.state["blame"] = max(0.0, self.blame - 5)
            return self._record("夸奖/道谢", now)
        return None

    def on_poke(self, now: int | None = None) -> None:
        """被拍一拍：被注意到是开心的；生气时被戳更烦（§9）。"""
        self._bump(social=0.05, joy=0.05)
        if self.mode == "angry":
            self.state["blame"] = _clamp(self.blame + 3, 0.0, 100.0)
        self.state["mode"] = self.mode
        self._record("被拍一拍", now, quiet=True)

    def on_miss(self, days: float) -> None:
        """主人好多天没说话 → 想念（§6 第一条）。"""
        if days <= 0:
            return
        self.drives["social"] = _clamp(self.drives["social"] + 0.1 * min(days, 7), 0.0, 0.8)
        self.drives["attachment"] = _clamp(self.drives["attachment"] + 0.03 * min(days, 7))
        self.state["mode"] = self.mode

    # ---------------------------------------------------------------- 呈现

    def render(self) -> str | None:
        """给模型的自然语言情绪提示；日常且没有明显偏离时返回 None（§8）。"""
        mode = self.mode
        notable = self._notable()
        if mode == "daily" and not notable:
            return None

        mood = {
            "daily": "你今天心情还算平稳",
            "aggrieved": "你今天有点委屈",
            "angry": "你现在还在生气",
            "afraid": "你有点被吓到，还不太敢多说",
            "avoidant": "你现在不太想说话",
        }[mode]
        reason = f"（{'；'.join(notable)}）" if notable else ""
        tail = {
            "daily": "，正常聊就好。",
            "aggrieved": "，语气会低一点，但仍然愿意聊。",
            "angry": "，不太想主动搭话，回答也会短。",
            "afraid": "，话少一点，别开玩笑。",
            "avoidant": "，能少说就少说。",
        }[mode]
        return f"<emotion>\n{mood}{reason}{tail}\n</emotion>"

    def describe(self) -> list[str]:
        """`/kahn mood` 用：九维进度条 + 档位 + 最近变化。"""
        lines = [f"心情：{self.mood_label()}（blame {self.blame:.1f}）"]
        for name in DRIVES:
            value = self.drives[name]
            filled = int(round(value * 10))
            bar = "█" * filled + "░" * (10 - filled)
            lines.append(f"{name:<10} {bar} {value:.2f}")
        if self.state["expectation"] > EXPECTATION_BASE + 0.05:
            lines.append(f"期待       {self.state['expectation']:.2f}")
        log = self.state.get("log") or []
        if log:
            last = log[-1]
            when = time.strftime("%m-%d %H:%M", time.localtime(int(last.get("ts", 0))))
            lines.append(f"最近变化：{when} {last.get('reason', '')} → {last.get('mode', '')}")

        for line in (self.render() or "").splitlines():
            if line.startswith("<") or not line.strip():
                continue
            lines.append(f"自述：{line.strip()}")
        return lines

    def snapshot(self) -> dict[str, Any]:
        self.state["mode"] = self.mode
        return self.state

    # ---------------------------------------------------------------- 内部

    def _bump(self, **deltas: float) -> None:
        for name, delta in deltas.items():
            if name in self.drives:
                self.drives[name] = _clamp(self.drives[name] + delta)
        self.state["mode"] = self.mode

    def _notable(self) -> list[str]:
        notes: list[str] = []
        if self.drives["social"] > 0.70:
            notes.append("主人好几天没来说话了")
        if self.state["expectation"] > 0.55:
            notes.append("心里惦记着快到的事")
        if self.drives["stress"] > 0.60:
            notes.append("最近有点绷着")
        if self.drives["joy"] > 0.75:
            notes.append("刚才挺开心的")
        if self.drives["fatigue"] > 0.60:
            notes.append("有点累")
        if self.blame >= AGGRIEVED_AT:
            notes.append("还在生主人的气")
        return notes

    def _record(self, reason: str, now: int | None, quiet: bool = False) -> str:
        self.state["mode"] = self.mode
        if not quiet:
            entry = {
                "ts": int(now if now is not None else time.time()),
                "reason": reason,
                "mode": self.state["mode"],
                "blame": round(self.blame, 1),
            }
            log = self.state.setdefault("log", [])
            log.append(entry)
            del log[:-MAX_LOG]
        return reason


def _hit(text: str, words: tuple[str, ...]) -> bool:
    return any(word in text for word in words)
