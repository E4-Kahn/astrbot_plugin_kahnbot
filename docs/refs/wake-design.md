# 唤醒设计（catch-all + 第四种唤醒）

> 状态：设计草案，2026-11-11
> 关联：REQUIREMENTS F10 / F11 / F12，capabilities 第 9、13 节

## 1. 一个 handler 干三件事

```python
@filter.event_message_type(EventMessageType.GROUP_MESSAGE, priority=100)
async def on_group_message(self, event: AstrMessageEvent):
    await self.recorder.record(event)          # ① 永远记录，不看唤醒

    reason = self.wake_reason(event)           # ② 判定：at / poke / name
    if reason is None:
        return                                 # 只当上下文，结束

    if not await self.quota.allow(event):      # ③ 配额
        return

    if reason == "poke" and not event.message_str:
        event.message_str = f"（{event.get_sender_name()} 拍了拍你）"

    event.is_at_or_wake_command = True         # ④ 放行默认 LLM 流程
```

要点：**记录、判定、放行是三个独立步骤**，任何一步都不该影响另一步。

记录前先过一道噪音过滤（F13）：`#sl` 这类固定文本不进流水，也不参与唤醒判定
（否则 SnowLuma 的状态命令会被当成群聊内容喂给后续汇总）：

```python
NOISE = ("#sl",)                      # 与 SnowLuma statusCommand.trigger 保持一致
if event.message_str.strip().lower() in NOISE:
    return                            # 不记录、不唤醒
```

## 2. 为什么必须自己置 `is_at_or_wake_command`

`core/platform/astr_message_event.py:98` 是普通属性，默认为 False，
注释写得很直白：

```
"""是否是 At 机器人或者带有唤醒词或者是私聊
(插件注册的事件监听器会让 is_wake 设为 True, 但是不会让这个属性置为 True)"""
```

而 `core/pipeline/process_stage/stage.py:63-76` 的 LLM 闸门只看这个属性：

```python
if (not event._has_send_oper and event.is_at_or_wake_command and not event.call_llm):
    async for _ in self.agent_sub_stage.process(event):
```

时机上也是安全的：同一个 `process()` 里，handler 在 `:47-60` 先执行，
闸门在 `:66` 才判断，所以 handler 里置位有效。

## 3. 坑

### 3.1 被唤醒路径上不能先 `event.send()`

`send` / `send_streaming` 会把 `_has_send_oper` 置 True（`astr_message_event.py:367`、`:583`），
而它是闸门条件之一。只要在置位 `is_at_or_wake_command` 之前发过任何消息，
默认 LLM 回复就会被跳过。

⇒ 配额耗尽时的提示、唤醒前的任何提醒，都要放在**不打算走 LLM** 的分支里。

### 3.2 拍一拍的 `message_str` 是空串

`aiocqhttp_platform_adapter.py:186` 把它置空，`:192-194` 只塞了个 `Poke` 组件。
不补文案，模型收到的是空 prompt。

**而且拍一拍拿不到昵称。** 适配器对通知类事件是这么构造发送者的：

```python
# aiocqhttp_platform_adapter.py:172-174
abm.sender = MessageMember(user_id=str(event.user_id), nickname=str(event.user_id))
```

昵称被直接写成 QQ 号。上游 SnowLuma 的拍一拍事件也确实不带昵称
（`packages/onebot/src/event-converter/to-notice.ts:141-152`，字段只有
`user_id` / `target_id` / `action` / `suffix` / `action_img_url`）。

⇒ 补文案时不要直接用 `get_sender_name()`，否则会变成"（123456789 拍了拍你）"。
用**流水表里的昵称缓存**兜底——catch-all 记录每条消息时顺手存了 `sender_name`，
按 `sender_id` 查最近一条即可：

```python
name = event.get_sender_name()
if not name or name == event.get_sender_id():
    name = await self.recorder.last_name_of(event.get_sender_id()) or event.get_sender_id()
event.message_str = f"（{name} 拍了拍你）"
```

发过言的人一定有缓存；从没发过言就退回 QQ 号，属于可接受的降级。

> 缓存表以 `sender_id` 为键、昵称只是值（`last_name_of(uid)`），**不要用昵称反查 uid**：
> 昵称可改、可重名，群友改个名字就能绕过配额或冒认身份。

### 3.3 拍一拍的目标判定用 `target_id()`

`components.py` 的 `BaseMessageComponent.target_id()` 已经内置了 `id → qq` 的回退
（跳过 None 和 `"0"`），直接调用即可，不需要自己写回退逻辑。

只响应"拍自己"：`comp.target_id() == event.get_self_id()`。

### 3.4 互拍不该触发

群友互拍产生的事件，`target_id` 是别人，直接忽略。这条和 3.3 是同一个判断。

### 3.5 名字唤醒的误伤

"只有叫到名字才回应"会误触发于：别人讨论她、复述别人的话、引用消息里带名字。
建议：

- 只在**消息正文**里找名字，扫引用文本要单独开关；
- 名字表可配置（默认 `鱼宝`、`大肥鱼`、`鱼呢`），中英文逗号都当分隔符；
- 命中后**不删**名字（删了"鱼宝你下周有空吗"会变成"你下周有空吗"，反而更怪）。

## 4. 配额（F12）

```sql
CREATE TABLE wake_quota (
  user_id TEXT NOT NULL,
  day     TEXT NOT NULL,   -- 本地日期 YYYY-MM-DD
  count   INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (user_id, day)
);
```

- 每次**通过唤醒判定**才 +1（被忽略的消息不计）。
- 管理员豁免：`self.context.get_config().get("admins_id", [])`（默认 `["astrbot"]`），
  比较时用字符串化的 `sender_id`。
- 超限：静默忽略（仍然记录），或按配置发一句"今天叫太多次啦"。**默认静默**，
  避免提示本身变成刷屏。
- 配额只在"唤醒"上生效，**不影响记录**——被限流的消息照样进流水。

## 5. 和"其余消息只当上下文"的配合

这是 catch-all 真正的价值所在：

- 没被唤醒的消息 → 只进 `message` 表。
- 被唤醒时 → `on_llm_request` 从 `message` 表取最近 N 条，拼进 `req.system_prompt`：

```
<recent_group_chat>
[10:31] 群友A：周末有人去爬山吗
[10:32] 群友B：我可能去
[10:33] 主人：鱼宝 我下周的安排是啥
</recent_group_chat>
```

这样模型既知道"刚才在聊什么"，也知道每句话是谁说的——正好补上 AstrBot 原生
历史不带归属的缺口（见 capabilities 第 8 节）。

## 6. 开关一览（建议写进 `_conf_schema.json`）

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `wake.on_poke` | true | 拍一拍唤醒 |
| `wake.on_name` | true | 名字唤醒 |
| `wake.names` | `["鱼宝","大肥鱼","鱼呢"]` | 名字表，支持中英文逗号分隔 |
| `wake.respond_to_others_poke` | false | 群友互拍是否响应 |
| `quota.enabled` | true | 是否启用每日配额 |
| `quota.daily` | 5 | 每人每天可唤醒次数 |
| `context.recent_messages` | 20 | 唤醒时注入的最近消息条数 |
