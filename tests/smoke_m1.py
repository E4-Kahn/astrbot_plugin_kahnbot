"""M1 离线冒烟：只验证数据层，不启动 AstrBot。

跑法（用 AstrBot 自带的 Python，或用装有 astrbot 的任意解释器）：

    "C:\\Users\\<你>\\AppData\\Roaming\\uv\\tools\\astrbot\\Scripts\\python.exe" tests/smoke_m1.py

注意：不用 tempfile.mkdtemp()——沙箱下它建出来的目录 ACL 不给写，SQLite 会报
"unable to open database file"。这里用普通 mkdir。
"""

from __future__ import annotations

import shutil
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT.parent))

# 导入 astrbot 会在当前目录铺一套 data/；把根指到系统临时目录，别弄脏仓库。
os.environ.setdefault("ASTRBOT_ROOT", str(Path(tempfile.gettempdir()) / "kahnbot-smoke-root"))

from astrbot_plugin_kahnbot.core.config import load_config  # noqa: E402
from astrbot_plugin_kahnbot.core.recorder import Recorder  # noqa: E402
from astrbot_plugin_kahnbot.core.store import SCHEMA_VERSION, Store  # noqa: E402

BASE = REPO_ROOT / ".smoke_tmp"


def tmpdir(name: str) -> Path:
    path = BASE / name
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    return path


def test_config() -> None:
    cfg = load_config({"owner_ids": "10001, 10002", "noise_patterns": "#sl，#help"})
    assert cfg.owner_ids == ("10001", "10002")
    assert cfg.is_owner("10002")
    assert not cfg.is_owner("20002")
    assert cfg.is_noise("#SL")
    assert not cfg.is_noise("正常聊天")
    empty = load_config(None)
    assert empty.enabled is True and empty.record_private is True
    print("config ok")


def test_store() -> None:
    db = tmpdir("store") / "kahnbot.db"
    store = Store(db)
    store.open()

    mode = store.conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal", mode
    assert store.get_meta("schema_version") == str(SCHEMA_VERSION)

    msg_id = store.add_message(
        ts=1762000000,
        umo="aiocqhttp:GroupMessage:123456",
        platform="aiocqhttp",
        group_id="123456",
        sender_id="10001",
        sender_name="主人",
        is_owner=True,
        is_bot=False,
        text="我下周三去杭州出差",
        msg_id="m1",
    )
    assert msg_id > 0
    store.add_message(
        ts=1762000100,
        umo="aiocqhttp:GroupMessage:123456",
        platform="aiocqhttp",
        group_id="123456",
        sender_id="20002",
        sender_name="张三",
        is_owner=False,
        is_bot=False,
        text="杭州最近降温",
        msg_id="m2",
    )
    assert store.count_messages() == 2
    rows = store.list_messages(limit=10)
    assert len(rows) == 2
    assert rows[0]["text"] == "杭州最近降温"  # 倒序
    assert store.last_name_of("10001") == "主人"
    assert store.last_name_of("99999") is None

    assert store.delete_message(msg_id) is True
    assert store.delete_message(999999) is False
    assert store.count_messages() == 1

    store.close()
    store2 = Store(db)
    store2.open()
    assert store2.count_messages() == 1
    assert store2.get_meta("schema_version") == str(SCHEMA_VERSION)
    store2.close()
    print("store ok")


class FakeEvent:
    def __init__(self, text, sender="10001", name="主人", group="123456", private=False):
        self._text = text
        self._sender = sender
        self._name = name
        self._group = group
        self._private = private
        self.unified_msg_origin = (
            "aiocqhttp:FriendMessage:10001" if private else f"aiocqhttp:GroupMessage:{group}"
        )
        self.message_obj = type("Obj", (), {"message_id": "mid", "self_id": "999"})()

    def get_message_str(self):
        return self._text

    def get_sender_id(self):
        return self._sender

    def get_sender_name(self):
        return self._name

    def get_group_id(self):
        return "" if self._private else self._group

    def get_self_id(self):
        return "999"

    def get_platform_name(self):
        return "aiocqhttp"

    def is_private_chat(self):
        return self._private


def test_recorder() -> None:
    store = Store(tmpdir("recorder") / "kahnbot.db")
    store.open()
    cfg = load_config({"owner_ids": ["10001"], "noise_patterns": ["#sl"]})
    rec = Recorder(store, cfg)

    assert rec.record(FakeEvent("普通人说话")) is True
    assert rec.record(FakeEvent("#SL")) is False  # 噪音（大小写不敏感）
    assert rec.record(FakeEvent("   ")) is False  # 空消息
    assert rec.record(FakeEvent("私聊内容", private=True)) is True
    assert rec.record(FakeEvent("自己说话", sender="999")) is False  # 机器人自己

    rows = store.list_messages(limit=10)
    texts = [r["text"] for r in rows]
    assert texts == ["私聊内容", "普通人说话"], texts
    assert rows[0]["group_id"] is None
    assert rows[1]["group_id"] == "123456"
    assert rows[1]["is_owner"] == 1
    store.close()
    print("recorder ok")


if __name__ == "__main__":
    test_config()
    test_store()
    test_recorder()
    shutil.rmtree(BASE, ignore_errors=True)
    print("ALL OK")
