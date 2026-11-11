# AstrBot 侧能力勘察（KahnBot 可用接口）

> 目的：把「AstrBot 4.29.4 到底提供了什么」固定成事实，供写需求时引用。
> 结论性需求不进这里，进 `docs/REQUIREMENTS.md`。
> 代码引用格式 `文件:行号`，路径相对 `site-packages/astrbot/`。

## 0. 版本基线

- `metadata.yaml` 当前声明 `astrbot_version: ">=4.29.4"`（2026-11-12 从 `>=4.24.0` 抬起）。
- 实测环境是 **4.29.4**（`astrbot --version`）。
- 本文所有接口都在 4.29.4 上核对过，**未逐一确认 4.24.x 是否存在**——这也正是把下限抬到
  4.29.4 的原因：只对核对过的版本承诺兼容。

## 1. 钩子全集（`core/star/register/star_handler.py`）

| 装饰器 | 行号 | 用途 |
|---|---|---|
| `command` / `command_group` | 76 / 209 | 指令 |
| `regex` | 321 | 正则触发 |
| `event_message_type` | 263 | 按消息类型（群/私聊/@ 等） |
| `platform_adapter_type` | 301 | 按平台 |
| `permission_type` | 336 | 权限 |
| `on_astrbot_loaded` / `on_platform_loaded` | 361 / 371 | 生命周期 |
| `on_plugin_loaded` / `on_plugin_unloaded` | 399 / 416 | 插件生命周期 |
| `on_plugin_error` | 381 | 异常 |
| `on_waiting_llm_request` | 433 | LLM 请求前置（可拦截/等待） |
| **`on_llm_request`** | **457** | **每轮 LLM 请求，可改 `req`** |
| `on_llm_response` | 480 | LLM 返回后 |
| `on_agent_begin` / `on_agent_done` | 503 / 531 | Agent 循环起止 |
| `on_using_llm_tool` / `on_llm_tool_respond` | 561 / 585 | 工具调用前后 |
| **`llm_tool`** | **610** | **注册 function-calling 工具** |
| `agent` | 721 | 自定义 Agent |
| `on_decorating_result` | 754 | 结果装饰（发消息前） |
| `after_message_sent` | 768 | 消息已发送 |

## 2. 请求注入点（记忆/状态注入的落点）

`core/star/register/star_handler.py:457` 的文档示例即契约：

```py
@on_llm_request()
async def h(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
    req.system_prompt += "..."
```

`ProviderRequest` 定义在 `core/provider/entities.py:194`，关键字段：

| 字段 | 含义 |
|---|---|
| `prompt` | 本轮用户输入 |
| `system_prompt` | 系统提示词（**可直接追加**） |
| `contexts` | OpenAI 格式历史消息列表 |
| `conversation` | `Conversation` 对象（含 `cid`、`history`） |
| `session_id` | 会话 ID |
| `func_tool` | 可用工具集 `ToolSet`，可 `add_tool` |
| `extra_user_content_parts` | 用户消息后的附加内容块 |

⇒ 状态卡 / 召回片段都可以在 `on_llm_request` 里追加到 `system_prompt`，**不需要改消息本身**。

## 3. 工具注册（`llm_tool`）

- `register_llm_tool(name=None, **kwargs)`，`name` 省略则用函数名（`core/star/register/star_handler.py:610`）。
- 参数说明**从 docstring 解析**（Google 风格 `Args:`，示例见该函数文档）。
- 首个参数是 `event: AstrMessageEvent`，其余参数即工具入参。
- ⇒ 检索式记忆可做成 `memory_search(query, ...)` 工具，由模型自主决定调用。

## 4. 会话与历史（**已有持久化，不必自建流水**）

- `ConversationManager`（`core/conversation_mgr.py:19`），按 `event.unified_msg_origin` 索引。
- 方法：`new_conversation:92`、`switch_conversation:126`、`delete_conversation:139`、
  `delete_conversations_by_user_id:160`、`get_curr_conversation_id:174`、
  `get_conversation:190`、`get_conversations:216`、`update_conversation:279`、
  `add_message_pair:357`、`get_human_readable_context:392`。
- `conversation.history` 是 **OpenAI 格式 JSON 字符串**，组装请求时 `req.contexts = json.loads(conversation.history)`（`core/astr_main_agent.py:1521`、`:1715`；`core/astr_agent_tool_exec.py:623`）。
- Agent 结果用 `utils/history_saver.py::persist_agent_history` 回写。

**含义**：原始对话 AstrBot 已经按会话存好了。KahnBot 的记忆层更适合做「提炼后的长期事实 + 检索」，而不是再存一份逐轮流水（否则同一份数据两份真相）。

## 5. 身份维度（隔离用什么键）

`core/platform/astr_message_event.py`：

| 方法 | 行号 | 说明 |
|---|---|---|
| `unified_msg_origin` | 158 | 会话级唯一键（平台+会话） |
| `get_platform_name` | 178 | 平台名 |
| `get_session_id` | 261 | 会话 ID |
| `get_group_id` | 265 | 群 ID（私聊为空） |
| `get_sender_id` | 273 | 发送者 ID |
| `get_sender_name` | 280 | 发送者昵称 |
| `is_private_chat` | 330 | 是否私聊 |

⇒ 四种天然隔离键都在手边：平台 / 会话(群) / 用户 / 私聊-群聊区分。具体选哪个是需求问题，不是技术限制。

## 6. 内置工具可参考

- `GetExecutionHistoryTool`、`GetGroupMessageHistoryTool`、`SendMessageToUserTool`（`core/astr_main_agent.py:70`、`:94`）。
- 群消息历史受配置 `group_message_history_enable` 控制，**默认 False**（`astr_main_agent.py:1928`）。

⇒ AstrBot 自己就把「翻历史」做成了工具而非自动注入，这个取舍值得参照（与 dsh-memory-keeper 的"模型自主检索"一致）。

## 7. 上下文压缩（对应 dsh 的"压缩后补原文"）

`core/astr_main_agent.py:219-227` 的配置族：

- `context_limit_reached_strategy`：`"truncate_by_turns"`（默认）/ `"llm_compress"`（`core/config/default.py:4175`）。
- `llm_compress_keep_recent_ratio`：默认 `0.15`，被钳在 `[0, 0.3]`（`core/agent/context/compressor.py:144`）。
- `llm_compress_instruction`、`llm_compress_provider_id`。

执行体：`ContextManager`（`core/agent/context/manager.py:11`），`process()` 在 `:46`，
**每一步 agent 循环、每次调 provider 之前**都会跑一遍（调用点 `core/agent/runners/tool_loop_agent_runner.py:831`，
日志 `[BefCompact]`/`[AftCompact]` 在 `:829`/`:840`）。

**钩子与压缩的先后顺序（已核实）**：

1. `OnLLMRequestEvent` 在 `core/pipeline/process_stage/method/agent_sub_stages/internal.py:307`
   （第三方路径 `third_party.py:333`）派发。
2. 之后才进入 runner 循环，循环里才有 `request_context_manager.process(...)`。

⇒ **插件在 `on_llm_request` 里看到的 `req.contexts` 是压缩前的原文**。

压缩行为细节（`manager.py:46-156`）：

- 先按轮次截断（`enforce_max_turns != -1` 时）。
- 超 `max_context_tokens` 才压缩；压缩后仍超限则**整轮丢弃最旧的**，但**永远保留最新一轮**。
- **system 消息不参与裁剪**（`:129-133` 挑出 `role == "system"` 段原样保留）。

⇒ 往 `system_prompt` 注入的内容**不会**被压缩吃掉。

⚠️ **没有任何插件可监听的事件表示"刚刚压缩了"**——全库只有 `manager.py:102/111/123` 三处日志。
所以 dsh-memory-keeper 那种"监听 compaction/summary 事件、把被摘要掉的原文补回来"的机制
**无法直接复刻**；要做得自己维护一份原文副本再和 `req.contexts` 做差集。

## 8. 说话人归属（用户踩过的坑，已核实）

### 8.1 `provider_settings.identifier`

- 默认 **False**（`core/config/default.py:156`）。
- 开启后的唯一效果（`core/astr_main_agent.py:999-1002`、`:1028-1032`）：
  在 `extra_user_content_parts` 追加
  `<system_reminder>User ID: {user_id}, Nickname: {nickname}</system_reminder>`，
  取 `event.message_obj.sender.user_id/nickname`。
- ⚠️ **它只标注"当前这条消息"是谁说的**，不给历史消息加名字。
  `contexts` 里存的是裸文本 + role，多人混在同一个 `user` 角色里。
- ⇒ "分不清谁在说话"是既有默认行为，不是 bug；**而且即使打开 identifier，翻历史时仍然分不清**。

### 8.2 带归属的历史表

`PlatformMessageHistoryManager`（`core/platform_message_history_mgr.py:7`）保存
`sender_id` / `sender_name`，按 (platform_id, user_id=umo) 分区，`max_messages` 控制保留条数。
写入方是内置 Star `astrbot`（`builtin_stars/astrbot/main.py:142-182`），**受配置门控**：
`provider_ltm_settings.group_message_history_enable`（默认 `False`，同文件 `:158`），且**只存群消息**。

## 9. 唤醒机制（决定"能不能收到所有群消息"）

`core/pipeline/waking_check/stage.py`：

- **任何插件 handler 的 filter 通过都会置 `event.is_wake = True`**（`:243-244`）。
- 但 `is_at_or_wake_command` **只有** @、`wake_prefix` 前缀、私聊、部分平台适配器才置真（`:124`、`:151`、`:164`）。
- 真正决定"要不要走 LLM"的是 `is_at_or_wake_command`（`process_stage/stage.py:68`、`star_request.py:117`）。

⇒ **注册 catch-all handler（如 `event_message_type(GROUP_MESSAGE)`）可以收到所有群消息，
而不会让机器人对每条消息都回答。** 这是"全都记"在技术上可行的依据。

结构上的直接证据（`core/pipeline/process_stage/stage.py`）：

```python
# :43-60  只要有 handler 被激活就执行——与 is_at_or_wake_command 无关
activated_handlers = event.get_extra("activated_handlers")
if activated_handlers:
    async for resp in self.star_request_sub_stage.process(event):
        ...

# :63-76  这里才是"是否自动回复"的闸门
if (not event._has_send_oper and event.is_at_or_wake_command and not event.call_llm):
    async for _ in self.agent_sub_stage.process(event):
        yield
```

两段是**先后并列**的：前一段不受唤醒条件约束，后一段才受。所以"记录"和"回复"
在架构上就是分开的两件事。

`EventMessageTypeFilter`（`core/star/filter/event_message_type.py:24-33`）只比对消息类型，
不涉及唤醒状态 ⇒ 每条群消息都会通过。

### 9.1 优先级语义

`core/star/star_handler.py:26` 用 `key=lambda h: -h.extras_configs["priority"]` 排序，
即**数值越大越先执行**。内置 `persist_group_message` 用 `maxsize - 2` 抢在最前（`builtin_stars/astrbot/main.py:139-141`）。

⇒ KahnBot 若要"先记录、再让别的 handler 处理"，需要给较高的 `priority`。

## 10. 对 KahnBot 的事实性结论（不含需求决策）

1. **不需要自建对话流水**——`history` 已在会话里，按 `unified_msg_origin` 取。
2. **注入点唯一且干净**——`req.system_prompt`，与消息内容解耦。
3. **检索的两种形态技术上都可行**——`llm_tool`（模型自主）或 `on_llm_request`（每轮自动），选哪个是产品决策。
4. **隔离维度不受限**——四种键都可用，等需求定。
5. **注入的内容不会被压缩掉**——压缩只裁 `contexts`，system 消息原样保留。
6. **"压缩后补原文"在 AstrBot 上要重新设计**——没有压缩事件，只能自存副本做差集，成本高。
7. **说话人归属必须自己做**——`identifier` 只管当前发言人；历史归属要靠插件在收消息时自己记
   （handler 里 `event.get_sender_id()` / `get_sender_name()` 可用）。

## 11. 待核实

- [ ] `on_llm_request` / `llm_tool` 最早出现在哪个版本 → 决定 `astrbot_version` 下限。
      只在 4.29.4 上实测过；保守做法是把下限抬到 `>=4.29.4`（待拍板）。
- [x] `data/plugin_data/<name>/` 的并发：M1 起开 WAL + 单连接串行写，`state.json` 用原子替换；
      多群并发写入的极端情况仍未压测。

## 12. Embedding Provider（给向量检索用）

AstrBot 有统一的向量化抽象，插件可以直接拿到用户在面板里配好的模型，不必自带模型文件。

```python
from astrbot.core.provider.provider import EmbeddingProvider

ep = self.context.get_provider_by_id(provider_id)      # context.py:456，返回可能是任意 provider 类型
all_eps = self.context.get_all_embedding_providers()   # context.py:499-501
vec = await ep.get_embedding(text)                     # 单条
vecs = await ep.get_embeddings_batch(texts)            # 批量，内部并发 + 重试
dim = ep.get_dim()
```

基类 `EmbeddingProvider` 在 `core/provider/provider.py:329`，方法 `get_embedding` / `get_embeddings` /
`get_embeddings_batch(batch_size=16, tasks_limit=3, max_retries=3, progress_callback=None)` / `get_dim` / `test`。

内置实现（`core/provider/sources/`）：

| 文件 | 用途 |
| --- | --- |
| `openai_embedding_source.py:26` | OpenAI 兼容接口；**SiliconFlow 的 `BAAI/bge-m3` 用它**（改 base_url + key） |
| `ollama_embedding_source.py:15` | 本地 Ollama，`bge-m3` 可完全离线 |
| `dashscope_embedding_source.py:22` / `gemini_embedding_source.py:17` / `nvidia_embedding_source.py:15` | 其他云端 |

⇒ 插件侧只需要一个 **Embedding Provider ID** 配置项。换模型、换供应商都不用改代码。

## 13. 拍一拍（Poke）

- 组件存在于 `core/message/components.py:755`（`Poke`，`type` 默认 `"126"`），
  在组件注册表 `:1082` 里映射为 `"poke"`。
- aiocqhttp 适配器把 OneBot 的 notice 事件转成消息事件，并塞一个 `Poke` 组件：
  `aiocqhttp_platform_adapter.py:192-194`（`sub_type == "poke"` 且有 `target_id`）。
  注意此时 `message_str` 是**空字符串**（`:186`），不能靠文本判断。
- **拍一拍不算唤醒**：`is_at_or_wake_command` 不会因它置真（全库检索无相关分支），
  所以 AstrBot 不会自动让 LLM 回应拍一拍。
- 响应侧支持对拍一拍回戳：`core/pipeline/respond/stage.py:56`（`Comp.Poke` 允许回复）。
  注意这里的 `Comp` 是 `core` 内部的模块别名（`import astrbot.core.message.components as Comp`），
  **插件里没有这个符号**，要写 `from astrbot.api.message_components import Poke` 再用 `isinstance`。

⇒ KahnBot 可以在 handler 里检查 `event.get_messages()` 里是否有 `Poke` 组件，
自己决定"当作唤醒"还是"只记录"。

## 14. 写代码前逐条核对过的 API（易错点）

这一节是实现阶段的"防踩坑清单"，每一条都在 4.29.4 源码里确认过。

### 14.1 优先级不是装饰器，是关键字参数

**没有 `@filter.priority(...)` 这个函数**：`astrbot/api/event/filter/__init__.py:46-78`
的 `__all__` 里没有 priority。

优先级走的是「任意 `register_*` 装饰器的 `**kwargs` → `md.extras_configs`」：

- `register_event_message_type(event_message_type, **kwargs)`（`core/star/register/star_handler.py:263-275`）
  把 `**kwargs` 原样交给 `get_handler_or_create`，后者把它存成 `md.extras_configs`（`:69`）。
- 默认值 0 是 registry.append 时才补的（`core/star/star_handler.py:21-22`）。
- 排序：`self._handlers.sort(key=lambda h: -h.extras_configs["priority"])`（`:26`）⇒ **数值越大越先执行**。

写法：

```python
@filter.event_message_type(EventMessageType.GROUP_MESSAGE, priority=100)
async def on_group_message(self, event: AstrMessageEvent): ...
```

**坑（很重要）**：`get_handler_or_create` 先按 `handler_full_name`（`模块_函数名`）查已有
metadata，命中就直接返回（`core/star/register/star_handler.py:50-53`）；`extras_configs`
只在**创建那一刻**赋值。装饰器自下而上执行，所以 **`priority=` 只有写在最内层（最先执行）
的那个装饰器上才生效**——把它拆成两个装饰器、或者写在上面那层，优先级会被静默丢弃。

### 14.2 钩子方法的绑定方式

`metadata.star_cls` 存的是**实例**（`star_manager.py:1230-1241`），注册时

```python
handler.handler = functools.partial(raw_handler, metadata.star_cls)   # :1268-1279
```

调用是 `await handler.handler(event, *args, **kwargs)`（`core/pipeline/context_utils.py:119`），
所以照常写 `async def on_llm_request(self, event, req)` 就行，不用 `@staticmethod`。

### 14.3 中止钩子要调 `event.stop_event()`

`call_event_hook` **不读 handler 的返回值**（`core/pipeline/context_utils.py:78-154`），
它只看 `event.is_stopped()`（`:148-154`）。而调用点写的是

```python
if await call_event_hook(event, EventType.OnLLMRequestEvent, req):
    return        # core/pipeline/process_stage/method/agent_sub_stages/internal.py:307
```

⇒ 想在 `on_llm_request` 里**阻止这次 LLM 请求**，必须 `event.stop_event()`；`return True` 无效。

### 14.4 导入路径

- `EventMessageType` 在 `astrbot.api.event.filter`（`filter/__init__.py:3-6`），
  **不在** `astrbot.api.event`（那里只导出 `AstrMessageEvent` / `AstrMessageEvent` 系列事件类）。
- 组件：`from astrbot.api.message_components import Poke, Plain, At`；`Comp` 只是 core 内部别名。
- `EmbeddingProvider` 在 `astrbot.core.provider.provider:329`。

### 14.5 Embedding Provider 的确切签名

| 成员 | 位置 | 签名 |
| --- | --- | --- |
| `get_embedding` | `core/provider/provider.py:336` | `async def get_embedding(self, text: str) -> list[float]` |
| `get_embeddings` | `:341` | `async def get_embeddings(self, text: list[str]) -> list[list[float]]` |
| `get_embeddings_batch` | `:353` | `async def get_embeddings_batch(self, text, batch_size=16, tasks_limit=3, max_retries=3, progress_callback=None)` |
| `get_dim` | `:346` | `def get_dim(self) -> int`（**同步**） |

`context.get_provider_by_id(provider_id)` 在 `core/star/context.py:456-485`，内部
`inst_map.get(...)`，**取不到只打 warning 并返回 None**，调用方必须判空。

### 14.6 数据目录

`StarTools.get_data_dir()`（`core/star/star_tools.py:244-298`）无参调用时沿调用栈找
本插件的 `star_map` 记录反查名字，`mkdir` 后返回 `resolve()` 过的 `Path`。
⇒ 在 `__init__` 里拿一次存成 `self.data_dir` 即可，不要在 handler 里反复调。

### 14.7 闸门与抑制默认 LLM

- 闸门（`core/pipeline/process_stage/stage.py:66-70`）：
  `not event._has_send_oper and event.is_at_or_wake_command and not event.call_llm`。
- `event.call_llm` 默认 `False`（`astr_message_event.py:145`），由 `should_call_llm(flag)` 设置
  （`:442-447`）；名字反直觉——`True` 表示**禁止**默认 LLM 链路。
- `event.get_self_id()` 在 `astr_message_event.py:269`。
- `EventMessageType` 是 `enum.Flag`（`GROUP_MESSAGE` / `PRIVATE_MESSAGE` / `OTHER_MESSAGE` / `ALL`，
  `core/star/filter/event_message_type.py:10-14`），filter 只比对 `event.get_message_type()`（`:28-32`）。

## 15. 注入位置与前缀缓存（2026-11-11 补，来自 fish_heart）

### 15.1 两个可写位置

| 位置 | 字段 | 生效时机 | 代价 |
| --- | --- | --- | --- |
| 系统提示 | `req.system_prompt` | 每次请求开头 | 每轮变化 ⇒ 其后所有历史按未命中重算 |
| 额外用户内容 | `req.extra_user_content_parts` | 用户消息之后 | 只让尾部变化 |

`ProviderRequest.extra_user_content_parts` 是 `list[ContentPart]`
（`core/provider/entities.py:203`），组装时**排在用户原始发言之后**
（`entities.py:301-316`：先 `prompt` 文本块，再逐个 part）。

### 15.2 为什么 system_prompt 影响前缀缓存

DeepSeek / Anthropic 等按"从开头到第一个变化点"命中缓存前缀。`system_prompt`
在消息序列最前面，它一变，后面整段历史全部失效；价格差约 10 倍
（fish_heart 注释 `main.py:3510-3519` 有同样的判断）。

### 15.3 `mark_as_temp()` 不能省

```python
from astrbot.core.agent.message import TextPart   # 注意：不是 astrbot.api.message_components
req.extra_user_content_parts.append(TextPart(text=body).mark_as_temp())
```

> `astrbot.api.message_components` 只是 `from astrbot.core.message.components import *`
> 的转发（整个文件就这一行），里面是 `Plain`/`At`/`Poke` 这类**消息组件**；
> `TextPart` 属于**内容块**（`core/agent/message.py:80`），两者不是一套东西。

- `ContentPart._no_save: bool`（`core/agent/message.py:25`），`mark_as_temp()` 置 True（`:68-71`）。
- 回存历史时跳过：`internal.py:518-524`（`if message.role in ["assistant","user"] and message._no_save: continue`）。
- 不调用 ⇒ 注入文本每轮累积进会话历史，越聊越长，且会进下一次的 `req.contexts`。

### 15.4 对 KahnBot 的含义

- 记忆注记与最近群聊，**默认都挂 `extra_user_content_parts`**；
  降级时才写 `system_prompt`（功能不受影响，只是缓存吃亏）。
- 纯静态内容（如"你是 KahnBot 的 …"）可以留在 `system_prompt`，因为它不逐轮变。
- 注记带上时间戳会逐轮变化，本来就不适合放系统提示。

## 16. 图片消息链路（2026-11-12 实测确认）

实测：SnowLuma 私聊发图 → 回复里描述了图中内容（“这只猫的表情好到位，汗都下来了”），
说明 OpenCode Go 的 `deepseek-v4.1-flash` 确实能看图，模型能力里的 `image` 声明是对的，不需要改配置。

事实（源码 + 落库数据双向确认）：

- 适配器只把 **text 段**累加进 `message_str`（`core/platform/sources/aiocqhttp/aiocqhttp_platform_adapter.py:231-252`）；
  图片段走通用分支 `ComponentTypes[t](**m["data"])` 变成 `Image` 组件（`:405-422`）。
  ⇒ **纯图消息 `event.message_str == ""`**，图只在 `event.message_obj.message` 里。
- AstrBot 组请求时会补上文字占位：落库的会话里，这条 user 轮的第一个 text 是 `[图片]`，
  后面跟 `[Image 1: original path <本地路径>]`（`core/utils/image_input.py:147-187` 生成），
  再加一个真实的 `image_url`（base64 data URL，`core/provider/sources/openai_source.py:186-209`）。
- 图片先下载到 `data/temp/media_image_<时间戳>_<随机>.jpg`；`core_lifecycle.py:319`
  有 `temp_dir_cleaner_task` 定期清理，**不能指望它长期存在**。
- 发给不支持视觉的模型时，`prepare_request_images(..., supports_image=False)` 会把视觉部分丢掉
  （`image_input.py:147-148`），只留路径提示文字。

对 KahnBot 的含义：

- recorder 用 `event.message_str` 记录 ⇒ 纯图必然为空。占位符应与之对齐：
  `message_str` 为空但含 `Image` ⇒ 记 `[图片]`，与模型在历史里看到的一致。
- 若以后要让“窗口内单独发的图”也参与本次回复，必须在**收到时**把 `data/temp` 里的文件快照到插件目录，
  否则请求时文件可能已被清理。
