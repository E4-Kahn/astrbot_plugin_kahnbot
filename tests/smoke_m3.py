"""M3 离线冒烟：情绪漂移、五档、主人情绪事件、拍一拍、state.json、心跳。不启动 AstrBot。

跑法：

    "C:\\Users\\<你>\\AppData\\Roaming\\uv\\tools\\astrbot\\Scripts\\python.exe" tests/smoke_m3.py
"""

from __future__ import annotations

import json
import shutil
import os
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT.parent))

# 导入 astrbot 会在当前目录铺一套 data/；把根指到系统临时目录，别弄脏仓库。
os.environ.setdefault("ASTRBOT_ROOT", str(Path(tempfile.gettempdir()) / "kahnbot-smoke-root"))

from astrbot_plugin_kahnbot.core import state as state_file  # noqa: E402
from astrbot_plugin_kahnbot.core.emotion import BASE, Emotion, default_state  # noqa: E402
from astrbot_plugin_kahnbot.core.heartbeat import ACTIVE_INTERVAL, Heartbeat  # noqa: E402

BASE_DIR = REPO_ROOT / ".smoke_tmp"


def tmpdir(name: str) -> Path:
    path = BASE_DIR / name
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    return path


def test_drift() -> None:
    emotion = Emotion(default_state(now=1000))
    emotion.drives["joy"] = 0.95
    emotion.drives["fatigue"] = 0.70
    emotion.state["blame"] = 60.0
    emotion.drift(8)

    assert emotion.drives["joy"] < 0.95, "joy 应该向基础值回落"
    assert emotion.drives["joy"] > BASE["joy"], "一次漂移不该直接落回基础值"
    assert emotion.drives["fatigue"] < 0.70
    assert emotion.blame < 60.0, "blame 应该随时间衰减"
    assert emotion.mode in {"daily", "aggrieved", "angry", "afraid"}

    # 闭式解 = 逐次漂移，二者应当一致
    once = Emotion(default_state(now=1000))
    once.drives["joy"] = 0.95
    once.drift(96)
    many = Emotion(default_state(now=1000))
    many.drives["joy"] = 0.95
    for _ in range(96):
        many.drift(1)
    assert abs(once.drives["joy"] - many.drives["joy"]) < 1e-6, (once.drives["joy"], many.drives["joy"])
    print("drift ok")


def test_modes() -> None:
    emotion = Emotion(default_state())
    assert emotion.mode == "daily"

    emotion.state["blame"] = 25.0
    assert emotion.mode == "aggrieved"
    emotion.state["blame"] = 45.0
    assert emotion.mode == "angry"
    emotion.state["blame"] = 71.0
    assert emotion.mode == "afraid"
    emotion.state["blame"] = 0.0
    emotion.drives["stress"] = 0.85
    assert emotion.mode == "afraid"
    print("modes ok")


def test_owner_events() -> None:
    emotion = Emotion(default_state())
    emotion.on_owner_text("你是不是傻啊，烦死了")
    assert emotion.blame == 15.0, emotion.blame
    assert emotion.mode == "daily"

    emotion.on_owner_text("对不起，我错了")
    assert emotion.blame == 0.0, "道歉应该把 blame 压回 0 但不为负"

    joy = emotion.drives["joy"]
    emotion.on_owner_text("谢谢你，今天辛苦啦")
    assert emotion.drives["joy"] > joy

    log = emotion.state["log"]
    assert [item["reason"] for item in log] == ["责骂", "道歉/哄", "夸奖/道谢"], log
    print("owner events ok")


def test_poke_and_render() -> None:
    emotion = Emotion(default_state())
    assert emotion.render() is None, "日常且无偏离时不注入情绪"

    social, joy = emotion.drives["social"], emotion.drives["joy"]
    emotion.on_poke()
    assert emotion.drives["social"] > social
    assert emotion.drives["joy"] > joy

    emotion.state["blame"] = 30.0
    text = emotion.render() or ""
    assert text.startswith("<emotion>") and "委屈" in text, text
    assert "30" not in text, "不该把数字报给模型"

    emotion.state["blame"] = 80.0
    assert "被吓到" in (emotion.render() or "")
    print("poke/render ok")


def test_state_roundtrip() -> None:
    path = tmpdir("state") / "state.json"
    emotion = Emotion(default_state(now=2000))
    emotion.state["blame"] = 33.0
    emotion.state["log"] = [{"ts": 2000, "reason": "责骂", "mode": "aggrieved", "blame": 33.0}]
    assert state_file.save_state(path, emotion.snapshot())
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["blame"] == 33.0
    assert not (path.parent / "state.tmp").exists(), "临时文件应被替换掉"

    loaded = Emotion(state_file.load_state(path))
    assert loaded.blame == 33.0
    assert loaded.mode == "aggrieved"
    assert loaded.state["log"][0]["reason"] == "责骂"

    # 旧版本/损坏的状态要能补全
    broken = Emotion({"drives": {"joy": "坏值"}, "blame": "x"})
    assert broken.drives["joy"] == BASE["joy"]
    assert broken.blame == 0.0
    print("state ok")


def test_heartbeat() -> None:
    path = tmpdir("hb") / "state.json"
    emotion = Emotion(default_state(now=int(time.time())))
    emotion.drives["joy"] = 0.9
    emotion.state["last_tick_ts"] = int(time.time()) - ACTIVE_INTERVAL - 10
    hb = Heartbeat("test", emotion, path, activity=lambda: time.time())

    assert not hb.running
    assert hb.maybe_tick() is True, "超过一个周期应该 tick"
    assert hb.ticks == 1
    assert path.exists(), "tick 后应落盘"
    assert hb.maybe_tick() is False, "刚 tick 过不该再 tick"
    assert emotion.drives["joy"] < 0.9

    # 错过的 tick 数用闭式解一次算完，且封顶
    emotion.state["last_tick_ts"] = int(time.time()) - ACTIVE_INTERVAL * 500
    assert hb.maybe_tick() is True
    assert hb.ticks == 2
    print("heartbeat ok")


def main() -> int:
    test_drift()
    test_modes()
    test_owner_events()
    test_poke_and_render()
    test_state_roundtrip()
    test_heartbeat()
    print("ALL OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
