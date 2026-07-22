"""向量记忆测试。"""

import pytest

from anima.memory.vector_memory import (
    VectorMemory,
    Embedder,
    _bm25_score,
    _tokenize,
)


@pytest.mark.asyncio
async def test_vector_memory_bm25_fallback(tmp_path):
    """无 embedding 时，BM25 关键词检索可用。"""
    db_path = str(tmp_path / "test.db")
    vm = VectorMemory(db_path=db_path, embedding=None)

    await vm.add("我喜欢猫和动漫", meta={"topic": "hobby"})
    await vm.add("今天天气不错", meta={"topic": "weather"})
    await vm.add("Python 是门好语言", meta={"topic": "tech"})

    hits = await vm.search("猫 动漫", top_k=5)
    assert len(hits) == 1
    assert "猫" in hits[0]["text"]


@pytest.mark.asyncio
async def test_vector_memory_recent(tmp_path):
    db_path = str(tmp_path / "test.db")
    vm = VectorMemory(db_path=db_path, embedding=None)
    await vm.add("A")
    await vm.add("B")
    recent = await vm.recent(top_k=2)
    assert len(recent) == 2
    assert recent[0]["text"] == "B"


def test_bm25_score():
    assert _bm25_score("猫 动漫", "我喜欢猫和动漫") > 0
    assert _bm25_score("python", "我喜欢猫") == 0


def test_tokenize():
    tokens = _tokenize("hello 世界 世界和平")
    assert "hello" in tokens
    assert "世界" in tokens
    assert "和平" in tokens


def test_embedder_hash_cache(tmp_path):
    """相同文本第二次调用应直接命中缓存。"""
    e = Embedder("http://x", "k", data_dir=str(tmp_path), cache=True)
    h1 = e._hash("test")
    h2 = e._hash("test")
    assert h1 == h2
    e._cache[h1] = [0.1, 0.2]
    # 缓存命中，不触发网络请求


class _FakeEmbedder:
    """确定性假 embedder，产出固定维度的向量（用于离线测试）。"""

    def __init__(self, dim: int):
        self.dim = dim

    async def embed(self, text: str):
        return [0.01 * (i % 7) for i in range(self.dim)]


@pytest.mark.asyncio
async def test_dimension_mismatch_self_heal(tmp_path):
    """embedding 模型切换导致向量表维度变化（1536→1024）时，
    _init_db 应自动 DROP+重建，而非崩溃 Dimension mismatch。"""
    db_path = str(tmp_path / "dim.db")

    # 第一阶段：用 1536 维嵌入建库
    vm1 = VectorMemory(db_path=db_path, embedding=_FakeEmbedder(1536),
                       embedding_dim=1536)
    await vm1.add("旧维度记忆")
    assert await vm1.search("旧维度记忆", top_k=3)

    # 第二阶段：切换到 1024 维嵌入，复用同一 db
    vm2 = VectorMemory(db_path=db_path, embedding=_FakeEmbedder(1024),
                       embedding_dim=1024)
    # 不应抛 Dimension mismatch
    ok = await vm2.add("新维度记忆")
    assert ok is True
    hits = await vm2.search("新维度记忆", top_k=3)
    assert any("新维度记忆" in h["text"] for h in hits)
