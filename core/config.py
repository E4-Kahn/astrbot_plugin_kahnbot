"""配置读取：把 WebUI 传进来的原始配置收敛成一个不可变的 Config。

AstrBot 传给插件的 `config` 是 `AstrBotConfig`（dict 的子类，带 save_config），
这里只读不写；取不到或类型不对时一律退回默认值，避免一条坏配置把插件拦在启动阶段。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

# SnowLuma 的状态命令；群里谁发一句都不该进流水（F13）
DEFAULT_NOISE: tuple[str, ...] = ("#sl",)


def _as_bool(raw: Mapping[str, Any], key: str, default: bool) -> bool:
    value = raw.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _as_str_tuple(
    raw: Mapping[str, Any],
    key: str,
    default: tuple[str, ...] = (),
) -> tuple[str, ...]:
    value = raw.get(key, default)
    if isinstance(value, str):
        # 也接受 "10001,10002" 这种写法，省得用户非要在表单里点加号
        parts: list[Any] = value.replace("，", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
        parts = list(value)
    else:
        parts = []
    return tuple(str(item).strip() for item in parts if str(item).strip())


@dataclass(frozen=True, slots=True)
class Config:
    """插件运行期配置。"""

    enabled: bool = True
    debug: bool = False
    # 主人（F3）：身份键一律是平台用户 ID，不用昵称
    owner_ids: tuple[str, ...] = ()
    # 记录过滤（F13）
    noise_patterns: tuple[str, ...] = DEFAULT_NOISE
    record_private: bool = True
    record_self: bool = False

    def is_owner(self, sender_id: str) -> bool:
        return bool(sender_id) and sender_id in self.owner_ids

    def is_noise(self, text: str) -> bool:
        """固定文本（大小写不敏感）不算消息，也不参与唤醒。"""
        lowered = text.strip().lower()
        return bool(lowered) and lowered in {p.strip().lower() for p in self.noise_patterns}


def load_config(raw: Mapping[str, Any] | None) -> Config:
    raw = raw if isinstance(raw, Mapping) else {}
    return Config(
        enabled=_as_bool(raw, "enabled", True),
        debug=_as_bool(raw, "debug", False),
        owner_ids=_as_str_tuple(raw, "owner_ids"),
        noise_patterns=_as_str_tuple(raw, "noise_patterns", DEFAULT_NOISE) or DEFAULT_NOISE,
        record_private=_as_bool(raw, "record_private", True),
        record_self=_as_bool(raw, "record_self", False),
    )
