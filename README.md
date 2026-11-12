# KahnBot

KahnBot 的 AstrBot 插件仓库（GitHub: [E4-Kahn/astrbot_plugin_kahnbot](https://github.com/E4-Kahn/astrbot_plugin_kahnbot)）。

> 当前进度 **M5（长期记忆与事实分层）**：她在 M4 的检索之上真的开始记事——私聊里或群里被叫到后
> 说「记住…」会写进长期记忆，重要度由模型打分，30 天没被提起且分数低的会归档；
> 群里听来的事只进见闻层（注入时标"未必属实"、永不提醒）；攒够六小时会自己汇总流水；
> 带时间的承诺到点会主动说一句，重启后仍然有效。
> 需求见 [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md)，实现顺序见 [docs/IMPLEMENTATION.md](docs/IMPLEMENTATION.md)。

## 环境要求

- AstrBot `>= 4.29.4`
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
| `enabled` | bool | `true` | 总开关：关闭后不记录、不唤醒、不发消息；`/kahn` 指令仍然可用（便于确认状态） |
| `debug` | bool | `false` | 输出更详细的调试日志 |
| `owner_ids` | list | `[]` | 主人 ID（QQ 号，不是昵称）；决定谁的话能进可提醒的事实层 |
| `noise_patterns` | list | `["#sl"]` | 记录过滤：这些固定文本不进流水、不参与唤醒 |
| `record_private` | bool | `true` | 是否记录私聊；私聊内容默认不会出现在群聊检索结果里 |
| `record_self` | bool | `false` | 是否记录机器人自己的消息（正常不需要） |
| `wake_on_poke` | bool | `true` | 拍一拍唤醒（拍别人的不触发） |
| `wake_on_name` | bool | `true` | 叫名字唤醒（只在消息正文里找） |
| `wake_names` | list | `["鱼宝","大肥鱼","鱼呢"]` | 名字表；填太常见的词会天天误触发 |
| `quota_enabled` | bool | `true` | 每日唤醒配额（主人、管理员、私聊不计） |
| `quota_daily` | int | `5` | 每人每天可唤醒次数，超了静默忽略但照样记录 |
| `debounce_seconds` | float | `2.0` | 防抖窗口：叫到后先等这么久，期间的新消息并成一次回复；填 0 关闭 |
| `debounce_max_seconds` | float | `5.0` | 窗口连续顺延时的总等待上限 |
| `block_stranger_private` | bool | `true` | 屏蔽陌生人私聊（不回复、不记录）；未配置 `owner_ids` 时不生效 |
| `inject_enabled` | bool | `true` | 是否在回复前检索并注入记忆 |
| `embedding_provider_id` | string | `""` | 向量模型 ID；留空自动用第一个启用的 embedding provider |
| `recall_top_k` | int | `5` | 每次最多取几条记忆 |
| `recall_min_score` | float | `0.35` | 检索分数下限（bge-m3 上无关句子约 0.29，别调到 0.3 以下） |
| `inject_max_chars` | int | `800` | 单次注入字符上限 |
| `dedupe_turns` | int | `5` | 同一条记忆多少轮内不重复注入；0 = 关闭去重 |
| `remember_enabled` | bool | `true` | 是否识别「记住…」并自动写入（判定只看句式和唤醒，不为每条消息调模型） |
| `score_importance` | bool | `true` | 写入后后台让模型打 0~100 分（拿不到保持 50）；只影响排序与归档 |
| `summary_enabled` | bool | `true` | 是否定时把聊天流水交给模型提炼长期记忆 |
| `summary_interval_hours` | int | `6` | 汇总间隔（小时）；每次汇总是完整一次模型调用 |
| `summary_max_items` | int | `10` | 单次最多写入几条，超出按置信度截断 |
| `summary_min_confidence` | float | `0.5` | 模型标的置信度低于此值直接丢掉 |
| `summary_max_chars` | int | `4000` | 一次汇总最多喂多少字流水，超出只留最近的 |
| `reminder_enabled` | bool | `true` | 是否到点提醒（只对主人说的话生效；群友说的永不提醒） |
| `timezone` | string | `""` | 解析"下周三""明天八点"用；留空跟随 AstrBot 全局设置 |
| `llm_provider_id` | string | `""` | 打分/汇总用的模型；留空用 AstrBot 当前默认对话模型 |

## 命令

| 命令 | 说明 |
| --- | --- |
| `/kahn` | 单独发送时列出所有子指令 |
| `/kahn status` | 查看版本、运行状态、数据目录、已记录条数、今日唤醒次数、当前心情 |
| `/kahn memory list [n]` | 查看最近 n 条流水（默认 10） |
| `/kahn memory facts [n]` | 查看记忆条目（含重要度、是否已被取代 / 已归档） |
| `/kahn memory add <内容> [重要度 N]` | 手动加一条记忆（仅主人 / 管理员）。内容可以带空格，"重要度 80" 写在最后 |
| `/kahn memory drop <fact_id>` | 删除一条记忆（连同待发提醒）；id 见 `facts` |
| `/kahn memory due [n]` | 查看待发的提醒（到期时间、会话、到点要说的话） |
| `/kahn memory summarize [force]` | 立刻跑一次汇总；`force` 连水位也不管（仅管理员） |
| `/kahn memory forget <id>` | 删除一条流水（仅主人 / AstrBot 管理员） |
| `/kahn recall <问句>` | 手动检索记忆，列出分数与来源（验证 M4 用）。问句可以带空格 |
| `/kahn mood` | 查看九维状态、当前档位与最近一次情绪变化 |
| `/kahn mood why` | 只看最近几次情绪变化的日志 |

## 目录结构

```text
astrbot_plugin_kahnbot/
├── main.py                  # 插件入口（只做注册与串联）
├── core/                    # 业务实现
│   ├── config.py            # 配置收敛与默认值
│   ├── store.py             # SQLite：流水 / 记忆索引 / 配额 / meta
│   ├── recorder.py          # catch-all 记录 + 噪音过滤 + 占位符
│   ├── wake.py              # 唤醒判定：@ / 拍一拍 / 名字 / 私聊
│   ├── emotion.py           # 九维情绪、五档、主人情绪事件
│   ├── heartbeat.py         # 唯一的钟：45/15 分钟 tick + 闭式解补跳
│   ├── state.py             # state.json 原子读写
│   ├── embeddings.py        # 复用 AstrBot 的 embedding provider（含降级）
│   ├── retriever.py         # 向量 + 字面混合排序、隐私/取代/归档过滤
│   ├── injector.py          # <memory> 拼装、预算截断、N 轮内去重
│   ├── when.py              # 中文时间短语 → due_ts（明天八点 / 下周三 / 3 天后）
│   ├── remember.py          # 「记住…」三层判定：唤醒 / 句式 / 回执
│   ├── memory.py            # 记忆写入：去重 / 取代 / 打分 / 归档 / 删除
│   ├── memoryfiles.py       # facts.md / observed.md / schedule.md 读写
│   ├── llm.py               # 打分与汇总用的模型客户端 + JSON 抽取
│   ├── summarizer.py        # 定时汇总：流水 → 模型提炼 → 落条目
│   └── reminder.py          # cron basic job 挂/摘 + 重启恢复 + 心跳兜底
├── tests/                   # 离线冒烟（不启动 AstrBot）
│   ├── smoke_m1.py          # 数据层
│   ├── smoke_m2.py          # 唤醒 / 配额 / 防抖
│   ├── smoke_m3.py          # 情绪漂移 / 五档 / 心跳补跳
│   ├── smoke_m4.py          # 分词 / 排序 / 过滤 / 注入预算与去重
│   └── smoke_m5.py          # 时间解析 / 记住判定 / md 往返 / 汇总 / 提醒
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

1. 把仓库放到 `AstrBot/data/plugins/astrbot_plugin_kahnbot`（或在插件目录里直接开发；
   Windows 上可以用 `mklink /J` 做目录联接，改代码不用来回拷）。
2. 改完代码后在 WebUI 插件页点「重载」，或者重启 AstrBot（`astrbot run` 不带 `--reload` 不会热更）。
3. 数据目录：`data/plugin_data/astrbot_plugin_kahnbot/`，用 `StarTools.get_data_dir()` 获取。
4. 离线冒烟：`python tests/smoke_m1.py` … `smoke_m5.py`，不需要启动 AstrBot。
   环境、交接状态与常见坑见 [docs/IMPLEMENTATION.md](docs/IMPLEMENTATION.md) 第 7 节。

## 更新日志

见 [CHANGELOG.md](CHANGELOG.md)。

## 许可

[MIT](LICENSE)
