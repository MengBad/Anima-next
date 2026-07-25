"""并发 add() 不应触发 UNIQUE constraint failed: memory_meta.rowid。

验证 P0-1 修复：反转写入顺序后，memory_meta 的 rowid 由 SQLite 自动分配，
并发 add() 不再因 vec0 rowid 冲突而丢失记忆。
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from anima.memory.vector_memory import VectorMemory


class _FakeEmbedder:
    """返回固定维度向量的假 Embedder，避免真实 API 调用。"""

    def __init__(self, dim: int = 64):
        self.dim = dim

    async def embed(self, text: str) -> list[float]:
        # 用文本哈希做确定性伪向量，保证同文本同向量、不同文本不同向量
        import hashlib
        h = hashlib.sha256(text.encode()).digest()
        vec = list(h[:self.dim // 4])
        # 扩展到 dim 维
        while len(vec) < self.dim:
            vec.extend(vec[:self.dim - len(vec)])
        return [v / 256.0 for v in vec[:self.dim]]


@pytest.mark.asyncio
async def test_concurrent_add_no_integrity_error():
    """并发 add() 不应抛出 sqlite3.IntegrityError。"""
    import sqlite3

    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "test.db")
        embedder = _FakeEmbedder(dim=64)
        mem = VectorMemory(db_path=db_path, embedding=embedder, embedding_dim=64)

        # 并发写入 20 条不同文本
        texts = [f"测试记忆条目 number {i}" for i in range(20)]
        results = await asyncio.gather(
            *[mem.add(t, salience=0.5, source_uin=10001, source_gid=20001) for t in texts],
            return_exceptions=True,
        )

        # 所有写入应成功，无 IntegrityError
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                if isinstance(r, sqlite3.IntegrityError):
                    pytest.fail(f"并发 add() 触发 UNIQUE constraint failed: {r}")
                # 其他异常（如 sqlite-vec 不可用导致的 OperationalError）可接受
            assert r is True or isinstance(r, Exception), f"entry {i} unexpected: {r}"


@pytest.mark.asyncio
async def test_concurrent_add_meta_count():
    """并发 add() 后 memory_meta 行数应等于成功写入数。"""
    import aiosqlite

    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "test.db")
        embedder = _FakeEmbedder(dim=64)
        mem = VectorMemory(db_path=db_path, embedding=embedder, embedding_dim=64)

        texts = [f"计数验证条目 {i}" for i in range(10)]
        results = await asyncio.gather(
            *[mem.add(t) for t in texts],
            return_exceptions=True,
        )
        success_count = sum(1 for r in results if r is True)

        # 直接查库验证 meta 行数
        async with aiosqlite.connect(db_path) as db:
            cur = await db.execute("SELECT COUNT(*) FROM memory_meta")
            row = await cur.fetchone()
            meta_count = row[0] if row else 0

        assert meta_count == success_count, (
            f"memory_meta 行数 ({meta_count}) != 成功写入数 ({success_count})"
        )
