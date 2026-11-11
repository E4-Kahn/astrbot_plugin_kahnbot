# KahnBot

KahnBot 的 AstrBot 插件仓库（GitHub: [E4-Kahn/astrbot_plugin_kahnbot](https://github.com/E4-Kahn/astrbot_plugin_kahnbot)）。

> 当前处于 **脚手架阶段**：插件加载、配置注入、日志、数据目录已经打通，`/kahn` 可以查看运行状态。
> 具体功能在 [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md) 里逐条定稿后实现。

## 环境要求

- AstrBot `>= 4.24.0`
- Python `>= 3.12`（AstrBot 自带要求）
- 无额外第三方依赖

## 安装

方式一：AstrBot WebUI → 插件 → 安装插件 → 填仓库地址 `https://github.com/E4-Kahn/astrbot_plugin_kahnbot`

方式二：手动 clone 到插件目录

```bash
cd AstrBot/data/plugins
git clone https://github.com/E4-Kahn/astrbot_plugin_kahnbot
```

装好后在 WebUI 插件页重载插件即可。插件标识符与目录名统一为 `astrbot_plugin_kahnbot`，配置文件为 `astrbot_plugin_kahnbot_config.json`。

## 配置

配置文件在 WebUI 插件页 → 插件配置，字段定义见 [`_conf_schema.json`](_conf_schema.json)。

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | 总开关，关闭后不响应指令、不主动发消息 |
| `debug` | bool | `false` | 输出更详细的调试日志 |

## 命令

| 命令 | 说明 |
| --- | --- |
| `/kahn` | 查看插件版本、状态和数据目录 |

## 目录结构

```text
astrbot_plugin_kahnbot/
├── main.py                  # 插件入口（注册串联）
├── metadata.yaml            # 插件元数据（名称/版本/作者/仓库）
├── _conf_schema.json        # WebUI 配置表单
├── requirements.txt         # 第三方依赖（暂无）
├── docs/
│   ├── REQUIREMENTS.md      # 需求清单（单一事实来源）
│   ├── IMPLEMENTATION.md    # 实现里程碑 M1~M6
│   ├── DEPLOYMENT.md        # 从零部署清单（SnowLuma / AstrBot / 插件 / 模型）
│   └── refs/                # 设计与调研笔记（索引见 docs/refs/README.md）
├── CHANGELOG.md
├── LICENSE
└── README.md
```

## 文档

| 文档 | 用途 |
| --- | --- |
| [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md) | 需求清单与未决问题，先定需求再写代码 |
| [docs/IMPLEMENTATION.md](docs/IMPLEMENTATION.md) | 里程碑、模块划分与每阶段验收 |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | 部署与模型配置清单 |
| [docs/refs/](docs/refs/README.md) | 参考插件笔记、AstrBot 能力核对、记忆/唤醒/情绪设计 |

## 开发

1. 把仓库放到 `AstrBot/data/plugins/astrbot_plugin_kahnbot`（或在插件目录里直接开发）。
2. 改完 `main.py` 后在 WebUI 插件页点「重载」，或者重启 AstrBot。
3. 数据目录：`data/plugin_data/astrbot_plugin_kahnbot/`，用 `StarTools.get_data_dir()` 获取。

## 更新日志

见 [CHANGELOG.md](CHANGELOG.md)。

## 许可

[MIT](LICENSE)
