# -*- coding: utf-8 -*-
"""VectorMemory.health() 报告正确性守卫（2026-10-17 定位）。

缺陷（生产实测，`anima doctor`）::

    ~ 向量记忆后端不可用（将降级为 BM25/无语义召回）

而同一台机上：``sqlite-vec 加载失败`` 7 天 0 次、``向量后端健康检查失败`` 0 次，
运行日志反复出现「向量记忆已启用」与「遗忘完成: chat=67 vector=11」
⇒ **向量后端实际正常工作**，doctor 报的是**假阴性**。

根因：``health()`` 的探测 gate 是实例级 ``self._vec_ready``，而该标志**只在
``_init_db()`` 里置位**，``health()`` 自己**不触发初始化**。doctor / 运维探针都是
「新建实例 → 立即 health()」，此时 ``_ready`` 与 ``_vec_ready`` 皆为 False
⇒ 探测整段被跳过 ⇒ **恒报 ``degraded`` / ``semantic=False``**。

回归来源：commit ``3f84464``（X6 记忆竞态修复）在引入 ``_vec_ready`` 时，
把 ``health()`` 的 gate 从模块级 ``_SQLITE_VEC_AVAILABLE`` 一起换成了实例级，
但没让 ``health()`` 触发初始化 ⇒ 引入本假阴性。

修法（本文件的判据目标）：``health()`` 用 **in-memory vec0 探测能力**；
再结合「本实例是否已初始化、初始化是否成功」给出最终 status ——
未初始化 ⇒ 以能力探测为准；已初始化但 ``_vec_ready=False`` ⇒ 真降级。
"""
from __future__ import annotations

import os
import pathlib
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from anima.memory.vector_memory import VectorMemory, _SQLITE_VEC_AVAILABLE  # noqa: E402

_DB_ROOT = pathlib.Path(os.environ.get("TEMP", "/tmp")) / "_anima_health_dbs"


def _fresh_db(name: str) -> str:
    """取一个保证干净的 db 路径（连 -wal/-shm 一起删）。"""
    _DB_ROOT.mkdir(parents=True, exist_ok=True)
    db = str(_DB_ROOT / name)
    for suf in ("", "-wal", "-shm"):
        pathlib.Path(db + suf).unlink(missing_ok=True)
    return db


class _FakeEmbedder:
    def __init__(self, dim: int = 64):
        self.dim = dim

    async def embed(self, text: str) -> list:
        return [0.01 * (i + 1) for i in range(self.dim)]

    async def close(self) -> None:
        pass


# ── 核心：新建实例（未初始化）必须报 ok ────────────────────────────────────
@pytest.mark.asyncio
async def test_health_ok_for_fresh_instance():
    """🔴 核心缺陷：doctor 就是「新建 → 立即 health()」这一路径。

    修复前：status='degraded' / semantic=False（假阴性，误报语义召回不可用）。
    """
    mem = VectorMemory(db_path=_fresh_db("fresh.db"),
                       embedding=_FakeEmbedder(64), embedding_dim=64)
    st = await mem.health()
    assert st["semantic"] is True, (
        "新建实例（含 embedding + sqlite-vec 可用）应报 semantic=True；"
        "实际=%s —— health() 的 gate 卡在未初始化的 _vec_ready 上" % st
    )
    assert st["status"] == "ok", "新建实例应报 ok，实际=%s" % st
    await mem.close()


# ── 无 embedding ⇒ 必然不可语义召回 ───────────────────────────────────────
@pytest.mark.asyncio
async def test_health_degraded_without_embedding():
    mem = VectorMemory(db_path=_fresh_db("noemb.db"),
                       embedding=None, embedding_dim=64)
    st = await mem.health()
    assert st["embedding"] is False
    assert st["semantic"] is False
    assert st["status"] != "ok", "无 embedding 不应报 ok"
    await mem.close()


# ── 已初始化但失败 ⇒ 真降级（不得被能力探测掩盖）─────────────────────────
@pytest.mark.asyncio
async def test_health_degraded_when_instance_init_failed():
    """本实例已走过 _init_db 且 _vec_ready=False（真降级）⇒ 必须报 degraded。

    这是与「未初始化」的关键区分：不能因为 in-memory 探测通过就粉饰真降级。
    """
    mem = VectorMemory(db_path=_fresh_db("initfail.db"),
                       embedding=_FakeEmbedder(64), embedding_dim=64)
    mem._ready = True          # 假装已完成初始化
    mem._vec_ready = False     # 但 vec0 没建成（真降级）
    st = await mem.health()
    assert st["status"] == "degraded", "已初始化但失败必须报 degraded，实际=%s" % st
    assert st["semantic"] is False
    await mem.close()


# ── 真实初始化成功后 ⇒ ok（端到端）──────────────────────────────────────
@pytest.mark.asyncio
async def test_health_ok_after_real_init():
    mem = VectorMemory(db_path=_fresh_db("real.db"),
                       embedding=_FakeEmbedder(64), embedding_dim=64)
    await mem.add("预热写入一条", salience=0.8)   # 触发 _init_db
    st = await mem.health()
    assert st["status"] == "ok", "真实初始化成功后应报 ok，实际=%s" % st
    await mem.close()


# ── 契约：绝不抛异常 ─────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_health_never_raises():
    mem = VectorMemory(db_path=_fresh_db("weird.db"),
                       embedding=_FakeEmbedder(64), embedding_dim=64)
    # 恶意维度 → in-memory 探测会失败，但必须返回 dict 而非抛异常
    mem.embedding_dim = -1
    st = await mem.health()
    assert isinstance(st, dict) and "status" in st
    await mem.close()


def test_sqlite_vec_flag_is_bool():
    """元守卫：本测试的前提是本地 sqlite-vec 可用（否则探测恒失败会掩盖缺陷）。"""
    assert isinstance(_SQLITE_VEC_AVAILABLE, bool)
    assert _SQLITE_VEC_AVAILABLE is True, (
        "本地 sqlite-vec 不可用 ⇒ 本文件的「新建应报 ok」判据无法成立，"
        "需先安装 sqlite-vec 再跑"
    )
