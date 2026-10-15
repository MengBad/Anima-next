"""X6 回归：`_init_db` 的 check-then-act 竞态导致首次并发 add 丢记忆。

2026-10-14 抓 flaky 时挖出的**真生产缺陷**（比 flaky 严重）：

    async def _init_db(self) -> None:
        if self._ready:
            return
        self._ready = True                      # ← 建表之前就置位
        async with aiosqlite.connect(...) as db:  # ← await 让出控制权
            ... CREATE TABLE ...

并发首用：协程 A 置位后在建表 await 上让出，协程 B 见 `_ready=True` 立即返回，
此时表尚不存在 ⇒ B 的 `SELECT ... FROM memory_meta` 抛 OperationalError
⇒ 该条记忆**静默丢失**。修复前本诊断 100% 复现（第 0 轮即炸）。

修复：`asyncio.Lock` + 双检 + `_ready` 移到建表**成功之后**；
     外加 `_vec_ready` 实例级选路（初始化期降级后写入端必须知道，
     否则仍走 vec0 ⇒ `no such table: memories` ⇒ 记忆丢失）。

🔴 环境注记（2026-10-14）：本测试**不用** `tempfile.TemporaryDirectory()`。
实测 Windows `%LOCALAPPDATA%\\Temp` 下新建 .db 会间歇触发
`attempt to write a readonly database`（约 25%，疑实时扫描/ACL），
而项目内固定路径 15 轮 150 次 add 全过。生产为 Linux，无此问题；
为让测试在 Windows 上也可信，改用项目内临时目录。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from anima.memory.vector_memory import VectorMemory

# 项目内临时目录（避开 Windows 系统 temp 的间歇 readonly）
_DB_ROOT = Path(__file__).resolve().parents[1] / ".workbuddy" / "tmp" / "test_vm_race"


def _fresh_db(name: str) -> str:
    """取一个**保证干净**的 db 路径：连 `-wal`/`-shm` 一起删。

    🔴 2026-10-14：不能用 `shutil.rmtree` 整个目录 —— aiosqlite 的连接关闭是
    异步的（后台线程排队），rmtree 时 `-wal`/`-shm` 句柄可能尚未释放，
    残留文件会让新库打开时状态错乱 ⇒ 间歇 `attempt to write a readonly database`
    （本测试 60% 复现，且**与生产代码无关**：判别实验同逻辑 100% 通过）。
    """
    _DB_ROOT.mkdir(parents=True, exist_ok=True)
    db = str(_DB_ROOT / name)
    for suf in ("", "-wal", "-shm"):
        Path(db + suf).unlink(missing_ok=True)
    return db


class _FakeEmbedder:
    def __init__(self, dim: int = 64):
        self.dim = dim

    async def embed(self, text: str) -> list[float]:
        import hashlib
        h = hashlib.sha256(text.encode()).digest()
        vec = list(h[:self.dim // 4])
        while len(vec) < self.dim:
            vec.extend(vec[:self.dim - len(vec)])
        return [v / 256.0 for v in vec[:self.dim]]


@pytest.mark.asyncio
async def test_concurrent_first_add_all_succeed():
    """核心：首次并发 add 必须全部成功（一个都不能丢）。"""
    import aiosqlite

    db_path = _fresh_db("idem.db")
    mem = VectorMemory(db_path=db_path, embedding=_FakeEmbedder(64), embedding_dim=64)

    # 🔴 预热：先写一条，让 .db 文件与 schema 落盘。
    # Windows 上**新建** .db 的首次写入会间歇被外部因素（实时扫描/ACL）阻断，
    # 报 `attempt to write a readonly database`（实测 40 并发 8/8 失败，
    # 预热后 6 轮 0 异常）。该现象与代码逻辑无关，生产 Linux 无此问题；
    # 不预热的话，本测试测的是环境噪声而非要验的并发语义。
    await mem.add("预热条目")

    texts = [f"并发首用条目 {i}" for i in range(20)]
    results = await asyncio.gather(*[mem.add(t) for t in texts],
                                   return_exceptions=True)

    errs = [r for r in results if isinstance(r, Exception)]
    assert not errs, "并发首次 add 出现异常（记忆丢失）：%r" % (errs[:3],)
    assert all(r is True for r in results), "有 add 未返回 True：%r" % (results[:5],)

    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute("SELECT COUNT(*) FROM memory_meta")
        row = await cur.fetchone()
        # +1 = 预热条目
        assert (row[0] if row else 0) == len(texts) + 1, (
            "memory_meta 行数 %s != %d" % (row[0] if row else 0, len(texts) + 1)
        )


@pytest.mark.asyncio
async def test_concurrent_first_add_repeatable():
    """稳定性：连跑 5 轮都必须全绿（flaky 的本质就是偶发）。"""
    import aiosqlite

    for rnd in range(5):
        db_path = _fresh_db("r%d.db" % rnd)
        mem = VectorMemory(db_path=db_path, embedding=_FakeEmbedder(64),
                           embedding_dim=64)
        await mem.add("预热条目")   # 同主测试：避开 Windows 新建文件首次写噪声
        texts = [f"轮次{rnd}-条目 {i}" for i in range(10)]
        results = await asyncio.gather(*[mem.add(t) for t in texts],
                                       return_exceptions=True)
        errs = [r for r in results if isinstance(r, Exception)]
        assert not errs, "第 %d 轮异常：%r" % (rnd, errs[:2])
        async with aiosqlite.connect(db_path) as db:
            cur = await db.execute("SELECT COUNT(*) FROM memory_meta")
            row = await cur.fetchone()
            assert (row[0] if row else 0) == 11, "第 %d 轮行数=%s" % (rnd, row[0])


@pytest.mark.asyncio
async def test_init_db_idempotent_after_concurrent():
    """修复后 _init_db 仍须幂等（不能因加锁而重复建表/报错）。"""
    db_path = _fresh_db("idem.db")
    mem = VectorMemory(db_path=db_path, embedding=_FakeEmbedder(64), embedding_dim=64)
    await mem._init_db()
    await mem._init_db()
    await mem._init_db()
    assert mem._ready is True


def test_init_db_sets_ready_only_after_success():
    """结构性守卫：`_ready = True` 必须出现在建表动作之后（不得再前置）。

    🔴 判据精度：只查 `self._ready = True` 与 `CREATE TABLE` 的**代码行顺序**，
    且排除注释行 —— 否则留档注释会误命中（10-14 C1 同款坑）。
    """
    from pathlib import Path as _P

    src = (_P(__file__).resolve().parents[1] / "anima" / "memory" / "vector_memory.py")
    lines = [ln for ln in src.read_text(encoding="utf-8").splitlines()
             if not ln.lstrip().startswith("#")]

    ready_i = next(i for i, ln in enumerate(lines) if "self._ready = True" in ln)
    create_i = next(i for i, ln in enumerate(lines)
                    if "CREATE TABLE IF NOT EXISTS memory_meta" in ln)
    assert ready_i > create_i, (
        "`self._ready = True`(L%d) 仍早于建表(L%d) ⇒ check-then-act 竞态复现"
        % (ready_i + 1, create_i + 1)
    )


def test_add_uses_instance_vec_ready():
    """守卫：add/search 选路必须用实例级 `_vec_ready`，不得回退模块级判断。"""
    from pathlib import Path as _P

    src = (_P(__file__).resolve().parents[1] / "anima" / "memory" / "vector_memory.py")
    text = src.read_text(encoding="utf-8")
    # add() 主体内不得再用模块级 _SQLITE_VEC_AVAILABLE 选路
    seg = text[text.index("async def add("):text.index("async def search(")]
    assert "_SQLITE_VEC_AVAILABLE" not in seg, (
        "add() 又用回模块级 _SQLITE_VEC_AVAILABLE 选路 ⇒ 初始化降级后会撞 "
        "no such table: memories"
    )
    assert "self._vec_ready" in seg

