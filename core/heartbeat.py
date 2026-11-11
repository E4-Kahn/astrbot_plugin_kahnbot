"""心跳：唯一的钟（M3 起只有情绪漂移，后续挂记忆老化与主动说话）。

- 每 30 秒醒一次，判断"该不该 tick"，而不是 sleep 一整个周期——
  避免长 sleep 在系统休眠/时钟跳变后失真（fish_heart 同样做法）。
- 关机几天再回来，用闭式解一次算完，不循环（`emotion.drift(missed)`）。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, Callable

from astrbot.api import logger

from . import state as state_file
from .emotion import Emotion

IDLE_INTERVAL = 45 * 60
ACTIVE_INTERVAL = 15 * 60
ACTIVE_WINDOW = 60 * 60
POLL_SECONDS = 30
MAX_MISSED_TICKS = 96


class Heartbeat:
    """一个可停止的轮询循环。所有异常都吞掉——心跳不能把插件带崩。"""

    def __init__(
        self,
        plugin_name: str,
        emotion: Emotion,
        state_path: Path,
        *,
        activity: Callable[[], float] | None = None,
        on_tick: Callable[[int], Any] | None = None,
    ) -> None:
        self.plugin_name = plugin_name
        self.emotion = emotion
        self.state_path = state_path
        self._activity = activity or (lambda: 0.0)
        self._on_tick = on_tick
        self._task: asyncio.Task | None = None
        self._stopped = False
        self.ticks = 0

    # ------------------------------------------------------------------ 生命周期

    def start(self) -> bool:
        """在事件循环里起一个后台任务；没有循环时返回 False（由调用方稍后重试）。"""
        if self._task is not None and not self._task.done():
            return True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        self._stopped = False
        self._task = loop.create_task(self._loop(), name=f"{self.plugin_name}-heartbeat")
        return True

    async def stop(self) -> None:
        self._stopped = True
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 - 关闭路径不该再抛
            pass

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    # ------------------------------------------------------------------ 循环

    async def _loop(self) -> None:
        while not self._stopped:
            try:
                await asyncio.sleep(POLL_SECONDS)
                if self._stopped:
                    break
                self.maybe_tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 心跳出错只记日志
                logger.exception("%s 心跳 tick 异常", self.plugin_name)

    def interval(self, now: float | None = None) -> int:
        """最近一小时有人说话就用活跃期节奏，否则慢一点。"""
        ts = time.time() if now is None else now
        return ACTIVE_INTERVAL if ts - float(self._activity() or 0) < ACTIVE_WINDOW else IDLE_INTERVAL

    def maybe_tick(self, now: float | None = None) -> bool:
        """到点就 tick 一次；返回是否真的执行了。"""
        ts = time.time() if now is None else now
        last = int(self.emotion.state.get("last_tick_ts") or 0)
        interval = self.interval(ts)
        if last and ts - last < interval:
            return False
        missed = 1 if not last else int((ts - last) // interval)
        missed = max(1, min(MAX_MISSED_TICKS, missed))
        self.tick(missed, now=ts)
        return True

    def tick(self, missed: int = 1, now: float | None = None) -> None:
        ts = time.time() if now is None else now
        self.emotion.drift(missed)
        self._apply_time_hooks(ts)
        self.emotion.state["last_tick_ts"] = int(ts)
        self.ticks += 1
        if self._on_tick is not None:
            try:
                self._on_tick(missed)
            except Exception:  # noqa: BLE001
                logger.exception("%s 心跳回调异常", self.plugin_name)
        self.save()

    def save(self) -> bool:
        return state_file.save_state(self.state_path, self.emotion.snapshot())

    # ------------------------------------------------------------------ 时间钩子

    def _apply_time_hooks(self, now: float) -> None:
        """长时间没人说话 → 想念（emotion-design §6 第一条）。"""
        last_owner = int(self.emotion.state.get("last_owner_ts") or 0)
        if not last_owner:
            return
        days = (now - last_owner) / 86400
        if days >= 3:
            self.emotion.on_miss(1.0)
