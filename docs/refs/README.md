# 参考插件笔记

这里存放对每个参考 AstrBot 插件的调研笔记。**只记录结论与证据，不抄源码**；
需求条目在 [`../REQUIREMENTS.md`](../REQUIREMENTS.md)，本目录只做支撑材料。

本地参考素材统一放在 `D:\workspace\refs\`（`astrbot_plugin_fish_heart`、`astrbot_plugin_sugar_debounce`、`dsh-memory-keeper`、`SnowLuma`），
笔记里的「来源」按这个布局写。

## 索引

| 文件 | 内容 |
| --- | --- |
| [astrbot-capabilities.md](astrbot-capabilities.md) | AstrBot 4.29.4 能力核对：钩子、注入位置、说话人归属、唤醒、Embedding、优先级、闸门 |
| [memory-design.md](memory-design.md) | 记忆层设计：数据落点、md/SQLite 结构、写入、检索、注入、老化、隐私、事实分层 |
| [wake-design.md](wake-design.md) | 唤醒与配额：handler 顺序、`is_at_or_wake_command`、拍一拍、`wake_quota`、噪音过滤 |
| [emotion-design.md](emotion-design.md) | 情绪与心跳：九维、五档、tick 与补跳、情绪↔记忆联动、主动说话、沉默补偿 |
| [dsh-memory-keeper.md](dsh-memory-keeper.md) | 参考插件 1：`dsh-memory-keeper`（检索层，非记忆系统） |
| [astrbot_plugin_fish_heart.md](astrbot_plugin_fish_heart.md) | 参考插件 2：`astrbot_plugin_fish_heart`（状态机 + 心跳 + 主动说话） |
| [astrbot_plugin_sugar_debounce.md](astrbot_plugin_sugar_debounce.md) | 参考插件 3：`astrbot_plugin_sugar_debounce`（消息防抖：连续短句合并成一次模型调用） |
| [deployment.md](deployment.md) | 部署配置评审的原始证据（端口、token 校验），清单本身见 [`../DEPLOYMENT.md`](../DEPLOYMENT.md) |

## 命名

`<插件标识符>.md`，例如 `astrbot_plugin_xyz.md`。插件标识符取 `metadata.yaml` 的 `name`。

## 模板

```markdown
# <插件显示名>（<插件标识符>）

- 来源：<本地路径 / 仓库地址>
- 版本 / 元数据：<version、要求 AstrBot 版本>
- 调研日期：YYYY-MM-DD
- 代码规模：<约 N 个 Python 文件 / N 行>

## 1. 速览

- 定位：<一句话>
- 命令表：
  | 指令 | 作用 | 权限/限制 |
- 配置项：<关键项，不是全量>
- 数据存储：<位置、格式、有无迁移>
- 第三方依赖：<有 / 无，分别是什么>

## 2. 机制

- 主链路：<消息进来后依次发生什么，带 file:line>
- 关键数据结构：
- 扩展点（如果有）：

## 3. 坑与坏味道

| 现象 | 位置 | 影响 |
| --- | --- | --- |

## 4. 对 KahnBot 的启示

- 值得借鉴：
- 不借鉴：
- 存疑待讨论：

## 5. 产出

- 新增/修改的需求编号：F?、S?、Q?
```

## 约定

- 引用代码必须带 `路径:行号`。
- 只写核实过的结论；推测要显式标注「推测」。
- 一个插件一份文件，不合并。
