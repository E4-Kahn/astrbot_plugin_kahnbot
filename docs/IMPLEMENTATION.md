# 实现计划（实施里程碑）

> 状态：草案 v1，2026-11-11。需求侧依据 [`REQUIREMENTS.md`](REQUIREMENTS.md)（F1~F13 已定稿），
> 设计依据 [`refs/memory-design.md`](refs/memory-design.md) 与 [`refs/wake-design.md`](refs/wake-design.md)。
> 本文只排顺序与验收，不写实现细节；细节以两份设计笔记为准。

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
│   └── injector.py         # on_llm_request 注入 <memory> / <recent_group_chat>
└── _conf_schema.json
```

依赖方向：`main → (recorder, wake, quota, retriever, injector, memory, briefing) → store / config`。
`store` 不依赖任何业务模块，便于单测。

## 2. 里程碑

### M1 —— 数据层与记录（可独立验证）

- 建 `data/plugin_data/astrbot_plugin_kahnbot/`，初始化 SQLite（`message` / `fact_index` / `wake_quota` / `meta`），开 WAL。
- catch-all handler：`event_message_type(GROUP_MESSAGE)` + `priority(100)`，**只写本地库**，不碰 LLM。
- 噪音过滤（F13）：`#sl` 等固定文本直接丢弃，记录与唤醒都不参与。
- 指令：`/kahn`（沿用）、`/kahn memory list`、`/kahn memory forget <id>`。
- 覆盖：F6、F8a、F11、F13。
- 验收：群里发三条 → 库里三条且带 `sender_id`/`sender_name`/`group_id`；发 `#sl` → 零条；重启进程数据仍在。

### M2 —— 唤醒与配额（用户能感知到"她活了"）

- `wake.py`：三种触发（@/前缀走原生、拍一拍、名字/关键词），命中后置 `event.is_at_or_wake_command = True`。
- 拍一拍补 `message_str`，昵称用 `last_name_of(uid)` 兜底。
- 配额（F12）：`wake_quota(user_id, day, count)`，管理员豁免，超限静默。
- 覆盖：F10、F12。
- 验收：`DEPLOYMENT.md` 验收表第 2~7 条全过。

### M3 —— 检索与注入（接 LLM）

- `embeddings.py`：`context.get_provider_by_id()` → `get_embedding()`，无 provider 时降级纯字面。
- `retriever.py`：混合排序 `0.6*余弦 + 0.4*字面`，隐私过滤（群聊只查 `public`）。
- 触发：`register_llm_tool("recall_memory")` + 关键词直查。
- `injector.py`：`on_llm_request` 注入 `<memory>`（相关事实）与 `<recent_group_chat>`（最近 N 条带发送者）。
- 覆盖：F5、F9。
- 验收：场景 S2——群聊里问某件只在私聊说过的事，回答中**不得出现**该内容（F9 隔离）；主人显式解禁后再问，能检索到。

### M4 —— 长期记忆（写入与老化）

- 显式"记住…"解析 → md 追加 + `fact_index` 插入 + 默认 `pinned=1`。
- `briefing.py`：定时（默认每小时）增量读流水 → LLM 提炼 → 去重落条目。
- 老化（F7）：30 天未命中，排序乘 `0.5 ** (天数/30)`；归档写入 `archive.md`，默认不参与检索，可显式查。
- 覆盖：F3、F4、F7。
- 验收：场景 S1/S3；手工把某条 `last_hit_ts` 改成 40 天前 → 排序下降且进归档。

### M5 —— 收尾

- `_conf_schema.json` 扩全：`embedding_provider_id`、`wake_keywords`、`wake_quota`、`noise_patterns`、`briefing_interval`、`recent_n`。
- README 补指令表与配置说明；CHANGELOG 起 `0.2.0`。
- 最小自测：`tests/test_store.py`（建表/写入/迁移）、`tests/test_retriever.py`（排序与隐私过滤）。
- 覆盖：全部。

## 3. 依赖与顺序

```
M1 ──► M2 ──► M4
 └────► M3 ──┘
```

M3 依赖 M1 的 `fact_index` 与 `message`；M4 依赖 M3 的检索命中回写 `last_hit_ts`。
若想更早看到效果，可先做 M1+M2（纯离线，不花 LLM 钱），再接 M3。

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
| SQLite 并发写 | `database is locked` | WAL + 单连接串行写 + 超时重试 |

## 6. 待补

- [ ] 汇总用什么模型（跟随 AstrBot 默认 provider 还是单独指定）。
- [ ] `/kahn` 指令的最终形态（现在是 status，之后要挂 memory 子指令）。
- [ ] 是否需要 WebUI 配置页（AstrBot 插件配置页自动由 schema 生成，够用则不做）。
