"""M8 冒烟：分段拆条与打字节奏。离线运行，不需要 AstrBot 实例。

    python tests/smoke_m8.py
"""

from __future__ import annotations

import asyncio
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
from astrbot_plugin_kahnbot.core.embeddings import EmbeddingClient  # noqa: E402
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


class _Provider:
    def get_dim(self):
        return 8


class _LateContext:
    """前两次还没有 provider（模拟 AstrBot 启动顺序），第三次才有。"""

    def __init__(self):
        self.calls = 0

    def get_all_embedding_providers(self):
        self.calls += 1
        return [] if self.calls < 3 else [_Provider()]

    def get_provider_by_id(self, provider_id):
        return None


def test_embed_wait_ready():
    # 没有 context：等不到，快速返回 False
    assert asyncio.run(EmbeddingClient(None).wait_ready(attempts=1, interval=0.0)) is False
    # provider 晚一点才出现：wait_ready 能自己重试到拿到
    client = EmbeddingClient(_LateContext())
    assert asyncio.run(client.wait_ready(attempts=4, interval=0.0)) is True
    assert client.available is True
    assert client.dim == 8
    print("embed wait_ready ok")


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
    test_embed_wait_ready()
    shutil.rmtree(BASE_DIR, ignore_errors=True)
    print("ALL OK", BASE_DIR)


if __name__ == "__main__":
    main()
