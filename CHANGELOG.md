# 更新日志

## [0.3.1] - 2026-11-12

### 修复

- `main.py`：`_count_proactive_send()` 里调了不存在的 `self._today()`（类里只有模块级 `_today()`），
  提醒发出去之后计数、写流水、回写历史一起抛 `AttributeError`——正好造成「说完就忘」。
  改用模块级函数，并给提醒/主动消息的收尾各包一层 `try/except`：计数失败不再拖累回写。
- `main.py`：`_send_text()` 发送成功后补记流水（`is_bot=1`），记忆回执也记一条——
  否则最近对话里永远看不到她自己说过的话，「你：」那一行形同虚设。
- `core/remember.py`：客气问句放宽。「能记住明天十点开会吗」「能不能帮我记住…」「麻烦你记一下…行吗」
  这类**有请求框架**的疑问句仍算委托；光秃秃的「记住我的生日吗」「你记住了吗」还是不当真。
  正文会先剥掉「吗 / 好吗 / 行吗」再入库。

### 新增

- `core/reset.py` + `/kahn reset [confirm]`：整机重置——清流水、记忆、提醒排期、情绪与主动计数，
  插件配置不动；`reset_backup_dir` 非空时先备份 `kahnbot.db` / `state.json` / `memory/`
  到 `<目录>/kahnbot-<时间>/`（用 sqlite 在线备份，服务运行中也能拿到一致快照）。
  备份失败直接中止重置，不会出现「以为备份了其实没有」。
  重置时还会给流水里出现过的每个会话在 AstrBot 侧开一个新对话，并清掉注入去重记录——
  否则模型仍能从旧对话里接上「刚聊过什么」，不算真正从零。
- `main.py`：启动时检查 AstrBot 的 `provider_settings.proactive_capability.add_cron_tools`，
  开着就在日志里 WARNING——模型会自己排提醒，和插件的提醒重复发（真机实测收到过两条）。
- `docs/DEPLOYMENT.md`：新增第 7 节「部署前必须关掉的开关」，写明配置路径与 WebUI 位置；
  第 9.1 节写整机重置与备份目录。

## [0.3.0] - 2026-11-12

### 新增（M7：情绪与上下文进对话）

- `core/kinds.py`：记忆类型按关键词推断（`plan` / `preference` / `relation` / `anniversary` / `intimacy` / `fact`），
  只在写入时跑一次正则，不调模型；`WARM_KINDS`（relation / intimacy）供生气档过滤用。
- `core/injector.py`：新增 `<recent_chat>` 块（`build_recent_chat`）——按当前会话取最近几句流水，
  她自己的话标「你：」，其余标昵称/主人；预算按 `recent_chat_max_chars` 从最旧的丢；
  `drop_warm()` 供生气档从检索结果里剔掉关系类记忆。
- `main.py`：`inject_memory` 重排为「未回话补偿 → 心情 → 检索记忆 → 最近对话」；
  心情每轮都贴（`inject_emotion`，含「心情平常」），回避档不注入记忆；
  生气档检索结果里剔除 `relation` / `intimacy`（F18 §5 的调制）。
- 提醒与主动消息共用「今天主动说了几句」的额度（`proactive_day` / `proactive_count`），
  提醒自身不受日限拦截；两者发出后都会记流水（`is_bot=1`）并回写会话历史（F16）。
- `core/emotion.py`：`on_plan_soon()`——三天内有 `plan` 类日程时抬 `expectation`（每天一次）；
  `on_anniversary()`——当天有纪念日时抬 `joy` / `attachment`（每天一次）；
  拍一拍在回避档里计入 `owner_msgs`，连着拍三次会「被拍得心软了」出来。
- `core/reminder.py`：纪念日按每年重复——到点说完把 `due_ts` 与 `schedule.md` 顺延到明年，
  job 不摘（crontab 每年同一时刻再命中）；新增 `on_sent` 回调。
- `core/memory.py`：写入时推断 `kind`（`kind_inference`）；`anniversary` 没写 `due_ts` 的，
  从正文里解析日期（「5 月 20 日是我们的纪念日」）。
- `core/summarizer.py`：汇总的 `kind` 白名单加入 `anniversary`，提示词要求保留日期。
- 配置新增 `inject_emotion`、`recent_chat_messages`、`recent_chat_max_chars`、`kind_inference`。
- 版本号 `0.2.0` → `0.3.0`。
- `tests/smoke_m7.py`：类型推断、最近对话拼装与预算、生气档过滤、心情联动（日程 / 纪念日、
  每天只一次）、拍一拍心软、写入推断、纪念日顺延与 job 保留、配置默认值与钳位的离线冒烟。

### 修复

- `tests/smoke_m6.py`：`check_avoidant()` 改为显式传 `NOW`——原来依赖「测试里的 NOW 约等于当前时间」，
  跑得晚一点会让两小时回避窗口越过 `NOW + 10000`，误判为「还没气消」。
- `core/reminder.py`：`cron_expression` 的分钟、小时补零（`05 09 12 11 *`），否则 crontab 解析失败；
  `poll_due()` 跳过已经挂了 cron job 的条目——纪念日到点后 `due_ts` 推的是明年而不是清空，
  兜底扫描会和 cron 各发一次，还会一路推到后年。
- `core/emotion.py`：`render()` 增加 `always` 参数，日常状态也能回一句「心情平常」；
  `main.inject_memory()` 用 `always=True`，才算真做到「每轮都贴」。
- `core/when.py`：新增「N 分钟后 / N 小时后 / 半小时后」，说「10 分钟后提醒我关火」也能排上，
  不用等到明天。
- `core/embeddings.py`：插件加载早于模型加载，原来把首次「找不到 embedding provider」缓存成
  永久失败，导致向量检索与近似去重全部退回字面匹配；改成 60 秒后重试、告警只打一次。

## [0.2.0] - 2026-11-12

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

### 新增（M5：长期记忆与事实分层）

- `core/remember.py`：三层判定——唤醒层（私聊常驻 / 群里被叫到）→ 句式层（"记住 / 记一下 / 记着 / 帮我记 /
  别忘了 / 提醒我"在句首或前 6 字内，且不是疑问、应答、转述）→ 回执层（回"好，我记下了"）。
  不做形态闸门，也不为"这句是不是要我记"调模型；群聊误判的代价比漏记高，所以句式层偏窄。
- `core/when.py`：中文时间短语 → `due_ts`，支持今天/明天/后天/大后天、下周X、N 天后、M 月 D 日、
  上午/下午/晚上 + N 点、14:30 等；抠不出来返回 None（宁可漏提醒，也不猜错）。
- `core/memoryfiles.py`：`memory/facts.md`（owner 层）、`memory/observed.md`（见闻层）、
  `memory/schedule.md`（提醒排期）三份可读、可手改的 md；行尾标记顺序固定
  〔重要度 N〕〔已被取代 by #id〕〔已归档〕〔#fact_id〕，`parse_line` 可原样读回。
- `core/memory.py`：所有写入的唯一出口。md5 精确去重；同 kind 余弦 >0.92 时显式写入标"已被取代"、
  汇总路径算重复跳过；双写 md + `fact_index`（md 是权威）；`due_ts` 只留给 owner 层；
  后台补打分、30 天未命中且重要度 <60 的归档、删除。
- `core/llm.py`：打分与汇总共用的模型客户端（`llm_provider_id` 留空跟随 AstrBot 当前默认对话模型），
  带超时、失败只告警一次、从回复里抽 JSON。
- `core/summarizer.py`：从 `message` 表取水位之后的增量流水，让模型提炼长期有用的事实；
  `is_bot=1` 只当上下文不参与提炼，`confidence < 0.5` 丢弃，单次最多 10 条，批内去重；
  只有拿到合法 JSON 才前移水位（`meta.last_summary_ts`），模型抽风时下轮重试。
- `core/reminder.py`：到点提醒。AstrBot 4.29.4 的 `add_basic_job()` 没有 `run_at`/`run_once`，
  改用精确到分钟的 5 段 crontab + `persistent=False`（job 只在内存、重启必丢），
  启动时 `restore()` 按 `fact_index.due_ts` 重挂；心跳 tick 里的 `poll_due()` 兜底，
  发前先清 `due_ts`，cron 与兜底同时到点也只发一次。
- 提醒文案落在 `schedule.md`（`提醒你一下：<正文>`，可直接改），不写死在 handler 里。
- `main.py`：唤醒后、防抖前拦截「记住…」——写入、回执、`stop_event()`；owner 的话进 `facts.md` 且可提醒，
  群友的话只进 `observed.md`（`visibility=public`、排序 ×0.6、永不提醒）。
  `platform == "cron"` 的消息在 catch-all 最前面直接挡掉。
- 新指令：`/kahn memory drop <fact_id>`、`/kahn memory due [n]`、`/kahn memory summarize [force]`；
  `facts` 显示层与到期时间，`status` 显示 owner/observed 计数与待发提醒数。
- 配置新增 `remember_enabled`、`score_importance`、`summary_enabled`、`summary_interval_hours`、
  `summary_max_items`、`summary_min_confidence`、`summary_max_chars`、`reminder_enabled`、
  `timezone`、`llm_provider_id`。
- `tests/smoke_m5.py`：时间解析、三层判定、md 往返、写入/去重/取代/归档、汇总（假 LLM）、
  提醒（假 cron：挂/到点/兜底/重启重挂/取消）、配置默认值与裁剪的离线冒烟。

### 新增（M6：主动说话与回避档）

- `core/proactive.py`：主动说话的情绪概率路。闸门顺序固定为
  总开关 → 回避档 → 免打扰（23:00–08:00）→ 冷却（默认 6 小时）→ 日限（默认 2 条）→ 沉默（默认 10 分钟）→
  掷骰；概率 `0.15 + 0.35×attachment + 0.25×social + 0.35×expectation`，封顶 0.85、低于 0.2 不掷。
  发送对象取最近一条流水所在的会话（她在哪说过话就在哪开口）。
- 文案由模型基于当前情绪、时间与最近三条 `owner` 记忆生成一句话（不超过 60 字）；模型不可用时退回模板，
  不会因为外部依赖哑掉。发送成功后回调 `main._remember_proactive_turn()` 把这一轮
  `{"role": "assistant", ...}` 写回会话历史（F16），下一轮模型才知道自己刚说过什么。
- "记忆确定路"（到期日程 / 纪念日）不在这里重复：由 `core/reminder.py` 在准确时刻发一条，
  避免同一天说两遍（写进 IMPLEMENTATION 的偏离说明）。
- `core/emotion.py` 回避档：`blame >= 75` 持续 30 分钟或被吓到（`stress >= 0.8`）进入，私聊最长 1 小时、
  群聊最长 2 小时，到时自动气消；一句道歉 / 哄（`APOLOGY_WORDS`）立刻出来，主人连发 3 条也会心软。
  进入时记录 `avoid_until`，`/kahn status` 能看到"躲到几点"。
- 回避期间 catch-all 照常记录消息，但 `stop_event()` 不回复，把话攒进 `state.json` 的 `unanswered`
  （最多 20 条）；下次开口时拼成 `<unanswered>` 块注入，提示"其实你都记得，只是当时没回"。
  例外（`_must_reply`）：@、拍一拍、"你是不是生气了 / 对不起 / 在吗" 这类话必须回；私聊不豁免。
- `main.py`：心跳维护依次跑 `reminder.poll_due()` → `proactive.maybe_run()` → `summarizer.maybe_run()` →
  `writer.maintain()`，结束后统一 `save_state()`（主动说话会改冷却与日限）。
- 新指令 `/kahn proactive [force]`：看闸门判定与概率；`force` 立刻真发一条（仅管理员）。
- 配置新增 `proactive_enabled`、`proactive_cooldown_hours`、`proactive_daily_limit`、`proactive_silence_minutes`。
- 版本号 `0.1.0` → `0.2.0`。
- `tests/smoke_m6.py`：闸门顺序（沉默 / 骰子 / 免打扰 / 冷却 / 日限 / 回避 / 开关）、发送与模板兜底、
  回写回调、回避档进入与三种退出、未回话攒存与上限、配置默认值与钳位的离线冒烟。

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
