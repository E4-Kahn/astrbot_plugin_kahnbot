# 鱼的心（astrbot_plugin_fish_heart）

- 来源：`D:\workspace\astrbot_plugin_fish_heart`（仓库 `Vivy33/astrbot_plugin_fish_heart`，v0.4.0，2026-11-09）
- 元数据：`astrbot_version: ">=4.24.2"`，`support_platforms: [aiocqhttp]`，MIT（衍生自 `astrbot_plugin_emotion_core`）
- 调研日期：2026-11-11
- 规模：`main.py` 6635 行、`thought_engine.py` 646 行、`_conf_schema.json` 385 行（13 个顶层块）、README 720 行、CHANGELOG 582 行（0.1.0→0.4.0 共 14 个版本）

## 1. 速览

**定位**：给角色装一颗"跳动的心"——九维驱动 + 两条快变量 + 五档责怪档 + 念头 + 心境注入 + 情绪驱动的主动说话。
**它明确不存记忆**：README §6 只存摘要，§8 直接写"fish_heart 不存记忆……要真的'记得住'，请配记忆插件"。

| 指令 | 作用 | 限制 |
| --- | --- | --- |
| `/心 status` | 心境、责怪、念头、主动目标 | 群里只对 `perception.owner_ids` 开放（`_can_use_commands`:2186） |
| `/心 calm/reset/tick/off/on` | 清责怪/重置/手动心跳/开关 | 同上 |
| `/心 umo` | 打印当前会话 UMO | **例外**，群里任何人可用 |
| `/adult on/off/status` | 成人层授权/暂停/状态 | 私聊鱼主 |

数据：单个 `data/plugin_data/astrbot_plugin_fish_heart/state.json`（README:630-637），**没有数据库**。
配置迁移：`_migrate_config_036`:1356、`_migrate_group_mode`:1418、`_migrate_desire_refresh`:1463、念头 v1→v2 `_migrate_thoughts`:64 与 `thought_engine.migrate`:588。

## 2. 机制

### 2.1 状态与心跳

- 九维 drives（attachment/curiosity/reflection/duty/social/fatigue/intimacy/stress/joy）+ expectation + blame_pressure；`new_state`:1125。
- 五档由 `mode_of(blame)`:1067 决定：日常 <25、委屈、生气、害怕、回避。
- 心跳：`initialize`:2480 建 `_loop`；每 30 秒醒一次，`_tick_interval`:2556 在 45→15 分钟之间按压力/疲惫收紧（urgency = 0.6*blame + 0.4*stress）。
- **补跳用闭式解**（`_tick`:2568）：`v = base + (v - base) × (1 - rate)^n`，`n` 封顶 `MAX_CATCHUP_TICKS`；耦合/念头/睡眠只跑一次——"关机三天不该叠三天"。

### 2.2 注入：不碰 system_prompt

`on_llm_request`:3584 一个钩子干完感知、状态更新、注入。关键在 `_attach_injection`:3509：

```python
part = TextPart(text=body)
parts.append(part.mark_as_temp())      # :3527-3529
```

注释给出的理由（:3510-3519）：DeepSeek 等前缀缓存按"开头→第一个变化点"命中，system prompt 每轮变会让后面整段历史按未命中重算，差价约 10 倍。`mark_as_temp()` 必须调，否则注记被回存进历史（AstrBot 在 `internal.py:522-524` 才跳过 `_no_save`）。

节流在 `_build_injection`:3846：签名 = 九维按 10 分桶 + 夸奖疲劳等级；**跨档必注**，同档内间隔 `inject.min_gap_minutes`（默认 20）才注；"已报到"按 umo 各存一份，最多 20 个会话（`:3900-3910`）。生气档不吃节流（每轮可能换对象）。

### 2.3 谁在说话

- `provider_settings.identifier` 帮不上忙（只标当前发言人），所以他们**自己记**：`_sender_id`:1599 / `_sender_label`:1622，生气按 `speaker_key` 分桶（`owner` vs `member:uid`，`:2201`）。
- 非主人：`_absorb_outsider_turn`:2337 只给气氛（社交/喜悦小加成），碰不到责怪与压力——"别人说好话安抚不了她"。
- 群友模式（`group_mode.enabled`）才放开全群影响，强度由 `member_scale`(0.5)/`member_negative_scale`(0.35)/`owner_negative_scale`(0.5) 控。
- 渠道权重 `_channel_weight`:1537：群聊按"距上次私聊多久"衰减，3 天没私聊时群里一句 ≈ 1/11。

### 2.4 主动说话

`_maybe_proactive`:4616 的闸门顺序：enabled → proactive.enabled → 非回避档 → 非睡眠 → 冷却(默认 6h) → 日限(默认 4) → 沉默(默认 10min) → 概率掷骰（`_proactive_kind`:4738）。
生成 `_gen_proactive_text`:5273 拼：人设（`persona_manager.get_default_persona_v3(umo)`:5497）→ 心境注入 → 带 `[MM-DD HH:MM]` 的真实时间线（`_real_transcript`:4883、`_timeline_line`:4995，没时间戳就写 `[时间不详]`）→ `<system_reminder>` 时间锚点。
发送前 **`_draft_conflict`:5136 自检**（只做"可证明"的检查）：与上次开头重复、他刚说要睡还问"睡了吗"、草稿声称的近况在记录里找不到 → 返回原因，触发重写或兜底。
发送 `_send_text`:5799：`context.send_message(umo, MessageChain([Plain(seg)]))`，按句末标点切 1–4 段，段间 `min(3.5, 0.8+len*0.06)` 秒；之后 `_append_history`:4933 用 `conversation_manager.update_conversation` 把 assistant 回写进历史（`asyncio.Lock` 保护）。

### 2.5 已读不回（回避档）

`_maybe_go_silent`:4261：前 5 条必不回、第 6 条 50%、第 7 条起必回；命令、真哄、道歉、说清楚**必回**；不回时 `event.stop_event()`（注释引用 `internal.py:307` / `context_utils.py` 的 `is_stopped()` 检查），被吞的消息存 `unanswered`，下次开口时补进注入（`_drain_unanswered`:4340）。

### 2.6 念头（thought_engine.py）

纯函数模块（不依赖 AstrBot，可单测）。10 种 kind，每种带 TTL（1.2–4h）、`trigger(drives, expectation, awaiting)` 权重、`reward`/`release`、`done` 文案。
生命周期：spawn → live（注入/主动开口尝试一次）→ settle（被满足，写 mood）或 fade（到期，驱动轻微回落）。
细节：`note_attempt` 连续 3 次没被接住就放弃（:412-435）；`SPEAK_BIAS` 让"想安静"降低开口概率（:471-482）；`migrate` 里**过期的直接丢、不续命**（:610-612），同 kind 只留一条 pending（:632-646）。

### 2.7 WebUI

`_register_web_apis`:5901 用 `context.register_web_api("/<plugin>/state", handler, ["GET"], desc)` 注册；前端 `pages/heart/`（`views/heart/` 是 AstrBot 旧路径的镜像，`tools/sync_pages.py` 同步）。面板只读状态 + 手动 tick/开关兜底。

## 3. 坑与坏味道

| 现象 | 位置 | 影响/教训 |
| --- | --- | --- |
| 心智注入写 system_prompt | `main.py:3510-3519` | 前缀缓存全失效（DeepSeek 差价 ~10×）。作者自己踩过，注释写得很清楚 |
| `TextPart` 忘了 `mark_as_temp()` | `:3517`；`internal.py:522-524` | 注记被持久化，逐轮累积成"真的历史" |
| 主动消息绕开事件管线 | README:646 | 其他插件的 `on_llm_response`（TTS/表情/过滤）看不到；必须手动回写历史，否则"说完就忘" |
| 连续两条 assistant 轮 | README:648 | OpenAI 兼容没问题，严格交替的协议可能报错 |
| 被 `stop_event()` 的 user 消息不进历史 | README:650 | 她沉默时那几句在别的插件看来不存在，得自己存 `unanswered` |
| `send_message` 传裸 QQ 号/群号 | README:674（`不合法的 session 字符串: not enough values to unpack (expected 3, got 1)`） | UMO 必须 `platform:MessageType:session_id` 三段；`_valid_umo`:1514 提前校验 |
| 关键词判定的误伤清单极长 | README §7 第 3 条（"累/怕/抱"不算撒娇、"我在"不算哄、"真好"移出、"滚"只认单独使用、"小笨蛋"昵称豁免、"你怎么看"排除…） | 词表+过滤堆到 4000 行仍未解决反讽/半真半假；作者自己列为"已知限制" |
| 全局单例一颗心 | README §7 第 1 条 | "服务多个人时说不清"——不按会话隔离 |
| 状态全量写单文件 | `_save_state`:2469 | 每次都写整个 state.json，只适合小状态 |

## 4. 对 KahnBot 的启示

**值得借鉴**

1. **注入挂 `extra_user_content_parts` + `mark_as_temp()`**，不碰 `system_prompt` —— 前缀缓存友好。这一条要改我们的设计（原方案是 `req.system_prompt +=`）。
2. **"可证明的自检"**：`_draft_conflict` 只查能证明的矛盾（重复开头、时间矛盾、记录里没有的近况），不猜语义、不再调一次 LLM。相比"让 LLM 严格点"，便宜且可测。
3. **时间线带真实时间**，取不到就写 `[时间不详]`，绝不让模型假装知道"刚刚"。
4. **补跳用闭式解**：离线 N 天就按公式一次算完，不循环。
5. **配置/状态都要有迁移函数**，并且"过期的别续命"。
6. **主动消息要回写历史**，否则下一轮她自己都不记得说过。

**不借鉴**

- 全局单例情绪（我们要按主人 + 会话隔离）。
- 关键词词表 + 几十条例外的感知方式（我们走"显式指令 + 定时汇总"，判定权交给 LLM）。
- 成人模式那一整套（与 KahnBot 定位无关）。
- 心跳驱动一切的架构：它是"情绪连续体"，我们是"检索 + 事实库"，不需要 30 秒循环。

**存疑 / 可讨论**

- 它证明了**插件可以完全绕开 catch-all**：只在 `on_llm_request` 里干活。但那样"她没被唤醒时说的话"就完全看不到——KahnBot 要记全部群消息，catch-all 仍必需。
- 它没有实现"在场感"（谁在场、谁刚才说了什么）以外的群聊记忆，说明这块确实是空白，KahnBot 有差异化空间。
- `group_mode` 的"全员影响 + 权重缩放"是一种可选的群聊参与度模型，值得作为 F12 配额之外的备选。

## 5. 产出

- 新需求候选（待用户拍板，暂记 C1–C6）：
  - C1 注入位置改为 `extra_user_content_parts` + `mark_as_temp()`（影响 F5/M3 实现）。
  - C2 注入节流：跨档/有新命中才注（写入 memory-design §7）。
  - C3 主动说话是否纳入 KahnBot（若纳入，复用 `_draft_conflict` 思路）。
  - C4 状态/配置迁移函数 + `meta` 表 schema_version（影响 M1）。
  - C5 群聊参与度模型：全员影响 + 权重缩放（备选，与 F12 配额二选一或叠加）。
  - C6 时间线注入格式统一为 `[MM-DD HH:MM] 说话人：内容`（影响 M3 的 `<recent_group_chat>`）。
