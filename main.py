"""KahnBot —— AstrBot 插件入口。

这里只做三件事：读配置、建数据层、注册 handler / 指令；业务逻辑都在 `core/` 下。

当前进度：M2（唤醒与配额）——群聊里只有 @ 她、拍一拍她、或者叫她的名字才会进 LLM，
其余消息只记录（`on_llm_request` 注入最近群聊是 M3 的事）；私聊必回；
连续短消息有 2 秒静默窗口，合并成一次回复。
"""

from __future__ import annotations

import asyncio
import datetime
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.api.star import Context, Star, StarTools

from .core import wake
from .core.config import Config, load_config
from .core.recorder import Recorder
from .core.store import Store

PLUGIN_NAME = "astrbot_plugin_kahnbot"
PLUGIN_VERSION = "0.1.0"
MAX_PENDING_TEXTS = 8


class KahnBot(Star):
    """KahnBot —— 会记事、有脾气的群友。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.cfg: Config = load_config(config)
        self.data_dir = StarTools.get_data_dir()
        (self.data_dir / "memory").mkdir(parents=True, exist_ok=True)

        self.store: Store | None = None
        self.recorder: Recorder | None = None
        try:
            store = Store(self.data_dir / "kahnbot.db")
            store.open()
            self.store = store
            self.recorder = Recorder(store, self.cfg)
        except Exception:
            logger.exception("%s 初始化数据库失败，记录功能不可用", PLUGIN_NAME)

        # 防抖窗口：umo -> {seq, deadline, texts, reason}
        self._pending: dict[str, dict[str, Any]] = {}

        logger.info(
            "%s v%s 已加载（enabled=%s，数据目录=%s，已记录 %s 条，名字表=%s）",
            PLUGIN_NAME,
            PLUGIN_VERSION,
            self.cfg.enabled,
            self.data_dir,
            self.store.count_messages() if self.store else "N/A",
            "/".join(self.cfg.wake_names) or "（空）",
        )
        if not self.cfg.owner_ids:
            logger.warning(
                "%s 尚未配置 owner_ids：现在所有私聊都会被当作主人处理，"
                "而\"屏蔽陌生人私聊\"与\"只有主人能影响情绪\"都不会生效。",
                PLUGIN_NAME,
            )

    # ------------------------------------------------------------------ 记录 + 唤醒

    @filter.event_message_type(
        EventMessageType.GROUP_MESSAGE | EventMessageType.PRIVATE_MESSAGE,
        priority=100,
    )
    async def on_message(self, event: AstrMessageEvent):
        """记录每条消息；命中唤醒后放行默认 LLM 流程。"""
        if not self.cfg.enabled or self.recorder is None:
            return

        sender_id = event.get_sender_id() or ""
        private = event.is_private_chat()

        # F24：陌生人的私聊不记录、不回复，也不让内置助手代答
        if private and self.cfg.stranger_block_active and not self._is_admin(sender_id):
            event.stop_event()
            if self.cfg.debug:
                logger.debug("KahnBot 忽略陌生人私聊 %s", sender_id)
            return

        text = event.get_message_str() or ""
        if self.cfg.is_noise(text):
            return

        try:
            self.recorder.record(event)
        except Exception:
            # 记录失败不能影响正常对话
            logger.exception("%s 记录消息失败", PLUGIN_NAME)

        # 指令由指令 handler 自己回，不参与防抖、也不需要放行 LLM
        if wake.is_command_event(event):
            return

        reason = wake.detect(event, self.cfg)
        if reason is None:
            return

        # 关键：@ 和私聊走到这里时 is_at_or_wake_command 已经是 True，
        # 先压回 False，等窗口结束再置真（见 refs/wake-design.md 第 2、3 节）
        event.is_at_or_wake_command = False

        if not self._quota_allows(sender_id, private):
            if self.cfg.debug:
                logger.debug("KahnBot 配额已满，静默忽略 %s 的 %s 唤醒", sender_id, reason)
            return

        merged = await self._settle(event, reason, text)
        if merged is None:
            # 窗口期间又来了新消息，由那一条接手
            return

        if reason == wake.POKE:
            event.message_str = merged or wake.poke_speech(event, self._display_name(event, sender_id))
        elif merged:
            event.message_str = merged

        event.is_at_or_wake_command = True
        self._bump_quota(sender_id, private)

        if self.cfg.debug:
            logger.debug("KahnBot 唤醒（%s）：%s", reason, event.message_str[:40])

    # ------------------------------------------------------------------ 指令

    @filter.command_group("kahn")
    def kahn(self) -> None:
        """KahnBot 指令。单独发送 /kahn 会列出所有子指令。"""

    @kahn.command("status")
    async def kahn_status(self, event: AstrMessageEvent):
        """查看 KahnBot 状态"""
        total = self.store.count_messages() if self.store else 0
        sender_id = event.get_sender_id() or ""
        quota = self._quota_used(sender_id, event.is_private_chat())
        lines = [
            f"KahnBot v{PLUGIN_VERSION}",
            f"状态：{'运行中' if self.cfg.enabled else '已禁用'}",
            f"数据目录：{self.data_dir}",
            f"已记录：{total} 条",
            f"唤醒：@ / 拍一拍 / {'、'.join(self.cfg.wake_names) or '（无名字）'}",
            f"陌生人私聊：{'屏蔽' if self.cfg.stranger_block_active else '不屏蔽（未配置主人）'}",
            f"今日唤醒：{quota} / {self.cfg.quota_daily}（主人与管理员不计）",
            "当前进度：M2（只唤醒，不检索记忆）。",
        ]
        yield event.plain_result("\n".join(lines))

    @kahn.group("memory")
    def kahn_memory(self) -> None:
        """记忆相关：list / forget"""

    @kahn_memory.command("list")
    async def kahn_memory_list(self, event: AstrMessageEvent, limit: int = 10):
        """查看最近的记录"""
        if self.store is None:
            yield event.plain_result("数据库不可用，请查看日志。")
            return
        rows = self.store.list_messages(limit=limit)
        if not rows:
            yield event.plain_result("还没有记录。")
            return
        lines = [f"最近 {len(rows)} 条记录（共 {self.store.count_messages()} 条）："]
        for row in rows:
            when = _fmt_ts(row["ts"])
            where = f"群{row['group_id']}" if row["group_id"] else "私聊"
            who = row["sender_name"] or row["sender_id"]
            flag = "★" if row["is_owner"] else ""
            text = row["text"].replace("\n", " ")
            if len(text) > 40:
                text = text[:40] + "…"
            lines.append(f"#{row['id']} {when} {where} {who}{flag}：{text}")
        yield event.plain_result("\n".join(lines))

    @kahn_memory.command("forget")
    async def kahn_memory_forget(self, event: AstrMessageEvent, row_id: int):
        """删除一条记录：#id 见 list"""
        if self.store is None:
            yield event.plain_result("数据库不可用，请查看日志。")
            return
        if not self._is_admin(event.get_sender_id()):
            yield event.plain_result("只有主人或 AstrBot 管理员可以删除记录。")
            return
        if self.store.delete_message(row_id):
            yield event.plain_result(f"已删除记录 #{row_id}。")
        else:
            yield event.plain_result(f"没有找到记录 #{row_id}。")

    async def terminate(self) -> None:
        """插件被卸载 / 重载 / 停用时调用。"""
        self._pending.clear()
        if self.store is not None:
            self.store.close()
            self.store = None
        logger.info("%s 已卸载", PLUGIN_NAME)

    # ------------------------------------------------------------------ 防抖

    async def _settle(self, event: AstrMessageEvent, reason: str, text: str) -> str | None:
        """等一个静默窗口，把连续短消息并成一条。

        返回合并后的文本；返回 None 表示"窗口期间有新消息接手了"。
        每条新消息都会把 seq +1，先到的那条醒来发现 seq 变了就悄悄退出。
        """
        if self.cfg.debounce_seconds <= 0:
            return text.strip()

        key = event.unified_msg_origin
        loop = asyncio.get_running_loop()
        now = loop.time()
        entry = self._pending.get(key)
        if entry is None:
            entry = {
                "seq": 0,
                "deadline": now + self.cfg.debounce_max_seconds,
                "texts": [],
                "reason": reason,
            }
            self._pending[key] = entry

        entry["seq"] += 1
        my_seq = entry["seq"]
        entry["reason"] = reason
        if text.strip():
            entry["texts"].append(text.strip())
            del entry["texts"][:-MAX_PENDING_TEXTS]

        remain = min(self.cfg.debounce_seconds, entry["deadline"] - now)
        if remain > 0:
            await asyncio.sleep(remain)
        if entry["seq"] != my_seq:
            return None

        self._pending.pop(key, None)
        return "\n".join(entry["texts"])

    # ------------------------------------------------------------------ 配额

    def _quota_allows(self, sender_id: str, private: bool) -> bool:
        """主人、管理员、私聊都不计入配额；只限制群里的其他人（F12）。"""
        if not self.cfg.quota_enabled or private or not sender_id:
            return True
        if self._is_admin(sender_id):
            return True
        if self.store is None:
            return True
        return self.store.wake_quota(sender_id, _today()) < self.cfg.quota_daily

    def _bump_quota(self, sender_id: str, private: bool) -> None:
        if not self.cfg.quota_enabled or private or not sender_id or self.store is None:
            return
        if self._is_admin(sender_id):
            return
        self.store.bump_wake_quota(sender_id, _today())

    def _quota_used(self, sender_id: str, private: bool) -> int:
        if private or not sender_id or self.store is None:
            return 0
        return self.store.wake_quota(sender_id, _today())

    # ------------------------------------------------------------------ 内部

    def _display_name(self, event: AstrMessageEvent, sender_id: str) -> str:
        """拍一拍事件没有昵称，用流水里的缓存兜底（wake-design §3.2）。"""
        name = (event.get_sender_name() or "").strip()
        if name and name != sender_id:
            return name
        if self.store is not None:
            cached = self.store.last_name_of(sender_id)
            if cached:
                return cached
        return sender_id

    def _is_admin(self, sender_id: str) -> bool:
        if not sender_id:
            return False
        if self.cfg.is_owner(sender_id):
            return True
        try:
            admins = self.context.get_config().get("admins_id", []) or []
        except Exception:
            admins = []
        return sender_id in {str(item) for item in admins}


def _today() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d")


def _fmt_ts(ts: int) -> str:
    return datetime.datetime.fromtimestamp(int(ts)).strftime("%m-%d %H:%M")
