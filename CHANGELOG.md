# 更新日志

## [Unreleased]

### 变更

- 仓库与插件目录更名为 `astrbot_plugin_kahnbot`，与插件标识符保持一致。原先仓库目录名为 `kahnbot`，与 `metadata.yaml` 的 `name` 不一致，会导致 WebUI 安装与手动 clone 得到两个不同目录、两份不同配置。

### 文档

- 需求清单 `docs/REQUIREMENTS.md` 整理为 F1–F22 与 Q1–Q12；记忆相关 F3–F9、唤醒 F10–F13、注入与主动说话 F14–F16、情绪与心跳 F17–F22 已确认。
- 新增 `docs/refs/`：`astrbot-capabilities.md`（AstrBot 4.29.4 能力核对，含踩坑）、`memory-design.md`（记忆数据与检索设计）、`wake-design.md`（唤醒与配额设计）、`emotion-design.md`（九维情绪、心跳、事实分层、主动说话）、`dsh-memory-keeper.md` 与 `astrbot_plugin_fish_heart.md`（参考插件调研）、`deployment.md`（部署评审记录）。
- 新增 `docs/IMPLEMENTATION.md`（M1–M6 里程碑）与 `docs/DEPLOYMENT.md`（从零部署清单，含 embedding / 会话模型配置）。
- 逐条核对文档里的源码引用（AstrBot 4.29.4 / SnowLuma），修正失效行号与错误的 `TextPart` 导入路径。

## [0.1.0] - 2026-11-11

### 新增

- 插件脚手架：`Star` 子类、配置注入（`_conf_schema.json`）、插件专用日志、数据目录。
- `/kahn` 指令：查看版本、运行状态与数据目录。
- 文档骨架：README、需求文档 `docs/REQUIREMENTS.md`。
