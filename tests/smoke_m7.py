"""M7 离线冒烟：类型推断、最近对话、心情联动、拍一拍心软、纪念日顺延、配置默认值。

和 m1~m6 一样：不连 AstrBot、不报警、不需要网络，跑完打印 ALL OK。
用法：python tests/smoke_m7.py
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT.parent))

from astrbot_plugin_kahnbot.core.config import Config, load_config  # noqa: E402
from astrbot_plugin_kahnbot.core import remember  # noqa: E402
from astrbot_plugin_kahnbot.core.emotion import (  # noqa: E402
    AVOID_OWNER_MSGS,
    Emotion,
    default_state,
)
from astrbot_plugin_kahnbot.core.injector import (  # noqa: E402
    build_recent_chat,
    drop_warm,
)
from astrbot_plugin_kahnbot.core.kinds import (  # noqa: E402
    ANNIVERSARY,
    FACT,
    INTIMACY,
    PLAN,
    PREFERENCE,
    RELATION,
    infer_kind,
    is_warm,
)
from astrbot_plugin_kahnbot.core.llm import assess  # noqa: E402
from astrbot_plugin_kahnbot.core.memory import MemoryWriter  # noqa: E402
from astrbot_plugin_kahnbot.core.memoryfiles import MemoryFiles  # noqa: E402
from astrbot_plugin_kahnbot.core.reminder import ReminderService, compose_phrase  # noqa: E402
from astrbot_plugin_kahnbot.core.reset import reset_all, snapshot  # noqa: E402
from astrbot_plugin_kahnbot.core.retriever import Hit  # noqa: E402
from astrbot_plugin_kahnbot.core.store import Store  # noqa: E402

BASE_DIR = REPO_ROOT / ".smoke_tmp"
NOW = int(time.mktime((2026, 11, 12, 10, 0, 0, 0, 0, -1)))
GROUP = "aiocqhttp:GroupMessage:100"
PRIVATE = "aiocqhttp:FriendMessage:10001"


def tmpdir(name: str) -> Path:
    path = BASE_DIR / name
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    return path


class FakeEmbedder:
    def resolve(self):
        return self

    async def embed(self, text: str) -> list[float]:
        vec = [0.0] * 8
        for ch in text:
            vec[ord(ch) % 8] += 1.0
        norm = sum(x * x for x in vec) ** 0.5 or 1.0
        return [x / norm for x in vec]


class FakeJob:
    job_type = "basic"

    def __init__(self, job_id: str, name: str, cron: str, handler) -> None:
        self.job_id = job_id
        self.name = name
        self.cron_expression = cron
        self.handler = handler


class FakeCron:
    def __init__(self) -> None:
        self.jobs: dict[str, FakeJob] = {}
        self.seq = 0

    async def add_basic_job(self, *, name, cron_expression, handler, description=None,
                            timezone=None, payload=None, enabled=True, persistent=False):
        self.seq += 1
        job = FakeJob(f"job{self.seq}", name, cron_expression, handler)
        self.jobs[job.job_id] = job
        return job

    async def list_jobs(self, job_type=None):
        return [j for j in self.jobs.values() if job_type in (None, j.job_type)]

    async def delete_job(self, job_id):
        return self.jobs.pop(job_id, None) is not None


class Recorder:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def __call__(self, umo: str, text: str) -> bool:
        self.sent.append((umo, text))
        return True


class Recorder2(Recorder):
    """on_sent 回调：记录参数即可。"""

    def __init__(self) -> None:
        super().__init__()
        self.hooks: list[tuple[str, str]] = []

    async def hook(self, umo: str, text: str) -> None:
        self.hooks.append((umo, text))


def seed_message(store: Store, *, umo: str = GROUP, sender: str = "10001",
                 name: str = "主人", text: str = "在吗", is_bot: int = 0,
                 is_owner: int = 1, ts: int | None = None, group: str | None = "100") -> None:
    store.add_message(
        ts=NOW - 60 if ts is None else ts,
        umo=umo,
        platform="aiocqhttp",
        group_id=group,
        sender_id=sender,
        sender_name=name,
        is_owner=is_owner,
        is_bot=is_bot,
        text=text,
    )


# ---------------------------------------------------------------------- 用例


def test_kinds() -> None:
    assert infer_kind("我下周要去杭州出差") == PLAN
    assert infer_kind("5 月 20 日是她的生日，别忘了") == ANNIVERSARY
    assert infer_kind("我妈最近腰不好") == RELATION
    assert infer_kind("我最讨厌香菜") == PREFERENCE
    assert infer_kind("我想你了") == INTIMACY
    assert infer_kind("公司楼下新开了家面馆") == FACT
    assert infer_kind("") == FACT
    # "我妈下周生日" 先认日子
    assert infer_kind("我妈下周生日") == ANNIVERSARY
    assert is_warm(RELATION) and is_warm(INTIMACY)
    assert not is_warm(FACT) and not is_warm(PLAN)
    print("ok 类型推断")


def test_recent_chat(base: Path) -> None:
    store = Store(base / "recent.db")
    store.open()
    try:
        seed_message(store, ts=NOW - 300, text="晚上吃啥")
        seed_message(store, umo=GROUP, sender="self", name="KahnBot", text="火锅！",
                     is_bot=1, is_owner=0, ts=NOW - 240)
        seed_message(store, sender="10002", name="小鱼", text="都行", is_owner=0,
                     ts=NOW - 180)
        seed_message(store, sender="self", name="KahnBot", text="那就火锅",
                     is_bot=1, is_owner=0, ts=NOW - 120)
        seed_message(store, umo=PRIVATE, sender="10001", text="私聊无关",
                     ts=NOW - 100, group=None)

        rows = store.recent_messages(GROUP, limit=10)
        assert len(rows) == 4, [r["text"] for r in rows]
        assert rows[0]["text"] == "晚上吃啥" and rows[-1]["text"] == "那就火锅"

        block = build_recent_chat(rows, max_chars=900)
        assert block.startswith("<recent_chat>") and block.endswith("</recent_chat>")
        assert "主人：晚上吃啥" in block
        assert "你：火锅！" in block
        assert "小鱼：都行" in block
        # 顺序是时间正序
        assert block.index("晚上吃啥") < block.index("火锅！") < block.index("都行")
        # 私聊的流水不串台
        assert "私聊无关" not in block

        small = build_recent_chat(rows, max_chars=100)
        assert "晚上吃啥" not in small  # 预算不够先丢最旧的
        assert "那就火锅" in small
        assert store.recent_messages(GROUP, limit=0) == []
        print("ok 最近对话")
    finally:
        store.close()


def test_drop_warm() -> None:
    def hit(kind: str, text: str) -> Hit:
        return Hit("id-" + kind, text, 0.9, "owner", 50, NOW, kind)

    hits = [hit(RELATION, "妈妈腰不好"), hit(FACT, "住杭州"), hit(INTIMACY, "想她了")]
    kept = drop_warm(hits)
    assert [h.kind for h in kept] == [FACT]
    print("ok 生气时收起暖记忆")


def test_emotion_hooks() -> None:
    emotion = Emotion(default_state(NOW))
    before = emotion.state["expectation"]
    assert emotion.on_plan_soon(NOW) is not None
    assert emotion.state["expectation"] > before
    raised = emotion.state["expectation"]
    assert emotion.on_plan_soon(NOW) is None  # 同一天只抬一次
    assert abs(emotion.state["expectation"] - raised) < 1e-9
    assert emotion.state["plan_soon_day"] == "2026-11-12"

    joy = emotion.drives["joy"]
    assert emotion.on_anniversary(NOW) is not None
    assert emotion.drives["joy"] > joy
    assert emotion.on_anniversary(NOW) is None

    assert Emotion(default_state(NOW)).render() is None
    always = Emotion(default_state(NOW)).render(always=True) or ""
    assert "平常" in always, always  # M7：每轮注入，日常也要贴一句
    print("ok 日程/纪念日联动")


def test_poke_softens(base: Path) -> None:
    emotion = Emotion(default_state(NOW))
    emotion._enter_avoidant("测试", NOW, private=True)
    assert emotion.is_avoidant()
    emotion.on_poke(owner=False, now=NOW)
    assert int(emotion.state["owner_msgs"]) == 0  # 别人拍不算
    for _ in range(AVOID_OWNER_MSGS - 1):
        emotion.on_poke(owner=True, now=NOW)
    assert emotion.is_avoidant(), "还没拍够，继续躲"
    emotion.on_poke(owner=True, now=NOW)
    assert not emotion.is_avoidant(), "拍够了就该心软"
    assert "心软" in (emotion.state["log"][-1]["reason"] if emotion.state["log"] else "")
    print("ok 拍多了会心软")


def test_writer_kind(base: Path) -> None:
    store = Store(base / "kind.db")
    store.open()
    try:
        files = MemoryFiles(base)
        cfg = load_config({})
        writer = MemoryWriter(store, files, FakeEmbedder(), cfg, None)
        r = asyncio.run(writer.add(text="下周三要去体检", source="owner", umo=PRIVATE))
        assert r.reason == "added"
        row = store.get_fact(r.fact_id)
        assert row["kind"] == PLAN, row["kind"]

        r2 = asyncio.run(writer.add(text="11 月 20 日是我们在一起的纪念日", source="owner", umo=PRIVATE))
        assert store.get_fact(r2.fact_id)["kind"] == ANNIVERSARY

        # 关掉推断就保持 fact
        cfg2 = load_config({"kind_inference": False})
        writer2 = MemoryWriter(store, files, FakeEmbedder(), cfg2, None)
        r3 = asyncio.run(writer2.add(text="我明天要去面试", source="owner", umo=PRIVATE))
        assert store.get_fact(r3.fact_id)["kind"] == FACT
        print("ok 写入时猜类型")
    finally:
        store.close()


def test_reminder_hooks(base: Path) -> None:
    store = Store(base / "remind.db")
    store.open()
    try:
        files = MemoryFiles(base)
        cfg = load_config({"timezone": "Asia/Shanghai"})
        writer = MemoryWriter(store, files, FakeEmbedder(), cfg, None)
        rec = Recorder2()
        cron = FakeCron()
        service = ReminderService(
            plugin_name="kahnbot",
            store=store,
            files=files,
            cfg=cfg,
            send=rec,
            context=type("Ctx", (), {"cron_manager": cron})(),
            tz_name="Asia/Shanghai",
            on_sent=rec.hook,
        )

        # 普通提醒：发完清 due、删排期、摘 job、回调一次
        due = int(time.time()) + 600
        r = asyncio.run(writer.add(text="明天交房租", source="owner", due_ts=due, umo=PRIVATE))
        assert store.get_fact(r.fact_id)["due_ts"] == due
        assert asyncio.run(service.schedule_fact(r.fact_id))
        assert len(cron.jobs) == 1
        asyncio.run(service._fire(r.fact_id))
        assert rec.sent and rec.sent[-1] == (PRIVATE, "提醒主人一下：明天交房租")
        assert rec.hooks and rec.hooks[-1][0] == PRIVATE
        assert store.get_fact(r.fact_id)["due_ts"] is None
        assert not cron.jobs
        assert files.schedule_phrase(r.fact_id) == ""

        # 纪念日：发完顺延到明年，job 留着，排期改写
        r2 = asyncio.run(writer.add(
            text="11 月 20 日是纪念日", source="owner", due_ts=due + 86400, umo=PRIVATE,
        ))
        assert store.get_fact(r2.fact_id)["kind"] == ANNIVERSARY
        asyncio.run(service.schedule_fact(r2.fact_id))
        assert len(cron.jobs) == 1
        asyncio.run(service._fire(r2.fact_id))
        nxt = store.get_fact(r2.fact_id)["due_ts"]
        assert nxt is not None and nxt > due + 300 * 86400
        assert len(cron.jobs) == 1, "纪念日的 job 要留着，明年还发"
        phrase = files.schedule_phrase(r2.fact_id)
        assert phrase and "提醒" in phrase
        assert rec.hooks[-1][0] == PRIVATE
        print("ok 提醒回调与纪念日顺延")
    finally:
        store.close()


def test_question_form() -> None:
    # 客气的委托也是委托："能不能/能 + 记住" 的疑问句要认
    r = remember.detect("鱼宝能记住明天早上10点开会吗", woken=True, wake_names=("鱼宝",))
    assert r is not None and r.text == "开会", r
    assert r.due_ts is not None

    r = remember.detect("能不能帮我记住周五交房租？", woken=True)
    assert r is not None and r.text == "交房租", r

    r = remember.detect("麻烦你记一下，下周三体检，行吗？", woken=True)
    assert r is not None and r.text == "体检", r

    # 光秃秃的"记住…吗"更像在问"你记得吗"，不认
    assert remember.detect("记住我的生日吗", woken=True) is None
    assert remember.detect("你记住了吗？", woken=True) is None
    assert remember.detect("记住了没", woken=True) is None
    # 陈述句照旧
    r = remember.detect("记住 明天下午三点开会", woken=True)
    assert r is not None and r.text == "开会"
    print("ok 客气问句也算记住")


def test_reset(base: Path) -> None:
    store = Store(base / "reset.db")
    store.open()
    try:
        files = MemoryFiles(base)
        files.ensure_files()
        cfg = load_config({})
        writer = MemoryWriter(store, files, FakeEmbedder(), cfg, None)
        asyncio.run(writer.add(text="下周三要去体检", source="owner", umo=PRIVATE, due_ts=NOW + 86400))
        store.add_message(
            ts=NOW,
            umo=PRIVATE,
            platform="aiocqhttp",
            group_id=None,
            sender_id="10001",
            sender_name="主人",
            is_owner=True,
            is_bot=False,
            text="记住 下周三要去体检",
        )
        store.set_meta("last_summary_ts", str(NOW))
        assert store.distinct_umos() == [PRIVATE]
        state_path = base / "state.json"
        state_path.write_text('{"blame": 80}', encoding="utf-8")

        # 备份 + 清空
        report = reset_all(
            store, files, state_path, backup_root=base / "backup", now=NOW
        )
        assert report["messages"] == 1 and report["facts"] == 1, report
        backup = Path(report["backup"])
        assert (backup / "kahnbot.db").exists()
        assert (backup / "state.json").exists()
        assert (backup / "memory" / "facts.md").exists()

        assert store.count_messages() == 0 and store.count_facts() == 0
        assert store.get_meta("last_summary_ts") is None
        assert not state_path.exists()
        assert files.load("owner") == [] and files.load("observed") == []
        assert files.scheduled() == []
        assert "长期记忆" in files.read("owner")  # 表头还在

        # 不配备份目录就只清不备
        asyncio.run(writer.add(text="我喜欢吃橘子", source="owner", umo=PRIVATE))
        report2 = reset_all(store, files, state_path)
        assert report2["backup"] == "" and store.count_facts() == 0
        print("ok 整机重置")
    finally:
        store.close()


def test_clean_body() -> None:
    # 写入时剥掉触发词与句首时间：到点提醒就不用把原指令念一遍
    assert remember.clean_body("10分钟后提醒我关火") == "关火"
    assert remember.clean_body("明天是我的生日") == "我的生日"
    assert remember.clean_body("明天早上10点开会") == "开会"
    assert remember.clean_body("下周三要去体检") == "要去体检"
    # 绝对日期不剥：纪念日的日期本身就是内容
    assert remember.clean_body("5 月 20 日是我们的纪念日") == "5 月 20 日是我们的纪念日"
    assert remember.clean_body("我喜欢冰美式") == "我喜欢冰美式"
    assert compose_phrase("关火") == "提醒主人一下：关火"
    print("ok 写入时剥掉指令语与时间")


class _AssessLLM:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.prompts: list[str] = []

    async def text(self, prompt, system_prompt=None):
        self.prompts.append(prompt)
        return self.reply


def test_assess() -> None:
    client = _AssessLLM('```json\n{"importance": 87, "reminder": "主人，灶上的火别忘了关"}\n```')
    data = asyncio.run(assess(client, "关火", due_ts=NOW + 600))
    assert data["importance"] == 87 and data["reminder"] == "主人，灶上的火别忘了关", data
    assert "关火" in client.prompts[0]

    # 模型不听话：至少把分数捞回来，文案留空（调用方退回模板）
    data = asyncio.run(assess(_AssessLLM("我觉得 72 分"), "关火"))
    assert data["importance"] == 72 and data["reminder"] == "", data

    # 拿不到模型也不能让写入路径炸
    assert asyncio.run(assess(None, "关火")) == {"importance": None, "reminder": ""}
    print("ok 打分与提醒文案同一次调用")


def test_config() -> None:
    cfg = load_config({})
    assert cfg.inject_emotion is True
    assert cfg.recent_chat_messages == 10
    assert cfg.recent_chat_max_chars == 600
    assert cfg.kind_inference is True

    cfg = load_config({
        "inject_emotion": False,
        "recent_chat_messages": 999,
        "recent_chat_max_chars": 5,
        "kind_inference": "false",
    })
    assert cfg.inject_emotion is False
    assert cfg.recent_chat_messages == 50
    assert cfg.recent_chat_max_chars == 100
    assert cfg.kind_inference is False
    assert Config().recent_chat_messages == 10
    print("ok 配置默认值")


def main() -> None:
    if BASE_DIR.exists():
        shutil.rmtree(BASE_DIR)
    BASE_DIR.mkdir(parents=True)
    try:
        test_kinds()
        test_recent_chat(tmpdir("recent"))
        test_drop_warm()
        test_emotion_hooks()
        test_poke_softens(tmpdir("poke"))
        test_writer_kind(tmpdir("kind"))
        test_reminder_hooks(tmpdir("remind"))
        test_question_form()
        test_clean_body()
        test_assess()
        test_reset(tmpdir("reset"))
        test_config()
    finally:
        shutil.rmtree(BASE_DIR, ignore_errors=True)
    print("ALL OK", BASE_DIR)


if __name__ == "__main__":
    main()
