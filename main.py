"""KahnBot —— AstrBot 插件入口。

这里只做三件事：读配置、建数据层、注册 handler / 指令；业务逻辑都在 `core/` 下。

当前进度：M6（主动说话与收尾）。她在 M5 的基础上会在沉默够久、情绪上来时自己开口
（冷却 6 小时、日限 2 条、免打扰时段不吵人）；生气到一定程度会"躲"起来不说话，
但被哄或主人一直找她就会出来，躲起来期间被吞的话下次会记得。
"""

from __future__ import annotations

import asyncio
import datetime
import time
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.api.message_components import Plain
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.star.filter.command import GreedyStr

try:  # TextPart 是内容块，不在 message_components 里（capabilities §13）
    from astrbot.core.agent.message import TextPart
except Exception:  # pragma: no cover - 版本差异时退回 system_prompt 注入
    TextPart = None  # type: ignore[assignment]

from .core import remember, wake
from .core.args import split_importance
from .core.config import Config, load_config
from .core.embeddings import EmbeddingClient
from .core.emotion import Emotion
from .core.heartbeat import Heartbeat
from .core.injector import Injector, build_block
from .core.llm import LLMClient
from .core.memory import MemoryWriter
from .core.memoryfiles import MemoryFiles
from .core.proactive import Proactive
from .core.recorder import Recorder
from .core.reminder import ReminderService, resolve_tz
from .core.retriever import Retriever
from .core.state import load_state, save_state
from .core.store import Store
from .core.summarizer import Summarizer
from .core.when import format_due

PLUGIN_NAME = "astrbot_plugin_kahnbot"
PLUGIN_VERSION = "0.2.0"
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
        self._tasks: set[asyncio.Task] = set()
        self._startup_task: asyncio.Task | None = None
        self._maintenance_task: asyncio.Task | None = None

        # 时区：插件没配就跟着 AstrBot 全局（默认 Asia/Shanghai）
        tz_name = self.cfg.timezone or self._global_timezone()
        self.tz_name = tz_name
        self.tz = resolve_tz(tz_name)

        # 检索与注入（M4）
        self.embedder = EmbeddingClient(self.context, self.cfg.embedding_provider_id)
        self.retriever = Retriever(self.store, self.embedder)
        self.injector = Injector(self.cfg.dedupe_turns)

        # 长期记忆（M5）
        self.files = MemoryFiles(self.data_dir)
        self.llm = LLMClient(self.context, self.cfg.llm_provider_id)
        self.writer: MemoryWriter | None = None
        if self.store is not None:
            self.writer = MemoryWriter(self.store, self.files, self.embedder, self.cfg, self.llm)
        self.summarizer: Summarizer | None = None
        if self.store is not None:
            self.summarizer = Summarizer(self.store, self.writer, self.llm, self.cfg)
        self.reminder = ReminderService(
            plugin_name=PLUGIN_NAME,
            store=self.store,
            files=self.files,
            cfg=self.cfg,
            send=self._send_text,
            context=self.context,
            tz_name=tz_name,
        )

        # 情绪 + 心跳（M3）：状态存 state.json，与记忆同目录
        self.state_path = self.data_dir / "state.json"
        self.emotion = Emotion(load_state(self.state_path))
        self._last_activity = 0.0
        # 主动说话（M6）：情绪概率路；到期提醒由 reminder 走确定路
        self.proactive = Proactive(
            store=self.store,
            emotion=self.emotion,
            llm=self.llm,
            cfg=self.cfg,
            send=self._send_text,
            tz=self.tz,
            on_sent=self._remember_proactive_turn,
        )
        self.heartbeat = Heartbeat(
            PLUGIN_NAME,
            self.emotion,
            self.state_path,
            activity=lambda: self._last_activity,
            on_tick=self._on_tick,
        )
        if not self.heartbeat.start():
            logger.warning("%s 心跳未启动（没有事件循环），将在第一条消息时重试", PLUGIN_NAME)
        # 首次加载就把状态落盘：心跳要到 45 分钟后才第一次 tick，别让人以为没生效
        save_state(self.state_path, self.emotion.snapshot())

        self._ensure_startup()
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
        # F16：提醒任务自己发的消息不进 catch-all，否则会被记录、被当成唤醒
        if (event.get_platform_name() or "") == "cron":
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

        # 指令是控制流量：不进流水、不推情绪（F13 的口径）。
        # 否则 /kahn memory add … 会被当成"主人说过的话"记下来，以后还会被汇总成记忆。
        if wake.is_command_event(event):
            return

        try:
            self.recorder.record(event)
        except Exception:
            # 记录失败不能影响正常对话
            logger.exception("%s 记录消息失败", PLUGIN_NAME)

        self._touch_emotion(event, sender_id, text)

        reason = wake.detect(event, self.cfg)
        if reason is None:
            return

        # M6：回避档。闹脾气时不出声，但把话攒着，下次开口时装作"其实都记得"。
        # @、拍一拍、以及"你是不是生气了 / 对不起"这类话能把她逼出来（Q14：私聊不豁免）。
        if self.emotion.is_avoidant() and not self._must_reply(reason, text):
            self.emotion.push_unanswered(text, event.unified_msg_origin or "")
            save_state(self.state_path, self.emotion.snapshot())
            if self.cfg.debug:
                logger.debug("KahnBot 正在回避，攒下这句：%s", text[:40])
            event.stop_event()
            return

        # 关键：@ 和私聊走到这里时 is_at_or_wake_command 已经是 True，
        # 先压回 False，等窗口结束再置真（见 refs/wake-design.md 第 2、3 节）
        event.is_at_or_wake_command = False

        if not self._quota_allows(sender_id, private):
            if self.cfg.debug:
                logger.debug("KahnBot 配额已满，静默忽略 %s 的 %s 唤醒", sender_id, reason)
            return

        # M5：先看这句是不是「记住…」；是就写库 + 回执，不进默认 LLM 流程
        if await self._try_remember(event, reason, text, sender_id, private):
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
        self.emotion.note_replied()

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
        self._inject_unanswered(req)
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
        owner_count, observed_count = self._fact_counts()
        pending = len(self.store.facts_with_due(int(time.time()))) if self.store else 0
        lines = [
            f"KahnBot v{PLUGIN_VERSION}",
            f"状态：{'运行中' if self.cfg.enabled else '已禁用'}",
            f"数据目录：{self.data_dir}",
            f"已记录：{total} 条",
            f"唤醒：@ / 拍一拍 / {'、'.join(self.cfg.wake_names) or '（无名字）'}",
            f"陌生人私聊：{'屏蔽' if self.cfg.stranger_block_active else '不屏蔽（未配置主人）'}",
            f"今日唤醒：{quota} / {self.cfg.quota_daily}（主人与管理员不计）",
            f"心情：{self.emotion.mood_label()}",
            f"记忆：{owner_count + observed_count} 条（owner {owner_count} / observed {observed_count}）；向量：{self.embedder.describe()}",
            f"提醒：{pending} 条待发；汇总：{'开' if self.cfg.summary_enabled else '关'}（{self.cfg.summary_interval_hours} 小时一次）",
            f"主动说话：{'开' if self.cfg.proactive_enabled else '关'}（冷却 {self.cfg.proactive_cooldown_hours:g} 小时、日限 {self.cfg.proactive_daily_limit} 条、沉默 {self.cfg.proactive_silence_minutes} 分钟才开口）",
            f"回避：{'正在躲，' + self._fmt_ts(int(self.emotion.state.get('avoid_until') or 0)) + ' 前不理人' if self.emotion.is_avoidant() else '没有'}；攒下的话 {len(self.emotion.state.get('unanswered') or [])} 条",
            f"注入：{'开' if self.cfg.inject_enabled else '关'}（top {self.cfg.recall_top_k}，上限 {self.cfg.inject_max_chars} 字，{self.cfg.dedupe_turns} 轮内不重复）",
            "当前进度：M6（会记事、会提醒、闹脾气会躲、沉默久了会主动开口）。",
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

    @kahn.command("proactive")
    async def kahn_proactive(self, event: AstrMessageEvent, mode: str = ""):
        """查看主动说话判定（/kahn proactive force 真的发一条，仅管理员）"""
        if not self._is_admin(event.get_sender_id()):
            yield event.plain_result("只有主人或 AstrBot 管理员可以使用。")
            return
        raw = (mode or "").strip().lower()
        force = raw in {"force", "now", "发", "立刻", "强制"}
        if force:
            text = await self.proactive.maybe_run()
            if text:
                yield event.plain_result(f"发出去了：{text}")
            else:
                yield event.plain_result("这次没发成（闸门没过，或发送失败），看日志。")
            return
        plan = self.proactive.decide()
        state = self.emotion.state
        chance = self.proactive._chance() if hasattr(self.proactive, "_chance") else 0.0
        lines = [
            f"开关：{'开' if self.cfg.proactive_enabled else '关'}；回避：{'是' if self.emotion.is_avoidant() else '否'}",
            f"今日已主动：{state.get('proactive_count') or 0} / {self.cfg.proactive_daily_limit}（{state.get('proactive_day') or '—'}）",
            f"上次主动：{_fmt_ts(int(state.get('last_proactive_ts') or 0))}；冷却 {self.cfg.proactive_cooldown_hours:g} 小时",
            f"概率：{chance:.2f}（沉默 {int((time.time() - float(state.get('last_owner_ts') or 0)) // 60)} 分钟）",
            f"判定：{'会开口 → ' + plan.umo + '（' + plan.reason + '）' if plan else '现在不开口'}",
            "加 force 可以真的发一条试试。",
        ]
        yield event.plain_result("\n".join(lines))

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
        """记忆相关：list / facts / add / drop / due / summarize / forget"""

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
            yield event.plain_result("还没有记忆条目。跟她说「记住…」试试，或者 /kahn memory add <内容>。")
            return
        limit = max(1, min(int(limit), 50))
        lines = [f"记忆条目（共 {len(rows)} 条，显示最近 {min(limit, len(rows))} 条）："]
        for row in rows[:limit]:
            when = _fmt_ts(row["created_ts"])
            flags = ["owner" if row["source"] == "owner" else "obs"]
            if row["superseded_by"]:
                flags.append(f"已被{row['superseded_by']}取代")
            if row["archived"]:
                flags.append("已归档")
            if row["due_ts"]:
                flags.append(f"到期 {format_due(int(row['due_ts']), self.tz)}")
            tail = "".join(f"〔{flag}〕" for flag in flags)
            lines.append(
                f"#{row['fact_id']} {when} 重要度{row['importance']} {row['text']}{tail}"
            )
        yield event.plain_result("\n".join(lines))

    @kahn_memory.command("add")
    async def kahn_memory_add(self, event: AstrMessageEvent, raw: GreedyStr = ""):
        """手动加一条记忆（也可以直接说「记住…」）"""
        if self.store is None or self.writer is None:
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
        scope = self._scope_of(event)
        label = "主人" if self._is_owner(event, sender_id) else "管理员"
        try:
            result = await self.writer.add(
                text=text,
                source="owner",
                kind="fact",
                importance=importance,
                scope=scope,
                speaker=label,
                uid=sender_id,
                umo=event.unified_msg_origin or "",
                visibility="private",
            )
        except Exception:
            logger.exception("%s 写入记忆失败", PLUGIN_NAME)
            yield event.plain_result("写入记忆失败，请查看日志。")
            return
        if result.reason != "added":
            yield event.plain_result("这条记忆已经记过了。")
            return
        if importance is None and self.cfg.score_importance:
            self._spawn(self._rescore(result.fact_id))
        yield event.plain_result(f"好，我记下了：{text}（#{result.fact_id}）")

    @kahn_memory.command("drop")
    async def kahn_memory_drop(self, event: AstrMessageEvent, fact_id: str = ""):
        """删除一条记忆：#id 见 facts"""
        if self.store is None or self.writer is None:
            yield event.plain_result("数据库不可用，请查看日志。")
            return
        if not self._is_admin(event.get_sender_id()):
            yield event.plain_result("只有主人或 AstrBot 管理员可以删除记忆。")
            return
        target = str(fact_id or "").strip().lstrip("#")
        if not target:
            yield event.plain_result("用法：/kahn memory drop <fact_id>")
            return
        await self.reminder.cancel(target)
        if await self.writer.drop(target):
            yield event.plain_result(f"已删除记忆 #{target}。")
        else:
            yield event.plain_result(f"没有找到记忆 #{target}。")

    @kahn_memory.command("due")
    async def kahn_memory_due(self, event: AstrMessageEvent, limit: int = 10):
        """查看待发的提醒"""
        if self.store is None:
            yield event.plain_result("数据库不可用，请查看日志。")
            return
        if not self._is_admin(event.get_sender_id()):
            yield event.plain_result("只有主人或 AstrBot 管理员可以查看提醒。")
            return
        rows = self.store.facts_with_due(int(time.time()))
        if not rows:
            yield event.plain_result("没有待发的提醒。")
            return
        limit = max(1, min(int(limit), 50))
        lines = [f"待发提醒（共 {len(rows)} 条）："]
        for row in rows[:limit]:
            phrase = self.files.schedule_phrase(row["fact_id"]) or row["text"]
            lines.append(
                f"#{row['fact_id']} {format_due(int(row['due_ts']), self.tz)} · {row['scope']}\n  {phrase}"
            )
        yield event.plain_result("\n".join(lines))

    @kahn_memory.command("summarize")
    async def kahn_memory_summarize(self, event: AstrMessageEvent, mode: str = ""):
        """立刻跑一次汇总（/kahn memory summarize force 连水位也不管）"""
        if self.store is None or self.summarizer is None:
            yield event.plain_result("数据库不可用，请查看日志。")
            return
        if not self._is_admin(event.get_sender_id()):
            yield event.plain_result("只有主人或 AstrBot 管理员可以触发汇总。")
            return
        force = str(mode or "").strip().lower() in {"force", "now", "强制", "立即"}
        try:
            stats = await self.summarizer.run(force=force)
        except Exception:
            logger.exception("%s 汇总失败", PLUGIN_NAME)
            yield event.plain_result("汇总失败，请查看日志。")
            return
        yield event.plain_result(
            f"汇总完成：{stats.get('rows', 0)} 条流水 → 候选 {stats.get('items', 0)} 条"
            f" → 写入 {stats.get('written', 0)} 条（{stats.get('reason', '')}）。"
        )

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
        for task in list(self._tasks):
            task.cancel()
        for task in (self._startup_task, self._maintenance_task):
            if task is not None and not task.done():
                task.cancel()
        await self.heartbeat.stop()
        save_state(self.state_path, self.emotion.snapshot())
        if self.store is not None:
            self.store.close()
            self.store = None
        logger.info("%s 已卸载", PLUGIN_NAME)

    # ------------------------------------------------------------------ 记忆写入

    async def _try_remember(
        self,
        event: AstrMessageEvent,
        reason: str,
        text: str,
        sender_id: str,
        private: bool,
    ) -> bool:
        """「记住…」：写库 + 回执 + 后台打分/挂提醒；返回是否已处理。"""
        if not self.cfg.remember_enabled or self.writer is None:
            return False
        intent = remember.detect(
            text,
            woken=True,
            private=private,
            wake_names=self.cfg.wake_names,
            tz=self.tz,
        )
        if intent is None:
            return False

        owner = self._is_owner(event, sender_id)
        speaker = "主人" if owner else (event.get_sender_name() or sender_id or "群友")
        try:
            result = await self.writer.add(
                text=intent.text,
                source="owner" if owner else "observed",
                kind="fact",
                importance=None,
                due_ts=intent.due_ts,
                scope=self._scope_of(event),
                speaker=speaker,
                uid=sender_id,
                umo=event.unified_msg_origin or "",
                visibility="private" if owner else "public",
            )
        except Exception:
            logger.exception("%s 自动写入记忆失败", PLUGIN_NAME)
            return False

        receipt = self._remember_receipt(intent, result, owner)
        try:
            await event.send(MessageChain([Plain(receipt)]))
        except Exception:
            logger.exception("%s 发送记忆回执失败", PLUGIN_NAME)
        event.stop_event()

        if result.reason == "added":
            if self.cfg.score_importance:
                self._spawn(self._rescore(result.fact_id))
            if intent.due_ts and owner:
                self._spawn(self.reminder.schedule_fact(result.fact_id))
        if self.cfg.debug:
            logger.debug(
                "KahnBot 自动记忆（%s）：%s → #%s（%s）",
                reason,
                intent.text[:40],
                result.fact_id,
                result.reason,
            )
        return True

    def _remember_receipt(self, intent, result, owner: bool) -> str:
        if result.reason == "duplicate":
            return "这条我记过了。"
        if result.reason != "added":
            return "这条先不记。"
        tail = "，替换了旧的那条" if result.superseded else ""
        due = ""
        if intent.due_ts and owner:
            due = f"；到 {format_due(intent.due_ts, self.tz)} 我提醒你"
        return f"好，我记下了：{intent.text}{due}（#{result.fact_id}{tail}）"

    async def _rescore(self, fact_id: str) -> None:
        try:
            await self.writer.rescore(fact_id)
        except Exception:
            logger.exception("%s 补打分失败（%s）", PLUGIN_NAME, fact_id)

    def _scope_of(self, event: AstrMessageEvent) -> str:
        group_id = event.get_group_id() or ""
        return f"群:{group_id}" if group_id else "私聊"

    # ------------------------------------------------------------------ 后台

    def _on_tick(self, missed: int) -> None:
        """心跳 tick 是同步回调：把维护工作丢给事件循环，别阻塞 tick。"""
        if self._maintenance_task is not None and not self._maintenance_task.done():
            return
        try:
            self._maintenance_task = asyncio.create_task(self._maintenance())
        except RuntimeError:  # pragma: no cover - 理论上 tick 总在循环里
            logger.debug("心跳 tick 没有事件循环，跳过维护")

    async def _maintenance(self) -> None:
        try:
            if self.reminder is not None:
                await self.reminder.poll_due()
            # M6：主动说话（情绪概率路）。放在提醒之后：到点的正事优先。
            if self.proactive is not None:
                await self.proactive.maybe_run()
            if self.summarizer is not None:
                await self.summarizer.maybe_run()
            if self.writer is not None:
                await self.writer.maintain()
            # 主动说话会改 state（冷却 / 日限），维护完统一落一次盘
            save_state(self.state_path, self.emotion.snapshot())
        except Exception:
            logger.exception("%s 后台维护失败", PLUGIN_NAME)

    def _ensure_startup(self) -> None:
        if self._startup_task is not None:
            return
        try:
            self._startup_task = asyncio.create_task(self._startup())
        except RuntimeError:
            # __init__ 有时拿不到运行中的循环；第一条消息时再补（与心跳同样的处理）
            self._startup_task = None

    async def _startup(self) -> None:
        try:
            self.files.ensure_files()
            if self.reminder is not None:
                await self.reminder.restore()
            if self.writer is not None:
                await self.writer.maintain()
            if self.retriever is not None and self.store is not None:
                await self.retriever.ensure_embeddings(limit=50)
        except Exception:
            logger.exception("%s 启动任务失败", PLUGIN_NAME)

    def _spawn(self, coro) -> None:
        """后台小任务：不阻塞回执，出问题只记日志。"""
        try:
            task = asyncio.create_task(coro)
        except RuntimeError:  # pragma: no cover
            coro.close()
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _send_text(self, umo: str, text: str) -> bool:
        """提醒用：直接按会话发一句，不经过 LLM。"""
        try:
            return bool(await self.context.send_message(umo, MessageChain([Plain(text)])))
        except Exception:
            logger.exception("%s 发送提醒失败（%s）", PLUGIN_NAME, umo)
            return False

    def _must_reply(self, reason: str, text: str) -> bool:
        """回避档里也必须回的情况：被叫到、被拍、被问"是不是生气了"、道歉。"""
        if reason in (wake.AT, wake.POKE):
            return True
        value = (text or "").strip()
        return any(word in value for word in ("对不起", "抱歉", "我错了", "别生气", "在吗", "你生气了", "是不是生气了"))

    def _inject_unanswered(self, req: ProviderRequest) -> None:
        """把回避期间攒下的话贴进本轮请求：她"其实都记得，只是当时没回"。"""
        if self.emotion.is_avoidant():
            return
        try:
            backlog = self.emotion.take_unanswered()
        except Exception:
            return
        if not backlog:
            return
        lines = ["<unanswered>", "你刚才不太想说话的时候，对方还发过这些："]
        for item in backlog[-5:]:
            lines.append(f"- {str((item or {}).get('text') or '')[:120]}")
        lines.append("（其实你都记得，只是当时没回。别逐条复述，自然地接上就行。）")
        lines.append("</unanswered>")
        block = "\n".join(lines)
        if TextPart is not None:
            req.extra_user_content_parts.append(TextPart(text=block).mark_as_temp())
        else:  # pragma: no cover
            req.system_prompt = (req.system_prompt or "") + "\n" + block

    async def _remember_proactive_turn(self, umo: str, text: str) -> None:
        """主动说的话也要进历史，否则下一轮模型不知道自己刚才说过什么（F16）。"""
        manager = getattr(self.context, "conversation_manager", None)
        if manager is None or not umo or not text:
            return
        try:
            cid = await manager.get_curr_conversation_id(umo)
            if not cid:
                return
            conv = await manager.get_conversation(umo, cid)
            history = list(getattr(conv, "content", None) or [])
            history.append({"role": "assistant", "content": text})
            await manager.update_conversation(umo, history=history)
        except Exception:
            logger.exception("%s 回写主动消息历史失败", PLUGIN_NAME)

    def _fact_counts(self) -> tuple[int, int]:
        if self.store is None:
            return 0, 0
        owner = observed = 0
        for row in self.store.facts(include_archived=True, include_superseded=True):
            if row["source"] == "owner":
                owner += 1
            else:
                observed += 1
        return owner, observed

    # ------------------------------------------------------------------ 情绪

    def _touch_emotion(self, event: AstrMessageEvent, sender_id: str, text: str) -> None:
        """把这条消息翻译成情绪变化（M3）。

        只有主人能推动 blame（F20）；心跳没起来时在这里补启动，
        因为 `__init__` 有时拿不到正在运行的事件循环。
        """
        self._last_activity = time.time()
        if not self.heartbeat.running and self.heartbeat.start():
            self._ensure_startup()
        try:
            changed = False
            if self._is_owner(event, sender_id):
                changed = self.emotion.on_owner_text(text) is not None
            if wake.is_poke_to_self(event, str(event.get_self_id() or "")):
                self.emotion.on_poke()
                changed = True
            # M6：回避档的进入/退出（blame 持续高位、被吓到、时间到了）
            if self.emotion.check_avoidant(private=event.is_private_chat()):
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

    def _global_timezone(self) -> str:
        try:
            return str(self.context.get_config().get("timezone") or "")
        except Exception:
            return ""

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
