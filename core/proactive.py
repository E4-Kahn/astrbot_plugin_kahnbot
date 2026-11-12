"""主动说话（M6，F16 情绪概率路）。

规则（refs/emotion-design.md §10）：

- 闸门顺序：总开关 → 主动说话开关 → 非回避档 → 不在免打扰时段 → 冷却 → 日限 → 沉默时长 → 掷骰；
- 冷却默认 6 小时、日限 2 条、沉默至少 10 分钟；
- 说话对象取最近一条流水所在的会话（她在哪儿说过话就在哪儿开口）；
- 文案由模型基于当前情绪 + 最近记下的事生成一句，失败时退回模板。

"记忆确定路"（到期日程 / 纪念日）由 `core/reminder.py` 在准确时刻发，
不在这里重复一遍——同一天说两遍比漏说更烦。
"""

from __future__ import annotations

import inspect
import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from .config import Config

logger = logging.getLogger(__name__)

# 免打扰时段：23 点到次日 8 点不主动开口（她也要睡觉）
QUIET_START = 23
QUIET_END = 8

DEFAULT_COOLDOWN_HOURS = 6
DEFAULT_DAILY_LIMIT = 2
DEFAULT_SILENCE_MINUTES = 10

# 情绪越高越想说；低于这个值连骰子都不掷
BASE_PROBABILITY = 0.15
ATTACHMENT_WEIGHT = 0.35
SOCIAL_WEIGHT = 0.25
EXPECTATION_WEIGHT = 0.35

FALLBACK_LINES = (
    "在忙吗？我刚刚突然想到你。",
    "今天过得怎么样呀。",
    "忽然有点想跟你说说话。",
)

PROACTIVE_SYSTEM = (
    "你是一个叫 KahnBot 的群友，正在用聊天软件和人聊天。"
    "现在是你主动开口——突然想到对方，想发一条消息。"
    "要求：只输出要发的那一两句话本身，不要解释、不要加引号、不要自称 AI；"
    "语气自然，像很熟的朋友随手发的一条，不要客套，不要问“有什么可以帮你”。"
)


@dataclass(frozen=True, slots=True)
class ProactivePlan:
    """一次主动说话的判定结果（不含文案）。"""

    kind: str  # emotion / fallback
    umo: str
    reason: str


class Proactive:
    """情绪概率路的主动说话。所有判定都是同步的，只有生成文案是异步的。"""

    def __init__(
        self,
        *,
        store,
        emotion,
        llm,
        cfg: Config,
        send: Callable[[str, str], Any],
        tz=None,
        on_sent: Callable[[str, str], Any] | None = None,
        clock: Callable[[], float] = time.time,
        rand: Callable[[], float] = random.random,
    ) -> None:
        self.store = store
        self.emotion = emotion
        self.llm = llm
        self.cfg = cfg
        self.send = send
        self.on_sent = on_sent
        self.tz = tz
        self.clock = clock
        self.rand = rand
        self._busy = False

    # ------------------------------------------------------------------ 判定

    def decide(self, now: float | None = None) -> ProactivePlan | None:
        """闸门顺序照 fish_heart；通过则返回计划，否则 None。"""
        if not self.cfg.proactive_enabled or self.store is None or self.emotion is None:
            return None
        if self.emotion.mode == "avoidant":
            return None  # 闹脾气时不想说话，也不主动说话

        now = float(now if now is not None else self.clock())
        local = self._local(now)
        if local.hour >= QUIET_START or local.hour < QUIET_END:
            return None

        state = self.emotion.state
        if now - float(state.get("last_proactive_ts") or 0) < self._cooldown_seconds():
            return None
        if self._daily_count(state, local) >= self.cfg.proactive_daily_limit:
            return None

        silence = now - float(state.get("last_owner_ts") or 0)
        if silence < self.cfg.proactive_silence_minutes * 60:
            return None

        # 说话的地方：最近一条流水所在的会话
        row = self._last_row()
        if row is None:
            return None
        umo = str(row["umo"] or "")
        if not umo:
            return None

        chance = self._chance()
        if chance <= 0:
            return None
        if self.rand() >= chance:
            return None
        return ProactivePlan(kind="emotion", umo=umo, reason=f"想念 {chance:.2f}")

    def _chance(self) -> float:
        drives = self.emotion.drives
        chance = (
            BASE_PROBABILITY
            + ATTACHMENT_WEIGHT * drives.get("attachment", 0.0)
            + SOCIAL_WEIGHT * drives.get("social", 0.0)
            + EXPECTATION_WEIGHT * float(self.emotion.state.get("expectation") or 0.0)
        )
        # 封顶：不能因为一周没说话就天天刷屏
        chance = min(0.85, chance)
        return chance if chance >= 0.2 else 0.0

    def _cooldown_seconds(self) -> float:
        return max(0.0, self.cfg.proactive_cooldown_hours) * 3600

    @staticmethod
    def _daily_count(state: dict[str, Any], local: datetime) -> int:
        day = local.strftime("%Y-%m-%d")
        if state.get("proactive_day") != day:
            return 0
        return int(state.get("proactive_count") or 0)

    def _last_row(self):
        try:
            rows = self.store.list_messages(limit=1)
        except Exception:
            logger.exception("主动说话：读取最近流水失败")
            return None
        return rows[0] if rows else None

    def _local(self, now: float) -> datetime:
        if self.tz is None:
            return datetime.fromtimestamp(now)
        return datetime.fromtimestamp(now, self.tz)

    # ------------------------------------------------------------------ 执行

    async def maybe_run(self, now: float | None = None) -> str | None:
        """通过闸门就发一条；返回发出的文本（没发则 None）。"""
        if self._busy:
            return None
        plan = self.decide(now)
        if plan is None:
            return None
        self._busy = True
        try:
            text = await self._compose(plan, now)
            if not text:
                return None
            ok = await self.send(plan.umo, text)
            if not ok:
                return None
            self._mark_sent(now)
            logger.info("KahnBot 主动开口（%s）：%s", plan.kind, text[:60])
            if self.on_sent is not None:
                try:
                    result = self.on_sent(plan.umo, text)
                    if inspect.isawaitable(result):
                        await result
                except Exception:
                    logger.exception("主动说话：回写历史失败（不影响已发出的消息）")
            return text
        finally:
            self._busy = False

    async def _compose(self, plan: ProactivePlan, now: float | None) -> str:
        prompt = self._prompt(plan, now)
        if self.llm is not None:
            try:
                text = await self.llm.text(prompt, system_prompt=PROACTIVE_SYSTEM)
            except Exception:
                text = None
            cleaned = _clean(text)
            if cleaned:
                return cleaned
        # 模型不可用：用模板，不能因为外部依赖就一辈子哑着
        return FALLBACK_LINES[int((now or self.clock())) // 3600 % len(FALLBACK_LINES)]

    def _prompt(self, plan: ProactivePlan, now: float | None) -> str:
        local = self._local(float(now if now is not None else self.clock()))
        lines = [
            f"现在是 {local.strftime('%Y-%m-%d %H:%M')}（{'周' + '一二三四五六日'[local.weekday()]}）。",
        ]
        mood = self.emotion.render()
        if mood:
            lines.append(mood)
        silence_min = int((self.clock() - float(self.emotion.state.get("last_owner_ts") or 0)) / 60)
        if silence_min >= 60:
            lines.append(f"你们已经 {silence_min // 60} 小时没说话了。")
        facts = self._recent_facts(3)
        if facts:
            lines.append("你最近记着的几件事：" + "；".join(facts))
        lines.append("写一条你想发给对方的消息（一两句，不要超过 60 字）。")
        return "\n".join(lines)

    def _recent_facts(self, limit: int) -> list[str]:
        try:
            rows = self.store.facts()
        except Exception:
            return []
        picked: list[str] = []
        for row in rows:
            if row["source"] != "owner" or row["archived"] or row["superseded_by"]:
                continue
            text = str(row["text"] or "").strip()
            if text:
                picked.append(text)
            if len(picked) >= limit:
                break
        return picked

    def _mark_sent(self, now: float | None) -> None:
        now = float(now if now is not None else self.clock())
        local = self._local(now)
        day = local.strftime("%Y-%m-%d")
        state = self.emotion.state
        state["last_proactive_ts"] = now
        if state.get("proactive_day") != day:
            state["proactive_day"] = day
            state["proactive_count"] = 0
        state["proactive_count"] = int(state.get("proactive_count") or 0) + 1


def _clean(text: str | None) -> str:
    if not text:
        return ""
    value = text.strip().strip("\"'“”‘’")
    # 模型偶尔会带前缀或换行成段；只留第一段非空内容
    for line in value.splitlines():
        line = line.strip()
        if line:
            value = line
            break
    if len(value) > 120:
        value = value[:120]
    return value


__all__ = ["Proactive", "ProactivePlan", "PROACTIVE_SYSTEM"]
