# 实现计划（实施里程碑）

> 状态：草案 v2，2026-11-11。需求侧依据 [`REQUIREMENTS.md`](REQUIREMENTS.md)（F1~F22），
> 设计依据 [`refs/memory-design.md`](refs/memory-design.md)、[`refs/wake-design.md`](refs/wake-design.md)
> 与 [`refs/emotion-design.md`](refs/emotion-design.md)。
> 本文只排顺序与验收，不写实现细节；细节以设计笔记为准。

## 0. 总原则

- **每个里程碑结束时插件都能正常加载、`/kahn` 可用**，不留半截状态。
- 先本地闭环（写入 → 能查），再接 LLM（检索 → 注入 → 汇总）。先跑通再加功能。
- 0.x 阶段不做兼容包袱：表结构变更直接改 schema，`meta` 里记版本号，启动时按版本重建。
- 代码文件按职责拆，`main.py` 只做注册与串联，避免回到脚手架那种"什么都塞入口文件"的形态。

## 1. 模块划分（拟）

```
astrbot_plugin_kahnbot/
├── main.py                 # 注册 handler / 指令 / 生命周期，不写业务
├── core/
│   ├── config.py           # 读配置、默认值、类型收敛
│   ├── store.py            # SQLite：建表、迁移、WAL、四张表读写
│   ├── recorder.py         # catch-all 记录 + 噪音过滤 + 昵称缓存
│   ├── wake.py             # 唤醒判定（at / poke / name）+ 放行
│   ├── quota.py            # 每日唤醒配额
│   ├── memory.py           # md 读写、pin、30 天衰减归档
│   ├── briefing.py         # 定时汇总：流水 → LLM 提炼 → 落条目
│   ├── retriever.py        # 向量 + 字面混合检索
│   ├── embeddings.py       # 复用 AstrBot Embedding Provider
│   ├── injector.py         # on_llm_request 注入 <memory> / <recent_group_chat> / <emotion>
│   ├── emotion.py          # 九维情绪、五档模式、拍一拍反应
│   ├── heartbeat.py        # 唯一的钟：tick 调度、补跳、主动说话触发
│   ├── proactive.py        # 主动消息生成、防重复、回写历史
│   └── trust.py            # 事实分层 owner / observed / raw
└── _conf_schema.json
```

依赖方向：`main → (recorder, wake, quota, retriever, injector, memory, briefing, emotion, heartbeat, proactive) → store / config`。
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
- 落地情况：`core/config.py`、`core/store.py`、`core/recorder.py` 已完成；离线冒烟通过；
  真机（独立测试根 `D:\workspace\_astrbot_test`，`astrbot run -p 6285`）插件加载无报错并建库成功
  （`kahnbot.db` 四表 + 三索引 + `meta.schema_version=1` + WAL）。

### M2 —— 唤醒与配额（用户能感知到"她活了"）

- `wake.py`：三种触发（@/前缀走原生、拍一拍、名字/关键词），命中后置 `event.is_at_or_wake_command = True`。
- 拍一拍补 `message_str`，昵称用 `last_name_of(uid)` 兜底。
- 配额（F12）：`wake_quota(user_id, day, count)`，管理员豁免，超限静默。
- 覆盖：F10、F12。
- 验收：`DEPLOYMENT.md` 验收表第 2~7 条全过。

### M3 —— 情绪层（纯离线，不花一分钱）

- `emotion.py`：九维驱动与 `base`/`rate` 常量表、`mode_of()` 五档；`blame` 只由主人推动（F20）。
- `heartbeat.py`：`asyncio.create_task` + `asyncio.sleep(30)` 轮询；基础 45 分钟、活跃期 15 分钟；
  补跳闭式解 `v = base + (v-base)*(1-rate)**n`（n 封顶 96）；tick 四步（漂移 → 记忆维护 →
  联动 → 主动判定占位）。
- `state.json` 持久化，`terminate` 时落盘。
- 指令 `/kahn mood`（F22）。
- 覆盖：F17、F18、F20、F22（F19 的联动点留接口）。
- 验收：手工把 `blame` 设到 50 → `/kahn mood` 显示生气档；重启后数值保留；
  模拟关机 6 小时再启动，一次 tick 就补到正确量级（不循环 72 次）。

### M4 —— 检索与注入（接 LLM）

- `embeddings.py`：`context.get_provider_by_id()` → `get_embedding()`；`embedding_provider_id` 留空时
  自动取第一个启用的 embedding provider；无 provider 时降级纯字面。
- `retriever.py`：混合排序 `0.6*余弦 + 0.4*字面`，隐私过滤（群聊只查 `public`）。
- 触发：`register_llm_tool("recall_memory")` + 关键词直查。
- `injector.py`：`on_llm_request` 注入 `<memory>`、`<recent_group_chat>`、`<emotion>`；
  `emotion` 参与调制（生气档过滤 `relation`/`intimacy` 条目，回避档不注记忆，日常不注 `<emotion>`）；
  挂 `req.extra_user_content_parts`（`TextPart(...).mark_as_temp()`），**不写 system_prompt**
  （F14，见 refs/astrbot-capabilities.md 第 15 节）。
- 覆盖：F5、F9、F14、F19。
- 验收：场景 S2——群聊里问某件只在私聊说过的事，回答中**不得出现**该内容（F9 隔离）；
  主人显式解禁后再问，能检索到。

### M5 —— 长期记忆与事实分层

- 显式"记住…"解析 → `facts.md` 追加 + `fact_index` 插入 + 默认 `pinned=1`。
- `trust.py` + `briefing.py`：汇总输出带 `source: owner|third_party`；只有 `owner` 升格进
  `facts.md`（可触发提醒），`third_party` 落 `observed.md`（注入时标注"未必属实"、排序 ×0.6）。
- 老化（F7）：30 天未命中，排序乘 `0.5 ** (天数/30)`；归档写入 `archive.md`，默认不参与检索。
- 覆盖：F3、F4、F7、F21。
- 验收：群里有人说"明天是管理员的生日" → 只进 `observed.md`，次日**不会**触发提醒；
  主人说"下周三体检" → 进 `facts.md`，次日能提醒。

### M6 —— 主动说话与收尾

- `proactive.py`：情绪概率路 + 记忆确定路（`owner` 层到期日程/纪念日）；
  冷却 6 小时、日限 2 条、沉默 10 分钟；生成后 `context.send_message()`，
  **必须** `conversation_manager.update_conversation()` 回写 assistant 轮（F16）。
- 回避档：`event.stop_event()` + `unanswered` 补偿。
- `_conf_schema.json` 扩全：`embedding_provider_id`、`wake_keywords`、`wake_quota_daily`、
  `noise_patterns`、`briefing_interval`、`recent_n`、`emotion.*`、`proactive.*`。
- README 补指令表与配置说明；CHANGELOG 起 `0.2.0`。
- 最小自测：`tests/test_store.py`（建表/写入/迁移）、`tests/test_retriever.py`（排序与隐私过滤）、
  `tests/test_emotion.py`（五档与补跳）。
- 覆盖：全部。

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
| 心跳任务与重载并发 | 重载后两个 tick 同时写库 | `terminate` 里 `task.cancel()` + `await`，与写库共用同一把锁 |
| SQLite 并发写 | `database is locked` | WAL + 单连接串行写 + 超时重试 |

## 6. 待补

- [ ] 汇总用什么模型（跟随 AstrBot 默认 provider 还是单独指定）。
- [ ] `/kahn` 指令的最终形态（现在是 status，之后要挂 memory / mood 子指令）。
- [ ] `/kahn memory forget` 是否限管理员。
- [ ] "记住…"识别方式（Q8：正则直存 / 唤醒后工具调用 / 折中）。
- [ ] 情绪参数（base/rate/阈值）初值，M3 跑一周后校准。
- [ ] 是否需要 WebUI 配置页（AstrBot 插件配置页自动由 schema 生成，够用则不做；`/kahn mood` 已覆盖查看需求）。
