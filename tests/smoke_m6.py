"""M6 离线冒烟：主动说话判定 + 回避档 + 未回话攒存 + 配置默认值。

和 m1~m5 一样：不连 AstrBot、不报警、不需要网络，跑完打印 ALL OK。
用法：python tests/smoke_m6.py
"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT.parent))

from astrbot_plugin_kahnbot.core.config import load_config  # noqa: E402
from astrbot_plugin_kahnbot.core.emotion import (  # noqa: E402
    AVOID_OWNER_MSGS,
    MAX_UNANSWERED,
    Emotion,
    default_state,
)
from astrbot_plugin_kahnbot.core.proactive import Proactive  # noqa: E402
from astrbot_plugin_kahnbot.core.store import Store  # noqa: E402

BASE_DIR = REPO_ROOT / ".smoke_tmp"
NOW = int(time.mktime((2026, 11, 12, 10, 0, 0, 0, 0, -1)))  # 本地 10:00，不在免打扰时段


def tmpdir(name: str) -> Path:
    path = BASE_DIR / name
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    return path


class FakeLLM:
    def __init__(self, reply: str | None = "在吗？突然想起你。") -> None:
        self.reply = reply
        self.calls: list[str] = []

    async def text(self, prompt: str, system_prompt: str | None = None) -> str | None:
        self.calls.append(prompt)
        return self.reply


class Recorder:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def __call__(self, umo: str, text: str) -> bool:
        self.sent.append((umo, text))
        return True


def make(store: Store, *, sent=None, emotion=None, llm=None, rand=lambda: 0.0):
    cfg = load_config({})
    emotion = emotion or Emotion(default_state(NOW))
    send = sent or Recorder()
    proactive = Proactive(
        store=store,
        emotion=emotion,
        llm=llm if llm is not None else FakeLLM(),
        cfg=cfg,
        send=send,
        tz=None,
        clock=lambda: NOW,
        rand=rand,
    )
    return cfg, emotion, send, proactive


def seed_message(store: Store, umo: str = "aiocqhttp:GroupMessage:100") -> None:
    store.add_message(
        ts=NOW - 600,
        umo=umo,
        platform="aiocqhttp",
        group_id="100",
        sender_id="10001",
        sender_name="主人",
        is_owner=1,
        is_bot=0,
        text="在吗",
        msg_id="x",
    )


def test_proactive_gates(base: Path) -> None:
    store = Store(base / "gates.db")
    store.open()
    try:
        seed_message(store)
        cfg, emotion, send, proactive = make(store)

        # 沉默时长不够：主人 1 分钟前才说过话
        emotion.state["last_owner_ts"] = NOW - 60
        assert proactive.decide() is None, "沉默不足 10 分钟不该开口"

        # 沉默够了，但骰子没中
        emotion.state["last_owner_ts"] = NOW - 3600
        _, _, _, unlucky = make(store, emotion=emotion, rand=lambda: 0.999)
        assert unlucky.decide() is None, "没掷中不该开口"

        # 掷中：应该拿到会话
        plan = proactive.decide()
        assert plan is not None and plan.umo == "aiocqhttp:GroupMessage:100", plan

        # 免打扰：凌晨 3 点
        midnight = int(time.mktime((2026, 11, 12, 3, 0, 0, 0, 0, -1)))
        assert proactive.decide(midnight) is None, "免打扰时段不该开口"

        # 冷却
        emotion.state["last_proactive_ts"] = NOW - 60
        assert proactive.decide() is None, "冷却中不该开口"
        emotion.state["last_proactive_ts"] = 0

        # 日限
        day = time.strftime("%Y-%m-%d", time.localtime(NOW))
        emotion.state["proactive_day"] = day
        emotion.state["proactive_count"] = cfg.proactive_daily_limit
        assert proactive.decide() is None, "到了日限不该开口"
        emotion.state["proactive_count"] = 0

        # 回避档
        emotion.state["mode"] = "avoidant"
        assert proactive.decide() is None, "躲起来时不该主动说话"
        emotion.state["mode"] = "daily"

        # 开关
        off = load_config({"proactive_enabled": False})
        emotion.state["last_owner_ts"] = NOW - 3600
        disabled = Proactive(
            store=store,
            emotion=emotion,
            llm=FakeLLM(),
            cfg=off,
            send=send,
            tz=None,
            clock=lambda: NOW,
            rand=lambda: 0.0,
        )
        assert disabled.decide() is None, "关掉开关就不该说话"
    finally:
        store.close()
    print("ok 主动说话的闸门顺序")


def test_proactive_send(base: Path) -> None:
    store = Store(base / "send.db")
    store.open()
    try:
        seed_message(store)
        sent = Recorder()
        emotion = Emotion(default_state(NOW))
        emotion.state["last_owner_ts"] = NOW - 3600
        llm = FakeLLM("忽然想到你，今天忙不忙？")
        cfg, emotion, sent, proactive = make(store, sent=sent, emotion=emotion, llm=llm)
        marks: list[tuple[str, str]] = []
        proactive.on_sent = lambda umo, text: marks.append((umo, text))

        import asyncio

        text = asyncio.run(proactive.maybe_run())
        assert text == "忽然想到你，今天忙不忙？", text
        assert sent.sent == [("aiocqhttp:GroupMessage:100", text)], sent.sent
        assert marks == [("aiocqhttp:GroupMessage:100", text)], "应该回调回写历史"
        assert emotion.state["proactive_count"] == 1
        assert emotion.state["proactive_day"] == time.strftime("%Y-%m-%d", time.localtime(NOW))

        # 立刻再来一次：冷却拦住
        assert asyncio.run(proactive.maybe_run()) is None, "冷却中不该连发"
        assert len(sent.sent) == 1

        # 模型挂了 → 模板兜底
        emotion.state["last_proactive_ts"] = 0
        bad = FakeLLM(None)
        _, _, sent2, proactive2 = make(store, emotion=emotion, llm=bad)
        fallback = asyncio.run(proactive2.maybe_run())
        assert fallback and len(fallback) > 4, fallback
        assert fallback in __import__(
            "astrbot_plugin_kahnbot.core.proactive", fromlist=["FALLBACK_LINES"]
        ).FALLBACK_LINES
    finally:
        store.close()
    print("ok 主动说话发送与兜底")


def test_avoidant(base: Path) -> None:
    emotion = Emotion(default_state(NOW))
    assert emotion.mode != "avoidant"

    # stress 飙到 0.8：立刻躲
    emotion.drives["stress"] = 0.9
    assert emotion.check_avoidant() is not None
    assert emotion.is_avoidant(), "被吓到应该立刻躲起来"
    assert emotion.state["avoid_until"] > NOW

    # 到时自动出来
    assert emotion.check_avoidant(NOW + 10000) is not None
    assert not emotion.is_avoidant(), "时间到了应该气消"

    # blame 高位要持续 30 分钟
    fresh = Emotion(default_state(NOW))
    fresh.state["blame"] = 80.0
    assert fresh.check_avoidant() is None, "刚开始生气不该立刻躲"
    assert fresh.check_avoidant(NOW + 60) is None
    assert fresh.check_avoidant(NOW + 31 * 60) is not None, "憋了半小时该躲了"
    assert fresh.is_avoidant()
    assert fresh.state["avoid_until"] - (NOW + 31 * 60) == 2 * 60 * 60, "群里最长两小时"

    # 一句道歉就哄好
    log = fresh.on_owner_text("对不起嘛，别生气了")
    assert log is not None and not fresh.is_avoidant(), "道歉应该把她哄出来"

    # 主人连发三条也会心软
    again = Emotion(default_state(NOW))
    again.drives["stress"] = 0.9
    again.check_avoidant()
    assert again.is_avoidant()
    for i in range(AVOID_OWNER_MSGS):
        result = again.on_owner_text(f"在吗{i}")
    assert result is not None and not again.is_avoidant(), "连发三条应该心软"

    # 未回话的攒存与清空
    again.push_unanswered("第一条")
    again.push_unanswered("  ")
    again.push_unanswered("第二条")
    items = again.take_unanswered()
    assert [item["text"] for item in items] == ["第一条", "第二条"], items
    assert again.state["unanswered"] == []

    # 最多留 20 条
    many = Emotion(default_state(NOW))
    for i in range(MAX_UNANSWERED + 5):
        many.push_unanswered(f"第{i}条")
    kept = many.state["unanswered"]
    assert len(kept) == MAX_UNANSWERED, len(kept)
    assert kept[-1]["text"] == f"第{MAX_UNANSWERED + 4}条"
    print("ok 回避档进入/退出与未回话")


def test_config(base: Path) -> None:
    cfg = load_config({})
    assert cfg.proactive_enabled is True
    assert cfg.proactive_cooldown_hours == 6.0
    assert cfg.proactive_daily_limit == 2
    assert cfg.proactive_silence_minutes == 10

    odd = load_config(
        {
            "proactive_cooldown_hours": 99,
            "proactive_daily_limit": -3,
            "proactive_silence_minutes": 5000,
            "proactive_enabled": "false",
        }
    )
    assert odd.proactive_cooldown_hours == 72.0, odd.proactive_cooldown_hours
    assert odd.proactive_daily_limit == 0, odd.proactive_daily_limit
    assert odd.proactive_silence_minutes == 720, odd.proactive_silence_minutes
    assert odd.proactive_enabled is False
    print("ok 配置默认值与钳位")


def main() -> None:
    BASE_DIR.mkdir(exist_ok=True)
    base = tmpdir("m6")
    try:
        test_proactive_gates(base)
        test_proactive_send(base)
        test_avoidant(base)
        test_config(base)
    finally:
        shutil.rmtree(base, ignore_errors=True)
        try:
            BASE_DIR.rmdir()
        except OSError:
            pass
    print("ALL OK", base)


if __name__ == "__main__":
    main()
