"""唤醒判定：@ / 拍一拍 / 叫名字 / 私聊。

只做"这一条消息算不算在叫她"的判断，不碰配额、不碰事件状态——
真正放行 LLM 的是 `main.py` 里那个 handler（见 refs/wake-design.md 第 1 节）。
"""

from __future__ import annotations

from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import At, Poke

from .config import Config

# 唤醒原因，写进日志用；顺序即优先级
AT = "at"
POKE = "poke"
NAME = "name"
PRIVATE = "private"


def message_components(event: AstrMessageEvent) -> list:
    obj = getattr(event, "message_obj", None)
    return list(getattr(obj, "message", None) or [])


def is_poke_to_self(event: AstrMessageEvent, self_id: str) -> bool:
    if not self_id:
        return False
    for comp in message_components(event):
        if isinstance(comp, Poke) and comp.target_id() == self_id:
            return True
    return False


def _is_at_self(event: AstrMessageEvent, self_id: str) -> bool:
    if not self_id:
        return False
    for comp in message_components(event):
        if isinstance(comp, At) and str(getattr(comp, "qq", "")) == self_id:
            return True
    return False


def _hit_name(text: str, names: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(name and name.lower() in lowered for name in names)


def detect(event: AstrMessageEvent, cfg: Config) -> str | None:
    """返回唤醒原因；返回 None 表示"只当上下文，别回"。"""
    self_id = str(event.get_self_id() or "")

    if cfg.wake_on_poke and is_poke_to_self(event, self_id):
        return POKE
    if _is_at_self(event, self_id):
        return AT
    if cfg.wake_on_name and _hit_name(event.get_message_str() or "", cfg.wake_names):
        return NAME
    if event.is_private_chat():
        # 私聊不叫她也在跟她说话
        return PRIVATE
    return None


def is_command_event(event: AstrMessageEvent) -> bool:
    """这条消息是否已经被某个指令 handler 认领。

    用 activated_handlers 里的 filter 类型判断，避免自己猜前缀：
    `wake_prefix`（默认 `/`）在 waking_check 阶段已经被剥掉，靠字符串看不出来。
    """
    getter = getattr(event, "get_extra", None)
    handlers = (getter("activated_handlers") if getter else None) or []
    for handler in handlers:
        for flt in getattr(handler, "event_filters", []) or []:
            if type(flt).__name__ in {"CommandFilter", "CommandGroupFilter"}:
                return True
    return False


def poke_speech(event: AstrMessageEvent, name: str) -> str:
    return f"（{name} 拍了拍你）"


__all__ = [
    "AT",
    "NAME",
    "POKE",
    "PRIVATE",
    "detect",
    "is_command_event",
    "is_poke_to_self",
    "message_components",
    "poke_speech",
]
