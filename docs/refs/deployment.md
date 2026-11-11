# 部署拓扑与配置评审（SnowLuma + AstrBot）

> 定稿日期：2026-11-11。本文只记录**运行环境**结论，不涉及插件代码。

## 1. 拓扑

```
QQ ←→ SnowLuma（OneBot v11 实现，TS）
        ↕ 反向 WebSocket（SnowLuma 作为客户端主动连）
     AstrBot（aiocqhttp 适配器，ws_reverse 服务端）
        ↕
     astrbot_plugin_kahnbot
```

AstrBot 的 aiocqhttp 适配器固定 `use_ws_reverse=True`
（`core/platform/sources/aiocqhttp/aiocqhttp_platform_adapter.py:55-60`），
即 **AstrBot 是服务端，SnowLuma 是客户端**。因此 SnowLuma 的 `wsServers`
（3001）和 `httpServers`（3000）都**不参与**这条链路。

## 2. 端口核对

| 端口 | 用途 | 用户配置 | 结论 |
|---|---|---|---|
| 3000 | SnowLuma OneBot HTTP | `127.0.0.1` | 与默认一致（`packages/onebot/src/config.ts:52-61`）；本链路用不到，绑定本机无害 |
| 3001 | SnowLuma OneBot WS Server | `127.0.0.1` | 与默认一致（`config.ts:66-74`）；用不到，同上 |
| 5099 | SnowLuma WebUI | `127.0.0.1` | 正确，SnowLuma 默认 5099（`packages/core/src/index.ts:110`） |
| 6199 | AstrBot `ws_reverse` | `0.0.0.0` | 与 AstrBot 默认一致（`core/config/default.py:376-377`）；见 §3 的风险 |

连接地址：`ws://127.0.0.1:6199/ws`，SnowLuma 侧 `wsClients` 的 `role` 用 `Universal`。
aiocqhttp 的反向 WS 端点就是 `/ws`（`aiocqhttp/__init__.py:140`）。

## 3. 唯一需要改的：6199 的暴露面

`ws_reverse_token` 默认是空串（`core/config/default.py:378`），而 aiocqhttp
**只有在 token 非空时才校验**（`aiocqhttp/__init__.py:493-504`，校验
`Authorization: Bearer <token>` 与 `X-Client-Role`）。

于是 `0.0.0.0:6199` + 空 token = **局域网内任何人都能冒充 OneBot 端连上来**，
后果不是"发错消息"而是：可伪造任意用户/群的消息进入 KahnBot 的记忆与流水。
对一个以"记人"为核心的插件，这等于记忆可被投毒。

两个方案，择一：

- **同机部署（推荐）**：`ws_reverse_host` 改 `127.0.0.1`。SnowLuma 与 AstrBot
  在同一台机器时完全够用，外部不可达。
- **必须跨机**（SnowLuma 在另一台机器 / WSL / 容器）：保留 `0.0.0.0`，但
  AstrBot 填 `ws_reverse_token`，SnowLuma 的对应 `wsClients` 项填同一个
  `accessToken`（客户端会发 `Authorization: Bearer`，见
  `packages/onebot/src/network/ws-client-adapter.ts:162-169`），
  再用系统防火墙只放行 SnowLuma 那台机器。

> SnowLuma 会主动发 `X-Self-ID` 与 `X-Client-Role`（`ws-client-adapter.ts:163-165`），
> 与 aiocqhttp 的要求对得上，两端握手无兼容问题。

## 4. `statusCommand.enabled=false` 是对的

- 默认 `enabled: true`、`trigger: '#sl'`（`packages/onebot/src/config.ts:38`、`:80`）。
- 命中后 SnowLuma 自己回版本/平台/运行时长；若 `swallow: true` 还会**不再转发**给下游
  （`packages/onebot/src/instance.ts:453`、`:526-560`）。
- 关掉后 `#sl` 会当普通群消息转发给 AstrBot，被 catch-all 记进流水——无害但属噪音，
  插件侧可在记录时按固定字符串过滤。

**结论（用户 2026-11-11 拍板）**：两头都做——SnowLuma 关 `statusCommand`，
插件侧记录时丢弃 `#sl`（见 `REQUIREMENTS.md` F13）。理由：即使 SnowLuma 关了，
没有过滤时噪音仍会进流水，甚至可能被定时汇总误当对话内容。

## 5. 启动顺序

1. 先起 AstrBot（`:6199` 开始监听）。
2. 再起 SnowLuma（客户端重连）。
3. 反过来也能连上，只是 SnowLuma 侧会先报几秒重连失败，属正常。

## 6. 待核实

- Windows 上 AstrBot 绑 `0.0.0.0` 首次会弹防火墙授权，需确认放行范围。
- SnowLuma 与 AstrBot 同时开启"消息格式化 array"两边是否一致（都要求 `message_format: array`）。
