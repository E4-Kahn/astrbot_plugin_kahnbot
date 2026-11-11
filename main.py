"""KahnBot —— AstrBot 插件。

当前处于**脚手架阶段**：目录结构、配置读取、日志与数据目录已经打通，
具体功能等需求文档（docs/REQUIREMENTS.md）定稿后再往里加。

目录约定
--------
main.py            插件入口，AstrBot 自动识别继承自 Star 的类
metadata.yaml      插件元数据（名称 / 版本 / 作者 / 仓库地址）
_conf_schema.json  WebUI 里的配置表单
docs/              需求与技术文档
"""

from __future__ import annotations

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

PLUGIN_NAME = "astrbot_plugin_kahnbot"
PLUGIN_VERSION = "0.1.0"


class KahnBot(Star):
    """KahnBot —— 插件骨架。

    命令：
    - /kahn：查看插件状态（版本、配置、数据目录）
    """

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.config: AstrBotConfig | dict = config if config is not None else {}
        self.enabled: bool = bool(self.config.get("enabled", True))
        self.data_dir = StarTools.get_data_dir()
        logger.info(
            "%s v%s 已加载（enabled=%s，数据目录=%s）",
            PLUGIN_NAME,
            PLUGIN_VERSION,
            self.enabled,
            self.data_dir,
        )

    @filter.command("kahn")
    async def kahn_status(self, event: AstrMessageEvent):
        """查看 KahnBot 状态"""
        if not self.enabled:
            yield event.plain_result("KahnBot 已在配置中被禁用。")
            return

        lines = [
            f"KahnBot v{PLUGIN_VERSION}",
            f"状态：{'运行中' if self.enabled else '已禁用'}",
            f"数据目录：{self.data_dir}",
            "功能开发中，命令会随需求文档逐步补齐。",
        ]
        yield event.plain_result("\n".join(lines))

    async def terminate(self) -> None:
        """插件被卸载 / 重载 / 停用时调用。"""
        logger.info("%s 已卸载", PLUGIN_NAME)
