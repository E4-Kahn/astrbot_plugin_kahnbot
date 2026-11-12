# 实现计划（实施里程碑）

> 状态：v7.2，2026-11-12；M1~M7 全部落地并跑过离线冒烟，0.3.1 / 0.3.2 为真机测试后的修复批次。
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
│   └── proactive.py        # 主动说话：情绪概率路、闸门、回写历史 ✅
├── tests/                  # 离线冒烟（不启动 AstrBot）✅ smoke_m1 ~ smoke_m7
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
  M6 主动说话与回避档、M7 情绪与上下文进对话，全部落地并跑过 `smoke_m1~m7`；版本 `0.3.2`
  （0.3.1 修真机崩溃、加整机重置；0.3.2 提醒不占日限、写入剥指令语、文案交给模型）。动手前先
  `git log --oneline` 确认要不要 push。
- **下一步（可选）**：真机跑一周校准情绪参数（`BASE` / `RATES` / 阈值 / 主动说话概率 / `_chance` 权重）；
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
  ```

  期望最后一行都是 `ALL OK`。临时目录 `.smoke_tmp/` 已 gitignore。
- **真机验证**：插件目录用 junction 链到 `AstrBot\data\plugins\astrbot_plugin_kahnbot`，
  改代码后**必须重启** `astrbot run`（不带 `--reload` 不会热更）。
  部署前先看 `DEPLOYMENT.md` 第 7 节：`add_cron_tools` 必须关，否则提醒发两遍。
  要重来一遍：`/kahn reset confirm`（配置 `reset_backup_dir` 非空时先备份）。
- **数据在哪**：`AstrBot\data\plugin_data\astrbot_plugin_kahnbot\` 下有 `kahnbot.db`（流水 + 记忆索引 + 配额）、
  `state.json`（情绪）、`memory\facts.md`（owner 层正文）、`memory\observed.md`（见闻层）、
  `memory\schedule.md`（提醒排期）。删库即回到零状态。
- **最容易踩的十二件事**：① 指令的 `/` 前缀在 `waking_check` 就被剥掉了，别靠字符串猜指令；
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
  ⑫ 提醒文案必须**先写 `schedule.md` 再挂 job**（`_finalize_reminder` 的顺序），反了就会把旧模板念出去。
- **文档看哪本**：需求与状态 → `REQUIREMENTS.md`；里程碑 → 本文件；记忆设计 → `refs/memory-design.md`；
  唤醒 → `refs/wake-design.md`；情绪 → `refs/emotion-design.md`；部署与模型 → `DEPLOYMENT.md`；
  AstrBot 内部机制核对 → `refs/astrbot-capabilities.md`。