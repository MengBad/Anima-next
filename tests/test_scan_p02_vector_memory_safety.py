# -*- coding: utf-8 -*-
"""P0-B2/B3：向量记忆去重、遗忘导出分页轮转全覆盖、维度不匹配备份化不清库。"""
import pytest

from anima.memory.vector_memory import VectorMemory


@pytest.mark.asyncio
async def test_add_dedupes_identical_text(tmp_path):
    vm = VectorMemory(db_path=str(tmp_path / "a.db"), embedding=None, embedding_dim=4)
    assert await vm.add("唯一文本内容甲", salience=0.5) is True
    assert await vm.add("唯一文本内容甲") is True          # 相同全文→跳过
    rows = []
    async with __import__("aiosqlite").connect(str(tmp_path / "a.db")) as db:
        cur = await db.execute("SELECT text FROM memory_meta")
        rows = await cur.fetchall()
    assert len(rows) == 1, "相同全文不得重复入库"


@pytest.mark.asyncio
async def test_dump_pagination_covers_all_rows(tmp_path):
    vm = VectorMemory(db_path=str(tmp_path / "b.db"), embedding=None, embedding_dim=4)
    for i in range(5):
        await vm.add(f"记忆条目编号{i}的内容主体", salience=0.5)
    seen = set()
    for _ in range(3):                       # limit=2 → 3页覆盖5行
        for item in await vm.dump_for_forgetting(limit=2):
            seen.add(item["rowid"])
    assert len(seen) == 5, f"分页轮转应覆盖全部5行, 实际{len(seen)}"


@pytest.mark.asyncio
async def test_dim_mismatch_backs_up_instead_of_wiping(tmp_path):
    import aiosqlite
    dbp = str(tmp_path / "c.db")
    # 预置旧维度(4)的表 + 一条meta
    vm_old = VectorMemory(db_path=dbp, embedding=None, embedding_dim=4)
    await vm_old._init_db()
    await vm_old.add("珍贵长期记忆不应被清空", salience=0.9)
    # 用不同维度打开 → 触发重建路径
    vm_new = VectorMemory(db_path=dbp, embedding=None, embedding_dim=1024)
    await vm_new._init_db()                  # 不应抛错、不应删数据
    async with aiosqlite.connect(dbp) as db:
        tables = {r[0] for r in (await (await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")).fetchall())}
        cur = await db.execute("SELECT COUNT(*) FROM memory_meta")
        live = (await cur.fetchone())[0]
        backups = [t for t in tables if t.startswith("memory_meta_dim_backup_")]
        if backups:
            bname = backups[0]
            cur = await db.execute(f"SELECT COUNT(*) FROM {bname}")
            bcount = (await cur.fetchone())[0]
        else:
            bcount = None
    assert live >= 1 or bcount == 1, "原meta要么仍在要么整表备份，不得清零"
    if bcount is not None:
        assert bcount == 1, "备份表保留原记忆"
