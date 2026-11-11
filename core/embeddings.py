"""向量化：复用 AstrBot 的 Embedding Provider，不在插件里打包模型。

为什么复用而不是自己带模型（见 `docs/refs/memory-design.md` 6.1）：
sentence-transformers / torch 装下来几百 MB，而 AstrBot 已经支持 OpenAI 兼容、
Ollama、DashScope 等一批 embedding 提供商；插件只需要一个 provider id。

降级：拿不到向量时返回 None，检索退回纯字面匹配（4.6 节）。
"""

from __future__ import annotations

import logging
from array import array
from typing import Any, Sequence

try:  # 离线跑测试时没有 astrbot 也能导入
    from astrbot.api import logger as _logger
except Exception:  # pragma: no cover
    _logger = logging.getLogger("astrbot_plugin_kahnbot.embeddings")


def pack_vector(vec: Sequence[float]) -> bytes:
    """float32 小端存进 BLOB；1024 维 ≈ 4KB。"""
    return array("f", [float(x) for x in vec]).tobytes()


def unpack_vector(blob: bytes | None) -> list[float]:
    if not blob:
        return []
    arr = array("f")
    arr.frombytes(blob)
    return list(arr)


class EmbeddingClient:
    """对一个 AstrBot embedding provider 的薄封装，带懒加载与失败缓存。"""

    def __init__(self, context: Any, provider_id: str = "") -> None:
        self._context = context
        self._provider_id = (provider_id or "").strip()
        self._provider: Any = None
        self._resolved = False
        self._failed = False
        self._dim = 0

    # ------------------------------------------------------------------ 解析

    @property
    def available(self) -> bool:
        return self._provider is not None

    @property
    def dim(self) -> int:
        return self._dim

    def resolve(self) -> Any:
        """取 provider；失败只告警一次，之后直接返回 None，不再骚扰日志。"""
        if self._resolved:
            return self._provider
        self._resolved = True
        if self._failed or self._context is None:
            return None
        try:
            provider = None
            if self._provider_id:
                provider = self._context.get_provider_by_id(self._provider_id)
                if provider is None:
                    _logger.warning(
                        "KahnBot 找不到 embedding provider「%s」，检索将退回字面匹配",
                        self._provider_id,
                    )
            else:
                providers = list(self._context.get_all_embedding_providers() or [])
                provider = providers[0] if providers else None
                if provider is None:
                    _logger.warning(
                        "KahnBot 没有可用的 embedding provider，检索将退回字面匹配"
                    )
        except Exception:  # pragma: no cover - 取决于 AstrBot 版本
            _logger.exception("KahnBot 解析 embedding provider 失败")
            provider = None
        if provider is None:
            self._failed = True
            return None
        self._provider = provider
        try:
            self._dim = int(provider.get_dim())
        except Exception:
            self._dim = 0
        return provider

    def describe(self) -> str:
        provider = self.resolve()
        if provider is None:
            return "未配置（检索只用字面匹配）"
        meta = getattr(provider, "meta", None)
        try:
            info = meta() if callable(meta) else None
            model = getattr(info, "model", "") or getattr(info, "id", "")
        except Exception:
            model = ""
        return f"{model or 'embedding'}（{self._dim or '?'} 维）"

    # ------------------------------------------------------------------ 调用

    async def embed(self, text: str) -> list[float] | None:
        provider = self.resolve()
        if provider is None or not text.strip():
            return None
        try:
            vec = await provider.get_embedding(text.strip())
        except Exception:
            _logger.exception("KahnBot 向量化失败，本条退回字面匹配")
            return None
        if not vec:
            return None
        if not self._dim:
            self._dim = len(vec)
        return [float(x) for x in vec]

    async def embed_many(self, texts: Sequence[str]) -> list[list[float] | None]:
        provider = self.resolve()
        if provider is None:
            return [None for _ in texts]
        out: list[list[float] | None] = []
        for text in texts:
            out.append(await self.embed(text))
        return out


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """余弦相似度；长度不一致或全零返回 0（不同 provider 换模型时会发生）。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / ((na**0.5) * (nb**0.5))


__all__ = ["EmbeddingClient", "pack_vector", "unpack_vector", "cosine"]
