# 部署清单（SnowLuma + AstrBot + KahnBot）

> 活文档：随项目推进持续补。当前记录的是「同机测试」形态，最终目标机为另一台机器，两者步骤相同、只改绑定地址与本机路径。
> 最后更新：2026-11-11

## 0. 一页速查

| 组件 | 版本 | 端口 | 绑定 |
|---|---|---|---|
| SnowLuma | 2.4.x（release 包，Node 24+） | 5099 WebUI | 127.0.0.1 |
| SnowLuma | 同上 | 3000 OneBot HTTP | 127.0.0.1（本链路用不到） |
| SnowLuma | 同上 | 3001 OneBot WS Server | 127.0.0.1（本链路用不到） |
| AstrBot | 4.29.4（pip/uv 包） | 6185 Dashboard | **127.0.0.1**（默认 0.0.0.0，需改） |
| AstrBot | 同上 | 6199 aiocqhttp ws_reverse | 127.0.0.1（同机）；跨机才用 0.0.0.0 + token |
| KahnBot | 0.1.0 | — | 作为插件装在 AstrBot 内 |

关键路径（以 `ASTRBOT_ROOT` 指代 AstrBot 工作目录）：

```
<ASTRBOT_ROOT>/                     # astrbot run 的工作目录，data 跟着它走
├── .astrbot                        # 标记文件，astrbot init 生成
└── data/
    ├── config/                     # cmd_config.json 等
    ├── plugins/astrbot_plugin_kahnbot/   # 插件目录，名字必须是这个
    └── plugin_data/astrbot_plugin_kahnbot/  # 插件数据（记忆）
```

## 1. 前置条件

- OS：Windows x64 或 Linux x64/arm64（SnowLuma 只出这两个平台的发行包）。
- Node.js **24+**（仅 SnowLuma 需要；用发行包时由 launcher 自带的运行时决定）。
- Python **3.12**（AstrBot 要求；用 uv 装可免手动管理）。
- `uv`、`git`。
- 一个可登录的 QQ 账号（有被风控的可能，建议用小号先跑通）。
- 可访问外网的 LLM / Embedding API（记忆检索要用）。

## 2. 部署 SnowLuma

1. 从 [Releases](https://github.com/SnowLuma/SnowLuma/releases) 下载对应平台完整发行包，解压到一个独立目录。
2. 启动：Windows 双击 `launcher.bat`；Linux `chmod +x launcher.sh && ./launcher.sh`。
3. 打开 `http://127.0.0.1:5099`，**初始密码看启动日志**（首次启动时打印）。
4. 在 WebUI 里导入 / 登录 QQ 账号，等待账号显示在线。
5. 记下每个账号的 QQ 号（后面插件侧要用它认人）。

> 许可证：SnowLuma 是「源码可见、非商业自托管」许可，不是 OSI 开源。自用没问题，别做商用分发。

## 3. 部署 AstrBot

```bash
# 1) 装（隔离环境，Python 3.12）
uv tool install astrbot --python 3.12
# Windows 上若提示 hardlink 失败，可加：setx UV_LINK_MODE copy

# 2) 建一个专用工作目录（data 会生成在这里，别随手找个目录）
mkdir D:\astrbot && cd /d D:\astrbot

# 3) 初始化（生成 .astrbot 标记 + data/{config,plugins,temp}）
astrbot init

# 4) 启动（--reload 便于开发插件时热重载）
astrbot run --reload
```

首次启动日志里有 `Initial password: ...`，用它登录 `http://127.0.0.1:6185`。
Dashboard 默认绑 `0.0.0.0:6185`，目标机上若不需要外部访问，改配置为 `127.0.0.1`。

## 4. 对接：AstrBot ← SnowLuma

AstrBot 的 aiocqhttp 适配器是**反向 WebSocket 服务端**，SnowLuma 当客户端连进来。

AstrBot 侧（WebUI → 平台适配器 → 新增 → aiocqhttp）：

| 配置项 | 值 |
|---|---|
| `ws_reverse_host` | `127.0.0.1`（同机） |
| `ws_reverse_port` | `6199` |
| `ws_reverse_token` | 同机可留空；跨机必须设，并两边一致 |

SnowLuma 侧（WebUI → OneBot → 网络 → 新增 WS 客户端）：

| 配置项 | 值 |
|---|---|
| `url` | `ws://127.0.0.1:6199/ws` |
| `role` | `Universal` |
| `accessToken` | 与 AstrBot 的 `ws_reverse_token` 相同（留空则两边都不填） |

另需在 SnowLuma 关闭内置状态命令：`statusCommand.enabled = false`
（默认 true、触发词 `#sl`，不关会把版本/平台/运行时长暴露给群友）。

连接成功的标志：AstrBot 日志出现 `aiocqhttp(OneBot v11) 适配器已连接。`

## 5. 安装 KahnBot 插件

```bash
cd <ASTRBOT_ROOT>/data/plugins
git clone https://github.com/E4-Kahn/astrbot_plugin_kahnbot.git
```

目录名必须是 `astrbot_plugin_kahnbot`（与 `metadata.yaml` 的 `name` 一致，配置文件名也由它推导）。
然后在 AstrBot WebUI「插件管理」里点重载，或重启进程。

验证：

- 插件列表出现 KahnBot，无加载报错；
- 私聊发 `/kahn`，返回版本/状态四行文本。

## 6. 模型配置（会话模型 + 嵌入模型）

两类模型职责不同，都要配：

| 用途 | 类型参数 | 说明 |
| --- | --- | --- |
| 说话、汇总、主动消息生成 | 会话模型（chat） | 任选一家，AstrBot 里已有则不用动 |
| 记忆检索的向量化 | `openai_embedding`（OpenAI API Embedding 适配器） | bge-m3 只做向量，不负责说话 |

### 6.1 嵌入模型（bge-m3）

- AstrBot WebUI → 服务提供商 → 新增，类型选 **OpenAI API Embedding 提供商适配器**：
  - API Base：`https://api.siliconflow.cn/v1`
  - 模型：`BAAI/bge-m3`（维度 1024）
  - API Key：自备
- 本地离线替代：类型 `ollama_embedding`，Base `http://localhost:11434`，模型 `bge-m3`。
- 插件配置 `embedding_provider_id` 留空 = 自动取第一个启用的 embedding provider，不必手填 UUID。

### 6.2 会话模型（OpenCode Go 特殊点）

OpenCode Go 要求请求带 **`x-opencode-session`**（每个会话一个稳定 ID），否则报
`400 Request is missing x-opencode-session`；还要求 User-Agent 能标识客户端（AstrBot 默认发
`astrbot/<version>`，已满足）。

- 在 provider source 的「自定义请求头」里加：`x-opencode-session: <固定 UUID>`（一个机器人一个值即可）。
- **实测可用的配置（2026-11-11，本机 `D:\workspace\Astrbot`）**：

  ```
  provider_source.type = openai_responses
  api_base             = https://opencode.ai/inference/go/v1/
  model                = deepseek-v4.1-flash
  custom_headers       = {"x-opencode-session": "<UUID>"}
  ```

  即网关自己按 model 路由到 chat completions / responses，**不需要**在模型名上区分端点类型。

- 注意：官方端点表（Endpoints 一节）写的是 `.../go/openai/v1/chat/completions`、
  `.../go/openai/v1/responses` 这种按 SDK 包分开的地址，**与实际网关不一致**（文档滞后），
  以实测为准；只有在 `.../go/v1/` 上遇到 404 时，才回头试 `.../go/openai/v1`。
- 出处：<https://opencode.ai/docs/console/go/> 的 "Where can I use it?" 一节（请求头要求）。
  其中 "Endpoints" 表格里的地址按实测判断是错的。

## 7. 联调验收清单

| # | 场景 | 期望 |
|---|---|---|
| 1 | 私聊发 `/kahn` | 返回状态 |
| 2 | 群里 @ 机器人说话 | 正常回复 |
| 3 | 群里不 @、只喊名字 | 触发回复（F10） |
| 4 | 群友拍一拍机器人 | 触发回复，文案带昵称而非 QQ 号 |
| 5 | 群里普通闲聊 | 不回复，但进流水（F11） |
| 6 | 群里发 `#sl` | 不回复，也不进流水（F13） |
| 7 | 同一人当天唤醒超 N 次 | 静默忽略，仍记录 |
| 8 | 群里说"记住…"后换群问 | 能检索到（F3 全局共享） |
| 9 | 私聊说的事在群里被问 | 不出现（F9 隐私） |
| 10 | 改昵称后再说话 | 记忆归因不变（按 QQ 号） |

## 8. 运维

- 备份：`<ASTRBOT_ROOT>/data/`（含插件数据）与 SnowLuma 的 `data/`、`onebot*.json`。
- 升级 AstrBot：`uv tool upgrade astrbot`；升级插件：`git pull` 后重载。
- 卸载插件：删 `data/plugins/astrbot_plugin_kahnbot`，数据在 `plugin_data/` 下可另行保留。

## 9. 待确认 / 待补

- [ ] 目标机的 OS 与网络（是否有公网 IP、是否走代理）。
- [ ] QQ 账号风控策略（是否需要固定 IP、是否用手机扫码登录）。
- [ ] AstrBot 版本下限是否需从 4.24.0 抬高（取决于用到的钩子，见 `refs/astrbot-capabilities.md` 第 11 节）。
- [ ] SnowLuma 与 AstrBot 的消息格式需同为 `array`。
