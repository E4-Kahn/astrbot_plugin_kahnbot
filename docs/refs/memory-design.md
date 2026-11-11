# 记忆功能设计草案

> 状态：覆盖 F3/F4/F5/F6/F7/F8/F9/F21 的设计，2026-11-12 定稿（M4/M5 按此实现）。
> 需求来源：`docs/REQUIREMENTS.md`；AstrBot 接口事实：`docs/refs/astrbot-capabilities.md`。
> 本轮三项变更（2026-11-12）：**取消 `pinned`，改用 0–100 重要度（写入时由 LLM 打分）**、
> 新增「已被取代」标记、提醒改由 AstrBot cron 调度（basic job + 我们自己的 handler）。

## 1. 范围与边界

做：

- 记录「谁在哪个会话说了什么」（流水）。
- 从中沉淀「长期事实」（记忆条目）。
- 让模型能检索这两者，并在回答时带上归属。

不做（本阶段）：

- 不重存对话上下文——AstrBot 的 `conversation.history` 已有，见 capabilities 第 4 节。
- 不做"压缩后补原文"——AstrBot 压缩无事件可监听，见 capabilities 第 7 节。
- 不做本地向量模型——避免 24MB 模型下载与原生依赖（dsh-memory-keeper 的坑）。

## 2. 数据落点

根目录：`StarTools.get_data_dir()` → `data/plugin_data/astrbot_plugin_kahnbot/`

```
data/plugin_data/astrbot_plugin_kahnbot/
├── memory/
│   ├── facts.md          # owner 层：主人亲口说的事实与约定（可触发提醒）
│   ├── observed.md       # observed 层：群友转述（只作背景，不触发提醒）
│   └── archive.md        # 归档（F7 定稿后启用）
├── kahnbot.db            # SQLite：流水 + 索引元数据
└── state.json            # 插件自身状态（情绪、最后汇总时间等）
```

理由：md 负责"人要看、要改"的长期事实；SQLite 负责"量大、要查、要过滤"的流水。
两份数据职责不重叠，不存在同一事实两处真相。

## 3. 记忆条目（md）格式

一行一条，沿用 dsh-memory-keeper 的可 grep 形态，但**扩展了归属与可见性**：

```md
- **2026-11-11** · [群:123456|主人|uid:10001|owner] 主人下周三要去杭州 〔重要度 70〕
- **2026-11-11** · [私聊|主人|uid:10001|owner] 主人最近在准备考试，不想被打扰 〔重要度 85〕
- **2026-11-11** · [群:123456|张三|uid:10002|obs] 张三说"明天是管理员的生日" 〔重要度 35〕
- **2026-11-11** · [私聊|主人|uid:10001|owner] 主人现在的作息是十一点睡 〔重要度 60〕〔已被取代 by #41〕
```

字段顺序：日期 · `[来源|说话人|uid|可信层]` 正文 · 可选后缀。

- `来源`：`群:<group_id>` 或 `私聊`
- `说话人`：`主人` / 其他人的昵称，**只给人读，不参与任何匹配**
- `uid`：平台用户 ID（QQ 号），**唯一身份键**——去重、归属、配额、解禁、检索过滤全用它
- `可信层`（F21）：`owner` = 主人亲口/显式记住（可触发主动提醒）；`obs` = 群友转述（只作背景，
  注入时标注"未必属实"，排序 ×0.6，**永不触发提醒**）
- `〔重要度 N〕`：0–100，**写入时由 LLM 打分**（见 5.1），决定检索排序与是否豁免归档；
  手改这一行即可调整，下次启动重建索引时生效
- `〔已被取代 by #id〕`：同主题有了更新的说法时，旧条不删、只标记；默认不参与检索（见 8.3）

> 归属是硬要求（F8/F8a）。AstrBot 的 `provider_settings.identifier` 只标当前发言人，
> 历史不带归属，所以必须在这里自己写死。

> **认人只用 uid，不用昵称。** 昵称可随时改、可重名，只在写入时记"当时叫什么"；
> 展示时按 uid 取最近一次见到的昵称渲染。群友改了昵称，记忆不丢、配额不重置。

## 4. 流水（SQLite）

```sql
CREATE TABLE message (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,        -- unix 秒
    umo         TEXT    NOT NULL,        -- unified_msg_origin
    platform    TEXT    NOT NULL,
    group_id    TEXT,                    -- 私聊为 NULL
    sender_id   TEXT    NOT NULL,        -- 身份键（QQ 号）
    sender_name TEXT,                    -- 当时昵称，仅供展示，可变
    is_owner    INTEGER NOT NULL DEFAULT 0,
    is_bot      INTEGER NOT NULL DEFAULT 0,  -- 机器人自己发的（含主动消息），汇总时不当成主人事实
    text        TEXT    NOT NULL,
    msg_id      TEXT                     -- 平台消息 ID，去重用
);
CREATE INDEX idx_msg_umo_ts ON message(umo, ts);
CREATE INDEX idx_msg_sender ON message(sender_id, ts);

CREATE TABLE fact_index (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    fact_id       TEXT    NOT NULL UNIQUE, -- md 行的稳定标识
    text          TEXT    NOT NULL,
    hash          TEXT    NOT NULL,        -- md5(text.strip())，精确去重
    scope         TEXT    NOT NULL,        -- owner / group:<id> / private
    source        TEXT    NOT NULL DEFAULT 'owner', -- F21：owner / observed
    visibility    TEXT    NOT NULL DEFAULT 'private', -- F9：private / public
    importance    INTEGER NOT NULL DEFAULT 50,  -- 0-100，写入时 LLM 打分
    created_ts    INTEGER NOT NULL,
    last_hit_ts   INTEGER,                 -- F7 用：被检索到的最后时间
    superseded_by TEXT,                    -- 同主题新条目的 fact_id；非空则默认不检索
    due_ts        INTEGER,                 -- 提醒时间（本地时区的 unix 秒），可空
    archived      INTEGER NOT NULL DEFAULT 0  -- F7：已归档则默认不参与检索
);
CREATE UNIQUE INDEX idx_fact_hash ON fact_index(hash);

CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
```

`fact_index` 是 md 的**索引**，不是第二份真相：md 是权威，索引可随时重建
（重建时按文件 mtime 或内容哈希判断，参考 dsh 的指纹思路）。

## 5. 写入路径（F4）

### 5.1 显式指令

主人说"记住…" / `/kahn remember …`：

1. 抽取正文（去掉指令词）。
2. **打分**：同一次调用里让模型给这条打 `importance`（0–100）——判据：会不会影响以后的回答、
   多久还会用到、是不是关于"人"而不是"事"。拿不到分数就默认 50，不阻塞写入。
3. **取代检查**：与已有 `fact_index` 条目算余弦，`> 0.92` 且主题相同（都是作息/都是行程…）→
   旧条标 `〔已被取代 by #新id〕`、`superseded_by` 填新 id。
4. 写入 `facts.md`，同时写 `fact_index`（`source=owner`、`visibility=private`）。
5. 来源与说话人取当前 `event`，不靠模型猜。
6. **回执**：回一句"好，我记下了：<正文>"（含条目 id 更好，方便 `/kahn memory forget`）。

### 5.2 定时汇总

后台任务，按 `state.json` 里 `last_summary_ts` 增量读 `message` 表：

1. 取主人最近 N 条 + 群内他人最近的发言。
2. 交给 LLM 提炼"值得长期记住的事实"，要求输出结构化条目。
3. 去重（文本相似 / 已存在于 `fact_index`）后写入。
4. 失败只记日志，不阻塞、不重试轰炸。

> 去重与节流是这块的主要风险：汇总过频会写出大量低价值条目。

> `is_bot=1` 的行（她自己的回复与主动消息）参与"最近上下文"，但**不参与事实提炼**，
> 否则她说过的话会被当成主人事实再喂回去。

**输入**（`message` 表按 `ts` 升序，只取 `last_summary_ts` 之后的）：

```
[11-11 20:31] 主人(10001)：我下周三去杭州出差
[11-11 20:33] 群友(20002)：杭州最近降温
```

**提示词要点**（模型用 AstrBot 当前默认会话模型，或配置项 `summary_provider_id` 指定）：

- 只抽"长期有用"的事实：偏好、计划、关系、承诺、关键日期、身份信息。
- 不要抽：寒暄、情绪、一次性的提问（**除了**明确写给主人的日期类转述，见下条）。
- 每条必须能独立读懂（不要出现"他/那个"这类指代）。
- 输出 JSON 数组，元素 `{"text": "...", "kind": "plan|preference|relation|fact", "source": "owner|third_party", "confidence": 0-1}`；没有可记的就输出 `[]`。
- **`source` 判定**：原文说话人是主人 → `owner`；是群友 → `third_party`（哪怕内容是"明天是管理员生日"）。
  只有 `owner` 进 `facts.md` 并能触发提醒；`third_party` 进 `observed.md`，注入时标注来源与"未必属实"。
- **不许编造**：只能写原文里出现过的信息；拿不准就不写。

**去重**：

1. 精确：`md5(text.strip())` 与 `fact_index.hash` 比对。
2. 近似：与已有条目算余弦，`> 0.92` 视为重复（保留旧的，更新 `last_hit_ts`）。
3. 单次写入上限（默认 10 条），超出按 `confidence` 截断——防一次汇总刷屏。

**成本**：一次汇总 = 1 次输入（约 2–4k token）+ 1 次输出（几百 token）；默认 6 小时一次，一天 4 次。
`confidence < 0.5` 的条目不入库，只留在流水里。

> **不设内容边界**（用户 2026-11-12 定）：她是单实例机器人，只跑在主人的私聊和一个 4~5 人的小群里，
> 不需要在提示词里加"只提炼与主人有关的事、群里的梗不提炼"这类限制。群里流传的梗、
> 群友的习惯，被提炼成 `observed` 条目就提炼——反正 `observed` 不进 `facts.md`、不触发提醒、注入时标注"未必属实"。

### 5.3 "记住…" 怎么被识别（Q8，2026-11-12 定）

`catch-all` 只写库、**从不调 LLM**（F11），所以未唤醒状态下说"记住 X"不会经过模型——
这是设计上刻意的：群里刷屏的话不该触发记忆写入。

**三层判定**（全部通过才进 F4）：

1. **唤醒层**：这条消息是冲她说的。`AT` / 名字唤醒 / **私聊**（私聊视为常驻唤醒，不需叫名字）。
   拍一拍没有文本，不参与。群友之间含"记住"的话，因为她没被唤醒，天然进不了这一层。
2. **句式层**：`记住 / 记一下 / 记着 / 帮我记 / 别忘了` 出现在**句首或前 6 个字内**，
   且不是疑问（`吗 / 么 / 没 / ？` 结尾）、不是应答（"我记住了""记住了没"）、不是转述（句子以"他说/她说/文章说"开头）。
3. **回执层**：落库后回一句"好，我记下了：…"。这是误判的最后一道防线——你看到不对，一句"删掉"就能纠正。

**明确不做的**：

- ~~形态闸门~~（长文/引用块/链接卡片判定）——2026-11-12 决定本轮不做。代价是：
  长文里以"记住"开头可能误判，靠回执层补偿。将来误判变多再加。
- 不用 LLM 判断"这句话是不是要我记"——省 token、行为可预测。

**"别忘了 / 提醒我"算不算记住**：算。这类话的意图是到点提醒，而提醒需要一条 owner 事实做依据，
所以同样走这三层，落库时额外填 `due_ts`（见第 11 节）。

## 6. 检索（F5）

两种入口，共用同一套查询：

1. **工具**：`memory_search(query, scope?)`，注册为 `llm_tool`，模型自主调用。
2. **关键词触发**：命中"你还记得 / 上次 / 之前"等词时自动检索一次并注入。

排序做**向量 + 字面混合**（2026-11-11 用户确认：倾向本地向量模型，可用 bge-m3 一类模型）。

### 6.1 向量从哪来：复用 AstrBot 的 Embedding Provider

不需要在插件里打包 torch / sentence-transformers（那会是 2GB 级依赖，也让插件从"零依赖"变成"重依赖"）。
AstrBot 已经提供统一的 Embedding 抽象，插件可直接取用：

```python
from astrbot.core.provider.provider import EmbeddingProvider

providers = self.context.get_all_embedding_providers()      # context.py:499
ep = self.context.get_provider_by_id(provider_id)          # context.py:456
vec = await ep.get_embedding("主人下周三要去杭州")           # 单条
vecs = await ep.get_embeddings_batch(texts, batch_size=16)  # 批量
dim = ep.get_dim()
```

AstrBot 4.29.4 自带的 Embedding 实现（`core/provider/sources/`）：

| 来源 | 文件 | 适配 |
| --- | --- | --- |
| OpenAI 兼容 | `openai_embedding_source.py` | SiliconFlow 的 `BAAI/bge-m3` 走这个（自定义 base_url + api key） |
| Ollama | `ollama_embedding_source.py` | 真正本地、免费的 bge-m3 |
| DashScope / Gemini / NVIDIA | 对应 `*_embedding_source.py` | 其他云端选项 |

⇒ 插件只需在配置里填**一个 Embedding Provider 的 ID**，具体用哪家由 AstrBot 面板决定。
用户换模型不用改插件代码。

向量存 `fact_index.embedding`（BLOB，`array('f')`/`struct` 序列化）。条目量级是几千条，
Python 里直接算余弦足够快，不需要 faiss。

### 6.2 混合排序

```
score = 0.6 * 余弦相似度
      + 0.4 * 字面匹配（分数化的 LIKE / 关键词命中）
      × (0.7 + 0.3 * importance / 100)      # 重要度最高把分数抬 43%，最低压 30%
      × 时间衰减（见第 8 节）
      × 0.6（observed 层，F21）
```

- **字面通道必须有**：人名、地名、数字这类精确 token，向量容易糊；而且它也是
  Embedding Provider 不可用时的降级路径（见 6.3 的 FTS 实测数据，两万条 3ms）。
- 权重可调，先按 0.6/0.4 起步。
- **阈值实测（2026-11-12，bge-m3 / 1024 维）**：语义无关的两句话余弦也有 **0.48**（相关对 0.875）。
  所以 `0.6*cos` 这一项的最小值约 0.29，默认下限必须 ≥0.3；插件默认取 **0.35**，
  否则库里任何一条都会"命中"。换更小的模型（如 nomic-embed-text）时这个基线会变，要重测。
- **重要度只影响排序**，不直接决定"留不留"（保留策略见第 8 节）；这样 LLM 打分漂一点，
  也只是排序抖动，不会让一条记忆凭空消失。

### 6.3 实测：FTS5 对中文的坑（2026-11-11 实测）

环境：AstrBot 自带 Python 3.12.16 / SQLite 3.53.1，FTS3/4/5 全部编译开启，
`trigram`、`unicode61`、`porter` 分词器均可创建。

| 查询 | 结果 |
| --- | --- |
| `MATCH '杭州出差'`（4 字） | 命中 |
| `MATCH '下周三'`（3 字） | 命中 |
| `MATCH '杭州'`（2 字） | **空** |
| `LIKE '%杭州%'` | 命中 |

**trigram 分词器要求查询至少 3 个字符，2 字中文查询静默返回空**——而中文里
"杭州""考试"这种两字词极常见，这是个会让人以为"记忆丢了"的坑。

性能（20000 条流水实测）：

| 方式 | 耗时 |
| --- | --- |
| `LIKE '%杭州%'` 全表扫描 | 2.9 ms |
| FTS5 trigram 建索引 | 39.2 ms |
| FTS5 `MATCH` | 0.2 ms |

⇒ 字面通道**初期直接用 `LIKE`**：两字查询正确、两万条 3ms 足够快、无索引同步负担。
等流水到十万级再上 FTS5，且必须保留 `LIKE` 兜底（≤2 字的查询）。

### 6.4 降级

Embedding Provider 未配置或调用失败 → 只用字面通道，并在日志里提示一次，
不报错、不阻塞对话。索引里已有向量的条目照常参与字面排序。

## 7. 注入（F5 的呈现面）

在 `on_llm_request` 里构造：

```
<memory>
- [2026-11-11] 主人下周三要去杭州（来源：群 123456，主人）
</memory>
```

- 只放**结论**，不放原文，控制 token。
- 必须带来源与说话人，否则模型会把别人的话当成主人的。

### 7.1 塞到哪里：不碰 `system_prompt`（F14，待拍板）

**首选**（借鉴 fish_heart，见 refs/astrbot_plugin_fish_heart.md §2.2）：

```python
from astrbot.core.agent.message import TextPart   # 内容块，不是 message_components
req.extra_user_content_parts.append(TextPart(text=body).mark_as_temp())
```

理由：DeepSeek 等前缀缓存按"开头 → 第一个变化点"命中。`system_prompt` 每轮都改，
会让它之后的整段历史全部按未命中重算；`extra_user_content_parts` 挂在**用户消息之后**
（`core/provider/entities.py:311-316`），只让尾部变化。

`mark_as_temp()` 不可省：否则该 part 会被回存进会话历史
（`core/agent/message.py:68-77` 置 `_no_save`；`internal.py:522-523` 只在 `_no_save` 时跳过）。

**降级**：若某次请求的 `extra_user_content_parts` 不可用（字段被适配器重置），退回
`req.system_prompt += body`——功能不受影响，只是缓存吃亏。

## 8. 老化（F7，2026-11-12 修订：取消 pin，改用重要度）

规则：**30 天没被提起、且重要度不高，就降权归档，永不删除。**

- **"被提起"= 检索命中**：每次检索返回该条目时更新 `last_hit_ts`，重置计时。
  （比"显式重述"稳：用户不一定每次都说"记住"；也比"关键词提及"省事。）
- **衰减曲线**：不删除，只在排序时乘 `0.5 ** (距 last_hit_ts 天数 / 30)`。
  刚提起的权重 1.0，30 天没提降到 0.5，60 天 0.25——**它仍然能被检索到**，只是排在后面。
- **归档**：`距 last_hit_ts > 30 天` **且 `importance < 60`** 的条目，后台任务移到 `archive.md`，
  从 `facts.md` 摘掉，`fact_index.archived=1`。归档条目默认不出现在检索结果，除非查询明确带
  "以前/很早之前"，或主人用指令显式查看。
- **`importance ≥ 60` 的条目不归档**（沿用 dsh-memory-keeper 的 `≥60 永不降权` 阈值），
  但**照样参与排序衰减**——它只是不会离开 `facts.md`。
- **为什么取消 `pinned`**（2026-11-12 用户定）：pin 是"你说了算"的布尔开关，但它和重要度管的是
  同一件事的两半（留不留 / 排多前），两个字段并存会出现"pin 了但重要度 20"这种自相矛盾的状态。
  现在只留一个连续值：重要度决定"排多前"，而对保留的影响浓缩成一条门槛（≥60 不归档）。
- 手动调整：改 md 行里的 `〔重要度 N〕`，或 `/kahn memory important <id> <0-100>`（重启/重建索引生效）。

### 8.3 已被取代（2026-11-12 新增）

"会变的事只留最新"——但旧的值不删，只标记：

- 写入新条目时，与同 `kind` 的旧条目算余弦，`> 0.92` 视为同主题 → 旧条标 `〔已被取代 by #<新id>〕`，
  `fact_index.superseded_by = <新fact_id>`。
- 检索默认过滤 `superseded_by IS NOT NULL` 的条目；`/kahn memory list --all` 能看到全部。
- 谁取代谁不需要模型判断，相似度 + kind 相同就够；判错也只是"少显示一条"，不会丢数据。

## 9. 隐私（F9，2026-11-11 定稿：选项 B）

`fact_index.visibility` 两档：

- `private`：来源为私聊（`event.is_private_chat()` 为真）的条目，默认值。
- `public`：主人显式解禁后才允许出现在**群聊**会话的检索结果里。

检索时的过滤：

```python
if not event.is_private_chat():
    rows = [r for r in rows if r.visibility == "public"]
```

即：**私聊里能查到一切，群聊里只能查到 public**。解禁的入口是主人指令
（如 `/kahn memory expose <id>`），默认全部 private。

这挡住了"在群里被问到私聊内容时顺嘴说出来"——模型拿不到那段上下文，
就不可能说漏。

## 10. 提醒与日程（F16，2026-11-12 定：方案 2）

**调度用 AstrBot 的 cron，发言用我们自己的 handler。**

AstrBot 4.29.4 自带 `CronJobManager`（`core/cron/manager.py:103`，底层 APScheduler，
任务持久化在 `data_v4.db` 的 `cron_jobs` 表，重启自恢复），且插件侧可直接用
`self.context.cron_manager`（`core/star/context.py:178`）。

选 basic job 而不是 active_agent job：

| | `add_basic_job`（我们选它） | `add_active_job`（内置 `future_task` 用） |
|---|---|---|
| 到点做什么 | 调我们注册的 handler，只发一句话 | 唤醒完整主 agent，带历史、带工具 |
| 成本 | 一次发送，几乎为零 | 一次完整 LLM 回合 |
| 可控性 | 我们决定说什么 | 模型自由发挥 |

实现要点：

1. 事实落 `facts.md` 时若带时间（"下周三体检"），`fact_index.due_ts` 记下解析后的时间戳；
   同时在 `schedule.md` 里加一行给人看的日程。
2. 建任务：`cron_manager.add_basic_job(name=..., cron_expression=None, run_at=due, handler=...)`——
   需要一次性任务时用 `run_once` 语义（见 `CronJobManager.add_basic_job` 的 `persistent` 参数）。
3. 到点：handler 读对应 fact，发一句提醒（`context.send_message(umo, ...)`），然后：
   - 标 `due_ts = NULL`（提醒只响一次）；
   - 如果是一次性任务，job 自己在 `run_once` 结束后删除。
4. **重启恢复**：basic job 的 handler 只存在内存 `_basic_handlers` 里，重启后核心会
   打日志 `Skip scheduling basic cron job … due to missing handler`。所以启动时要自己扫一遍
   `fact_index` 里 `due_ts > now` 的条目，按 job payload 里存的 `fact_id` 重新挂 handler
   （或干脆用我们自己的 `meta.last_due_scan` 重建任务）。**这一步不做，重启后提醒就哑了。**

**风险**：

- cron 唤醒的事件平台是 `cron`（`core/cron/events.py:17` 的 `CronMessageEvent`），
  会进我们的 catch-all。**必须挡住**：`platform == "cron"` 或 `sender_id` 为空的消息不记录、
  不计配额、不更新情绪——否则提醒本身会被当成一条"消息"污染流水。
- 时区：`fact_index.due_ts` 存本地时区时间，建 job 时用 `ZoneInfo(config["timezone"])`，
  AstrBot 配置里没写就跟随系统。

## 11. 待定与风险

- [x] F14 注入位置、F15 注入预算与去重、F16 主动说话 → 均已拍板（Q5~Q7），见 REQUIREMENTS 功能表。
- [ ] SQLite 并发：多群同时写入需 `WAL` 模式 + 单连接串行化，否则易锁（实测 `journal_mode=wal` 可正常启用，M1 已开）。
- [ ] md 与索引一致性：只改 md 不改库（手动编辑）时，需在启动/检索时校验重建。
- [ ] 汇总任务与消息高峰的冲突：放后台队列，别在事件管线里跑。
- [ ] 私聊/群聊的 `umo` 差别要让模型明确知道，否则回答会串场。
- [ ] LLM 打分的稳定性：同一条事实两次打分可能差 20 分。缓解——分数只影响排序；
      "是否归档"用 `importance < 60 且非 owner 承诺类` 一起判，避免一次低分就被归档。
- [ ] basic job 的重启恢复（第 10 节第 4 条）尚未实现，M5 前必须补。
- [ ] 提醒文案不要由 handler 硬编码：`schedule.md` 里存一句"到时候怎么说"，handler 直接发；
      否则所有提醒都是一个腔调。
