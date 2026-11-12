"""到点提醒（F16 的确定路）。

AstrBot 4.29.4 的 `add_basic_job` 没有 `run_at`/`run_once` 参数，只有 5 段 crontab。
所以这里用「精确到分钟的 crontab + `persistent=False`」：job 只活在内存里，
重启必然丢，启动时按 `fact_index.due_ts` 重挂——正好是设计里要的"重启恢复"。

两层兜底：

1. cron 到点 → `_fire`（先清 due_ts，天然幂等）；
2. 心跳 tick 里的 `poll_due` 扫 `due_ts <= now`，cron 不可用/错过也能发出去。

发完只清 `due_ts`，事实本身留在 `facts.md` 里继续可检索。
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from functools import partial
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)


PHRASE_PREFIX = "提醒主人一下："


def compose_phrase(text: str, kind: str = "") -> str:
    """兜底文案：模型没写出那句话时用。

    事实原文在写入时已经剥掉了"提醒我""10 分钟后"这类指令语，所以这里只加一句称呼，
    不再重复"提醒你一下：10分钟后提醒我关火"那种别扭话。
    """
    body = " ".join(str(text or "").split())
    if not body:
        return PHRASE_PREFIX.rstrip("：")
    return f"{PHRASE_PREFIX}{body}"


def resolve_tz(name: str | None):
    """时区名 → tzinfo；空值或非法值返回 None（用系统本地时区）。"""
    if not name:
        return None
    try:
        return ZoneInfo(str(name))
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("时区 %r 无效，提醒按系统本地时区计算", name)
        return None


class ReminderService:
    def __init__(
        self,
        *,
        plugin_name: str,
        store,
        files,
        cfg,
        send,
        context=None,
        tz_name: str | None = None,
        on_sent=None,
    ) -> None:
        self.plugin_name = plugin_name
        self.store = store
        self.files = files
        self.cfg = cfg
        self.send = send
        self.context = context
        # 主程序已经算好了"插件配置 → AstrBot 全局"的兜底时区，优先用它
        self.tz_name = tz_name if tz_name is not None else (cfg.timezone or "")
        self.tz = resolve_tz(self.tz_name)
        # 发出去之后回调主程序：回写会话历史、占一次主动说话的额度（F16）
        self.on_sent = on_sent
        self._jobs: dict[str, str] = {}
        self._firing: set[str] = set()

    # ------------------------------------------------------------------ 工具

    @property
    def prefix(self) -> str:
        return f"{self.plugin_name}:remind:"

    def job_name(self, fact_id: str) -> str:
        return f"{self.prefix}{fact_id}"

    def cron_expression(self, due_ts: int) -> str:
        dt = datetime.fromtimestamp(int(due_ts), self.tz)
        return f"{dt.minute:02d} {dt.hour:02d} {dt.day} {dt.month} *"

    def _cron(self):
        return getattr(self.context, "cron_manager", None)

    async def _maybe_await(self, value):
        if hasattr(value, "__await__"):
            return await value
        return value

    # ------------------------------------------------------------------ 挂/摘

    async def schedule_fact(self, fact_id: str, phrase: str = "") -> bool:
        if not self.cfg.reminder_enabled:
            return False
        row = self.store.get_fact(fact_id)
        if row is None or not row["due_ts"]:
            return False
        due = int(row["due_ts"])
        if due <= int(time.time()) + 30:
            return False
        cron = self._cron()
        if cron is None:
            return False
        name = self.job_name(fact_id)
        # 排期与措辞落 schedule.md：到点时不用再拼，改文案也只改文件（memory-design §10）
        try:
            phrase = (
                (phrase or "").strip()
                or self.files.schedule_phrase(fact_id)
                or compose_phrase(row["text"], row["kind"] or "")
            )
            self.files.add_schedule(due, row["scope"] or "私聊", fact_id, phrase, self.tz)
        except Exception:  # noqa: BLE001 - 排期文件写不了也要把 job 挂上
            logger.exception("写 schedule.md 失败（%s）", fact_id)
        try:
            job = await self._maybe_await(
                cron.add_basic_job(
                    name=name,
                    cron_expression=self.cron_expression(due),
                    handler=partial(self._fire, fact_id),
                    description=f"KahnBot 提醒 #{fact_id}",
                    timezone=self.tz_name or None,
                    persistent=False,
                )
            )
        except TypeError:
            # 老版本/参数不合：不带 description/timezone 再试一次。
            try:
                job = await self._maybe_await(
                    cron.add_basic_job(
                        name=name,
                        cron_expression=self.cron_expression(due),
                        handler=partial(self._fire, fact_id),
                        persistent=False,
                    )
                )
            except Exception:  # noqa: BLE001
                logger.exception("挂提醒失败（%s），交给心跳兜底", fact_id)
                return False
        except Exception:  # noqa: BLE001
            logger.exception("挂提醒失败（%s），交给心跳兜底", fact_id)
            return False
        job_id = getattr(job, "job_id", "")
        if job_id:
            self._jobs[fact_id] = str(job_id)
        logger.info("已挂提醒 #%s 于 %s", fact_id, self.cron_expression(due))
        return True

    async def restore(self) -> int:
        """启动时重挂：先清掉上一次进程留下的同名 job，再按 due_ts 扫一遍。"""
        if not self.cfg.reminder_enabled:
            return 0
        cron = self._cron()
        if cron is None:
            logger.info("cron_manager 不可用，提醒只靠心跳兜底")
            return 0
        try:
            jobs = await self._maybe_await(cron.list_jobs("basic"))
        except Exception:  # noqa: BLE001
            logger.exception("读取 cron 列表失败")
            return 0
        for job in jobs or []:
            name = getattr(job, "name", "") or ""
            if not name.startswith(self.prefix):
                continue
            try:
                await self._maybe_await(cron.delete_job(job.job_id))
            except Exception:  # noqa: BLE001
                logger.debug("清理旧提醒 job 失败：%s", name)

        now = int(time.time())
        count = 0
        for row in self.store.facts_with_due(now):
            if await self.schedule_fact(row["fact_id"]):
                count += 1
        if count:
            logger.info("重启恢复：重挂了 %s 个提醒", count)
        return count

    # ------------------------------------------------------------------ 触发

    def next_anniversary(self, due_ts: int) -> int:
        """纪念日发完之后推到明年同一时刻（2 月 29 日落到 2 月 28 日）。"""
        dt = datetime.fromtimestamp(int(due_ts), self.tz)
        try:
            nxt = dt.replace(year=dt.year + 1)
        except ValueError:
            nxt = dt.replace(year=dt.year + 1, day=28)
        return int(nxt.timestamp())

    async def _fire(self, fact_id: str) -> bool:
        if fact_id in self._firing:
            return False
        self._firing.add(fact_id)
        try:
            row = self.store.get_fact(fact_id)
            if row is None:
                await self._drop_job(fact_id)
                return False
            phrase = self.files.schedule_phrase(fact_id)
            umo = row["umo"] or ""
            if row["due_ts"] is None:
                await self._drop_job(fact_id)
                return False
            due = int(row["due_ts"])
            anniversary = (row["kind"] or "") == "anniversary"
            if anniversary:
                # 纪念日按年重复：due 推到明年，crontab（月/日/时/分）明年还会再命中
                try:
                    self.store.set_due(fact_id, self.next_anniversary(due))
                    self.files.add_schedule(
                        self.next_anniversary(due),
                        row["scope"] or "私聊",
                        fact_id,
                        phrase or compose_phrase(row["text"], row["kind"] or ""),
                        self.tz,
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("纪念日顺延失败（%s）", fact_id)
                    self.store.clear_due(fact_id)
            else:
                # 先清 due：cron 和兜底扫描同时到点也只会发一次。
                self.store.clear_due(fact_id)
                try:
                    self.files.remove_schedule(fact_id)
                except Exception:  # noqa: BLE001
                    logger.exception("清理 schedule.md 失败（%s）", fact_id)
                await self._drop_job(fact_id)
            if not umo:
                logger.warning("提醒 #%s 没有会话来源，发不出去", fact_id)
                return False
            text = phrase or compose_phrase(row["text"], row["kind"] or "")
            ok = bool(await self.send(umo, text))
            if ok:
                self.store.touch_facts([fact_id], int(time.time()))
                if self.on_sent is not None:
                    try:
                        await self._maybe_await(self.on_sent(umo, text))
                    except Exception:  # noqa: BLE001 - 回写失败不影响已发出的消息
                        logger.exception("提醒发送后的回调失败（%s）", fact_id)
            return ok
        finally:
            self._firing.discard(fact_id)

    async def _drop_job(self, fact_id: str) -> None:
        job_id = self._jobs.pop(fact_id, "")
        cron = self._cron()
        if not job_id or cron is None:
            return
        try:
            await self._maybe_await(cron.delete_job(job_id))
        except Exception:  # noqa: BLE001
            logger.debug("删除提醒 job 失败（%s）", fact_id)

    async def poll_due(self, now: int | None = None) -> int:
        """兜底：把已经到点、还没发出去的提醒发掉。

        已经挂了 cron job 的条目交给人 job 自己触发——同一条不能既被 cron 发一次、
        又被兜底扫一次（纪念日尤其明显：它到点后 due 推到明年而不是清空）。
        """
        if not self.cfg.reminder_enabled:
            return 0
        now = int(now if now is not None else time.time())
        fired = 0
        for row in self.store.due_facts(now):
            if row["fact_id"] in self._jobs:
                continue
            if await self._fire(row["fact_id"]):
                fired += 1
        return fired

    async def cancel(self, fact_id: str) -> None:
        self.store.clear_due(fact_id)
        self.files.remove_schedule(fact_id)
        await self._drop_job(fact_id)


__all__ = ["ReminderService", "resolve_tz"]
