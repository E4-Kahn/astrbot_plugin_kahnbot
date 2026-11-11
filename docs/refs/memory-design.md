# 记忆功能设计草案

> 状态：**草案**，仅覆盖已确认的需求（F3/F4/F5/F6/F8）。
> 老化（F7）与隐私（F9）的具体形态**尚未定**，本文只留接口位。
> 需求来源：`docs/REQUIREMENTS.md`；AstrBot 接口事实：`docs/refs/astrbot-capabilities.md`。

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
- **2026-11-11** · [群:123456|主人|uid:10001|owner] 主人下周三要去杭州
- **2026-11-11** · [私聊|主人|uid:10001|owner] 主人最近在准备考试，不想被打扰
- **2026-11-11** · [群:123456|张三|uid:10002|obs] 张三说"明天是管理员的生日"
```

字段顺序：日期 · `[来源|说话人|uid|可信层]` 正文。

- `来源`：`群:<group_id>` 或 `私聊`
- `说话人`：`主人` / 其他人的昵称，**只给人读，不参与任何匹配**
- `uid`：平台用户 ID（QQ 号），**唯一身份键**——去重、归属、配额、解禁、检索过滤全用它
- `可信层`（F21）：`owner` = 主人亲口/显式记住（可触发主动提醒）；`obs` = 群友转述（只作背景，
  注入时标注"未必属实"，排序 ×0.6，**永不触发提醒**）

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
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fact_id     TEXT    NOT NULL UNIQUE, -- md 行的稳定标识
    text        TEXT    NOT NULL,
    hash        TEXT    NOT NULL,        -- md5(text.strip())，精确去重
    scope       TEXT    NOT NULL,        -- owner / group:<id> / private
    source      TEXT    NOT NULL DEFAULT 'owner', -- F21：owner / observed
    visibility  TEXT    NOT NULL DEFAULT 'private', -- F9：private / public
    created_ts  INTEGER NOT NULL,
    last_hit_ts INTEGER,                 -- F7 用：被检索到的最后时间
    pinned      INTEGER NOT NULL DEFAULT 0,
    archived    INTEGER NOT NULL DEFAULT 0  -- F7：已归档则默认不参与检索
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
2. 写入 `facts.md`，同时写 `fact_index`。
3. 来源与说话人取当前 `event`，不靠模型猜。

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

### 5.3 "记住…" 怎么被识别（Q8，待拍板）

`catch-all` 只写库、**从不调 LLM**（F11），所以未唤醒状态下说"记住 X"不会经过模型。三条路：

| 方案 | 机制 | 代价 |
| --- | --- | --- |
| A. 正则直存 | catch-all 里匹配 `记住\|记一下\|别忘了` → 直接落库 | 零成本，但会误存（"记住带伞"这类自言自语） |
| B. 唤醒后工具调用 | 被唤醒时才让模型调 `remember_fact` | 准确，但主人必须同时 @/叫名字 |
| C. A + 唤醒时确认 | 正则先存草稿，下次唤醒时让模型确认/改写 | 折中，多一次交互 |

倾向 **B**（准确优先），A 作为可选开关。

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
      + pinned 加权
      × 时间衰减（见第 8 节）
```

- **字面通道必须有**：人名、地名、数字这类精确 token，向量容易糊；而且它也是
  Embedding Provider 不可用时的降级路径（见 6.3 的 FTS 实测数据，两万条 3ms）。
- 权重可调，先按 0.6/0.4 起步。

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

## 8. 老化（F7，2026-11-11 定稿）

规则：**30 天没被提起就降权归档，永不删除。**

- **"被提起"= 检索命中**：每次检索返回该条目时更新 `last_hit_ts`，重置计时。
  （比"显式重述"稳：用户不一定每次都说"记住"；也比"关键词提及"省事。）
- **`pinned`**：显式说"记住…"写入的条目默认 `pinned=1`。pin 的条目不参与衰减，
  也不进归档，除非主人手动取消。
- **衰减曲线**：不删除，只在排序时乘 `0.5 ** (距 last_hit_ts 天数 / 30)`。
  刚提起的权重 1.0，30 天没提降到 0.5，60 天 0.25——**它仍然能被检索到**，只是排在后面。
- **归档**：超过 30 天且 `pinned=0` 的条目，后台任务移到 `archive.md`，从 `facts.md` 摘掉，
  `fact_index.archived=1`。归档条目**默认不出现在检索结果**，除非查询明确带"以前/很早之前"，
  或用户用指令显式查看。

为什么不做"自动淘汰"（当初的选项 C）：记忆被删是不可逆的，而"降权"已经把效果拿到了；
真要清理，给主人一个指令手动删。这也符合"不删除"的原始诉求。

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

## 10. 待定与风险

- [ ] F14 注入位置（`extra_user_content_parts` vs `system_prompt`）、F15 注入节流、F16 主动说话 → 等用户拍板 Q5~Q7。
- [ ] SQLite 并发：多群同时写入需 `WAL` 模式 + 单连接串行化，否则易锁（实测 `journal_mode=wal` 可正常启用）。
- [ ] md 与索引一致性：只改 md 不改库（手动编辑）时，需在启动/检索时校验重建。
- [ ] 汇总任务与消息高峰的冲突：放后台队列，别在事件管线里跑。
- [ ] 私聊/群聊的 `umo` 差别要让模型明确知道，否则回答会串场。
