"""向量记忆 threshold 过滤回归（工单6，P1-1）。

旧逻辑：rerank 重排后 / 无 reranker 的向量路径都忽略了 search 的 threshold 参数，
会召回与用户提问无关的旧记忆污染上下文。修复：两处都按 threshold 过滤。
（BM25 降级路径的分数尺度与余弦相似度不同，不套用同一阈值，保持原有行为。）
"""
from __future__ import annotations

import types

import pytest

from anima.memory.vector_memory import VectorMemory, _SQLITE_VEC_AVAILABLE


class _FakeEmbedder:
    """确定性固定维度 embedder，便于离线测试（不调网络）。"""

    def __init__(self, dim: int = 16):
        self.dim = dim

    async def embed(self, text: str):
        v = [0.0] * self.dim
        for ch in text:
            v[ord(ch) % self.dim] += 1.0
        norm = sum(x * x for x in v) ** 0.5 or 1.0
        return [x / norm for x in v]


class _FakeReranker:
    def __init__(self, scores: list[float]):
        self.scores = scores

    async def rerank(self, query, documents):
        return [(i, s) for i, s in enumerate(self.scores)]


@pytest.mark.asyncio
@pytest.mark.skipif(not _SQLITE_VEC_AVAILABLE, reason="needs sqlite_vec")
async def test_rerank_respects_threshold(tmp_path):
    vm = VectorMemory(
        db_path=str(tmp_path / "v.db"),
        embedding=_FakeEmbedder(16),
        embedding_dim=16,
        reranker=_FakeReranker([0.9, 0.2, 0.8]),
    )
    await vm.add("苹果是一种水果")
    await vm.add("香蕉是黄色的水果")
    await vm.add("汽车是交通工具")
    # 三候选 rerank 分数 [0.9, 0.2, 0.8]，threshold=0.75 → 仅保留 >=0.75 的两条
    hits = await vm.search("水果", top_k=5, threshold=0.75)
    assert len(hits) == 2, f"应仅保留 >=0.75 的两条，实际 {len(hits)}"
    assert all(h["similarity"] >= 0.75 for h in hits)


@pytest.mark.asyncio
@pytest.mark.skipif(not _SQLITE_VEC_AVAILABLE, reason="needs sqlite_vec")
async def test_vector_no_reranker_respects_threshold(tmp_path):
    vm = VectorMemory(
        db_path=str(tmp_path / "v2.db"),
        embedding=_FakeEmbedder(16),
        embedding_dim=16,
        reranker=None,  # 无 reranker 路径
    )
    await vm.add("苹果是一种水果")
    await vm.add("香蕉是黄色的水果")
    await vm.add("汽车是交通工具")
    # threshold 较高时应过滤掉弱相关项（相似度低于阈值不返回）
    hits = await vm.search("水果", top_k=5, threshold=0.99)
    # 固定向量下，所有文本相似度一致；若恰好都低于阈值则过滤干净，
    # 关键断言：返回的每条都满足 threshold（不会混入低于阈值的项）
    assert all(h["similarity"] >= 0.99 for h in hits)


@pytest.mark.asyncio
async def test_bm25_path_unchanged_by_cosine_threshold(tmp_path):
    """BM25 降级路径不受余弦阈值影响（分数尺度不同），保持原有召回行为。"""
    vm = VectorMemory(db_path=str(tmp_path / "bm.db"), embedding=None)
    await vm.add("我喜欢猫和动漫", meta={"topic": "hobby"})
    await vm.add("今天天气不错", meta={"topic": "weather"})
    await vm.add("Python 是门好语言", meta={"topic": "tech"})
    hits = await vm.search("猫 动漫", top_k=5)
    assert len(hits) == 1
    assert "猫" in hits[0]["text"]
