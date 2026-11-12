# 实现计划（实施里程碑）

> 状态：v8.1，2026-11-12；M1~M8 全部落地并跑过离线冒烟，0.3.1 / 0.3.2 / 0.4.1 为真机测试后的修复批次，M8 = 0.4.0。
> 需求侧依据 [`REQUIREMENTS.md`](REQUIREMENTS.md)（F1~F24），
> 设计依据 [`refs/memory-design.md`](refs/memory-design.md)、[`refs/wake-design.md`](refs/wake-design.md)
> 与 [`refs/emotion-design.md`](refs/emotion-design.md)。
> 本文只排顺序与验收，不写实现细节；细节以设计笔记为准。

## 0. 总原则

- **每个里程碑结束时插件都能正常加载、`/kahn` 可用**，不留半截状态。
- 先本地闭环（写入 → 能查），再接 LLM（检索 → 注入 → 汇总）。先跑通再加功能。
- 0.x 阶段不做兼容包袱：表结构变更直接改 schema，`meta` 里记版本号，启动时按版本重建。
- 代码文件按职责拆，`main.py` 只做注册与串联，避免回到脚手架那种"什么都塞入口文件"的形态。
- **工作区洁净**：测试/调试产物只准落在 `.smoke_tmp/` 或系统临时目录；不在仓库根、也不在 `D:\workspace` 根
  留 `data/`、`*.log`、`*.db`、脚本副本。每次真机验证后清掉临时实例目录与日志，收尾自查 `git status`
  和根目录列表。（由来：2026-11-12 M3 调试时，在 `D:\workspace` 根误留了 `data/` 与两个 `_m3_*.log`。）

## 1. 模块划分（拟 → 已落地部分按实际更新，2026-11-12）

```
astrbot_plugin_kahnbot/
├── main.py                 # 注册 handler / 指令 / 生命周期，不写业务
├── core/
│   ├── config.py           # 读配置、默认值、类型收敛 ✅
│   ├── store.py            # SQLite：建表、迁移、WAL、四张表读写 ✅
│   ├── recorder.py         # catch-all 记录 + 噪音过滤 + 昵称缓存 ✅
│   ├── wake.py             # 唤醒判定（at / poke / name）+ 放行 ✅
│   ├── emotion.py          # 九维情绪、五档模式、拍一拍反应 ✅
│   ├── heartbeat.py        # 唯一的钟：tick 调度、补跳、后台维护 ✅
│   ├── state.py            # state.json 原子读写 ✅
│   ├── quota.py            # （未单独建文件）配额落在 store.py + main.py
│   ├── retriever.py        # 向量 + 字面混合检索 ✅
│   ├── embeddings.py       # 复用 AstrBot Embedding Provider ✅
│   ├── injector.py         # on_llm_request 注入 <memory> / <recent_chat> + N 轮去重 ✅
│   ├── kinds.py            # 记忆类型推断（plan / preference / relation / anniversary…）✅
│   ├── when.py             # 中文时间短语 → due_ts ✅
│   ├── remember.py         # 「记住…」三层判定 ✅
│   ├── memory.py           # 记忆写入：去重/取代/打分/归档/删除（含分层）✅
│   ├── memoryfiles.py      # facts.md / observed.md / schedule.md 读写 ✅
│   ├── llm.py              # 打分与汇总用的模型客户端 + JSON 抽取 ✅
│   ├── summarizer.py       # 定时汇总：流水 → LLM 提炼 → 落条目 ✅
│   ├── reminder.py         # cron basic job 挂/摘 + 重启恢复 + 兜底 ✅
│   ├── segments.py         # 分段发送：拆条规则 + 打字间隔 ✅
│   └── proactive.py        # 主动说话：情绪概率路、闸门、回写历史 ✅
├── tests/                  # 离线冒烟（不启动 AstrBot）✅ smoke_m1 ~ smoke_m8
└── _conf_schema.json
```

依赖方向：`main → (recorder, wake, retriever, injector, remember, memory, summarizer, reminder, emotion, heartbeat, proactive) → store / config`。
`store` 不依赖任何业务模块，便于单测。`heartbeat` 是唯一调度者，其它模块不自建定时任务。

## 2. 里程碑

### M1 —— 数据层与记录（可独立验证）✅ 2026-11-11

- 建 `data/plugin_data/astrbot_plugin_kahnbot/`，初始化 SQLite（`message` / `fact_index` / `wake_quota` / `meta`），开 WAL。
- catch-all handler：`@filter.event_message_type(EventMessageType.GROUP_MESSAGE | EventMessageType.PRIVATE_MESSAGE, priority=100)`
  （priority 只能写在**最内层**装饰器上，见 capabilities §14.1），**只写本地库**，不碰 LLM。
- 噪音过滤（F13）：`#sl` 等固定文本直接丢弃，记录与唤醒都不参与。
- 指令：指令组 `/kahn` + 子指令 `/kahn status`、`/kahn memory list [n]`、`/kahn memory forget <id>`；
  单独发 `/kahn` 由 `CommandGroupFilter` 列出子指令树（见 capabilities §14.8）。
- 覆盖：F1、F6（流水部分）、F8a、F11、F13。
- 验收：群里发三条 → 库里三条且带 `sender_id`/`sender_name`/`group_id`；发 `#sl` → 零条；重启进程数据仍在。
- 落地情况：`core/config.py`、`core/store.py`、`core/recorder.py` 已完成；离线冒烟见 `tests/smoke_m1.py`（通过）；
  真机（`D:\workspace\Astrbot`，插件目录用 junction 链到仓库，`astrbot run`）插件加载无报错并建库成功
  （`data/plugin_data/astrbot_plugin_kahnbot/kahnbot.db`，四表 + 三索引 + `meta.schema_version=1` + WAL）。

### M2 —— 唤醒与配额（用户能感知到"她活了"）✅ 2026-11-12

- `wake.py`：三种触发（@/前缀走原生、拍一拍、名字/关键词），命中后置 `event.is_at_or_wake_command = True`。
- 拍一拍补 `message_str`，昵称用 `last_name_of(uid)` 兜底。
- 配额（F12）：`wake_quota(user_id, day, count)`，管理员豁免，超限静默。
- 防抖（F23）：`_settle()` 静默窗口，`debounce_seconds` 可顺延、`debounce_max_seconds` 封顶；指令不排队。
- 陌生人私聊（F24）：不记、不回、`stop_event()`；`owner_ids` 为空时自动不生效（否则会连主人一起挡）。
- 覆盖：F10、F12、F23、F24。
- 落地情况：`core/wake.py` 新增，`main.py` 的 catch-all 扩成"记录 → 唤醒判定 → 配额 → 防抖 → 放行"；
  离线冒烟 `tests/smoke_m2.py`（唤醒判定/指令识别/占位符/配额/防抖与上限）通过；
  真机 `astrbot run` 加载无报错，日志确认名字表与 `owner_ids` 未配置的告警。
- 验收：`DEPLOYMENT.md` 验收表第 2~7 条全过（待群里实测）。

### M3 —— 情绪层（纯离线，不花一分钱）✅ 2026-11-12

- `emotion.py`：九维驱动与 `base`/`rate` 常量表、`mode_of()` 五档；`blame` 只由主人推动（F20）。
- `heartbeat.py`：`asyncio.create_task` + `asyncio.sleep(30)` 轮询；基础 45 分钟、活跃期 15 分钟；
  补跳闭式解 `v = base + (v-base)*(1-rate)**n`（n 封顶 96）；tick 四步（漂移 → 记忆维护 →
  联动 → 主动判定占位）。
- `state.json` 持久化，`terminate` 时落盘。
- 指令 `/kahn mood`（F22）。
- 覆盖：F17、F18、F20、F22（F19 的联动点留接口）。
- 落地情况：`core/emotion.py`、`core/heartbeat.py`、`core/state.py` 新增；`main.py` 在 catch-all
  里把主人消息翻译成情绪事件（夸奖/责骂/道歉），拍一拍单独加分；心跳在没有事件循环时延迟到
  第一条消息启动；`state.json` 在加载、tick、情绪事件、卸载四个时机落盘。
  离线冒烟 `tests/smoke_m3.py`（漂移收敛、闭式解与逐次等价、五档边界、主人事件、拍一拍、
  渲染、状态补全、心跳补跳封顶）通过；真机 `astrbot run` 加载无报错，`state.json` 已生成。
- 验收：手工把 `blame` 设到 50 → `/kahn mood` 显示生气档；重启后数值保留；
  模拟关机 6 小时再启动，一次 tick 就补到正确量级（不循环 72 次）。

### M4 —— 检索与注入（接 LLM）✅ 2026-11-12

- `embeddings.py`：`context.get_provider_by_id()` / `get_all_embedding_providers()` → `get_embedding()`；
  `embedding_provider_id` 留空自动取第一个启用的 embedding provider；无 provider 或调用失败时
  静默降级为纯字面（只告警一次，不刷日志）。向量用 float32 存 BLOB（1024 维 ≈ 4KB）。
- `retriever.py`：混合排序 `(0.6*余弦 + 0.4*字面) × (0.7+0.3*重要度/100) × 时间衰减 × 0.6(observed)`；
  中文 bigram + 英文数字分词；过滤 `archived`、`superseded_by`，群聊上下文只查 `visibility='public'`；
  命中即回写 `last_hit_ts`（F7 的"被提起"）。
- `injector.py`：`<memory>` 块拼装 + 预算截断 + N 轮内去重（F15）；没有新命中就整段不注入。
- 触发：`on_llm_request(priority=-1000)` 自动注入（M4 采用"自动注入 + 去重"；LLM 工具
  `recall_memory` 留到确有必要再加）；`/kahn recall <问句>` 是手动验证入口。
- 注入位置：`req.extra_user_content_parts` 追加 `TextPart(...).mark_as_temp()`，**不写 system_prompt**
  （F14，见 refs/astrbot-capabilities.md 第 15 节）；拿不到 `TextPart` 时才退回 system_prompt。
- 写入（本里程碑只做最小实现，供检索验证）：`/kahn memory add <内容> [重要度 N]` → md + 索引 + 向量；
  「记住…」自动识别、LLM 打分、汇总提炼留到 M5。正文参数用 `GreedyStr` 整段接收
  （AstrBot 按空格切位置参数，普通 `str` 会把带空格的正文切碎），"重要度 N"从末尾剥离，
  见 `core/args.py::split_importance`。
- 指令不进流水：catch-all 原本连 `/kahn …` 也记，`/` 前缀又已被 `waking_check` 剥掉，
  看起来像"主人说了这句话"；现在按 `activated_handlers` 里的 filter 类型识别指令，
  记录与情绪推进都跳过（`core/recorder.py`、`core/wake.py::is_command_event`）。
- 覆盖：F5、F9、F14、F15。
- 落地情况：`core/embeddings.py`、`core/retriever.py`、`core/injector.py` 新增；`Store` 的
  `fact_index` 加 `kind/importance/superseded_by/due_ts/embedding` 五列；离线冒烟
  `tests/smoke_m4.py`（分词/字面分/存储往返/过滤/排序/无向量降级/注入去重与预算）通过。
- 验收：`/kahn recall 下周三` 能列出相关条目与分数；对话里问一个只在记忆里的事，回答里体现；
  群聊里问同一件私聊的事，回答里**不出现**该内容（F9）；连续两轮都命中的条目第二轮不重复注入。

### M5 —— 长期记忆与事实分层 ✅ 2026-11-12

设计见 [`refs/memory-design.md`](refs/memory-design.md)（2026-11-12 定稿）。

- 写入（F4）：三层判定——唤醒层（私聊常驻 / 群里被叫到才进）→ 句式层（"记住"在句首或前 6 字，
  且不是疑问、应答、转述）→ 回执层（回一句"好，我记下了"）。**不做形态闸门**，也不用 LLM 判断
  "是不是要我记"；"别忘了 / 提醒我"同样算记住，并解析出 `due_ts`。
- 重要度（F7）：写入时让 LLM 给 0–100 打分（拿不到默认 50），只影响排序；
  `30 天未命中且重要度 < 60` 才归档，`≥60` 不归档但仍参与时间衰减。取消原来的 `pinned` 布尔位。
- 已被取代（§8.3）：同 `kind` 且余弦 >0.92 时，旧条目标 `〔已被取代 by #新id〕`，默认不再检索。
- 分层（F21）：`owner` → `facts.md`（可触发提醒）；`observed` → `observed.md`
  （注入标"未必属实"、排序 ×0.6、永不提醒）；`raw` 只留流水。
- 汇总：定时从 `message` 表取增量 → LLM 输出 `{"text","kind","confidence"}` → md5 精确去重 +
  余弦 >0.92 近似去重 → 单次上限 10 条、`confidence < 0.5` 丢弃。**不设内容边界**
  （单实例、私聊 + 4~5 人小群，群里的梗被提炼成 `observed` 就提炼）。
- 提醒（F16 的确定路）：落 `due_ts` + `schedule.md` → `context.cron_manager.add_basic_job()` 挂自己的
  handler → 到点发一句并清 `due_ts`。坑：basic job 的 handler 只在内存，**重启后核心会跳过它**
  （`Skip scheduling basic cron job … due to missing handler`），启动时要按 `fact_index.due_ts` 重新挂；
  `platform == "cron"` 的 `CronMessageEvent` 必须挡在 catch-all 之外。
- 覆盖：F3、F4、F7、F16（确定路）、F21。
- 落地情况：新增 `core/when.py`（中文时间短语 → `due_ts`）、`core/remember.py`（三层判定）、
  `core/memoryfiles.py`（`facts.md` / `observed.md` / `schedule.md` 三份 md 的读写与往返）、
  `core/llm.py`（打分/汇总用的模型客户端 + JSON 抽取）、`core/memory.py`（`MemoryWriter`：
  双写、md5 去重、>0.92 取代、补打分、归档、删除）、`core/summarizer.py`（增量汇总 + 水位）、
  `core/reminder.py`（挂 cron、重启重挂、心跳兜底）；`Store.fact_index` 加 `umo` 列与
  `messages_since / facts_with_due / due_facts / archivable` 等方法；`main.py` 在唤醒后、
  防抖前拦截「记住…」（回执走 `event.send()` + `stop_event()`），指令新增
  `memory drop|due|summarize`。离线冒烟 `tests/smoke_m5.py` 通过。
- **与设计的偏离（AstrBot 4.29.4 实测）**：`add_basic_job()` 没有 `run_at` / `run_once` 参数，
  只能填 5 段 crontab。一次性提醒改为「精确到分钟的 crontab + `persistent=False`」——
  job 只活在内存、重启必丢，正好由启动时的 `restore()` 按 `due_ts` 重挂来恢复；
  另加心跳 tick 里的 `poll_due()` 兜底（cron 不可用或错过触发也能发出去，发前先清 `due_ts` 保证只发一次）。
- **与设计的偏离（成本）**：定时汇总挂在心跳 tick 上（`on_tick` 里起任务，禁止并发），
  没另开线程或后台队列；水位存 `meta.last_summary_ts`，重启后第一次只把水位设到当下、
  不回头啃历史（避免一次灌入大量旧事）。
- 验收：群里有人说"明天是管理员的生日" → 只进 `observed.md`，次日**不会**触发提醒；
  主人说"下周三体检" → 进 `facts.md`，到点能提醒；重启后提醒仍然有效。

### M6 —— 主动说话与收尾 ✅ 2026-11-12

- `core/proactive.py`：情绪概率路。闸门顺序固定为
  总开关 → 回避档 → 免打扰（23:00–08:00）→ 冷却（`proactive_cooldown_hours`，默认 6 小时）→
  日限（`proactive_daily_limit`，默认 2 条）→ 沉默（默认 10 分钟）→ 掷骰。
  概率 `0.15 + 0.35×attachment + 0.25×social + 0.35×expectation`，封顶 0.85，低于 0.2 连骰子都不掷。
  说话对象取最近一条流水所在的会话；文案由 LLM 基于情绪 + 时间 + 最近三条 owner 记忆生成，
  模型不可用退回模板。
- 回写历史（F16）：发送成功后回调 `main._remember_proactive_turn()`，把
  `{"role": "assistant", "content": text}` 追加进当前会话（`get_curr_conversation_id` →
  `get_conversation` → `update_conversation`），异常只记日志。**这一步不能省**，否则她"说完就忘"。
- 回避档：`blame >= 75` 持续 30 分钟或 `stress >= 0.8` 进入；私聊上限 1 小时、群聊 2 小时，
  到时自动气消；道歉/哄立刻出来，主人连发 3 条也会心软。回避期间 catch-all 照常记录但
  `stop_event()`，把话攒进 `state.json` 的 `unanswered`（最多 20 条），下次开口拼成 `<unanswered>`
  块注入（"其实你都记得，只是当时没回"）。例外 `_must_reply`：@、
  "你是不是生气了 / 对不起 / 在吗" 必须回（M7 起拍一拍不再算例外，改由"拍三次会心软"处理）；私聊不豁免（Q14）。
- 落地情况：新增 `core/proactive.py`、`tests/smoke_m6.py`；`core/emotion.py` 加回避档状态与
  `check_avoidant / push_unanswered / take_unanswered / note_replied`；`main.py` 接线
  （catch-all 的回避拦截、注入 `<unanswered>`、心跳维护顺序 `poll_due → proactive → summarizer → maintain`
  并统一落盘）、新指令 `/kahn proactive [force]`、`status` 增加主动说话与回避状态。
  配置新增 `proactive_enabled`、`proactive_cooldown_hours`、`proactive_daily_limit`、
  `proactive_silence_minutes`；版本号升到 `0.2.0`。
- **与设计的偏离（去重）**：设计里"记忆确定路"指到期日程/纪念日也走 `proactive.py`；
  实现时把它留在 M5 的 `core/reminder.py`（到点发一句、带 `schedule.md` 文案），
  `proactive.py` 只做情绪概率路，避免同一件事一天说两遍。
- 验收：沉默 10 分钟以上、冷却与日限未满时，她会在最近说话的会话里主动发一句；
  `send_message` 失败不消耗冷却/日限；回避期间被 @ 仍会回，普通消息攒着；
  道歉或连发 3 条后恢复说话，攒下的话在下一轮注入里可见。

### M7 —— 情绪与上下文进对话 ✅ 2026-11-12

一句话：把已经算出来的东西真的用上——心情、最近聊天、生气档的调制、提醒的账本。

- 注入顺序（`main.inject_memory`）：未回话补偿 → `<emotion>`（`inject_emotion`，含"心情平常"）→
  `<memory>`（`inject_enabled`，回避档跳过）→ `<recent_chat>`（`recent_chat_messages`，回避档跳过）。
  每块都走 `extra_user_content_parts` + `mark_as_temp()`，失败才退回 `system_prompt`。
- `<recent_chat>`（F11/F14）：按 `umo` 取最近 N 条流水（含 `is_bot=1`），她自己的话标「你：」，
  其余标主人/昵称；按 `recent_chat_max_chars` 从最旧的丢。**不跨会话**：私聊里看不到群聊。
- 生气档调制（emotion-design §5）：检索命中里剔除 `WARM_KINDS`（relation / intimacy），
  不让她气头上还念着"你最喜欢我"。
- 提醒也是主动消息（F16）：`ReminderService` 增加 `on_sent` 回调，`main` 里
  ① 和 proactive 共用「今天主动说了几句」的额度（`proactive_day` / `proactive_count`），
  提醒自身不被日限拦；② 记流水（`is_bot=1`、`sender_id="self"`）；③ 回写 assistant 轮。
  和主动消息一样：发出去了就算"她说过"，下一个汇总或下一轮对话都看得见。
- 记忆类型 `core/kinds.py`：关键词正则推断 `plan / preference / relation / anniversary / intimacy / fact`，
  写入时跑一次（`kind_inference`），不调模型。选项里放弃让 LLM 判类型，理由是每条消息都调模型太贵、也不稳。
- `anniversary`：进 `summarizer` 的 kind 白名单；没写 `due_ts` 的从正文解析日期（`parse_due`）；
  到点说完把 `due_ts` 与 `schedule.md` 顺延到明年、job 不摘（crontab 每年同一时刻再命中），
  2 月 29 日顺延落到 2 月 28 日。
- 心情联动（F19 六个联动点补齐）：
  - 三天内有 `plan` 类日程 → `emotion.on_plan_soon()`，`expectation +0.20`（每天一次）；
  - 当天有 `anniversary` → `emotion.on_anniversary()`，`joy +0.25`、`attachment +0.10`（每天一次）；
  - 拍一拍在回避档里计入 `owner_msgs`，连着拍三次 → "被拍得心软了"出来。
  前两项挂在心跳维护的 `_apply_schedule_hooks()` 里，查的是 `fact_index.due_ts`，不额外建表。
- 落地情况：新增 `core/kinds.py`、`tests/smoke_m7.py`；`core/injector.py` 加 `<recent_chat>` 与
  `drop_warm`；`core/store.py` 加 `recent_messages()`；`core/emotion.py` 加
  `plan_soon_day / anniversary_day` 与三个钩子；`core/reminder.py` 加 `on_sent` 与纪念日顺延；
  `core/memory.py` 加类型推断与 anniversary 日期解析；配置新增 `inject_emotion`、
  `recent_chat_messages`、`recent_chat_max_chars`、`kind_inference`；版本号升到 `0.3.0`。
- 验收：群里问"今天心情咋样"她的语气和 `/kahn mood` 对得上；刚聊过的话题下一轮她还接得住；
  生气档里检索结果不含关系类记忆，也不把私聊细节带进群（按人判定，见 REQUIREMENTS §5）；
  提醒发出后 `/kahn memory list` 能看到 `[bot]` 那一行，下一轮对话她知道"我刚说过"；
  生日/纪念日到点说一句，明年同一天还会再说。

### 0.3.1 —— 真机测试后的修复批次 ✅ 2026-11-12

不是新里程碑，只记这次真机跑出来的问题与修法（详细条目见 `CHANGELOG.md`）：

- **提醒发完就崩**：`_count_proactive_send()` 调了类里不存在的 `self._today()`，导致计数、流水、
  回写历史一起抛 `AttributeError`——提醒发出去了，但下一轮她不记得。改用模块级 `_today()`，
  并把两个收尾回调各包 `try/except`：计数失败不能拖累回写。
- **她自己的话没进流水**：最近对话的「你：」永远是空的。`_send_text()` 成功后补记 `is_bot=1`，
  记忆回执也记一条。
- **客气问句不认**："能记住…吗" 被当成疑问句丢掉。判定加"请求框架"（能 / 能不能 / 帮我 / 麻烦你），
  有框架的疑问句仍算委托，正文剥掉"吗 / 好吗 / 行吗"。
- **整机重置**：`core/reset.py` + `/kahn reset [confirm]`，清数据不动配置；`reset_backup_dir`
  非空时先做 sqlite 在线备份（开发测试用，正式部署留空）。重置顺便给每个会话在 AstrBot 侧
  开新对话并清掉注入去重，模型侧也不再有旧上下文。
- **双套定时**：AstrBot 的 `provider_settings.proactive_capability.add_cron_tools`（`future_task` 工具）
  会让模型自己再排一遍提醒。部署文档第 7 节写明要关；插件启动时检测到开着就打 WARNING。

### 0.3.2 —— 提醒的措辞与额度 ✅ 2026-11-12

- **提醒不占日限**：`_after_reminder_sent` 不再调计数，只回写历史。`proactive_daily_limit`
  重新只约束她主动搭话；到点的正事不该挤掉平时的额度（用户 m01954）。
  代价是理论上一天可以既提醒多次、又主动搭话 2 次——提醒数量由事实本身决定，不算骚扰源头。
- **写入时剥指令语与时间**：`core/remember.py::clean_body` 剥句首的触发词（记住/提醒我/别忘了）
  与相对时间（今天/明天/下周X/N 分钟后/时段/钟点），"10分钟后提醒我关火"存成"关火"。
  **不剥绝对日期**——"5月20日是我们的纪念日"里日期就是内容，剥了就废了。
  时间信息全部由 `due_ts` 承担，事实正文只留"要记的那件事"。
- **到点说什么由模型写**：`core/llm.py::assess` 把"重要度 + reminder 一句话"合并进同一次调用
  （不增加请求数），`MemoryWriter.rescore` 返回文案，`main._finalize_reminder` 先拿文案再
  `schedule_fact(fact_id, phrase=...)`——顺序反过来就会把旧模板写进 `schedule.md`。
  模型不给/超时退回 `compose_phrase`（「提醒主人一下：关火」）。
  到点仍然不经过模型（读 `schedule.md` 直接发），保持 basic job 的零成本。

### M8 —— 分段发送（0.4.0）✅ 2026-11-12

**目标**：不再一长段砸过去，而是像人一样一条条发；条数与节奏由插件决定，不依赖 AstrBot 原生分段。

- **为什么不用原生**：4.29.4 的 `ResultDecorateStage` 只按标点机械切、没有条数上限、间隔固定
  1.5~3.5 秒，而且是全局开关，对所有插件的回复生效。原生分割发生在 `on_decorating_result`
  之后、发送之前——所以钩子里自己 `clear_result()` 再发，就能完全接管。
- **拆条规则**（`core/segments.py::split_bubbles`）：总长 < 8 字不拆（"嗯""知道啦"）；
  > 300 字或含代码块不拆；换行是模型自己选的断点，按段保留；同一行按 `。！？!?~…；` 切，
  短于 4 字的句子并到下一句；超过 `segmented_max_bubbles` 的并进最后一条，不丢字。
- **节奏**：`bubble_delay = clamp(0.7 + 0.09 × 字数, 0.7, 3.5) + jitter(0~0.4)` 秒，
  对齐参考项目 dsh-aurabot-qq 的"约每秒 9 字"。
- **心情调制**：`_bubble_limit()` 在 angry / avoidant 时返回 1——生气时话短，一条说完。
- **正在输入**：`_show_typing` 只在私聊调 OneBot `set_input_status`（`event_type=1`）；
  协议端不支持时只记一条 debug 并永久跳过。群聊没有这个能力。
- **安全边界**：只处理 `platform == "aiocqhttp"` 且 `event.get_extra("provider_request")` 存在的
  结果——指令结果是控制流，不拆；带图片/语音/引用组件的回复不拆。引用与 at 设置（
  `platform_settings.reply_with_quote` / `reply_with_mention`）手动挂到第一条上。
- **历史不受影响**：assistant 轮由 agent 层在装饰钩子之前写好，拆条不改变历史内容，
  下一轮看到的仍是完整的一句。
- 配置：`segmented_reply`、`segmented_max_bubbles`（1~5）、`segmented_typing`。
  部署侧必须关掉原生 `platform_settings.segmented_reply.enable`，见 `DEPLOYMENT.md` 第 7 节。
- 验收：`tests/smoke_m8.py` 通过；真机上一条 3 句的回复变成 2~3 条，间隔随长度变化，
  私聊能看到"正在输入"，群里没有。

### 0.4.1 —— 启动期的向量就绪 ✅ 2026-11-12

**问题**：真机启动时日志会出现「暂无可用的 embedding provider」。查 core_lifecycle.py 确认
这是必然的——AstrBot 先 plugin_manager.reload()（插件在这里 __init__），后
provider_manager.initialize()（模型这时才实例化），插件加载时 provider 列表一定是空的。

- 原来的处理是"失败后冷却 60 秒、下次调用再试"，但启动补向量只在启动那一刻跑一次，必然落空：
  启动那几分钟写入的记忆没有向量，近似去重与向量检索要等下次重启才补得回来。
- 现在 EmbeddingClient.wait_ready(attempts=12, interval=10) 在后台等 provider 出现（最多约两分钟），
  main._warm_embeddings() 拿到后再补 200 条历史记忆；_backfill_embeddings() 每次维护补 20 条，
  换 embedding provider 之后也会慢慢补齐。
- 告警文案改成说明"插件先加载、模型后初始化"；/kahn status 在没就绪时显示"未就绪（启动后自动重试）"。
- 验收：	ests/smoke_m8.py::test_embed_wait_ready 通过（provider 晚到也能等到）；真机启动后
  一分钟内应看到「向量就绪：openai_embedding（1024 维）」。

## 3. 依赖与顺序

```
M1 ──► M2 ──► M4 ──► M5 ──► M6
 └────► M3 ──┘
```

M3（情绪）只依赖 M1 的 `state.json`，**可以并行先做**；M4 依赖 M3 的档位输出做注入调制；
M5 依赖 M4 的检索命中回写 `last_hit_ts`；M6 依赖 F21 的分层，否则会把谣言当提醒发出去。
若想零成本看到效果，先做 M1+M2+M3（全部离线，不调 LLM）。

## 4. 每阶段的固定动作

- 改完先 `git diff` 自查，确认没有把调试代码/临时打印带进去。
- 在 AstrBot 里重载插件验证加载无报错（`astrbot run --reload` 期间）。
- 更新 `REQUIREMENTS.md` 的状态列（F 编号 → 已实现）与 CHANGELOG `[Unreleased]`。
- 不做"顺手重构"：与本阶段无关的改动一律不进这个提交。

## 5. 已知风险（按出现顺序）

| 风险 | 影响 | 缓解 |
|---|---|---|
| `data/plugin_data/` 与插件目录名绑定 | 改名即丢数据 | 目录名已定死为 `astrbot_plugin_kahnbot` |
| 定时汇总调用 LLM 产生费用 | 成本不可控 | 只汇总增量、可配置间隔、可关闭 |
| Embedding 维度变化 | 旧向量失效 | `meta` 记维度与模型名，不匹配则重建索引 |
| 拍一拍拿不到昵称 | 文案显示 QQ 号 | 流水表昵称缓存兜底 |
| 唤醒后 `event.send()` 早于放行 | 回复被跳过 | 唤醒路径上禁止提前 send（wake-design §3.1） |
| 主动消息不回写历史 | 她"说完就忘"，且其它插件看不到 | `send_message` 成功后必须 `update_conversation`（F16 验收项） |
| 群友转述被当成事实 | 谣言变提醒 | 事实三层（F21），提醒只读 `owner` 层 |
| 心跳任务与重载并发 | 重载后两个 tick 同时写库 | `terminate` 里 `task.cancel()` + `await`；心跳只写 `state.json`（原子替换），不碰 SQLite |
| SQLite 并发写 | `database is locked` | WAL + 单连接串行写 + 超时重试 |

## 6. 待补

- [x] 汇总用什么模型：`llm_provider_id` 留空跟随 AstrBot 当前默认对话模型，可单独指定；
  打分与汇总共用同一个客户端（`core/llm.py`）。
- [x] `/kahn` 指令的最终形态：指令组 + `status` / `memory list|facts|add|drop|due|summarize|forget` / `mood [why]` 已落地。
- [x] `/kahn memory forget` 是否限管理员：限主人或 AstrBot 管理员。
- [x] "记住…"识别方式（Q8）：三层判定，形态闸门本轮不做（见 M5）。
- [ ] 情绪参数（base/rate/阈值）初值，M3 跑一周后校准。
- [ ] 是否需要 WebUI 配置页（AstrBot 插件配置页自动由 schema 生成，够用则不做；`/kahn mood` 已覆盖查看需求）。
- [x] `astrbot_version` 下限：已抬到 `>=4.29.4`。
- [ ] md 与索引的一致性校验（`fact_index` 可重建，chores 里还没有校验命令）。
- [ ] `/kahn memory expose <id>`（把 private 记忆放给群聊检索，memory-design §9）。
## 7. 当前状态与交接（2026-11-12）

给"换一台机器接着写"用的最小上下文。

- **做到哪了**：M1 数据层、M2 唤醒与配额、M3 情绪层、M4 检索与注入、M5 长期记忆与事实分层、
  M6 主动说话与回避档、M7 情绪与上下文进对话、M8 分段发送，全部落地并跑过 `smoke_m1~m8`；版本 `0.4.1`
  （0.3.1 修真机崩溃、加整机重置；0.3.2 提醒不占日限、写入剥指令语、文案交给模型；0.4.0 分段发送；
  0.4.1 启动期等 embedding provider 就绪、卸载时等后台任务退出）。动手前先
  `git log --oneline` 确认要不要 push。
- **下一步**：已有明确方向的四件事在 §8 待讨论方案里（检索性能、`[沉默]`、人物小档案、表情包、空间日记）；
  其余可选：真机跑一周校准情绪参数（`BASE` / `RATES` / 阈值 / 主动说话概率 / `_chance` 权重）；
  补 `md ↔ fact_index` 一致性校验命令；`/kahn memory expose <id>`（memory-design §9）。
- **跑测试**（不需要启动 AstrBot，用 AstrBot 自带的 Python）：

  ```powershell
  & "$env:APPDATA\uv\tools\astrbot\Scripts\python.exe" tests\smoke_m1.py
  & "$env:APPDATA\uv\tools\astrbot\Scripts\python.exe" tests\smoke_m2.py
  & "$env:APPDATA\uv\tools\astrbot\Scripts\python.exe" tests\smoke_m3.py
  & "$env:APPDATA\uv\tools\astrbot\Scripts\python.exe" tests\smoke_m4.py
  & "$env:APPDATA\uv\tools\astrbot\Scripts\python.exe" tests\smoke_m5.py
  & "$env:APPDATA\uv\tools\astrbot\Scripts\python.exe" tests\smoke_m6.py
  & "$env:APPDATA\uv\tools\astrbot\Scripts\python.exe" tests\smoke_m7.py
  & "$env:APPDATA\uv\tools\astrbot\Scripts\python.exe" tests\smoke_m8.py
  ```

  期望最后一行都是 `ALL OK`。临时目录 `.smoke_tmp/` 已 gitignore。
- **真机验证**：插件目录用 junction 链到 `AstrBot\data\plugins\astrbot_plugin_kahnbot`，
  改代码后**必须重启** `astrbot run`（不带 `--reload` 不会热更）。
  部署前先看 `DEPLOYMENT.md` 第 7 节：`add_cron_tools` 必须关，否则提醒发两遍。
  要重来一遍：`/kahn reset confirm`（配置 `reset_backup_dir` 非空时先备份）。
- **交接时的 git 状态**（2026-11-12）：`origin/main` 在 `0c37934`，本地领先 3 个提交
  （`b032e26` 卸载等后台任务、`072e9d6` 0.4.1 文档、`83deea6` 待讨论方案），加上本次文档整理尚未提交；
  要不要 push 问主人，别自作主张。本机没配 git 身份时用本地临时身份提交即可
  （`git -c user.name=<本地名> -c user.email=<本地邮箱> commit`），不要把真实邮箱写进仓库。
- **数据在哪**：`AstrBot\data\plugin_data\astrbot_plugin_kahnbot\` 下有 `kahnbot.db`（流水 + 记忆索引 + 配额）、
  `state.json`（情绪）、`memory\facts.md`（owner 层正文）、`memory\observed.md`（见闻层）、
  `memory\schedule.md`（提醒排期）。删库即回到零状态。
- **最容易踩的十三件事**：① 指令的 `/` 前缀在 `waking_check` 就被剥掉了，别靠字符串猜指令；
  ② 指令正文要用 `GreedyStr`，否则带空格的内容被切碎；③ 注入走 `extra_user_content_parts` +
  `mark_as_temp()`，写 system_prompt 会毁掉前缀缓存；④ 跑 `astrbot run` 时把输出重定向到
  `$env:TEMP\astrbot-run.log`，不要写 `D:\workspace` 根（已经踩过两次，见前文"工作区洁净"）；
  ⑤ `add_basic_job()` 没有 `run_at`，一次性提醒只能用 5 段 crontab + `persistent=False`，
  重启必须靠 `ReminderService.restore()` 重挂；⑥ 「记住…」的回执走 `event.send()` 后**必须** `stop_event()`，
  否则同一句会再进默认 LLM 流程，等于回两遍；⑦ 主动消息/提醒发出去后**必须**回写会话历史
  （`update_conversation`）并记 `is_bot=1` 流水，否则下一轮她自己都不知道刚说过；
  ⑧ 回避档拦截要放在 `wake.detect()` 之后、配额/防抖之前，否则 @ 和拍一拍也会被吞掉；
  ⑨ 纪念日到点后 `due_ts` 推到明年、**job 不摘**——摘了就只响这一次，明年不会再说；
  ⑩ `<emotion>` / `<memory>` / `<recent_chat>` 三块都是临时内容，必须 `mark_as_temp()`，
  否则会把它们写进会话历史、越滚越长；⑪ 提醒**不占**主动额度（0.3.2 起），
  `proactive_daily_limit` 只拦主动搭话，别把提醒也算进去；
  ⑫ 提醒文案必须**先写 `schedule.md` 再挂 job**（`_finalize_reminder` 的顺序），反了就会把旧模板念出去；
  ⑬ 分段发送必须在 `on_decorating_result` 里 `clear_result()` 后自己发，并且**关掉原生**
  `platform_settings.segmented_reply.enable`——两边同时开会被切两遍；指令结果没有
  `provider_request`，正好可以靠它区分"模型回复"和"控制流"。
- **文档看哪本**：需求与状态 → `REQUIREMENTS.md`；里程碑 → 本文件；记忆设计 → `refs/memory-design.md`；
  唤醒 → `refs/wake-design.md`；情绪 → `refs/emotion-design.md`；部署与模型 → `DEPLOYMENT.md`；
  AstrBot 内部机制核对 → `refs/astrbot-capabilities.md`。

## 8. 待讨论方案（草案，未排期）

"想到了、但还没定要不要做 / 怎么做"的东西放这里，落地后把条目挪进上面的里程碑。
状态：`草案` → `待拍板` → `实施中` → `已落地` / `搁置`。

| # | 事项 | 状态 | 阻塞点 |
|---|---|---|---|
| P1 | 检索性能优化（numpy 矩阵 / 字面预过滤） | 草案 | 选哪种方案 |
| P2 | `[沉默]`（她可以选择不回） | 待拍板（规则已验） | 兜底与配置细节 |
| P3 | 人物小档案（跨群按人） | 草案 | 存储形态、更新时机 |
| P4 | 表情包（只用 QQ 收藏夹，禁 emoji） | 草案 | 触发策略 |
| P5 | 空间日记（SnowLuma 已支持 QZone） | 草案 | 发布频率与内容边界 |

### P1 检索性能优化

**问题**：`Retriever.search` 每次把全表向量从 BLOB 解成 Python list 再逐条算余弦。
1024 维下每条向量展开成 Python float 要 44 KB（float32 只有 4 KB）。

本机实测（1024 维、真实检索代码）：

| 记忆条数 | 单次检索 | 瞬时内存 |
|---|---|---|
| 1,000 | 107 ms | +44 MB |
| 5,000 | 545 ms | +220 MB |
| 10,000 | 1,100 ms | +440 MB |

拆解（1 万条）：读库 0.11 s、解包 0.34 s、算余弦 0.98 s。

**两种改法**

- **numpy 矩阵缓存**：启动时把全表向量读成 `float32` 矩阵（N×1024）常驻，每次检索一次
  `矩阵 @ 查询向量`，再对 top-N 算字面分；新增/删除记忆时增量改一行。
  内存按 4 KB/条常驻，构建时瞬时翻倍。
- **字面预过滤**：先用分词命中数给全表打一遍字面分（不碰向量），取前 200 条再解包算余弦。
  几乎不吃内存、不用维护缓存，但每次仍要线性扫全表文本（1 万条约 50 ms）。

实测对照（1 万条）：

| 方案 | 耗时 | 常驻内存 |
|---|---|---|
| 现状 | 1,100 ms | 440 MB 峰值 |
| numpy 矩阵 | ~2.3 ms | 39 MB |
| 字面预过滤 | ~55 ms | ~0 |

**内存预算（部署机为 8 GB、常态占用约 60%，即只剩 3 GB 左右）**

| 记忆条数 | 现状峰值 | numpy 常驻 | numpy 构建瞬时 |
|---|---|---|---|
| 2,000 | 88 MB | 8 MB | ~16 MB |
| 5,000 | 220 MB | 20 MB | ~40 MB |
| 10,000 | 440 MB | 40 MB | ~80 MB |
| 20,000 | 880 MB | 80 MB | ~160 MB |
| 50,000 | 2.2 GB | 200 MB | ~400 MB |

现状在 7 千条左右就会把 3 GB 空闲吃光；numpy 方案到 5 万条也只占 200 MB（常驻），
对 8 GB 机器是安全的。若还想再省，可以存 `float16`（2 KB/条，检索前转 float32），
代价是余弦精度略降（对 top-k 排序基本无影响）。

**建议**：numpy 矩阵为主（AstrBot 环境自带 numpy，不算新增依赖），缺失时退回字面预过滤；
同时给"参与检索的记忆条数"一个硬上限（默认 2 万，超出的先按重要度 + 时间归档）。
两者共用同一个打分函数，只是候选集来源不同。

**磁盘**：记忆 4.54 KB/条（1 万条 45 MB），机械硬盘随机读约 10 ms/次；
启动时读 40 MB 矩阵约 1–2 s，可以接受；流水 0.29 KB/条，10 万条 29 MB。

**待定**：是否值得为 2 万条以上做分库/分表（目前看不需要）。

### P2 `[沉默]`

**动机**：真实聊天里不是每句都要回。对方只是"嗯""好""晚安"收尾时，再接一句反而假。

**不要用"想不想"这种措辞**——模型没有意愿，只有判断规则。实测（deepseek-v4.1-flash，
16 个场景各一次调用）：规则写成"谁先开口 + 这句是不是收尾"后 14/16 符合预期；
写成"不想接就不回"时，连"主人先道晚安"都会沉默（把"晚安"本身当成收尾）。

**建议的两层**（用户已同意）

1. **程序侧硬规则**（先跑，不花 token）：她说了最后一句 且 当前消息是收尾词
   （嗯/好/好的/晚安/知道了/行/OK/睡了…，长度 ≤6）→ 直接沉默，不进模型。
2. **提示词层**：只在硬规则判断不了时由模型决定。提示里写：

   > 判断要不要回，只看谁先开口、这句是不是在收尾：
   > - 上一句是你说的，对方只是在回应你（你道晚安他回晚安、你叮嘱完他回"嗯/好/知道了"）→ 不回；
   > - 对方先开口（哪怕只是"晚安"）、在问你、开新话题、撒娇、夸你 → 回。

**兜底**：模型输出的 `[沉默]` 只用来处理边缘情况；连续沉默上限默认 1，超了强制回一句；
沉默不计入唤醒配额但要记流水，并塞进 `unanswered`，下次开口自然接上。

**待定**：收尾词表范围（"哦""嗯嗯""行吧"算不算）；私聊是否允许沉默（默认允许）。

### P3 人物小档案（跨群按人）

**动机**：现在记忆是"一条条事实"，缺"对某个人的整体印象"；注入时也更容易写成一句人话。

**用户补充（2026-11-12）**：有些群友同时在两个群，档案要**跨群按人合并**；
除主人外不允许私聊（`block_stranger_private`），所以群友档案只可能来自群聊。

**方案**

1. 身份键只用 uid（不按群）；新表 `profile(person_id, name, relation, note, style, updated_ts, dirty_ts)`，
   不动 `fact_index`。
2. `note` ≤120 字、第三人称（"他是主人的同事，最近在准备考试，喜欢喝冰美式"）；
   多个群里的发言合并成同一份档案，来源群记在 `relation` 或单独的 `sources` 里。
3. 生成：挂在 6 小时汇总里，只处理"脏了的人"（新流水 >N 条或超过 3 天），
   输入旧档案 + 最近消息（可跨群），输出合并后的新档案；多人可合并进一次调用。
4. 注入：按对话对象注入 `<profile>` 块（≤120 字）。主人在私聊/群里都注入主人的档案；
   群友档案只在该群出现的人身上注入（内容可以来自别的群，但只在这个人面前用）。
5. `style`：她自己养成的说法（≤3 条 × 30 字），汇总时一起更新。

**成本**：每人每 3 天最多一次，约 300–600 tokens，可批量。

**待定**：档案和 `fact_index` 的关系（档案里提到的细节要不要同时留成事实）；档案是否给群友看。

### P4 表情包（只用 QQ 收藏夹）

**用户补充（2026-11-12）**：**禁止使用 emoji**（包括 QQ 内置小黄脸）；
她的表情包由主人登录她的 QQ，收藏到"表情收藏夹"并命名；插件重载后要能检测到有哪些表情。

**SnowLuma 已具备的接口**（见其 OneBot actions，已核对）：

- `fetch_custom_face_detail`：列出账号收藏表情，返回
  `{emoji_id, resId, url, md5, desc}`；`desc` 就是收藏时设的描述（用作"名字"）。
- `fetch_custom_face`：只要 URL 或 emoji_id 列表（`return_type=url|id`）。
- `send_custom_face`：`emoji_id`（或 32 位 MD5）发送收藏表情，保留原样式。
- `add_custom_face` / `delete_custom_face` / `modify_custom_face` / `move_custom_face_to_front`：管理用。

**方案**

1. 插件启动/重载时调 `fetch_custom_face_detail`（count 取大一点），把 `desc`（空则用 md5 前 8 位）
   映射到 `emoji_id`，缓存成"名字 → 表情"表；失败只记日志，不影响其它功能。
2. 提示里列出可用表情名字，要求模型只在"被逗笑 / 撒娇 / 附和"时用
   `<sticker:名字>` 这种标记，插件解析后改发 `send_custom_face`。
3. 频率：每轮最多 1 个、小概率触发（可配），不进流水（与图片一致，只留占位符）。
4. 不做 emoji；模型输出的小黄脸/emoji 字符在出站前剥掉（`segments` 或装饰钩子里统一处理）。

**待定**：触发概率、是否允许在群聊里连续两条都带表情。

### P5 空间日记（SnowLuma 已支持）

**用户补充（2026-11-12）**：项目用的是 SnowLuma，不是 NapCat；先确认它有没有空间接口。

**核实结果**：SnowLuma 的 OneBot 扩展里有完整 QQ 空间接口——`send_qzone_msg`（发布）、
`get_qzone_msg_list` / `get_qzone_feeds` / `get_qzone_public_feeds`（读取）、`comment_qzone`、
`like_qzone`、`delete_qzone_msg`、`get_qzone_profile`、`uploadQzoneImages`（配图）等。

**方案（草案）**：每天/每几天从当天的记忆与情绪里挑一件小事，写 1–2 句"说说"，配图可选；
调用 `send_qzone_msg` 发布；失败只记日志、不重试轰炸。频率默认低频（如每周 1–3 条），可配。

**风险**：登录态与风控（SnowLuma 负责）；内容审核；发布频率过高像营销号。

**待定**：发布节奏、是否允许配图、日记内容与 `observed` 层的边界（群友的事不能当自己的事发）。

### 附：运行时画像（实测，2026-11-12）

| 项 | 数值 |
|---|---|
| 空进程导入（解释器 + numpy + astrbot 依赖） | 179–188 MB |
| 插件常驻对象 | < 5 MB |
| 流水每条 | 0.29 KB |
| 记忆每条（含 1024 维 float32 向量） | 4.54 KB |
| 单轮对话 token | 3.0–3.8k（实测账单） |
| 单轮 token 上限 | 约 8–10k（AstrBot 默认 50 轮截断 + 128k 兜底） |
| 后台调用 | 记住打分 160–390 tokens；汇总 1–2k / 6 小时 |

### 附：记忆归档规则（常被问）

**归档 ≠ 删除**。归档是把一条记忆标成"旧了、默认不参与检索"，文件和数据库里都还在，
`/kahn memory facts` 能看到（带〔已归档〕标记），需要时也能恢复。

触发条件（三个同时满足）：

1. 重要度 < 60（LLM 写入时打的 0–100 分）；
2. 30 天没被检索命中过（`last_hit_ts` 空则看创建时间）；
3. 没有待发提醒（`due_ts` 为空）。

为什么这么设计：重要度 ≥60 的（承诺、纪念日、关键偏好）永远不归档，只参与时间衰减；
低重要度又长期没人提的（"今天吃了麻辣烫"）才收起来，避免检索被噪音淹没。

时间线示例：

- 11-12 记下"主人喜欢冰美式"（重要度 70）→ 永远不归档；
- 11-12 记下"今天外卖点了麻辣烫"（重要度 20）→ 12-12 之后若一次都没被检索到，进归档；
- "下周三体检"（重要度 80，带 `due_ts`）→ 提醒发完 `due_ts` 清空，但重要度 80 仍不归档。