# KahnBot

KahnBot 的 AstrBot 插件仓库（GitHub: [E4-Kahn/kahnbot](https://github.com/E4-Kahn/kahnbot)）。

> 当前处于 **脚手架阶段**：插件加载、配置注入、日志、数据目录已经打通，`/kahn` 可以查看运行状态。
> 具体功能在 [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md) 里逐条定稿后实现。

## 环境要求

- AstrBot `>= 4.24.0`
- Python `>= 3.12`（AstrBot 自带要求）
- 无额外第三方依赖

## 安装

方式一：AstrBot WebUI → 插件 → 安装插件 → 填仓库地址 `https://github.com/E4-Kahn/kahnbot`

方式二：手动 clone 到插件目录

```bash
cd AstrBot/data/plugins
git clone https://github.com/E4-Kahn/kahnbot
```

装好后在 WebUI 插件页重载插件即可。插件标识符是 `astrbot_plugin_kahnbot`，目录名是 `kahnbot`。

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
kahnbot/
├── main.py              # 插件入口，继承 Star 的类会被自动识别
├── metadata.yaml        # 插件元数据（名称/版本/作者/仓库）
├── _conf_schema.json    # WebUI 配置表单
├── requirements.txt     # 第三方依赖（暂无）
├── docs/
│   └── REQUIREMENTS.md  # 需求文档
├── CHANGELOG.md
├── LICENSE
└── README.md
```

## 开发

1. 把仓库放到 `AstrBot/data/plugins/kahnbot`（或在插件目录里直接开发）。
2. 改完 `main.py` 后在 WebUI 插件页点「重载」，或者重启 AstrBot。
3. 数据目录：`data/plugin_data/astrbot_plugin_kahnbot/`，用 `StarTools.get_data_dir()` 获取。

## 更新日志

见 [CHANGELOG.md](CHANGELOG.md)。

## 许可

[MIT](LICENSE)
