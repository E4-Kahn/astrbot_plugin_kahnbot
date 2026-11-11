"""catch-all 记录：把群聊 / 私聊的每条消息写进本地流水。

这里**不判断唤醒、不调 LLM、不发送任何东西**（F11）。记录是记录，回复是回复，
两件事在 AstrBot 里本来就是分开的（见 refs/astrbot-capabilities.md 第 9 节）。
"""

from __future__ import annotations

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from .config import Config
from .store import Store, now_ts


class Recorder:
    def __init__(self, store: Store, cfg: Config) -> None:
        self.store = store
        self.cfg = cfg

    def record(self, event: AstrMessageEvent) -> bool:
        """写入一条流水；被过滤或无需记录时返回 False。"""
        text = (event.get_message_str() or "").strip()
        if not text:
            # 拍一拍这类空消息暂时不记（M2 会把它们补成可读文本）
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
