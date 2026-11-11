"""KahnBot —— AstrBot 插件入口。

这里只做三件事：读配置、建数据层、注册 handler / 指令；业务逻辑都在 `core/` 下。

当前进度：M1（数据层与记录）——群聊/私聊消息进本地 SQLite，`#sl` 之类固定文本被丢弃，
`/kahn status`、`/kahn memory list`、`/kahn memory forget <id>` 可用。不调 LLM。
"""

from __future__ import annotations

import datetime

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.api.star import Context, Star, StarTools

from .core.config import Config, load_config
from .core.recorder import Recorder
from .core.store import Store

PLUGIN_NAME = "astrbot_plugin_kahnbot"
PLUGIN_VERSION = "0.1.0"


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

        logger.info(
            "%s v%s 已加载（enabled=%s，数据目录=%s，已记录 %s 条）",
            PLUGIN_NAME,
            PLUGIN_VERSION,
            self.cfg.enabled,
            self.data_dir,
            self.store.count_messages() if self.store else "N/A",
        )

    # ------------------------------------------------------------------ 记录

    @filter.event_message_type(
        EventMessageType.GROUP_MESSAGE | EventMessageType.PRIVATE_MESSAGE,
        priority=100,
    )
    async def on_message(self, event: AstrMessageEvent):
        """把收到的消息记进流水（不唤醒、不回复、不调 LLM）。"""
        if not self.cfg.enabled or self.recorder is None:
            return
        try:
            self.recorder.record(event)
        except Exception:
            # 记录失败不能影响正常对话
            logger.exception("%s 记录消息失败", PLUGIN_NAME)

    # ------------------------------------------------------------------ 指令

    @filter.command_group("kahn")
    def kahn(self) -> None:
        """KahnBot 指令。单独发送 /kahn 会列出所有子指令。"""

    @kahn.command("status")
    async def kahn_status(self, event: AstrMessageEvent):
        """查看 KahnBot 状态"""
        total = self.store.count_messages() if self.store else 0
        lines = [
            f"KahnBot v{PLUGIN_VERSION}",
            f"状态：{'运行中' if self.cfg.enabled else '已禁用'}",
            f"数据目录：{self.data_dir}",
            f"已记录：{total} 条",
            "当前进度：M1（只记录，不调模型）。",
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
        if self.store is not None:
            self.store.close()
            self.store = None
        logger.info("%s 已卸载", PLUGIN_NAME)

    # ------------------------------------------------------------------ 内部

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


def _fmt_ts(ts: int) -> str:
    return datetime.datetime.fromtimestamp(int(ts)).strftime("%m-%d %H:%M")
