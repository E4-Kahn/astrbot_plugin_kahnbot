"""配置读取：把 WebUI 传进来的原始配置收敛成一个不可变的 Config。

AstrBot 传给插件的 `config` 是 `AstrBotConfig`（dict 的子类，带 save_config），
这里只读不写；取不到或类型不对时一律退回默认值，避免一条坏配置把插件拦在启动阶段。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

# SnowLuma 的状态命令；群里谁发一句都不该进流水（F13）
DEFAULT_NOISE: tuple[str, ...] = ("#sl",)

# 默认的名字表（F10）；中英文逗号都当分隔符
DEFAULT_NAMES: tuple[str, ...] = ("鱼宝", "大肥鱼", "鱼呢")


def _as_bool(raw: Mapping[str, Any], key: str, default: bool) -> bool:
    value = raw.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _as_str_tuple(
    raw: Mapping[str, Any],
    key: str,
    default: tuple[str, ...] = (),
) -> tuple[str, ...]:
    value = raw.get(key, default)
    if isinstance(value, str):
        # 也接受 "10001,10002" 这种写法，省得用户非要在表单里点加号
        parts: list[Any] = value.replace("，", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
        parts = list(value)
    else:
        parts = []
    return tuple(str(item).strip() for item in parts if str(item).strip())


def _as_int(raw: Mapping[str, Any], key: str, default: int, low: int, high: int) -> int:
    try:
        value = int(raw.get(key, default))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def _as_float(
    raw: Mapping[str, Any],
    key: str,
    default: float,
    low: float,
    high: float,
) -> float:
    try:
        value = float(raw.get(key, default))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


@dataclass(frozen=True, slots=True)
class Config:
    """插件运行期配置。"""

    enabled: bool = True
    debug: bool = False
    # 主人（F3）：身份键一律是平台用户 ID，不用昵称
    owner_ids: tuple[str, ...] = ()
    # 记录过滤（F13）
    noise_patterns: tuple[str, ...] = DEFAULT_NOISE
    record_private: bool = True
    record_self: bool = False
    # 唤醒（F10 / F12）
    wake_on_poke: bool = True
    wake_on_name: bool = True
    wake_names: tuple[str, ...] = DEFAULT_NAMES
    quota_enabled: bool = True
    quota_daily: int = 5
    # 防抖（F23）
    debounce_seconds: float = 2.0
    debounce_max_seconds: float = 5.0
    # 陌生人私聊（F24）
    block_stranger_private: bool = True
    # 检索与注入（F5 / F14 / F15）
    inject_enabled: bool = True
    embedding_provider_id: str = ""
    recall_top_k: int = 5
    recall_min_score: float = 0.35
    inject_max_chars: int = 800
    dedupe_turns: int = 5
    # 写入（F4 / F7）
    remember_enabled: bool = True
    score_importance: bool = True
    # 汇总（F4 定时路）
    summary_enabled: bool = True
    summary_interval_hours: int = 6
    summary_max_items: int = 10
    summary_min_confidence: float = 0.5
    summary_max_chars: int = 4000
    # 提醒（F16）
    reminder_enabled: bool = True
    # 主动说话（F16 情绪路，M6）
    proactive_enabled: bool = True
    proactive_cooldown_hours: float = 6.0
    proactive_daily_limit: int = 2
    proactive_silence_minutes: int = 10
    timezone: str = ""
    # 模型（留空用 AstrBot 当前默认对话模型）
    llm_provider_id: str = ""

    def is_owner(self, sender_id: str) -> bool:
        return bool(sender_id) and sender_id in self.owner_ids

    @property
    def stranger_block_active(self) -> bool:
        """只有配了主人之后才敢屏蔽私聊。

        反过来（`owner_ids` 空还照屏蔽）会让主人的私聊也被拦掉，
        表现就是"装上插件以后私聊彻底不回"，排查起来很费劲，所以这里失败即放行。
        """
        return self.block_stranger_private and bool(self.owner_ids)


    def is_noise(self, text: str) -> bool:
        """固定文本（大小写不敏感）不算消息，也不参与唤醒。"""
        lowered = text.strip().lower()
        return bool(lowered) and lowered in {p.strip().lower() for p in self.noise_patterns}


def load_config(raw: Mapping[str, Any] | None) -> Config:
    raw = raw if isinstance(raw, Mapping) else {}
    return Config(
        enabled=_as_bool(raw, "enabled", True),
        debug=_as_bool(raw, "debug", False),
        owner_ids=_as_str_tuple(raw, "owner_ids"),
        noise_patterns=_as_str_tuple(raw, "noise_patterns", DEFAULT_NOISE) or DEFAULT_NOISE,
        record_private=_as_bool(raw, "record_private", True),
        record_self=_as_bool(raw, "record_self", False),
        wake_on_poke=_as_bool(raw, "wake_on_poke", True),
        wake_on_name=_as_bool(raw, "wake_on_name", True),
        wake_names=_as_str_tuple(raw, "wake_names", DEFAULT_NAMES) or DEFAULT_NAMES,
        quota_enabled=_as_bool(raw, "quota_enabled", True),
        quota_daily=_as_int(raw, "quota_daily", 5, 0, 200),
        debounce_seconds=_as_float(raw, "debounce_seconds", 2.0, 0.0, 10.0),
        debounce_max_seconds=_as_float(raw, "debounce_max_seconds", 5.0, 0.0, 30.0),
        block_stranger_private=_as_bool(raw, "block_stranger_private", True),
        inject_enabled=_as_bool(raw, "inject_enabled", True),
        embedding_provider_id=str(raw.get("embedding_provider_id") or "").strip(),
        recall_top_k=_as_int(raw, "recall_top_k", 5, 1, 20),
        recall_min_score=_as_float(raw, "recall_min_score", 0.35, 0.0, 1.0),
        inject_max_chars=_as_int(raw, "inject_max_chars", 800, 100, 4000),
        dedupe_turns=_as_int(raw, "dedupe_turns", 5, 0, 50),
        remember_enabled=_as_bool(raw, "remember_enabled", True),
        score_importance=_as_bool(raw, "score_importance", True),
        summary_enabled=_as_bool(raw, "summary_enabled", True),
        summary_interval_hours=_as_int(raw, "summary_interval_hours", 6, 1, 168),
        summary_max_items=_as_int(raw, "summary_max_items", 10, 1, 50),
        summary_min_confidence=_as_float(raw, "summary_min_confidence", 0.5, 0.0, 1.0),
        summary_max_chars=_as_int(raw, "summary_max_chars", 4000, 500, 20000),
        reminder_enabled=_as_bool(raw, "reminder_enabled", True),
        proactive_enabled=_as_bool(raw, "proactive_enabled", True),
        proactive_cooldown_hours=_as_float(raw, "proactive_cooldown_hours", 6.0, 0.0, 72.0),
        proactive_daily_limit=_as_int(raw, "proactive_daily_limit", 2, 0, 10),
        proactive_silence_minutes=_as_int(raw, "proactive_silence_minutes", 10, 0, 720),
        timezone=str(raw.get("timezone") or "").strip(),
        llm_provider_id=str(raw.get("llm_provider_id") or "").strip(),
    )
