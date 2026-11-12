"""M5 离线冒烟：时间解析、「记住…」判定、md 往返、写入/取代/归档、汇总、提醒。

不启动 AstrBot，不调真模型：embedder 是字符哈希，LLM 与 cron_manager 都是假的。

跑法：

    "C:\\Users\\<你>\\AppData\\Roaming\\uv\\tools\\astrbot\\Scripts\\python.exe" tests/smoke_m5.py
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT.parent))

from astrbot_plugin_kahnbot.core import remember  # noqa: E402
from astrbot_plugin_kahnbot.core.config import Config, load_config  # noqa: E402
from astrbot_plugin_kahnbot.core.llm import extract_json  # noqa: E402
from astrbot_plugin_kahnbot.core.memory import MemoryWriter, fact_hash, layer_of  # noqa: E402
from astrbot_plugin_kahnbot.core.memoryfiles import (  # noqa: E402
    MemoryFiles,
    entry_line,
    parse_line,
)
from astrbot_plugin_kahnbot.core.reminder import ReminderService, resolve_tz  # noqa: E402
from astrbot_plugin_kahnbot.core.store import Store  # noqa: E402
from astrbot_plugin_kahnbot.core.summarizer import Summarizer  # noqa: E402
from astrbot_plugin_kahnbot.core.when import format_due, parse_due  # noqa: E402

BASE_DIR = REPO_ROOT / ".smoke_tmp"
NOW = datetime(2026, 11, 12, 9, 0, 0).timestamp()  # 2026-11-12 是星期四
TZ = resolve_tz("Asia/Shanghai")


def tmpdir(name: str) -> Path:
    path = BASE_DIR / name
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    return path


class FakeEmbedder:
    """按字符哈希散成 8 维向量：同样文字同样向量，差一两个字的余弦接近 1。"""

    def resolve(self):
        return self

    async def embed(self, text: str) -> list[float]:
        vec = [0.0] * 8
        for ch in text:
            vec[ord(ch) % 8] += 1.0
        norm = sum(x * x for x in vec) ** 0.5 or 1.0
        return [x / norm for x in vec]


class FakeLLM:
    def __init__(self, reply: str | list[str] = "[]") -> None:
        self.replies = reply if isinstance(reply, list) else [reply]
        self.prompts: list[str] = []

    async def text(self, prompt: str, system_prompt: str | None = None) -> str | None:
        self.prompts.append(prompt)
        if not self.replies:
            return None
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


class FakeJob:
    def __init__(self, job_id: str, name: str, cron: str, handler, payload: dict) -> None:
        self.job_id = job_id
        self.name = name
        self.cron_expression = cron
        self.handler = handler
        self.payload = payload
        self.job_type = "basic"


class FakeCron:
    def __init__(self) -> None:
        self.jobs: dict[str, FakeJob] = {}
        self.seq = 0

    async def add_basic_job(self, *, name, cron_expression, handler, description=None,
                            timezone=None, payload=None, enabled=True, persistent=False):
        self.seq += 1
        job = FakeJob(f"job{self.seq}", name, cron_expression, handler, payload or {})
        self.jobs[job.job_id] = job
        return job

    async def list_jobs(self, job_type: str | None = None):
        return [j for j in self.jobs.values() if job_type is None or j.job_type == job_type]

    async def delete_job(self, job_id):
        return self.jobs.pop(job_id, None) is not None


def make_writer(tmp: Path, cfg: Config | None = None, llm=None):
    store = Store(tmp / "k.db")
    store.open()
    files = MemoryFiles(tmp)
    writer = MemoryWriter(store, files, FakeEmbedder(), cfg or Config(), llm)
    return store, files, writer


def add(store: Store, writer: MemoryWriter, text: str, **kw):
    return asyncio.run(writer.add(text=text, **kw))


def test_when() -> None:
    def due(text: str):
        return parse_due(text, now=NOW, tz=TZ)

    assert due("明天去体检") == datetime(2026, 11, 13, 9, 0).timestamp()
    assert due("下周三去体检") == datetime(2026, 11, 18, 9, 0).timestamp()
    assert due("后天下午 3 点开会") == datetime(2026, 11, 14, 15, 0).timestamp()
    assert due("明晚八点") == datetime(2026, 11, 13, 20, 0).timestamp()
    assert due("3 天后交报告") == datetime(2026, 11, 15, 9, 0).timestamp()
    assert due("14:30 打电话") == datetime(2026, 11, 12, 14, 30).timestamp()
    assert due("8 点半") == datetime(2026, 11, 13, 8, 30).timestamp(), "今天已过，应顺延到明天"
    assert due("5 月 20 日") == datetime(2027, 5, 20, 9, 0).timestamp(), "已经过去的日期应算明年"
    assert due("这周日") is not None
    assert due("10 分钟后提醒我关火") == datetime(2026, 11, 12, 9, 10).timestamp()
    assert due("半小时后") == datetime(2026, 11, 12, 9, 30).timestamp()
    assert due("2 小时后") == datetime(2026, 11, 12, 11, 0).timestamp()
    assert due("随便说点什么") is None
    assert due("昨天") is None, "不猜过去的日期"
    assert format_due(int(NOW), TZ) == "2026-11-12 09:00"
    print("when ok")


def test_remember_detect() -> None:
    hit = remember.detect("记住，下周三要去体检", woken=True, now=NOW, tz=TZ)
    assert hit is not None and hit.text == "下周三要去体检", hit
    assert hit.due_ts == datetime(2026, 11, 18, 9, 0).timestamp(), hit.due_ts
    assert hit.kind == "remember"

    remind = remember.detect("别忘了明天交报告", woken=True, now=NOW, tz=TZ)
    assert remind is not None and remind.kind == "remind" and remind.text == "明天交报告", remind

    assert remember.detect("记住这件事", woken=False) is None, "没被叫到不认（唤醒层）"
    assert remember.detect("我记住了", woken=True) is None, "应答不是要我记"
    assert remember.detect("记住没？", woken=True) is None, "疑问不是要我记"
    assert remember.detect("他说记住这个人", woken=True) is None, "转述不是主人交代的"
    assert remember.detect("你好呀", woken=True) is None

    named = remember.detect("鱼宝记住，我喜欢冰美式", woken=True, wake_names=("鱼宝",))
    assert named is not None and named.text == "我喜欢冰美式", named

    private = remember.detect("帮我记一下，密码是 abcdef", woken=True, private=True)
    assert private is not None and private.text == "密码是 abcdef", private
    print("remember ok")


def test_md_roundtrip() -> None:
    entry = {
        "date": "2026-11-12",
        "scope": "私聊",
        "speaker": "主人",
        "uid": "10001",
        "layer": "owner",
        "text": "下周三要去体检",
        "importance": 70,
        "fact_id": "abc123",
    }
    line = entry_line(entry)
    assert line.startswith("- **2026-11-12** · [私聊|主人|uid:10001|owner]"), line
    back = parse_line(line)
    assert back is not None
    assert back["text"] == "下周三要去体检", back
    assert back["importance"] == 70 and back["fact_id"] == "abc123"
    assert back["layer"] == "owner" and back["uid"] == "10001"

    marked = parse_line(entry_line(dict(entry, superseded_by="new1", archived=True)))
    assert marked["superseded_by"] == "new1" and marked["archived"] is True

    assert parse_line("# 标题") is None
    assert parse_line("- 不是条目") is None
    print("md ok")


def test_writer(tmp: Path) -> None:
    store, files, writer = make_writer(tmp)
    files.ensure_files()

    due = int(datetime(2026, 11, 18, 9, 0).timestamp())
    res = add(store, writer, "下周三要去体检", due_ts=due, speaker="主人", uid="10001")
    assert res.reason == "added", res
    row = store.get_fact(res.fact_id)
    assert row["source"] == "owner" and row["visibility"] == "private"
    assert int(row["due_ts"]) == due, "owner 层的到期时间应保留"
    assert row["importance"] == 50, "没打分时默认 50"

    owner_entries = files.load("owner")
    assert len(owner_entries) == 1 and owner_entries[0]["fact_id"] == res.fact_id
    assert files.load("observed") == []

    # 重复写入（md5 相同）
    again = add(store, writer, "下周三要去体检")
    assert again.reason == "duplicate" and again.fact_id == res.fact_id
    assert store.count_facts() == 1

    # 群友说的：进 observed、due 被丢掉、visibility=public
    obs = add(
        store,
        writer,
        "明天是管理员的生日",
        source="observed",
        visibility="public",
        scope="群:123",
        speaker="张三",
        uid="20002",
        due_ts=due,
    )
    assert obs.reason == "added"
    observed_row = store.get_fact(obs.fact_id)
    assert observed_row["source"] == "observed" and observed_row["due_ts"] is None, "observed 永不提醒"
    assert observed_row["visibility"] == "public"
    assert files.load("observed") and files.load("observed")[0]["layer"] == "observed"
    assert layer_of("observed") == "observed" and layer_of("owner") == "owner"

    # 近似重复 → 显式写入算取代
    near_text = "下周三要去体检呀"
    assert abs(len(near_text) - len("下周三要去体检")) == 1
    near = add(store, writer, near_text, due_ts=due)
    assert near.reason == "added" and near.superseded == res.fact_id, near
    assert store.get_fact(res.fact_id)["superseded_by"] == near.fact_id
    old_line = [e for e in files.load("owner") if e["fact_id"] == res.fact_id][0]
    assert old_line["superseded_by"] == near.fact_id, old_line

    # 近似重复 → 汇总路径算重复跳过
    skipped = asyncio.run(
        writer.add(text="下周三要去体检呀呀", near_duplicate="skip", source="owner")
    )
    assert skipped.reason in {"near_duplicate", "duplicate"}, skipped
    assert skipped.fact_id == near.fact_id

    # 删除
    assert asyncio.run(writer.drop(near.fact_id)) is True
    assert store.get_fact(near.fact_id) is None
    assert all(e["fact_id"] != near.fact_id for e in files.load("owner"))

    assert fact_hash("a b") == fact_hash("a  b"), "md5 去重应先归一化空白"
    store.close()
    print("writer ok")


def test_archive(tmp: Path) -> None:
    store, files, writer = make_writer(tmp)
    files.ensure_files()
    old = int(time.time()) - 40 * 86400
    for text, importance in (("很久没提起的小事", 40), ("很久没提起但重要的事", 80)):
        store.add_fact(
            fact_id=hashlib.md5(text.encode()).hexdigest()[:12],
            text=text,
            hash_=fact_hash(text),
            scope="私聊",
            source="owner",
            visibility="private",
            kind="fact",
            importance=importance,
            created_ts=old,
        )
    row_high = store.facts()[0]
    store.set_importance(row_high["fact_id"], 80)
    n = asyncio.run(writer.maintain())
    assert n >= 1
    archived = [r for r in store.facts(include_archived=True) if r["archived"]]
    assert len(archived) == 1 and archived[0]["importance"] < 60, [r["text"] for r in archived]
    assert store.facts() == [r for r in store.facts() if not r["archived"]]
    store.close()
    print("archive ok")


def test_summary(tmp: Path) -> None:
    store, files, writer = make_writer(tmp)
    files.ensure_files()
    now = int(time.time())
    store.add_message(ts=now - 100, umo="g:1", platform="aiocqhttp", group_id="1",
                      sender_id="10001", sender_name="主人", is_owner=True, is_bot=False,
                      text="我下周三去杭州出差")
    store.add_message(ts=now - 50, umo="g:1", platform="aiocqhttp", group_id="1",
                      sender_id="99999", sender_name="她", is_owner=False, is_bot=True,
                      text="路上小心")
    store.add_message(ts=now, umo="g:1", platform="aiocqhttp", group_id="1",
                      sender_id="20002", sender_name="张三", is_owner=False, is_bot=False,
                      text="明天是管理员生日")

    replies = [
        json.dumps(
            [
                {"text": "主人下周三去杭州出差", "kind": "plan", "source": "owner", "confidence": 0.9},
                {"text": "明天是管理员的生日", "kind": "fact", "source": "third_party", "confidence": 0.8},
                {"text": "没什么把握的一条", "kind": "fact", "source": "owner", "confidence": 0.3},
                {"text": "", "kind": "fact", "source": "owner", "confidence": 0.9},
            ],
            ensure_ascii=False,
        ),
        "[]",
    ]
    llm = FakeLLM(replies)
    cfg = Config(summary_min_confidence=0.5, summary_max_items=10, summary_interval_hours=6)
    summ = Summarizer(store, writer, llm, cfg)

    stats = asyncio.run(summ.run(force=True))
    assert stats["written"] == 2, stats
    assert "路上小心" not in llm.prompts[0], "is_bot 的话不该出现在提示词里"
    assert "我下周三去杭州出差" in llm.prompts[0]
    owner_rows = [r for r in store.facts() if r["source"] == "owner"]
    assert owner_rows and owner_rows[0]["text"] == "主人下周三去杭州出差"
    obs_rows = [r for r in store.facts() if r["source"] == "observed"]
    assert obs_rows and obs_rows[0]["visibility"] == "public"
    assert store.get_meta("last_summary_ts") == str(now)

    # 没有新流水 → 不再调模型
    before = len(llm.prompts)
    stats = asyncio.run(summ.run(force=True))
    assert stats["reason"] == "no rows" and len(llm.prompts) == before

    # 水位不动时不跑
    assert asyncio.run(summ.maybe_run(now + 60)) is False

    # 坏 JSON：水位不前移，下轮重试
    store.set_meta("last_summary_ts", str(now - 200))
    store.add_message(ts=now + 1, umo="g:1", platform="aiocqhttp", group_id="1",
                      sender_id="10001", sender_name="主人", is_owner=True, is_bot=False,
                      text="再记一条")
    bad = Summarizer(store, writer, FakeLLM("不是 JSON"), cfg)
    stats = asyncio.run(bad.run(force=True))
    assert stats["reason"] == "bad json" and stats["written"] == 0
    assert store.get_meta("last_summary_ts") == str(now - 200), "坏 JSON 不能前移水位"
    store.close()
    print("summary ok")


def test_reminder(tmp: Path) -> None:
    store, files, writer = make_writer(tmp)
    files.ensure_files()
    sent: list[tuple[str, str]] = []

    async def send(umo: str, text: str) -> bool:
        sent.append((umo, text))
        return True

    cron = FakeCron()
    due = int(time.time()) + 3600
    res = add(store, writer, "下周三要去体检", due_ts=due, umo="g:1", scope="群:1")

    svc = ReminderService(
        plugin_name="astrbot_plugin_kahnbot",
        store=store,
        files=files,
        cfg=Config(),
        send=send,
        context=None,
        tz_name="Asia/Shanghai",
    )
    svc.context = type("Ctx", (), {"cron_manager": cron})()
    assert svc.cron_expression(due) == datetime.fromtimestamp(due, TZ).strftime("%M %H %d %m *")

    async def scenario() -> None:
        assert await svc.schedule_fact(res.fact_id), "应当挂上 job"
        assert len(cron.jobs) == 1
        job = next(iter(cron.jobs.values()))
        assert job.name == f"astrbot_plugin_kahnbot:remind:{res.fact_id}", job.name
        assert job.cron_expression.count(" ") == 4, "必须是 5 段 crontab"
        sched = files.scheduled()
        assert len(sched) == 1 and sched[0]["fact_id"] == res.fact_id
        assert svc.cron_expression(due)

        # 到点：清 due、删排期、发一句
        await svc._fire(res.fact_id)
        assert sent and sent[0][0] == "g:1", sent
        assert "提醒你一下" in sent[0][1], sent
        row = store.get_fact(res.fact_id)
        assert row["due_ts"] is None and not files.scheduled()
        assert cron.jobs == {}, "发完应摘掉 job"

        # 兜底扫描：due 已过也发
        store.set_due(res.fact_id, int(time.time()) - 10)
        files.add_schedule(int(time.time()) - 10, "群:1", res.fact_id, "该体检了", TZ)
        assert await svc.poll_due() == 1
        assert sent[-1][1] == "该体检了", sent
        assert store.get_fact(res.fact_id)["due_ts"] is None

        # 重启恢复：清掉上一进程的 job，再按 due_ts 重挂
        stale = FakeJob("old1", "astrbot_plugin_kahnbot:remind:stale", "0 0 1 1 *", print, {})
        cron.jobs[stale.job_id] = stale
        store.set_due(res.fact_id, int(time.time()) + 7200)
        n = await svc.restore()
        assert n == 1, n
        assert "old1" not in cron.jobs, "旧的 job 应被清理"
        assert len(cron.jobs) == 1 and "old1" not in cron.jobs

        # 取消
        await svc.cancel(res.fact_id)
        assert store.get_fact(res.fact_id)["due_ts"] is None and not files.scheduled()

    asyncio.run(scenario())
    store.close()
    print("reminder ok")


def test_config() -> None:
    cfg = load_config({})
    assert cfg.remember_enabled and cfg.score_importance and cfg.summary_enabled and cfg.reminder_enabled
    assert cfg.summary_interval_hours == 6 and cfg.summary_max_items == 10
    assert cfg.summary_min_confidence == 0.5 and cfg.summary_max_chars == 4000
    assert cfg.timezone == "" and cfg.llm_provider_id == ""

    raw = {
        "remember_enabled": False,
        "summary_interval_hours": "0",  # 会被钳到 1
        "summary_max_items": 99,  # 钳到 50
        "summary_min_confidence": "1.5",  # 钳到 1.0
        "timezone": "Asia/Tokyo",
        "llm_provider_id": " deepseek ",
    }
    cfg = load_config(raw)
    assert cfg.remember_enabled is False
    assert cfg.summary_interval_hours == 1 and cfg.summary_max_items == 50
    assert cfg.summary_min_confidence == 1.0
    assert cfg.timezone == "Asia/Tokyo" and cfg.llm_provider_id == "deepseek"

    assert extract_json('```json\n[{"a": 1}]\n```') == [{"a": 1}]
    assert extract_json("好的，结果是：[1, 2] 以上") == [1, 2]
    assert extract_json("没有 JSON") is None
    print("config ok")


def main() -> None:
    base = tmpdir("m5")
    test_when()
    test_remember_detect()
    test_md_roundtrip()
    test_writer(tmpdir("m5_writer"))
    test_archive(tmpdir("m5_archive"))
    test_summary(tmpdir("m5_summary"))
    test_reminder(tmpdir("m5_remind"))
    test_config()
    shutil.rmtree(BASE_DIR, ignore_errors=True)
    print("ALL OK", base)


if __name__ == "__main__":
    main()
