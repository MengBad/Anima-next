"""表情包嵌入语义检索（对标 astrbot_plugin_stealer 的 embedding_service，走 Anima 向量库）。

复用 Anima 的 Embedder 客户端（`anima.memory.vector_memory.Embedder`，OpenAI 兼容实施例）
做语义检索 bonus：把库里的表情元数据播文段打成向量，查询时间步做 cosine 检索，
把命中 path 转成语义相似分供上层 smart-select 加权。向量库未配/失败时静默降级为空——
上层用 BM25 + 混合相似度兜底（与 stealer 的「嵌入未就绪走 BM25」同构）。
"""
from __future__ import annotations

import asyncio
import math
import threading
from typing import Any, Optional

import logging
logger = logging.getLogger("anima.stickers.embedding")


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def build_text(category: str, desc: str = "", tags: Optional[list] = None,
               scenes: Optional[list] = None) -> str:
    """把表情元数据拼成一段可嵌入的文本。"""
    parts = [str(category or "").strip()]
    if desc:
        parts.append(str(desc).strip())
    parts.extend(str(t) for t in (tags or []) if str(t).strip())
    parts.extend(str(s) for s in (scenes or []) if str(s).strip())
    return " ".join(p for p in parts if p)


class StickerEmbedder:
    """为表情库维护一个轻量内存嵌入索引，提供余弦语义检索。

    可嵌入文本仅存 {hash -> text}（向量不落盘），embedding 本身由 Anima Embedder
    的持久化缓存承载。__init__ 同步接入 Embedder；任何失败都视为不可用。
    """

    def __init__(self, embedder: Any = None, dimension: int = 0):
        self._embedder = embedder
        self._dim = max(64, int(dimension))
        self._lock = threading.Lock()
        self._vectors: dict[str, list[float]] = {}
        self._texts: dict[str, str] = {}

    @classmethod
    def from_core(cls, core: Any = None) -> "StickerEmbedder":
        """从 AnimaCore 里取 Embedder 构造；取不到返回不可用实例。"""
        vm = getattr(core, "vector_memory", None) if core else None
        embedder = getattr(vm, "embedding", None)
        dim = int(getattr(vm, "embedding_dim", 0) or 0)
        return cls(embedder, dim)

    @property
    def available(self) -> bool:
        return self._embedder is not None

    async def _embed(self, text: str) -> Optional[list[float]]:
        if self._embedder is None:
            return None
        try:
            vec = await self._embedder.embed(text)
            if not vec:
                return None
            return list(vec)
        except Exception:
            logger.exception("表情 embedding 获取失败")
            return None

    async def index(self, sticker_hash: str, text: str) -> bool:
        """入库/更新一条。embedding 失败则对该条忽略（后续检索自然召回不到）。"""
        if self._embedder is None or not sticker_hash or not text:
            return False
        vec = await self._embed(text)
        if vec is None:
            return False
        with self._lock:
            self._vectors[sticker_hash] = vec
            self._texts[sticker_hash] = text
        return True

    def remove(self, sticker_hash: str) -> None:
        with self._lock:
            self._vectors.pop(sticker_hash, None)
            self._texts.pop(sticker_hash, None)

    def clear(self) -> None:
        with self._lock:
            self._vectors.clear()
            self._texts.clear()

    async def search(self, query: str, top_k: int = 8) -> list[tuple[str, float]]:
        """查询嵌入余弦检索，返回 [(sticker_hash, score0.1)]（score∈[0,1]）。"""
        if self._embedder is None or not self._vectors:
            return []
        vec = await self._embed(query)
        if vec is None:
            return []
        scored: list[tuple[str, float]] = []
        with self._lock:
            items = list(self._vectors.items())
        for h, v in items:
            s = _cosine(vec, v)
            if s > 0:
                scored.append((h, s))
        scored.sort(key=lambda x: -x[1])
        return scored[:top_k]
