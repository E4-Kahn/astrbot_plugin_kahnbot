"""KahnBot —— AstrBot 插件入口。

这里只做三件事：读配置、建数据层、注册 handler / 指令；业务逻辑都在 `core/` 下。

当前进度：M3（情绪层）——她有九个情绪维度和五个档位，靠心跳漂移；
群聊里只有 @ 她、拍一拍她、或者叫她的名字才会进 LLM；私聊必回；
连续短消息有 2 秒静默窗口，合并成一次回复。
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import time
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.star.filter.command import GreedyStr

try:  # TextPart 是内容块，不在 message_components 里（capabilities §13）
    from astrbot.core.agent.message import TextPart
except Exception:  # pragma: no cover - 版本差异时退回 system_prompt 注入
    TextPart = None  # type: ignore[assignment]

from .core import wake
from .core.args import split_importance
from .core.config import Config, load_config
from .core.embeddings import EmbeddingClient, pack_vector
from .core.emotion import Emotion
from .core.heartbeat import Heartbeat
from .core.injector import Injector, build_block
from .core.recorder import Recorder
from .core.retriever import Retriever
from .core.state import load_state, save_state
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

        # 检索与注入（M4）
        self.embedder = EmbeddingClient(self.context, self.cfg.embedding_provider_id)
        self.retriever = Retriever(self.store, self.embedder)
        self.injector = Injector(self.cfg.dedupe_turns)

        # 情绪 + 心跳（M3）：状态存 state.json，与记忆同目录
        self.state_path = self.data_dir / "state.json"
        self.emotion = Emotion(load_state(self.state_path))
        self._last_activity = 0.0
        self.heartbeat = Heartbeat(
            PLUGIN_NAME,
            self.emotion,
            self.state_path,
            activity=lambda: self._last_activity,
        )
        if not self.heartbeat.start():
            logger.warning("%s 心跳未启动（没有事件循环），将在第一条消息时重试", PLUGIN_NAME)
        # 首次加载就把状态落盘：心跳要到 45 分钟后才第一次 tick，别让人以为没生效
        save_state(self.state_path, self.emotion.snapshot())

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

        self._touch_emotion(event, sender_id, text)

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

    # ------------------------------------------------------------------ 检索注入

    @filter.on_llm_request(priority=-1000)
    async def inject_memory(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        """把检索到的长期记忆贴到本轮请求里（F14/F15；不动 system_prompt）。"""
        if not self.cfg.enabled or not self.cfg.inject_enabled or self.store is None:
            return
        if (event.get_platform_name() or "") == "cron":
            return  # 提醒任务不查记忆，免得自己提醒自己
        query = (req.prompt or event.get_message_str() or "").strip()
        if len(query) < 2:
            return

        umo = event.unified_msg_origin or ""
        owner_context = self._is_owner(event, event.get_sender_id() or "")
        self.injector.next_turn(umo)
        try:
            hits = await self.retriever.search(
                query,
                owner_context=owner_context,
                top_k=self.cfg.recall_top_k,
                min_score=self.cfg.recall_min_score,
            )
        except Exception:
            logger.exception("%s 检索失败", PLUGIN_NAME)
            return
        if not hits:
            return

        fresh = self.injector.fresh(hits, umo)
        if not fresh:
            if self.cfg.debug:
                logger.debug("%s 本轮命中 %s 条但都在去重窗口内，跳过注入", PLUGIN_NAME, len(hits))
            return

        block, used = build_block(fresh, self.cfg.inject_max_chars)
        if not block:
            return
        if TextPart is not None:
            req.extra_user_content_parts.append(TextPart(text=block).mark_as_temp())
        else:  # pragma: no cover - 只在旧版本 AstrBot 上走这里
            req.system_prompt = (req.system_prompt or "") + "\n" + block
        self.injector.mark(used, umo)

        if self.cfg.debug:
            logger.info(
                "%s 注入 %s 条记忆 / %s 字（query=%r）：\n%s",
                PLUGIN_NAME,
                len(used),
                len(block),
                query[:30],
                block,
            )

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
            f"心情：{self.emotion.mood_label()}",
            f"记忆：{self.store.count_facts() if self.store else 0} 条；向量：{self.embedder.describe()}",
            f"注入：{'开' if self.cfg.inject_enabled else '关'}（top {self.cfg.recall_top_k}，上限 {self.cfg.inject_max_chars} 字，{self.cfg.dedupe_turns} 轮内不重复）",
            "当前进度：M4（能检索、会注入；「记住…」自动写入留到 M5）。",
        ]
        yield event.plain_result("\n".join(lines))

    @kahn.command("mood")
    async def kahn_mood(self, event: AstrMessageEvent, topic: str = ""):
        """查看她的心情（/kahn mood why 看最近的变化）"""
        raw = (topic or "").strip()
        if not raw:
            raw = (event.get_message_str() or "").split(maxsplit=2)[-1].strip()
        if raw in {"why", "原因", "为什么"}:
            log = self.emotion.state.get("log") or []
            if not log:
                yield event.plain_result("还没有记录到情绪变化。")
                return
            lines = ["最近几次情绪变化："]
            for item in log[-8:]:
                when = time.strftime("%m-%d %H:%M", time.localtime(int(item.get("ts", 0))))
                lines.append(
                    f"{when} {item.get('reason', '')} → {item.get('mode', '')}"
                    f"（blame {item.get('blame', 0)}）"
                )
            yield event.plain_result("\n".join(lines))
            return
        yield event.plain_result("\n".join(self.emotion.describe()))

    @kahn.command("recall")
    async def kahn_recall(self, event: AstrMessageEvent, query: GreedyStr = ""):
        """检索记忆（/kahn recall 我下周有什么安排）"""
        if self.store is None:
            yield event.plain_result("数据库不可用，请查看日志。")
            return
        if not self._is_admin(event.get_sender_id()):
            yield event.plain_result("只有主人或 AstrBot 管理员可以使用检索。")
            return
        raw = str(query or "").strip()
        if not raw:
            yield event.plain_result("用法：/kahn recall <问题或关键词>")
            return

        hits = await self.retriever.search(
            raw,
            owner_context=True,
            top_k=self.cfg.recall_top_k,
            min_score=0.0,
        )
        if not hits:
            yield event.plain_result(f"没有检索到与「{raw}」相关的记忆。")
            return
        lines = [f"与「{raw}」相关（共 {len(hits)} 条，向量={self.embedder.describe()}）："]
        for hit in hits:
            when = _fmt_ts(hit.created_ts)
            flag = "" if hit.source == "owner" else "（转述）"
            lines.append(
                f"#{hit.fact_id} {hit.score:.2f} 重要度{hit.importance} {when}{flag}\n  {hit.text}"
            )
        yield event.plain_result("\n".join(lines))

    @kahn.group("memory")
    def kahn_memory(self) -> None:
        """记忆相关：list / facts / add / forget"""

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

    @kahn_memory.command("facts")
    async def kahn_memory_facts(self, event: AstrMessageEvent, limit: int = 10):
        """查看记忆条目"""
        if self.store is None:
            yield event.plain_result("数据库不可用，请查看日志。")
            return
        if not self._is_admin(event.get_sender_id()):
            yield event.plain_result("只有主人或 AstrBot 管理员可以查看记忆。")
            return
        rows = self.store.facts(include_archived=True, include_superseded=True)
        if not rows:
            yield event.plain_result("还没有记忆条目。/kahn memory add <内容> 可以先加一条。")
            return
        limit = max(1, min(int(limit), 50))
        lines = [f"记忆条目（共 {len(rows)} 条，显示最近 {min(limit, len(rows))} 条）："]
        for row in rows[:limit]:
            when = _fmt_ts(row["created_ts"])
            flags = []
            if row["superseded_by"]:
                flags.append(f"已被{row['superseded_by']}取代")
            if row["archived"]:
                flags.append("已归档")
            if row["source"] != "owner":
                flags.append("转述")
            tail = "".join(f"〔{flag}〕" for flag in flags)
            lines.append(
                f"#{row['fact_id']} {when} 重要度{row['importance']} {row['text']}{tail}"
            )
        yield event.plain_result("\n".join(lines))

    @kahn_memory.command("add")
    async def kahn_memory_add(self, event: AstrMessageEvent, raw: GreedyStr = ""):
        """手动加一条记忆（M5 会由"记住…"自动触发）"""
        if self.store is None:
            yield event.plain_result("数据库不可用，请查看日志。")
            return
        sender_id = event.get_sender_id() or ""
        if not self._is_owner(event, sender_id) and not self._is_admin(sender_id):
            yield event.plain_result("只有主人或 AstrBot 管理员可以添加记忆。")
            return
        text, importance = split_importance(str(raw or ""))
        text = text.strip()
        if not text:
            yield event.plain_result("用法：/kahn memory add <内容> [重要度 0-100]")
            return
        try:
            fact_id = await self._remember(
                text, 50 if importance is None else importance, event
            )
        except Exception:
            logger.exception("%s 写入记忆失败", PLUGIN_NAME)
            yield event.plain_result("写入记忆失败，请查看日志。")
            return
        if fact_id is None:
            yield event.plain_result("这条记忆已经记过了。")
            return
        yield event.plain_result(f"好，我记下了：{text}（#{fact_id}）")

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
        await self.heartbeat.stop()
        save_state(self.state_path, self.emotion.snapshot())
        if self.store is not None:
            self.store.close()
            self.store = None
        logger.info("%s 已卸载", PLUGIN_NAME)

    # ------------------------------------------------------------------ 记忆写入

    async def _remember(self, text: str, importance: int, event: AstrMessageEvent) -> str | None:
        """写一条 owner 事实：md + 索引（+ 向量）。重复内容返回 None。"""
        if self.store is None:
            return None
        text = " ".join(text.split())
        if not text:
            return None
        digest = hashlib.md5(text.encode("utf-8")).hexdigest()
        if self.store.fact_by_hash(digest) is not None:
            return None

        importance = max(0, min(100, int(importance)))
        now = int(time.time())
        fact_id = digest[:12]
        group_id = event.get_group_id() or ""
        scope = f"group:{group_id}" if group_id else "private"

        vec = await self.embedder.embed(text) if self.embedder.resolve() else None
        row_id = self.store.add_fact(
            fact_id=fact_id,
            text=text,
            hash_=digest,
            scope=scope,
            source="owner",
            visibility="private",
            kind="fact",
            importance=importance,
            created_ts=now,
            embedding=pack_vector(vec) if vec else None,
        )
        if row_id is None:
            return None

        # md 是权威（索引可重建）：一行一条
        label = "主人" if self._is_owner(event, event.get_sender_id() or "") else "管理员"
        line = (
            f"- **{time.strftime('%Y-%m-%d', time.localtime(now))}** · "
            f"[{scope}|{label}|uid:{event.get_sender_id() or ''}|owner] {text}"
            f" 〔重要度 {importance}〕"
        )
        try:
            path = self.data_dir / "memory" / "facts.md"
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except Exception:
            logger.exception("%s 写 facts.md 失败（索引已写入）", PLUGIN_NAME)
        return fact_id

    # ------------------------------------------------------------------ 情绪

    def _touch_emotion(self, event: AstrMessageEvent, sender_id: str, text: str) -> None:
        """把这条消息翻译成情绪变化（M3）。

        只有主人能推动 blame（F20）；心跳没起来时在这里补启动，
        因为 `__init__` 有时拿不到正在运行的事件循环。
        """
        self._last_activity = time.time()
        if not self.heartbeat.running:
            self.heartbeat.start()
        try:
            changed = False
            if self._is_owner(event, sender_id):
                changed = self.emotion.on_owner_text(text) is not None
            if wake.is_poke_to_self(event, str(event.get_self_id() or "")):
                self.emotion.on_poke()
                changed = True
            if changed:
                # 状态很小（几百字节），有事就落盘，免得进程被杀丢脾气
                save_state(self.state_path, self.emotion.snapshot())
        except Exception:
            logger.exception("%s 更新情绪失败", PLUGIN_NAME)

    def _is_owner(self, event: AstrMessageEvent, sender_id: str) -> bool:
        if self.cfg.is_owner(sender_id):
            return True
        # 没配 owner_ids 时，把私聊当作主人（与 M1 的失败即放行保持一致）
        return not self.cfg.owner_ids and event.is_private_chat()

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
