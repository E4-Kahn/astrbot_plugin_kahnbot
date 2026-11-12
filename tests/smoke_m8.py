"""M8 冒烟：分段拆条与打字节奏。离线运行，不需要 AstrBot 实例。

    python tests/smoke_m8.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT.parent))

from astrbot_plugin_kahnbot.core.config import load_config  # noqa: E402
from astrbot_plugin_kahnbot.core.segments import (  # noqa: E402
    BASE_DELAY,
    MAX_DELAY,
    bubble_delay,
    split_bubbles,
)

BASE_DIR = REPO_ROOT / ".smoke_tmp"


def test_split_basic():
    bubbles = split_bubbles("今天好累。想早点睡。")
    assert len(bubbles) == 2, bubbles
    assert bubbles[0] == "今天好累。"
    assert bubbles[1] == "想早点睡。"
    print("split basic ok", bubbles)


def test_short_and_single():
    assert split_bubbles("嗯") == ["嗯"]
    assert split_bubbles("好的我知道啦") == ["好的我知道啦"]
    assert split_bubbles("") == []
    print("short ok")


def test_newlines():
    bubbles = split_bubbles("在的\n刚在忙，没看手机")
    assert len(bubbles) == 2, bubbles
    assert bubbles[0] == "在的"
    print("newline ok", bubbles)


def test_max_bubbles():
    text = "第一句。第二句。第三句。第四句。"
    bubbles = split_bubbles(text, max_bubbles=3)
    assert len(bubbles) == 3, bubbles
    assert "".join(bubbles).replace(" ", "") == text.replace(" ", "")
    print("max bubbles ok", bubbles)


def test_no_split_cases():
    long_text = "啊" * 400
    assert split_bubbles(long_text) == [long_text]
    code = "看这个：\n```py\nprint(1)\n```\n就这样。"
    assert split_bubbles(code) == [code]
    print("no-split ok")


def test_merge_short():
    # "好。" 太短，应该并到下一句，最后只剩一条
    bubbles = split_bubbles("好。今天天气不错，我们出去走走吧。")
    assert len(bubbles) == 1, bubbles
    print("merge short ok", bubbles)


def test_delay():
    short = bubble_delay("嗯", jitter=0.0)
    long = bubble_delay("这一句话有点长了，打字要花一点时间", jitter=0.0)
    assert short >= BASE_DELAY
    assert long > short
    assert bubble_delay("啊" * 200, jitter=0.0) == MAX_DELAY
    assert bubble_delay("嗯", jitter=0.3) == short + 0.3
    print("delay ok", round(short, 2), round(long, 2))


def test_config():
    cfg = load_config({})
    assert cfg.segmented_reply is True
    assert cfg.segmented_max_bubbles == 3
    assert cfg.segmented_typing is True
    clamp = load_config({"segmented_max_bubbles": 99})
    assert clamp.segmented_max_bubbles == 5
    assert load_config({"segmented_max_bubbles": 0}).segmented_max_bubbles == 1
    assert load_config({"segmented_reply": False}).segmented_reply is False
    print("config ok")


def main():
    if BASE_DIR.exists():
        shutil.rmtree(BASE_DIR)
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    test_split_basic()
    test_short_and_single()
    test_newlines()
    test_max_bubbles()
    test_no_split_cases()
    test_merge_short()
    test_delay()
    test_config()
    shutil.rmtree(BASE_DIR, ignore_errors=True)
    print("ALL OK", BASE_DIR)


if __name__ == "__main__":
    main()
