"""catch-all 记录：把群聊 / 私聊的每条消息写进本地流水。

这里**不判断唤醒、不调 LLM、不发送任何东西**（F11）。记录是记录，回复是回复，
两件事在 AstrBot 里本来就是分开的（见 refs/astrbot-capabilities.md 第 9 节）。
"""

from __future__ import annotations

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from .config import Config
from .store import Store, now_ts
from .wake import is_command_event

# 纯表情/图片/语音这类消息 `message_str` 是空的（见 capabilities §16），
# 记录时补一个和模型看到的一致的占位符，否则这条消息在流水里直接消失。
_PLACEHOLDERS: dict[str, str] = {
    "Image": "[图片]",
    "Face": "[表情]",
    "Record": "[语音]",
    "Video": "[视频]",
    "File": "[文件]",
    "Poke": "[拍一拍]",
    "Node": "[合并转发]",
    "Nodes": "[合并转发]",
}


def placeholder_text(event: AstrMessageEvent) -> str:
    obj = getattr(event, "message_obj", None)
    labels: list[str] = []
    for comp in getattr(obj, "message", None) or []:
        label = _PLACEHOLDERS.get(type(comp).__name__)
        if label and label not in labels:
            labels.append(label)
    return " ".join(labels)


class Recorder:
    def __init__(self, store: Store, cfg: Config) -> None:
        self.store = store
        self.cfg = cfg

    def record(self, event: AstrMessageEvent) -> bool:
        """写入一条流水；被过滤或无需记录时返回 False。"""
        # 指令是控制流量，不是聊天内容：/kahn … 不进流水（F13）
        if is_command_event(event):
            return False

        text = (event.get_message_str() or "").strip()
        if not text:
            text = placeholder_text(event)
        if not text:
            return False
        if self.cfg.is_noise(text):
            return False

        sender_id = event.get_sender_id() or ""
        if not sender_id:
            return False
        self_id = event.get_self_id() or ""
        if not self.cfg.record_self and self_id and sender_id == self_id:
            return False

        private = event.is_private_chat()
        if private and not self.cfg.record_private:
            return False

        group_id = "" if private else (event.get_group_id() or "")
        message_obj = getattr(event, "message_obj", None)
        msg_id = str(getattr(message_obj, "message_id", "") or "")

        self.store.add_message(
            ts=now_ts(),
            umo=event.unified_msg_origin,
            platform=event.get_platform_name(),
            group_id=group_id or None,
            sender_id=sender_id,
            sender_name=event.get_sender_name() or "",
            is_owner=self.cfg.is_owner(sender_id),
            is_bot=bool(self_id and sender_id == self_id),
            text=text,
            msg_id=msg_id,
        )
        if self.cfg.debug:
            logger.debug(
                "KahnBot 记录 %s(%s) @ %s: %s",
                event.get_sender_name(),
                sender_id,
                group_id or "私聊",
                text[:30],
            )
        return True
