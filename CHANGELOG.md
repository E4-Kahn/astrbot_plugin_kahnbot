# 更新日志

## [Unreleased]

### 新增（M1：数据层与记录）

- `core/store.py`：SQLite 数据层，四张表（`message` / `fact_index` / `wake_quota` / `meta`）+ WAL + 缺列自动补齐；数据落在 `data/plugin_data/astrbot_plugin_kahnbot/kahnbot.db`。
- `core/recorder.py`：catch-all handler（优先级 100）把群聊/私聊消息写进流水，带 `sender_id`/`sender_name`/`group_id`/`is_owner`/`is_bot`，不唤醒、不调模型；`#sl` 等固定文本按 `noise_patterns` 丢弃。
- 指令改为指令组：`/kahn status`、`/kahn memory list [n]`、`/kahn memory forget <id>`（删除仅限主人 / AstrBot 管理员）；单独发 `/kahn` 列出子指令。
- 配置新增 `owner_ids`、`noise_patterns`、`record_private`、`record_self`。

### 新增（M2：唤醒与配额）

- `core/wake.py`：唤醒判定——@ 她、拍一拍她（只认拍自己的）、消息正文出现名字、私聊；此外还负责识别"这条消息是否已经被指令 handler 认领"（靠 `activated_handlers` 里的 filter 类型，不猜前缀）。
- 命中唤醒后由 catch-all handler 置 `event.is_at_or_wake_command = True` 放行默认 LLM 流程；未命中的消息只进流水，不调模型。
- 每日唤醒配额（`wake_quota` 表）：群友超过 `quota_daily` 后静默忽略（仍然记录），主人 / 管理员 / 私聊不计。
- 消息防抖（F23）：被叫到后等 `debounce_seconds`（默认 2 秒）静默窗口，期间的新消息合并成一次回复；窗口可顺延，总等待不超过 `debounce_max_seconds`；指令不参与排队。
- 陌生人私聊屏蔽（F24）：不是主人 / 管理员的私聊不回复、不记录，并 `stop_event()` 阻止内置助手代答；未配置 `owner_ids` 时自动不生效，避免把主人一起挡掉。
- 拍一拍补文案：`message_str` 为空时补"（某某 拍了拍你）"，昵称取流水缓存（拍一拍事件本身拿不到昵称）。
- 纯图片 / 表情 / 语音 / 文件消息按 `[图片]`、`[表情]`、`[语音]`、`[文件]` 占位入流水，不再被当成空消息丢掉。
- 配置新增 `wake_on_poke`、`wake_on_name`、`wake_names`、`quota_enabled`、`quota_daily`、`debounce_seconds`、`debounce_max_seconds`、`block_stranger_private`。
- `tests/smoke_m2.py`：唤醒判定、指令识别、占位符、配额、防抖窗口与等待上限的离线冒烟。

### 新增（M3：情绪层）

- `core/emotion.py`：九维情绪（attachment / curiosity / reflection / duty / social / fatigue / intimacy / stress / joy）、`blame`、`expectation`，每个维度有自己的回归速率；五档 `daily` / `aggrieved` / `angry` / `afraid` / `avoidant`（`avoidant` 留给 M6 的"躲"）。
- 只有主人能推动 `blame`（F20）：夸奖 / 道谢降 5，责骂涨 15，道歉 / 哄降 30；群友的话只进流水。
- 拍一拍：被拍 +社交 +愉悦；生气档被拍会额外涨 `blame`（被戳更烦）。
- `core/heartbeat.py`：唯一的钟——基础 45 分钟、最近一小时有人说话时 15 分钟；30 秒轮询 + 到点判定；关机回来用闭式解 `v = base + (v-base)*(1-rate)^n` 一次补完（n 封顶 96）。
- `core/state.py` + `data/<插件>/state.json`：临时文件 + `os.replace` 原子落盘，旧版本缺字段自动补全。
- `/kahn mood`：九维进度条 + 档位 + 最近一次情绪变化；`/kahn mood why` 看情绪日志。
- `<emotion>` 的自然语言渲染已经就绪（日常且无偏离时不注入），真正挂到请求上是 M4 的事。
- `tests/smoke_m3.py`：漂移收敛、闭式解与逐次漂移等价、五档边界、主人事件、拍一拍、渲染、状态补全、心跳补跳的离线冒烟。

### 新增（M4：检索与注入）

- `core/embeddings.py`：复用 AstrBot 的 embedding provider（`embedding_provider_id` 留空则取第一个启用的）；
  向量以 float32 存 BLOB（1024 维 ≈ 4KB）；provider 缺失或调用失败时静默降级为纯字面，只告警一次。
- `core/retriever.py`：混合排序 `(0.6*余弦 + 0.4*字面) × (0.7+0.3*重要度/100) × 时间衰减 × 0.6(转述)`；
  中文 bigram + 英文数字分词；过滤已归档与已被取代；群聊上下文只查 `visibility='public'`；命中回写 `last_hit_ts`。
- `core/injector.py`：`<memory>` 块拼装、单次字符预算截断、同一事实 N 轮内不重复；没有新命中就整段不注入。
- `on_llm_request(priority=-1000)` 自动注入：`extra_user_content_parts` + `mark_as_temp()`，不写 system_prompt。
- `fact_index` 加 `kind` / `importance` / `superseded_by` / `due_ts` / `embedding` 五列（加列式迁移，旧库自动补）。
- 新指令：`/kahn recall <问句>`（手动检索，显示分数与来源）、`/kahn memory facts [n]`、`/kahn memory add <内容> [重要度]`。
- 配置新增 `inject_enabled`、`embedding_provider_id`、`recall_top_k`、`recall_min_score`、`inject_max_chars`、`dedupe_turns`。
- 实测 bge-m3：无关句子余弦约 0.48，因此 `recall_min_score` 默认取 0.35（低于 0.3 会什么都在命中）。
- `tests/smoke_m4.py`：分词、字面分、存储往返、隐私/取代/归档过滤、排序、无向量降级、注入预算与去重的离线冒烟。
### 修复

- `/kahn memory add`、`/kahn recall` 的正文参数改为 `GreedyStr`：之前用普通 `str`，AstrBot 按空格切参数，
  带空格的内容会被切碎，末尾的"重要度 80"还会被当成第二个位置参数并抛
  `参数 importance 类型错误`。现在正文整段接收，"重要度 N"从末尾剥离（`core/args.py`）。
- `/kahn …` 指令不再进流水：catch-all 记录原本把指令也当成聊天记下来（`/` 前缀已被
  waking_check 剥掉，所以看起来像"主人说了 kahn memory add …"）。现在按 `activated_handlers`
  里的 filter 类型识别指令并跳过，同时也跳过情绪推进。
### 变更

- 仓库与插件目录更名为 `astrbot_plugin_kahnbot`，与插件标识符保持一致。原先仓库目录名为 `kahnbot`，与 `metadata.yaml` 的 `name` 不一致，会导致 WebUI 安装与手动 clone 得到两个不同目录、两份不同配置。

### 文档

- 需求清单 `docs/REQUIREMENTS.md` 整理为 F1–F24 与 Q1–Q12；记忆相关 F3–F9、唤醒 F10–F13、注入与主动说话 F14–F16、情绪与心跳 F17–F22、防抖与陌生人私聊 F23–F24 已确认；"不做什么"与使用范围（私聊 + 4~5 人小群）已写实。
- 新增 `docs/refs/`：`astrbot-capabilities.md`（AstrBot 4.29.4 能力核对，含踩坑）、`memory-design.md`（记忆数据与检索设计）、`wake-design.md`（唤醒与配额设计）、`emotion-design.md`（九维情绪、心跳、事实分层、主动说话）、`dsh-memory-keeper.md` 与 `astrbot_plugin_fish_heart.md`（参考插件调研）、`deployment.md`（部署评审记录）。
- 新增 `docs/IMPLEMENTATION.md`（M1–M6 里程碑）与 `docs/DEPLOYMENT.md`（从零部署清单，含 embedding / 会话模型配置）。
- 逐条核对文档里的源码引用（AstrBot 4.29.4 / SnowLuma），修正失效行号与错误的 `TextPart` 导入路径。
- 兼容下限抬到 AstrBot `>= 4.29.4`（只在 4.29.4 实测过）；补齐「背景与目标」；新增「工作区洁净」约定（测试产物只落 `.smoke_tmp/` 或系统临时目录）。

## [0.1.0] - 2026-11-11

### 新增

- 插件脚手架：`Star` 子类、配置注入（`_conf_schema.json`）、插件专用日志、数据目录。
- `/kahn` 指令：查看版本、运行状态与数据目录。
- 文档骨架：README、需求文档 `docs/REQUIREMENTS.md`。
