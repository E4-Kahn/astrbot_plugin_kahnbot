"""M4 离线冒烟：分词、字面分、衰减、混合排序、隐私/superseded/归档过滤、注入去重与预算。

不启动 AstrBot，也不调真模型：用一个假 embedder 提供固定维度的向量。

跑法：

    "C:\\Users\\<你>\\AppData\\Roaming\\uv\\tools\\astrbot\\Scripts\\python.exe" tests/smoke_m4.py
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT.parent))

from astrbot_plugin_kahnbot.core.embeddings import pack_vector, unpack_vector  # noqa: E402
from astrbot_plugin_kahnbot.core.injector import Injector, build_block  # noqa: E402
from astrbot_plugin_kahnbot.core.retriever import Hit, Retriever, literal_score, tokens  # noqa: E402
from astrbot_plugin_kahnbot.core.store import Store  # noqa: E402

BASE_DIR = REPO_ROOT / ".smoke_tmp"


def tmpdir(name: str) -> Path:
    path = BASE_DIR / name
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    return path


class FakeEmbedder:
    """按字符哈希散成一个 8 维向量：同样文字得到同样向量，不同文字基本不同。"""

    def __init__(self) -> None:
        self.calls = 0

    def resolve(self) -> "FakeEmbedder":
        return self

    async def embed(self, text: str) -> list[float]:
        self.calls += 1
        vec = [0.0] * 8
        for ch in text:
            vec[ord(ch) % 8] += 1.0
        norm = sum(x * x for x in vec) ** 0.5 or 1.0
        return [x / norm for x in vec]


def seed(store: Store, text: str, *, importance: int = 50, source: str = "owner",
         visibility: str = "private", kind: str = "fact", created: int | None = None,
         last_hit: int | None = None, superseded_by: str | None = None,
         archived: int = 0, embedding: bool = True) -> str:
    digest = hashlib.md5(text.encode("utf-8")).hexdigest()
    fact_id = digest[:12]
    store.add_fact(
        fact_id=fact_id,
        text=text,
        hash_=digest,
        scope="private",
        source=source,
        visibility=visibility,
        kind=kind,
        importance=importance,
        created_ts=int(created if created is not None else time.time()) - 86400 * 10,
        embedding=pack_vector([1.0] + [0.0] * 7) if embedding else None,
    )
    if last_hit is not None:
        store.touch_facts([fact_id], last_hit)
    if superseded_by:
        store.conn.execute(
            "UPDATE fact_index SET superseded_by = ? WHERE fact_id = ?",
            (superseded_by, fact_id),
        )
        store.conn.commit()
    if archived:
        store.conn.execute("UPDATE fact_index SET archived = 1 WHERE fact_id = ?", (fact_id,))
        store.conn.commit()
    return fact_id


def test_tokens() -> None:
    got = tokens("下周三去杭州出差 Dec 3")
    assert "下周" in got and "杭州" in got and "出差" in got, got
    assert "dec" in got, got
    assert literal_score(tokens("杭州"), "主人下周三要去杭州") > 0.9
    assert literal_score(tokens("体检"), "主人下周三要去杭州") == 0.0
    print("tokens ok")


def test_store_roundtrip(tmp: Path) -> None:
    store = Store(tmp / "k.db")
    store.open()
    fid = seed(store, "主人下周三要去杭州体检", importance=70)
    row = store.get_fact(fid)
    assert row is not None and row["importance"] == 70
    assert len(unpack_vector(row["embedding"])) == 8
    assert store.count_facts() == 1
    # 同 hash 再写一次应被拒
    assert seed(store, "主人下周三要去杭州体检") == fid
    assert store.count_facts() == 1
    store.close()
    print("store ok")


def test_visibility_and_filters(tmp: Path) -> None:
    store = Store(tmp / "k.db")
    store.open()
    keep = seed(store, "主人下周三要去杭州体检", importance=70)
    observed = seed(store, "张三说下周要考试", source="observed", importance=40)
    superseded = seed(store, "主人以前住在滨江", superseded_by="newer")
    archived = seed(store, "主人十年前的地址是城西", archived=1)
    retriever = Retriever(store, FakeEmbedder())

    hits = asyncio.run(retriever.search("下周 杭州 体检", owner_context=True, min_score=0.0))
    ids = {h.fact_id for h in hits}
    assert keep in ids, ids
    assert superseded not in ids, "已被取代的条目不该出现在检索结果里"
    assert archived not in ids, "已归档的条目不该出现在检索结果里"
    assert observed in ids, "owner 上下文里转述条目仍可见（带权重折扣）"

    # 群聊上下文看不到 private 条目
    hits_group = asyncio.run(retriever.search("下周 杭州 体检", owner_context=False, min_score=0.0))
    assert hits_group == [], [h.text for h in hits_group]

    # 一条 private 的笔记，公开后群聊可见
    store.conn.execute("UPDATE fact_index SET visibility = 'public' WHERE fact_id = ?", (keep,))
    store.conn.commit()
    hits_group = asyncio.run(retriever.search("杭州体检", owner_context=False, min_score=0.0))
    assert [h.fact_id for h in hits_group] == [keep]
    store.close()
    print("filters ok")


def test_ranking(tmp: Path) -> None:
    store = Store(tmp / "k.db")
    store.open()
    now = int(time.time())
    # 同一条事实，重要度高的应排前面
    a = seed(store, "主人下周三要去杭州体检", importance=90, last_hit=now)
    seed(store, "主人下周三要去杭州开会", importance=30, last_hit=now)
    retriever = Retriever(store, FakeEmbedder())
    hits = asyncio.run(retriever.search("下周三杭州", owner_context=True, min_score=0.0))
    assert hits[0].fact_id == a, [(h.fact_id, h.score) for h in hits]

    # 时间衰减：很久没被提起的同类条目排后面
    old = seed(store, "主人下周三去杭州做体检", importance=90, last_hit=now - 86400 * 120)
    hits = asyncio.run(Retriever(store, FakeEmbedder()).search(
        "下周三杭州体检", owner_context=True, min_score=0.0
    ))
    assert hits[0].fact_id != old, [(h.fact_id, h.score) for h in hits]
    store.close()
    print("ranking ok")


def test_no_vector(tmp: Path) -> None:
    """provider 不可用时不报错，按字面分数检索。"""
    store = Store(tmp / "k.db")
    store.open()
    seed(store, "主人下周三要去杭州体检", embedding=False)
    retriever = Retriever(store, None)
    hits = asyncio.run(retriever.search("杭州", owner_context=True, min_score=0.1))
    assert len(hits) == 1 and hits[0].score > 0
    store.close()
    print("no-vector ok")


def test_injector() -> None:
    hits = [
        Hit("f1", "主人下周三要去杭州体检", 0.9, "owner", 70, int(time.time()), "fact"),
        Hit("f2", "张三说明天是生日", 0.6, "observed", 35, int(time.time()), "fact"),
    ]
    inj = Injector(dedupe_turns=5)
    umo = "kahnbot:GroupMessage:1"
    inj.next_turn(umo)
    assert len(inj.fresh(hits, umo)) == 2
    inj.mark([h.fact_id for h in hits], umo)
    for _ in range(4):
        inj.next_turn(umo)
    assert inj.fresh(hits, umo) == [], "5 轮内应全部去重"
    inj.next_turn(umo)  # 第 6 轮
    assert len(inj.fresh(hits, umo)) == 2, "过窗口后应重新可见"

    block, used = build_block(hits, max_chars=200)
    assert used and block.startswith("<memory>") and block.endswith("</memory>")
    assert "未必属实" in block, block
    block2, used2 = build_block(hits, max_chars=20)
    assert block2 == "" and used2 == [], "预算太小应整段放弃"
    print("injector ok")


def main() -> None:
    base = tmpdir("m4")
    test_tokens()
    test_store_roundtrip(tmpdir("m4_store"))
    test_visibility_and_filters(tmpdir("m4_filter"))
    test_ranking(tmpdir("m4_rank"))
    test_no_vector(tmpdir("m4_novec"))
    test_injector()
    shutil.rmtree(BASE_DIR, ignore_errors=True)
    print("ALL OK", base)


if __name__ == "__main__":
    main()
