"""KahnBot —— AstrBot 插件入口。

这里只做三件事：读配置、建数据层、注册 handler / 指令；业务逻辑都在 `core/` 下。

当前进度：M8（分段发送）：回复拆成 1~3 条，按字数估打字间隔，私聊显示"正在输入"，
不再依赖 AstrBot 原生的机械分段。
"""

from __future__ import annotations

import asyncio
import datetime
import random
import time
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.api.message_components import At, Plain, Reply
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
from .core.emotion import Emotion, default_state
from .core.heartbeat import Heartbeat
from .core.injector import Injector, build_block, build_recent_chat, drop_warm
from .core.llm import LLMClient
from .core.memory import MemoryWriter
from .core.memoryfiles import MemoryFiles
from .core.proactive import Proactive
from .core.recorder import Recorder
from .core.reminder import ReminderService, resolve_tz
from .core.reset import reset_all
from .core.retriever import Retriever
from .core.segments import JITTER, bubble_delay, split_bubbles
from .core.state import load_state, save_state
from .core.store import Store, now_ts
from .core.summarizer import Summarizer
from .core.when import format_due

PLUGIN_NAME = "astrbot_plugin_kahnbot"
PLUGIN_VERSION = "0.4.1"
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
            self.writer = MemoryWriter(
                self.store, self.files, self.embedder, self.cfg, self.llm, tz=self.tz
            )
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
            on_sent=self._after_reminder_sent,
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
            on_sent=self._after_proactive_sent,
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

        # 平台把她自己的消息回推过来时，只按开关记录、绝不唤醒——
        # 否则私聊里她会回自己，吵个没完。
        self_id = event.get_self_id() or ""
        if self_id and sender_id == self_id:
            try:
                self.recorder.record(event)
            except Exception:
                logger.exception("%s 记录自己的消息失败", PLUGIN_NAME)
            return

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
        """把心情、长期记忆、最近几句对话贴到本轮请求里（F11/F14/F15/F19）。"""
        if not self.cfg.enabled or self.store is None:
            return
        if (event.get_platform_name() or "") == "cron":
            return  # 提醒任务不查记忆，免得自己提醒自己
        query = (req.prompt or event.get_message_str() or "").strip()
        if len(query) < 2:
            return

        umo = event.unified_msg_origin or ""
        owner_context = self._is_owner(event, event.get_sender_id() or "")
        avoidant = self.emotion.is_avoidant()
        self._inject_unanswered(req)

        # 心情每轮都贴：哪怕只是"平常"，语气也该是连续的（F19）
        if self.cfg.inject_emotion:
            self._append_part(req, self.emotion.render(always=True))

        # 回避档里先不翻记忆——正在气头上，翻出来只会更难受（emotion-design §5）
        if self.cfg.inject_enabled and not avoidant:
            await self._inject_recall(req, query, umo, owner_context)

        # 最近几句对话：让她接得上话头（F11/F14）；回避时也不贴
        if self.cfg.recent_chat_messages > 0 and not avoidant:
            self._inject_recent_chat(req, umo)

    async def _inject_recall(
        self,
        req: ProviderRequest,
        query: str,
        umo: str,
        owner_context: bool,
    ) -> None:
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
        if self.emotion.mode == "angry":
            hits = drop_warm(hits)  # 生气时先把"暖"的记忆收起来
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
        self._append_part(req, block)
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

    def _inject_recent_chat(self, req: ProviderRequest, umo: str) -> None:
        try:
            rows = self.store.recent_messages(umo, limit=self.cfg.recent_chat_messages)
        except Exception:
            logger.exception("%s 读取最近对话失败", PLUGIN_NAME)
            return
        block = build_recent_chat(rows, self.cfg.recent_chat_max_chars)
        self._append_part(req, block)

    def _append_part(self, req: ProviderRequest, text: str | None) -> None:
        """临时内容块：不写 system_prompt，否则前缀缓存每轮都会失效（capabilities §13）。"""
        if not text:
            return
        if TextPart is not None:
            req.extra_user_content_parts.append(TextPart(text=text).mark_as_temp())
        else:  # pragma: no cover - 只在旧版本 AstrBot 上走这里
            req.system_prompt = (req.system_prompt or "") + "\n" + text

    # ------------------------------------------------------------------ 分段发送

    @filter.on_decorating_result(priority=1000)
    async def segment_reply(self, event: AstrMessageEvent) -> None:
        """把模型的一整段回复拆成几条，按打字节奏发出去（M8）。

        原生 segmented_reply 只按标点切、没有条数上限、间隔固定；这里自己
        拆自己发，条数与节奏都由我们决定，原生开关应该保持关闭。
        """
        if not self.cfg.segmented_reply:
            return
        if (event.get_platform_name() or "") != "aiocqhttp":
            return
        # 只处理模型回复：指令结果是控制流，不该被拆成好几条
        if event.get_extra("provider_request") is None:
            return
        result = event.get_result()
        if result is None or not result.chain:
            return
        if not all(isinstance(comp, Plain) for comp in result.chain):
            return  # 带图片、语音、引用等的回复不拆

        text = "".join(comp.text for comp in result.chain).strip()
        bubbles = split_bubbles(text, max_bubbles=self._bubble_limit())
        if len(bubbles) <= 1:
            return

        event.clear_result()  # 不再让 respond stage 发原文
        header = self._reply_header(event)
        for index, bubble in enumerate(bubbles):
            chain = MessageChain([header, Plain(bubble)]) if index == 0 and header else MessageChain([Plain(bubble)])
            try:
                await event.send(chain)
            except Exception:
                logger.exception("%s 分段发送失败", PLUGIN_NAME)
                return
            if index < len(bubbles) - 1:
                await self._show_typing(event)
                await asyncio.sleep(bubble_delay(bubble, jitter=random.uniform(0.0, JITTER)))

        if self.cfg.debug:
            logger.debug("KahnBot 分 %s 条发送：%s", len(bubbles), " / ".join(bubbles))

    def _bubble_limit(self) -> int:
        """生气或回避时只发一条：话少、气短，才像在闹别扭。"""
        if self.emotion.mode in ("angry", "avoidant"):
            return 1
        return self.cfg.segmented_max_bubbles

    def _reply_header(self, event: AstrMessageEvent):
        """沿用 AstrBot 的引用 / at 设置，只是把它挂到第一条上。"""
        config = self.context.get_config()
        settings = (config or {}).get("platform_settings") or {}
        header = []
        if settings.get("reply_with_quote"):
            message_id = getattr(getattr(event, "message_obj", None), "message_id", None)
            if message_id:
                header.append(Reply(id=message_id))
        if settings.get("reply_with_mention") and not event.is_private_chat():
            header.append(At(qq=event.get_sender_id(), name=event.get_sender_name()))
        return header

    async def _show_typing(self, event: AstrMessageEvent) -> None:
        """私聊里让 QQ 显示"正在输入"。不支持的平台/协议端静默跳过。"""
        if not self.cfg.segmented_typing or not event.is_private_chat():
            return
        if getattr(self, "_typing_supported", True) is False:
            return
        bot = getattr(event, "bot", None)
        if bot is None or not hasattr(bot, "call_action"):
            return
        try:
            await bot.call_action(
                "set_input_status",
                user_id=int(event.get_sender_id()),
                event_type=1,
            )
        except Exception:
            # 协议端不认这个扩展 API 时只报一次，之后不再尝试
            self._typing_supported = False
            if self.cfg.debug:
                logger.debug("%s 协议端不支持 set_input_status，跳过「正在输入」", PLUGIN_NAME)

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
            f"上下文：心情{'贴' if self.cfg.inject_emotion else '不贴'}；最近对话 {self.cfg.recent_chat_messages} 条（上限 {self.cfg.recent_chat_max_chars} 字）",
            f"分段发送：{'开' if self.cfg.segmented_reply else '关'}（最多 {self.cfg.segmented_max_bubbles} 条；{'私聊显示正在输入' if self.cfg.segmented_typing else '不显示正在输入'}）",
            "当前进度：M8（回复拆成几条慢慢发；提醒会记进历史）。",
        ]
        yield event.plain_result("\n".join(lines))

    @kahn.command("reset")
    async def kahn_reset(self, event: AstrMessageEvent, mode: str = ""):
        """清空所有数据（记忆、流水、提醒、情绪）；配置不动。要加 confirm"""
        if not self._is_owner(event, event.get_sender_id() or ""):
            yield event.plain_result("只有主人可以重置数据。")
            return
        confirm = str(mode or "").strip().lower() in {"confirm", "确认", "yes", "是"}
        if not confirm:
            owner_count, observed_count = self._fact_counts()
            total = self.store.count_messages() if self.store else 0
            backup = f"会先备份到 {self.cfg.reset_backup_dir}" if self.cfg.reset_backup_dir else "不备份"
            yield event.plain_result(
                f"这会清空：流水 {total} 条、记忆 {owner_count + observed_count} 条"
                f"（owner {owner_count} / observed {observed_count}）、提醒排期、情绪与主动说话计数。\n"
                f"插件配置不动；{backup}。\n"
                "确定的话发送：/kahn reset confirm"
            )
            return

        # AstrBot 自己的会话历史也要清：否则她还能从旧对话里接上"刚聊过什么"。
        try:
            umos = self.store.distinct_umos() if self.store else []
        except Exception:
            umos = []

        try:
            report = reset_all(
                self.store,
                self.files,
                self.state_path,
                backup_root=self.cfg.reset_backup_dir or None,
            )
        except Exception:
            logger.exception("%s 重置失败", PLUGIN_NAME)
            yield event.plain_result("重置失败，请查看日志；数据没有改动。")
            return

        # 内存里的情绪、提醒 job、检索缓存也要跟着归零，否则下一个 tick 又把旧状态写回来。
        try:
            self.emotion.state = default_state(int(time.time()))
            self.emotion.state["mode"] = self.emotion.mode
        except Exception:
            logger.exception("%s 重置情绪失败", PLUGIN_NAME)
        try:
            if self.injector is not None:
                self.injector.reset()
        except Exception:
            logger.exception("%s 重置注入去重失败", PLUGIN_NAME)
        try:
            for umo in umos:
                await self.context.conversation_manager.new_conversation(umo)
        except Exception:
            logger.exception("%s 重置 AstrBot 会话失败（可手动 /new）", PLUGIN_NAME)
        try:
            await self.reminder.restore()
        except Exception:
            logger.exception("%s 重置提醒 job 失败", PLUGIN_NAME)
        try:
            self.files.ensure_files()
        except Exception:
            logger.exception("%s 重建记忆文件失败", PLUGIN_NAME)

        lines = [
            f"已重置：清掉流水 {report.get('messages', 0)} 条、记忆 {report.get('facts', 0)} 条、提醒排期与情绪状态。",
        ]
        if umos:
            lines.append(f"给 {len(umos)} 个会话开了新对话（清掉模型侧的上下文）。")
        if report.get("backup"):
            lines.append(f"备份在：{report['backup']}")
        lines.append("插件配置没有动。")
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
        # 先取消、再等它们真的退出，然后才关数据库：否则后台任务可能还握着连接，
        # 取消得干净，重载时才不会冒出 "Task was destroyed but it is pending"。
        pending = {
            task
            for task in (*self._tasks, self._startup_task, self._maintenance_task)
            if task is not None and not task.done()
        }
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.clear()
        self._startup_task = None
        self._maintenance_task = None
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
            self._record_outgoing(event.unified_msg_origin or "", receipt)
        except Exception:
            logger.exception("%s 发送记忆回执失败", PLUGIN_NAME)
        event.stop_event()

        if result.reason == "added":
            # 打分与"到点说什么"是同一次调用：先让模型写文案，再挂提醒，
            # 这样 schedule.md 里存的就是那句给主人听的话，而不是原指令。
            self._spawn(self._finalize_reminder(result.fact_id, bool(intent.due_ts and owner)))
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

    async def _finalize_reminder(self, fact_id: str, due: bool) -> None:
        """后台：打分 + 写提醒文案，然后挂提醒。

        顺序很重要：`schedule.md` 里的文案来自这次调用，挂 job 时必须已经写好；
        反过来的话，到点发出去的就是"提醒你一下：10分钟后提醒我关火"。
        """
        phrase = ""
        try:
            phrase = await self.writer.rescore(fact_id)
        except Exception:
            logger.exception("%s 补打分失败（%s）", PLUGIN_NAME, fact_id)
        if not due:
            return
        try:
            await self.reminder.schedule_fact(fact_id, phrase=phrase)
        except Exception:
            logger.exception("%s 挂提醒失败（%s）", PLUGIN_NAME, fact_id)

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
            # M7：临近日程/纪念日 → 惦记、高兴（F19 的两个联动点）
            self._apply_schedule_hooks()
            # M6：主动说话（情绪概率路）。放在提醒之后：到点的正事优先。
            if self.proactive is not None:
                await self.proactive.maybe_run()
            if self.summarizer is not None:
                await self.summarizer.maybe_run()
            if self.writer is not None:
                await self.writer.maintain()
            await self._backfill_embeddings()
            # 主动说话会改 state（冷却 / 日限），维护完统一落一次盘
            save_state(self.state_path, self.emotion.snapshot())
        except Exception:
            logger.exception("%s 后台维护失败", PLUGIN_NAME)

    def _apply_schedule_hooks(self) -> None:
        """把"日程/纪念日"翻译成心情变化；每天最多各触发一次（emotion 内部去重）。"""
        if self.store is None:
            return
        now = int(time.time())
        changed = False
        try:
            soon = [
                row
                for row in self.store.facts_with_due(now, now + 3 * 86400)
                if (row["kind"] or "") == "plan"
            ]
            if soon and self.emotion.on_plan_soon(now):
                changed = True
        except Exception:
            logger.exception("%s 检查临近日程失败", PLUGIN_NAME)
        try:
            local = datetime.datetime.fromtimestamp(now, self.tz)
            day_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
            day_end = day_start + datetime.timedelta(days=1)
            today = [
                row
                for row in self.store.facts_with_due(
                    int(day_start.timestamp()) - 1, int(day_end.timestamp())
                )
                if (row["kind"] or "") == "anniversary"
            ]
            if today and self.emotion.on_anniversary(now):
                changed = True
        except Exception:
            logger.exception("%s 检查纪念日失败", PLUGIN_NAME)
        if changed:
            save_state(self.state_path, self.emotion.snapshot())

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
            self._warn_conflicts()
            if self.reminder is not None:
                await self.reminder.restore()
            if self.writer is not None:
                await self.writer.maintain()
            if self.retriever is not None and self.embedder is not None:
                await self._warm_embeddings()
        except Exception:
            logger.exception("%s 启动任务失败", PLUGIN_NAME)

    async def _backfill_embeddings(self) -> None:
        """维护时顺手补向量：启动那几分钟写入的记忆、换 provider 之后的旧数据。"""
        if self.retriever is None or self.embedder is None or not self.embedder.available:
            return
        done = await self.retriever.ensure_embeddings(limit=20)
        if done:
            logger.debug("%s 补算 %s 条记忆的向量", PLUGIN_NAME, done)

    async def _warm_embeddings(self) -> None:
        """等 provider 初始化完，再把没有向量的记忆补上。

        插件加载早于模型初始化（AstrBot 里先 `plugin_manager.reload()`、后
        `provider_manager.initialize()`），启动瞬间拿不到 embedding provider 是必然的。
        不在这里等一会儿的话，启动那几分钟写入的记忆会一直没有向量，近似去重和
        向量检索要等到下次重启才补得回来。
        """
        if not await self.embedder.wait_ready():
            logger.warning(
                "%s 等了约两分钟仍没拿到 embedding provider，检索继续用字面匹配", PLUGIN_NAME
            )
            return
        done = await self.retriever.ensure_embeddings(limit=200)
        logger.info(
            "%s 向量就绪：%s%s",
            PLUGIN_NAME,
            self.embedder.describe(),
            f"，补齐 {done} 条历史记忆" if done else "",
        )

    def _warn_conflicts(self) -> None:
        """启动时把"会和本插件打架"的 AstrBot 开关指出来（配置在 WebUI，插件改不了）。"""
        try:
            settings = self.context.get_config() or {}
        except Exception:  # pragma: no cover
            return
        provider_settings = settings.get("provider_settings") or {}
        capability = provider_settings.get("proactive_capability") or {}
        if capability.get("add_cron_tools"):
            logger.warning(
                "%s：AstrBot 给模型的 future_task 工具是开着的"
                "（provider_settings.proactive_capability.add_cron_tools=true）。"
                "模型会自己排提醒，和插件的提醒重复发；建议在 WebUI 配置里关掉，"
                "见 docs/DEPLOYMENT.md「部署前必须改的两项」。",
                PLUGIN_NAME,
            )

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
        """提醒/主动消息用：直接按会话发一句，不经过 LLM。"""
        try:
            ok = bool(await self.context.send_message(umo, MessageChain([Plain(text)])))
        except Exception:
            logger.exception("%s 发送失败（%s）", PLUGIN_NAME, umo)
            return False
        if ok:
            # 她说过的话要进流水，最近对话里才能标成"你："（F11/F14）。
            self._record_outgoing(umo, text)
        return ok

    def _must_reply(self, reason: str, text: str) -> bool:
        """回避档里也一定要回的情况：被叫到名字、被问"是不是生气了"、道歉。"""
        if reason == wake.AT:
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

    # ------------------------------------------------------------------ 主动消息收尾

    async def _after_proactive_sent(self, umo: str, text: str) -> None:
        """主动说完一句：记进流水（最近对话能看到）、回写会话历史（下一轮知道自己说过）。"""
        try:
            await self._write_assistant_turn(umo, text)
        except Exception:
            logger.exception("%s 主动消息回写历史失败（%s）", PLUGIN_NAME, umo)

    async def _after_reminder_sent(self, umo: str, text: str) -> None:
        """提醒也是"她主动说的"：进流水、回写历史（F16）。

        不占主动说话的日限：到点的正事不该挤掉她平时搭话的额度。
        """
        try:
            await self._write_assistant_turn(umo, text)
        except Exception:
            logger.exception("%s 提醒回写历史失败（%s）", PLUGIN_NAME, umo)

    def _record_outgoing(self, umo: str, text: str) -> None:
        """她说的话也进流水：最近对话里标成"你："，但汇总时不参与事实提炼。"""
        if self.store is None or not umo or not text:
            return
        try:
            parts = umo.split(":")
            platform = parts[0] if parts else ""
            group_id = parts[2] if len(parts) > 2 and "Group" in parts[1] else None
            self.store.add_message(
                ts=now_ts(),
                umo=umo,
                platform=platform,
                group_id=group_id,
                sender_id="self",
                sender_name=PLUGIN_NAME,
                is_owner=False,
                is_bot=True,
                text=text[:500],
            )
        except Exception:
            logger.exception("%s 记录主动消息失败", PLUGIN_NAME)

    async def _write_assistant_turn(self, umo: str, text: str) -> None:
        """绕开事件管线发出去的话，AstrBot 不会自己写进 history，必须手动补一轮。"""
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
                # 回避档里被主人连着拍会心软（M7），所以要告诉他这一下是谁拍的
                self.emotion.on_poke(owner=self._is_owner(event, sender_id))
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
