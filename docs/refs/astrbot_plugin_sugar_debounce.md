# astrbot_plugin_sugar_debounce（蔗糖的消息防抖）调研笔记

- 本地素材：`D:\workspace\refs\astrbot_plugin_sugar_debounce`
- 版本：1.0.3（`metadata.yaml`），author SucRoseZT，`astrbot_version: ">=4.25,<5"`
- 规模：`main.py` 688 行 / 28.9KB，`_conf_schema.json` 3.4KB，README 69 行，CHANGELOG 3.9KB
- 研究目的：用户提出"消息防抖"需求（F23），看它怎么把连续短句合成一次模型调用

## 1. 它解决什么

用户连发几条（"我想买个" + "键盘"、图 + 配文）时，机器人对每条各回一次，回复前言不搭后语。
它把窗口内到达的多条消息**合并成一条**再交给模型，只回一次。

## 2. 机制

- **捕获点**：只用 `@filter.on_waiting_llm_request(priority=1000)`（`main.py:136`），
  即"这条消息确实要进 LLM"时才介入；另有 `on_llm_request` / `on_using_llm_tool` /
  `on_decorating_result` 三个辅助钩子（`:185`、`:191`、`:205`）。
- **聚合键**：`_conversation_key(event)`（`:529`）= `f"{umo}|{scope}|user:{sender_id}"`，
  同一会话同一用户的消息进同一个 `Burst`（`:42`）。
- **合并**：`_capture`（`:215`）把新消息并入 burst 并改写最新 event 的 `message_str` /
  `message_obj.message`（`_apply_merged` `:300`）；上一条 event 被标记 `stale`，
  其 pipeline task 收到 `cancel()`，不会真的去调模型。
- **两种节奏**（`:158-182`）：`_delay_ms()`（`:378-381`）——含图用 `image_debounce_ms`
  （默认 1000），纯文字用 `debounce_ms`（默认 0，即"首条立刻进模型"）。
  延迟为 0 时靠"新消息到达就取消上一轮重新生成"实现合并，首字不卡。
- **与核心 follow-up 机制抢消息**：AstrBot 4.29+ 会在机器人回复期间把用户消息截成"追加回合"。
  它 monkeypatch 了 `astrbot.core.pipeline.process_stage.method.agent_sub_stages.internal.try_capture_follow_up`
  （核心在 `internal.py:203` 调，`:226` 才到 `on_waiting_llm_request`），让核心在防抖活跃时让路。
- **图片快照**：收到图立刻按 sha1 落到 `data/plugin_data/<插件>/media`，900 秒未被引用则清理——
  因为原消息被取消后平台临时文件可能已经没了。

## 3. 坑（作者自己踩过的）

- 早期用 `@filter.regex` 通配捕获所有消息，导致 `is_wake` / `is_at_or_wake_command` 对每条消息置真，
  群聊里每条都尝试回复，还污染其它插件的分流判定（`main.py:126-134` 的长注释）。
  **这条对我们直接有用**：catch-all 必须用 `event_message_type` 过滤，不能顺手置唤醒位。
- 取消 pipeline task 时 `context_utils` 用 `except BaseException` 记 ERROR + Traceback，
  所以要自己接 `CancelledError` 并判断是不是"自己取消的"（`:164-173`）。
- stale 事件的 `result.chain` 要清空，否则后置阶段还可能把上一条的回复发出去（`:205-211`）。
- 两个防抖插件同时开必然打架（README:69）。

## 4. 我们不打算照抄的原因

它的输入是"已经决定要进 LLM 的消息"——它在**模型调用前**合并，代价是要能取消已经开跑的
pipeline task、要处理 stale 结果、要和核心 follow-up 抢消息。

KahnBot 的闸门在更前面：一条群消息要不要唤醒、要不要回，是我们在 catch-all handler 里
自己决定的（`is_at_or_wake_command`）。所以更简单的位置是**唤醒侧**：
在决定"要回"之后不立刻置位，而是等一小段静默窗口，确认没有后续消息再置位。
不需要取消任何东西，也不需要 monkeypatch 核心。

## 5. 可借鉴的点

- 纯文字默认 0 延迟、靠"取消重生成"合并的思路——我们做不到（我们还没开始生成），但可以用
  静默窗口近似，且只在"已被唤醒"的会话上开窗口，未唤醒的群消息不受影响。
- 图片要快照才能安全合并：如果以后要合并图片消息，这一点必须抄。
- `ignore_prefixes`（`/`、`!` 开头的命令不合并）：我们的 `/kahn` 指令也必须绕开窗口。
- 上限：`max_messages` / `max_chars` / `max_images`，防止用户刷屏把窗口无限拖长。

## 6. 待拍板

见 `docs/REQUIREMENTS.md` 的 Q13：窗口时长、@/拍一拍是否参与、图片是否合并、
是否改写 `message_str`（还是只靠 `<recent_group_chat>` 注入补齐前几句）。
