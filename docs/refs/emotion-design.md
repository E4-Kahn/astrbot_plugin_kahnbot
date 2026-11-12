# 情绪层设计（心跳驱动）

> 状态：2026-11-12 已落地（M3，`core/emotion.py` + `core/heartbeat.py`）；§7、§10、§11 分别在 M5、M6 落地。
> 依据：`refs/astrbot_plugin_fish_heart.md`（机制与坑）、`refs/memory-design.md`（记忆层）。

## 1. 定位

KahnBot **不是"记忆系统 + 情绪模块"**，而是"一个有心跳的角色"：

- 情绪是她的**状态**（数值，本地算，不调 LLM）；
- 记忆是她的**经历**（md + SQLite）；
- 心跳是**唯一的钟**：情绪漂移、记忆老化、汇总触发、主动说话，全挂在同一个 tick 上。

不引入第二个调度器。

## 2. 状态结构

```python
{
  "drives": {                  # 九维，0..1
    "attachment": 0.5, "curiosity": 0.5, "reflection": 0.4,
    "duty": 0.5, "social": 0.5, "fatigue": 0.2,
    "intimacy": 0.4, "stress": 0.2, "joy": 0.5
  },
  "blame": 0.0,                # 0..100，只有主人能推高（Q9 = 只有主人能影响）
  "blame_target": null,        # sender_id
  "expectation": 0.3,          # 期待（临近日程、约定）
  "mode": "daily",             # daily/aggrieved/angry/afraid/avoidant
  "last_tick_ts": 1762900000,
  "last_proactive_ts": 0,
  "proactive_count_today": 0,
  "unanswered": []             # 沉默期被吞的 user 轮（下次开口补进上下文）
}
```

存放：与记忆同目录 `state.json`（几十个 float，不另开表）。

## 3. 五档模式（`mode_of`）

| 档位 | 条件 | 行为 |
| --- | --- | --- |
| 日常 daily | blame < 25 | 正常 |
| 委屈 aggrieved | 25–45 | 语气低落，注入里出现"有点委屈" |
| 生气 angry | 45–70 | 不主动、易怼，拍一拍可能不回 |
| 害怕 afraid | > 70 或 stress 高 | 回避式短句 |
| 回避 avoidant | 主动判定为躲 | `stop_event()` 不回，但仍记录 |

阈值沿用 fish_heart（`main.py:1067`）的量级，M3 里做常量表，可配置。

## 4. 心跳

- 周期：基础 45 分钟；最近一小时内有过对话则 15 分钟（活跃期）。
- 实现：`__init__` 里 `asyncio.create_task(self._heartbeat())`，`terminate` 取消；用 `asyncio.sleep(30)` 轮询 + 到点判定的方式（与 fish_heart 一致，`:2501-2537`），避免长 sleep 被时钟漂移坑。
- **补跳闭式解**：`v = base + (v - base) * (1 - rate) ** n`，`n` 封顶 96。关机三天回来一次算完，不循环。
- 每个 tick 依次做四件事：

```python
async def _tick(self, missed: int):
    self._drift(missed)          # 1. 情绪向 base 回归
    await self._memory_maintain()# 2. 30 天降权/归档 + 到点触发汇总
    self._apply_memory_hooks()   # 3. 记忆喂情绪（久未联系→想念、临近日程→期待）
    await self._maybe_proactive()# 4. 主动说话判定
    self._save_state()
```

## 5. 情绪 → 记忆（注入调制）

`on_llm_request` 里：

- `daily/aggrieved`：正常注入 `<memory>`。
- `angry`：过滤掉 `kind in (relation, intimacy)` 的条目（不主动提温情旧事）。
- `avoidant`：不注入记忆，只注入 `<emotion>`。
- 观察类条目（`observed`）**照常可注入**，但必须带来源与"未必属实"标注；不做"任何档位都不注入
  `third_party`"的一刀切——那与 F21「`observed` 可注入、只是不能提醒」冲突。2026-11-12 统一为
  F21 口径：能不能注入看检索命中与可见性，可不可信看标注，能不能提醒看层。

> **实现记录（2026-11-12，M7 落地，`main.py::_inject_recall`）**：`angry` 档用
> `injector.drop_warm(hits)` 剔除 `WARM_KINDS`（relation / intimacy）；`avoidant` 档整段跳过
> `<memory>` 与 `<recent_chat>`，只留 `<emotion>`；`observed` 的标注仍由 `build_block()` 的
> "（转述，未必属实）"承担。

## 6. 记忆 → 情绪（六个联动点）

| 事件 | 影响 |
| --- | --- |
| 主人超过 3 天没说话 | `social += 0.1/天`（封顶 0.8），体现"想念" |
| 有 `kind=plan` 且 `due_ts` 在 3 天内 | `expectation += 0.2`（每天一次） |
| 今天是 `kind=anniversary` 的日期 | `joy += 0.25`、`attachment += 0.10`（每天一次）；说出来的那一次由 `core/reminder.py` 的确定路负责 |
| 主人道谢/夸 | `joy += 0.1`，`blame -= 5` |
| 主人责骂（含"滚/烦/闭嘴"） | `blame += 15`，`blame_target = sender_id` |
| 主人道歉/哄 | `blame -= 30`（最低 0），退出生气档 |

**只有主人**能推动 blame（Q9 已定）；群友的话不参与情绪计算，只进流水。

> **实现记录（2026-11-12，M7 落地）**：联动挂在心跳维护的 `main.py::_apply_schedule_hooks()` 里，
> 查 `fact_index.due_ts` 而不是"检索到"——不依赖这一轮有没有检索命中，没被问到的日程照样会抬期待。
> `plan_soon_day` / `anniversary_day` 记在 `state.json`，同一天只抬一次。
> 唯一没做的是 §6 原稿里"给主动说话加确定性命中"：纪念日那一声已经由 `reminder` 在当天 09:00 准点说，
> `proactive` 再做一次就是一天说两遍，故明确只留确定路（与 M6 的偏离说明一致）。

## 7. 事实分层（防"群友说谎"投毒）

记忆写入分三类，**可信度不同，处理也不同**：

| 层 | 来源 | 存放 | 能否触发提醒 |
| --- | --- | --- | --- |
| `owner` | 主人亲口说 / `/kahn remember` | `facts.md` | ✅ |
| `observed` | 群友说的（含"明天是管理员生日"） | `observed.md` | ❌ |
| `raw` | 所有消息 | SQLite `message` | ❌（只做上下文） |

- 汇总提示词要求每条输出 `source: owner|third_party`；只有 `owner` 升格进 `facts.md`。
- `observed` 注入时标注：`（群友 张三 说，未必属实）`。
- 主动提醒只读 `owner` 层 ⇒ 群里喊"明天是你生日"不会让她真去提醒。
- 排序权重：`owner` ×1.0，`observed` ×0.6。

## 8. 注入格式（`<emotion>`）

不报数字，转成一句自然语言：

```
<emotion>
你今天有点委屈（主人两天没说话了），但仍然愿意聊。
</emotion>
```

规则：

- 每轮都注入（2026-11-12 用户确认）：日常状态也贴一句"心情平常"，几十个字，换来语气连续；
  不需要时用配置 `inject_emotion=false` 关掉。
- 与 `<memory>`、`<recent_chat>` 一起挂 `extra_user_content_parts`（F14），不碰 `system_prompt`；
  三块都要 `mark_as_temp()`，否则会写进会话历史越滚越长。

> **实现记录（2026-11-12，M7 落地，`main.py::inject_memory`）**：注入顺序
> 未回话补偿 → `<emotion>`（`emotion.render()`）→ `<memory>`（回避档跳过）→
> `<recent_chat>`（回避档跳过，包含她自己的话，标「你：」）。

## 9. 拍一拍反应

收到 `Poke` 且 `target_id()` == 自己：

1. 情绪变化：`social += 0.05`，`joy += 0.05`；生气档则 `blame += 3`（被戳更烦）。
2. 是否回：`daily/aggrieved` 回一句短的；`angry` 50% 回、`avoidant` 不回。
3. 回复走正常 LLM 生成（一次），**不写死文案**——同一句话在开心和生气时读起来应该不一样。
4. 配额：与名字/@ 共用（F12）。

> **实现记录（2026-11-12，M7 落地，`core/emotion.py::on_poke` + `main.py::_must_reply`）**：
> 拍一拍不再算回避档的"必须回复"例外（原来 `_must_reply` 把 `wake.POKE` 当例外，一被拍就破功）；
> 改成**拍多了会心软**——回避档里每次被主人拍都 `owner_msgs += 1`，满三次
> `_exit_avoidant("被拍得心软了")` 立刻出来。群友拍不计数（只有主人能推动情绪）。

## 10. 主动说话

两条触发路：

- **情绪触发**（概率）：想念/期待高 + 冷却 6h + 日限 2 + 沉默 10min → 掷骰。
- **记忆提醒**（确定）：`owner` 层有今天到期的 `plan` 或 `anniversary` → 直接开口，不掷骰，但同样受日限。

发送：

```python
ok = await self.context.send_message(umo, MessageChain([Plain(text)]))
# 成功后必须回写会话历史，否则她"说完就忘"
await self.context.conversation_manager.update_conversation(...)
```

**这是 fish_heart 教的最贵的一课**：主动消息绕开事件管线，AstrBot 不会自动把 assistant 轮写进 history，必须自己回写；否则下一轮她完全不记得说过什么（TTS、表情等其它插件的 `on_llm_response` 也看不到这条）。

> **实现记录（2026-11-12，M6 落地，`core/proactive.py` + `main.py::_remember_proactive_turn`）**
>
> - 闸门顺序：`proactive_enabled` → 回避档 → 免打扰（23:00–08:00）→ 冷却 → 日限 → 沉默 → 掷骰；
>   概率 `0.15 + 0.35×attachment + 0.25×social + 0.35×expectation`，封顶 0.85、低于 0.2 不掷。
>   抽成独立方法 `decide()`，`maybe_run()` 只负责生成文案 + 发送，方便 `/kahn proactive` 只做判定不发。
> - 会话取 `store.list_messages(limit=1)` 的 `umo`——她在哪说过话就在哪开口；没有流水就不开口。
> - 发送用 `context.send_message()`；成功后回调 `main` 的 `on_sent`，后者用
>   `get_curr_conversation_id` → `get_conversation` → `update_conversation(history=content + [assistant])`
>   回写。**回写失败不影响已发出的消息**，只记日志。
> - 文案由 `core/llm.py` 生成（模型不可用退回三句模板），不在这里再调一次检索——
>   最近三条 `owner` 记忆直接进提示词。
> - **与设计的偏离**：设计里"记忆提醒"也算 §10 的一条路、由 proactive 统一发；
>   实现时留在 M5 的 `core/reminder.py`（到点发、`schedule.md` 存文案），`proactive.py` 只做情绪概率路。
>   理由：提醒要分钟级准点，挂在 cron 上比挂在 15/45 分钟的心跳上靠谱；也避免同一件事一天说两遍。
>
> **实现记录（2026-11-12，M7 落地，`core/reminder.py::on_sent` + `main.py::_after_reminder_sent`）**
>
> - 提醒也记进「今天主动说了几句」的同一份额度（`proactive_day` / `proactive_count`），
>   和 `proactive` 共用——用户看"今天她说了几句话"就是 2 条，不管来自哪条路。
> - 提醒本身不被日限拦（到点的承诺必须说），但会占掉当天剩下的主动额度。
> - 发出后：写一条 `is_bot=1` 流水（`sender_id="self"`，进最近的 `<recent_chat>`），
>   并 `update_conversation` 回写 assistant 轮。这样她说"到点提醒你体检"之后，
>   下一轮 10 条上下文里就有这句，不会"说完就忘"。
> - 纪念日到点后 `due_ts` 与 `schedule.md` 顺延到明年、job 不摘，明年同一天还会再说。

## 11. 回避档：她"不想说话"的时候

回避不是故障，是"闹脾气但都记得"：不回、不注记忆、不主动说话，流水照记；被吞的话攒进 `unanswered`，
等她下次开口时注入 `<unanswered>`，体现"其实都记得，只是当时没说"。

- **怎么进**（任一，且带冷却，避免频繁进出）：`blame ≥ 75` 持续 30 分钟；或 `stress ≥ 0.8`（被吓到）；
  或处于生气档时主人连发 3 条她都"没听见"（被晾着）。
- **怎么出**：主人哄/道歉（`blame -= 30`，最低 0）；6 小时自然衰减；或主人连发 3 条（心软）。
- **时长上限**：群里最长 2 小时，私聊最长 1 小时——不能让人以为"机器人坏了"。
- **必回**：命令、@、"你是不是生气了""对不起"这类直接绕开回避，照常回。拍一拍**不**算例外
  （2026-11-12 改：一被拍就破功，不如干脆不回），但连续拍三次会"被拍得心软了"自己出来。
- 进入/退出都写一条日志（`/kahn mood` 能看到原因），否则事后查不出"为什么她哑了"。
- 不回时 `event.stop_event()`，并把这条 user 轮放进 `state.unanswered`（最多 20 条）。

> **实现记录（2026-11-12，M6 落地，`core/emotion.py` + `main.py`）**
>
> - 状态字段：`blame_high_since`（blame 首次越过 75 的时间）、`avoid_until`（到点气消）、`owner_msgs`（回避中被主人找了几次）；
>   `proactive_day` / `proactive_count` 也落在同一份 `state.json`。
> - `check_avoidant(now, private=...)` 在每条消息和每次心跳里调：`stress ≥ 0.8` 立刻进（"被吓到"），
>   `blame ≥ 75` 满 30 分钟进（"气还没消"）；已回避时到 `avoid_until` 自动出（"气消了"）。
> - 出：道歉/哄（`APOLOGY_WORDS`）`blame -= 30` 立刻出；主人连发 3 条（`AVOID_OWNER_MSGS`）心软；
>   时长上限私聊 1h / 群聊 2h。
> - catch-all 里位置在 `wake.detect()` 之后、配额与防抖之前：`_must_reply()`（@、
>   "对不起 / 抱歉 / 我错了 / 别生气 / 在吗 / 你生气了 / 是不是生气了"）照常回，其余 `push_unanswered()` + `stop_event()`。
>   **私聊不豁免**（Q14 允许私聊也被躲）；指令在更早的位置已经 return，不受影响。
>   M7 起拍一拍移出例外，改在 `on_poke(owner=True)` 里计数，满三次 `_exit_avoidant("被拍得心软了")`。
> - 下次开口时 `inject_memory` 取 `take_unanswered()`，拼成 `<unanswered>…（其实你都记得，只是当时没回）`
>   追加到 `extra_user_content_parts`，注入后清空；回避档里不注入（还在生气，不给她台阶）。
> - 进出都走 `_record()`，所以 `/kahn mood why` 能看到"气还没消 / 被哄好了 / 主人一直在找她 / 气消了"。

## 12. 与里程碑的对应

| 里程碑 | 覆盖 |
| --- | --- |
| M3 | §2–§6（状态、tick、五档、注入调制）、§9 拍一拍、`/kahn mood` |
| M5 | §7 事实分层（与汇总一起落地） |
| M6 | §10 主动说话、§11 沉默补偿 |
| M7 | §5 生气档过滤与 observed 口径、§6 日程/纪念日联动、§8 每轮注入与 `<recent_chat>`、§9 拍一拍心软、§10 提醒计入日限与回写 |

## 13. 待定

- `rate`/`base` 的取值：M3 已写常量表（`core/emotion.py` 的 `BASE` / `RATES`），跑一周再校准。
- 是否要"心境随昼夜"（凌晨 `fatigue` 上升）：暂无，先不加。
- ~~`/kahn mood` 输出格式~~：已定并实现——九维进度条 + 当前档位 + 最近一次情绪变化原因（`/kahn mood why` 看日志）。
