"""M2 离线冒烟：唤醒判定、占位符、配额、防抖窗口。不启动 AstrBot。

跑法：

    "C:\\Users\\<你>\\AppData\\Roaming\\uv\\tools\\astrbot\\Scripts\\python.exe" tests/smoke_m2.py
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT.parent))

from astrbot.api.message_components import At, Face, Image, Poke  # noqa: E402

from astrbot_plugin_kahnbot.core import wake  # noqa: E402
from astrbot_plugin_kahnbot.core.config import load_config  # noqa: E402
from astrbot_plugin_kahnbot.core.recorder import Recorder  # noqa: E402
from astrbot_plugin_kahnbot.core.store import Store  # noqa: E402
from astrbot_plugin_kahnbot.main import KahnBot  # noqa: E402

BASE = REPO_ROOT / ".smoke_tmp"
SELF_ID = "3609107527"


def tmpdir(name: str) -> Path:
    path = BASE / name
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    return path


class FakeEvent:
    def __init__(
        self,
        text: str = "",
        sender: str = "10001",
        name: str = "主人",
        group: str | None = "123456",
        private: bool = False,
        components: list | None = None,
        activated: list | None = None,
    ) -> None:
        self._text = text
        self._sender = sender
        self._name = name
        self._group = group
        self._private = private
        self.unified_msg_origin = (
            f"kahnbot:FriendMessage:{sender}" if private else f"kahnbot:GroupMessage:{group}"
        )
        self.message_obj = type(
            "Obj",
            (),
            {"message_id": f"mid-{text or 'x'}", "message": components or [], "self_id": SELF_ID},
        )()
        self.is_at_or_wake_command = private
        self._extra = {"activated_handlers": activated or []}
        self.stopped = False

    # astr_message_event 的接口
    def get_message_str(self):
        return self._text

    def get_sender_id(self):
        return self._sender

    def get_sender_name(self):
        return self._name

    def get_group_id(self):
        return "" if self._private else (self._group or "")

    def get_self_id(self):
        return SELF_ID

    def get_platform_name(self):
        return "aiocqhttp"

    def is_private_chat(self):
        return bool(self._private)

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)

    def set_extra(self, key, value):
        self._extra[key] = value

    def stop_event(self):
        self.stopped = True


class FakeContext:
    def __init__(self, admins=None):
        self._cfg = {"admins_id": admins or ["astrbot"]}

    def get_config(self):
        return self._cfg


def test_wake_detect() -> None:
    cfg = load_config({"wake_names": "鱼宝,大肥鱼"})
    assert wake.detect(FakeEvent("随便聊聊"), cfg) is None
    assert wake.detect(FakeEvent("鱼宝在吗"), cfg) == wake.NAME
    assert wake.detect(FakeEvent("大肥鱼，出来"), cfg) == wake.NAME
    assert wake.detect(FakeEvent("", components=[At(qq=SELF_ID), ]), cfg) == wake.AT
    assert wake.detect(FakeEvent("", components=[At(qq="999")]), cfg) is None
    assert wake.detect(FakeEvent("", components=[Poke(id=SELF_ID)]), cfg) == wake.POKE
    assert wake.detect(FakeEvent("", components=[Poke(id="999")]), cfg) is None  # 群友互拍
    assert wake.detect(FakeEvent("在吗", private=True), cfg) == wake.PRIVATE

    no_name = load_config({"wake_on_name": False})
    assert wake.detect(FakeEvent("鱼宝在吗"), no_name) is None
    no_poke = load_config({"wake_on_poke": False})
    assert wake.detect(FakeEvent("", components=[Poke(id=SELF_ID)]), no_poke) is None
    print("wake.detect ok")


def test_command_detect() -> None:
    class CommandFilter:
        pass

    handler = type("H", (), {"event_filters": [CommandFilter()]})()
    assert wake.is_command_event(FakeEvent("kahn status", activated=[handler])) is True
    assert wake.is_command_event(FakeEvent("鱼宝在吗")) is False
    print("wake.is_command_event ok")


def test_placeholders() -> None:
    store = Store(tmpdir("placeholder") / "kahnbot.db")
    store.open()
    rec = Recorder(store, load_config({"owner_ids": ["10001"]}))
    assert rec.record(FakeEvent("", components=[Image(file="a.jpg")])) is True
    assert rec.record(FakeEvent("", components=[Face(id=1)])) is True
    assert rec.record(FakeEvent("", components=[Poke(id=SELF_ID)])) is True
    texts = [row["text"] for row in store.list_messages(limit=10)]
    assert sorted(texts) == ["[图片]", "[拍一拍]", "[表情]"], texts
    store.close()
    print("placeholder ok")


def test_quota() -> None:
    store = Store(tmpdir("quota") / "kahnbot.db")
    store.open()
    kb = object.__new__(KahnBot)
    kb.cfg = load_config({"quota_daily": 2, "owner_ids": ["10001"]})
    kb.store = store
    kb.context = FakeContext()

    assert kb._quota_allows("20002", private=False) is True
    kb._bump_quota("20002", private=False)
    kb._bump_quota("20002", private=False)
    assert kb._quota_used("20002", private=False) == 2
    assert kb._quota_allows("20002", private=False) is False  # 用完了
    assert kb._quota_allows("10001", private=False) is True  # 主人不计
    assert kb._quota_allows("20002", private=True) is True  # 私聊不计
    assert kb._quota_allows("astrbot", private=False) is True  # 管理员不计
    store.close()
    print("quota ok")


def test_debounce() -> None:
    kb = object.__new__(KahnBot)
    kb.cfg = load_config({"debounce_seconds": 0.12, "debounce_max_seconds": 1.0})
    kb._pending = {}

    async def scenario() -> None:
        first = asyncio.create_task(kb._settle(FakeEvent("鱼宝"), wake.NAME, "鱼宝"))
        await asyncio.sleep(0.03)
        second = asyncio.create_task(kb._settle(FakeEvent("在吗"), wake.NAME, "在吗"))
        await asyncio.sleep(0.03)
        third = asyncio.create_task(kb._settle(FakeEvent("下周有空吗"), wake.NAME, "下周有空吗"))
        assert await first is None
        assert await second is None
        assert await third == "鱼宝\n在吗\n下周有空吗"
        assert kb._pending == {}

        # 窗口关闭时直接透传
        kb.cfg = load_config({"debounce_seconds": 0})
        assert await kb._settle(FakeEvent("单句"), wake.NAME, "单句") == "单句"

    asyncio.run(scenario())
    print("debounce ok")


def test_settle_cap() -> None:
    """连续消息不断顺延，但总等待不超过 debounce_max_seconds。"""
    kb = object.__new__(KahnBot)
    kb.cfg = load_config({"debounce_seconds": 5.0, "debounce_max_seconds": 0.2})
    kb._pending = {}

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        start = loop.time()
        result = await kb._settle(FakeEvent("一句"), wake.NAME, "一句")
        elapsed = loop.time() - start
        assert result == "一句"
        assert elapsed < 0.5, elapsed  # 被上限裁到 0.2s

    asyncio.run(scenario())
    print("settle cap ok")


if __name__ == "__main__":
    test_wake_detect()
    test_command_detect()
    test_placeholders()
    test_quota()
    test_debounce()
    test_settle_cap()
    shutil.rmtree(BASE, ignore_errors=True)
    print("ALL OK")
