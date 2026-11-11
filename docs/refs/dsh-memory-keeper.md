# dsh-memory-keeper（DSH cordis 插件，非 AstrBot 插件）

- 来源：`D:\workspace\refs\dsh-memory-keeper`（GitHub: flywalker68/dsh-memory-keeper）
- 调研日期：2026-11-11
- 代码规模：`lib/` 5 个 JS 文件约 108 KB（index.js 43.9 / search.js 28.2 / state.js 25.3 / recall.js 6.4 / embedding.js 4.0），`test.js` 34 KB，`client/client.js` 18.8 KB
- 宿主：DSH（DeepSeek Harness）0.2.x，cordis 插件模型；Node 24+

## 1. 速览

**定位：检索层，不是完整记忆系统**（README:7）。它读用户已经在写的 md，不替用户写；
逐轮回放、增量提取、摘要、遗忘这些明确划给宿主或"整理层"。

- 数据：纯本地 md，**不调 API、不需要 key**（README:5）
- 工具 5 个：`memory_search`、`chat_search`、`memory_check`、`memory_touch`、`persona_edit`
- 非工具能力 3 个：状态卡（每轮注入 system prompt）、压缩后补原文（compaction 事件）、Web 侧栏面板
- 配置 18 项（lib/index.js:32-67）
- 第三方依赖：`@xenova/transformers` + `sharp`（本地向量模型 bge-small-zh-v1.5，约 24 MB，可关）

生命周期被切成四段（README:65-90）：**写下来（模型按 AGENTS.md 自己写）→ 变成能搜的（切块建索引）→
用起来（检索/状态卡/补原文）→ 维护（降权/归档/精简）**。代码只保证第 2、3 段和第 4 段的机械部分。

## 2. 机制

### 2.1 数据格式（这是全系统的地基）

- 一行一条记忆，`- ` 开头；**行首日期** `- **YYYY-MM-DD** · 正文`（`examples/记忆格式说明.md:16-28`）
- 切块规则（`lib/search.js:359`）：`text` 截前 **300 字**；`< 6 字` 的行跳过；`#` 标题、`>` 引用、`---` 分隔线跳过；
  带「已被取代」标记的跳过
- 块结构：`{ file, line, text }`，`chunkKey = md5(text)`，`INDEX_SCHEMA_VERSION = 3`（search.js:406）
- 特殊文件有特殊解析：`13-重要记忆.md` 认 `〔重要度 0-100〕`、`〔署名〕`；`07-日程.md` 是表格；
  `日记/YYYY-MM/YYYY-MM-DD.md` 认 `〔钩子：…〕` 并加权

### 2.2 索引与重建

- 源文件指纹 = 路径 + 大小 + mtime（search.js:375），变了自动重建，用户无需手动
- `loadOrBuild` 有 10 分钟 TTL 缓存（search.js:409）
- 索引落盘 `<workspace>/.tools/memory-index.json`，向量落 `memory-vectors.json`（JSON + base64，暴力余弦）

### 2.3 检索

- **词面**：中文 bigram + IDF 加权，带停用词表（search.js:96-118）
- **向量**：bge-small-zh-v1.5（int8，24 MB，懒加载，走 hf-mirror）
- **融合**：RRF，`1/(k+rank+1)` 相加，`k = 60`（search.js:617-630）
- **闸门**：向量最高余弦 < `minRelevanceScore`（默认 **0.5**）→ 直接判"没有可靠记忆"，不硬塞弱相关块
  （README:342；0.5 是在 502 块语料上标定的：无答案查询最高 0.447、有答案最低 0.565）
- **时间**：`resolveQueryDate` 解析「三天前 / 上周 / 一个月前 / 10月31号」（search.js:267）；
  `dateBoost` 0.1 只影响排序；`beforeDate` 在词面与向量**两路都过滤**（README:34 强调只堵一路会漏）
- **遗忘线**：日记只检索最近 `recentMonths = 6` 个月，更老的"算忘了"——不删、不进检索

### 2.4 返回层（关键设计）

命中后回原文件取 `full`（整行，突破 300 字截断）与 `context`（前后各 1 行），
**不进索引、不参与打分、`text` 一个字节不动**（README:360）。因此这类改动不用升 schema、不用重建索引，
可以拿"索引快照逐字节相同"当硬验收。

### 2.5 状态卡

组装 system prompt 时注入，缓存 `refreshMinutes = 5`（state.js:402 `buildStatus`）。字段：
认识 N 天（初次日期）、日记进度（昨天写没写/多角色各节是否补全）、今天与明天日程、
体积（**只在跨 200 / 500 KB 阈值时才报**，避免每写一个文件就破缓存）、半衰期候选、人设漂移信号。

> 明确原则：**状态卡只报事实，不下命令**（运行规则.md:139）。

### 2.6 压缩后补原文

`compaction/summary` 事件触发 → 解 DSH 会话日志（`.jsonl.zstd`，多帧拼接，zlib）→
按被压掉的 `shadowedSeqs` 取最近 `compressRecallTurns = 10` 轮 → 单条超 1200 字截断 →
总字数安全阀 12000 → 写 `~/.dsh/compress-recall.json` → 以 systemPrompt 段注入，**维持到下次压缩**（lib/recall.js）。
有 14 条正则过滤"系统注入噪声"，避免把环境提示当对话补回去（recall.js:48-60）。

### 2.7 维护与加固

- `memory_touch`：真正引用了某段才手动 +1（插件不猜）
- 提过 3 次不再进遗忘候选；`staleDays = 90` 只报不删
- 关系片段 `〔重要度 ≥60〕` 永不降权
- `persona_edit`：改人设前自动备份 + 变更日志，保留 2 份后归档

## 3. 坑与坏味道

| 现象 | 位置 | 影响 |
| --- | --- | --- |
| `workspace` 留空 → `process.cwd()`，在 DSH 里是 profile 目录 | index.js:71、README:377 | 日记 0 篇、搜不到东西，**且不报错**（现已加 WARN） |
| `sharp` 缺二进制 → 向量整个挂掉，只剩词面 | README:199-207 | 不阻断启动、不报错，检索质量静默下降；靠返回头"（向量+词面）"字样自检 |
| 索引字段改动要同步三处（切块/序列化/schema 版本） | README:359 | 漏一处 = 字段丢或索引不重建 |
| `text` 的 300 字截断绑死 `chunkKey = md5(text)` | README:361 | 放宽截断 = 向量全废 + 重建 |
| `< 6 字` 的行直接丢 | examples/记忆格式说明.md:13 | "怕辣""住杭州"这类短事实会丢 |
| 写入完全靠用户自觉（AGENTS.md） | README:61 | 没有写入层 = 空转；插件不催不补 |
| 状态卡每轮注入 | index.js:824 | 固定 token 成本；体积提示为此做了阈值节流 |
| 20 项配置 | index.js:32-67 | 上手成本高，作者自己也要在 README 里解释"一般只改 workspace" |

## 4. 对 KahnBot 的启示

### 值得借鉴

1. **记忆正文是原话 md，不是摘要**——可读、可手改、可 grep、可 diff，出问题能修
2. **双路检索 + RRF**：字面管精确、向量管意思，按名次融合而不是硬拼分数
3. **最低分闸门**：宁可不答，也不把弱相关块塞给模型（对抗"检索幻觉"）
4. **返回层与索引解耦**：命中后回文件补整行和上下文，索引不动
5. **时间是一等公民**：行首日期、时间窗口双路过滤、查询里的相对时间解析
6. **压缩后补原文**：摘要保住"做了什么"，保不住"当时怎么说的"
7. **只报不删**的维护哲学：半衰期候选、归档，不自动删
8. **状态卡只报事实不下命令**

### 不借鉴

1. **18 项配置**——QQ/微信机器人用户不会调；KahnBot 该给 2~3 个开关
2. **本地向量模型 + sharp**——24 MB 下载 + 原生二进制，在 AstrBot 的部署环境里是安装事故源头
3. **靠 AGENTS.md 自觉写入**——AstrBot 没有这层"宿主规则文件"，写入必须由插件自己触发
4. **每轮注入状态卡**——私聊尚可，群聊每轮注入不现实，且 token 成本乘以群数

### 存疑待讨论

1. **单人 vs 多人**：DSH 是"一个主人"的助手，AstrBot 是"很多用户 + 很多群"。记忆隔离维度
   （`user_id` / 会话 / 群）是这套设计里**完全没有**的东西，必须重做
2. **没有 compaction 事件**：AstrBot 的上下文管理是否可 hook、压缩后能否补原文，未核实
3. **谁来写记忆**：没有 AGENTS.md，写入要么靠 LLM 自动抽取，要么靠显式指令

## 5. 产出

- 候选需求（待用户拍板，暂不写入 REQUIREMENTS.md）：F3 记忆写入方式、F4 记忆检索、F5 记忆隔离维度、
  F6 记忆维护/遗忘、F7 状态注入、F8 记忆存储形态、F9 隐私与清理
- 相关未决问题：Q3 记忆服务对象、Q4 检索触发方式、Q5 存储形态
