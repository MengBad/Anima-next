# -*- coding: utf-8 -*-
"""发展层级 1：记忆沉淀闭环回归（细节→要点→特质）。"""
import asyncio
import json
import time

import pytest

from anima.memory import consolidation
from anima.memory.consolidation import run_daily_consolidation


class FakeLLM:
    def __init__(self, gist="小测喜欢研究天体物理，家中养猫。"):
        self.gist = gist
        self.calls = 0

    async def chat(self, messages, temperature=0.3, max_tokens=120, **kw):
        self.calls += 1
        return self.gist


class FakeVM:
    def __init__(self):
        self.added = []

    async def add(self, text, meta=None, salience=0.5, source_uin=None, source_gid=None):
        self.added.append((text, meta, salience))
        return True


class FakeCore:
    def __init__(self, data_dir, llm, vm):
        self.config = type("C", (), {"persistence": type("P", (), {"data_dir": data_dir})()})()
        self.llm = llm
        self.vector_memory = vm


def _seed_aged_memory(tmp_path, uid=4242, n=3, age_days=6.5):
    from anima.memory.chat_memory import get_chat_memory, reset_chat_memory
    reset_chat_memory()
    cm = get_chat_memory(str(tmp_path))
    ts = time.time() - age_days * 86400
    for i in range(n):
        cm._store[f"group_990005_{uid}"][f"round_{i}"] = [{
            "role": "user", "name": "小测", "content": f"第{i}条：我在研究土星环的结构",
            "time": ts + i, "ts": ts + i,
        }]


@pytest.mark.asyncio
async def test_consolidation_distills_aged_details(tmp_path):
    """老化细节 → LLM 要点 → 向量层；条目标记 seen 不重复沉淀"""
    data_dir = str(tmp_path)
    _seed_aged_memory(tmp_path)
    llm = FakeLLM()
    vm = FakeVM()
    core = FakeCore(data_dir, llm, vm)
    stats = await run_daily_consolidation(core)
    assert stats["scanned"] == 3
    assert stats["gists"] >= 1
    assert vm.added and vm.added[0][1].get("kind") == "gist"
    stats2 = await run_daily_consolidation(core)
    assert stats2["scanned"] == 0 and stats2["gists"] == 0


@pytest.mark.asyncio
async def test_repeated_gist_promotes_to_trait(tmp_path):
    """同一主题要点反复出现 ≥2 次晋升 self_notes 特质"""
    from anima.cognitive.learning import get_learning
    data_dir = str(tmp_path)
    llm = FakeLLM(gist="小测在研究土星环结构。")
    vm = FakeVM()
    core = FakeCore(data_dir, llm, vm)
    _seed_aged_memory(tmp_path)
    await run_daily_consolidation(core)
    _seed_aged_memory(tmp_path)
    await run_daily_consolidation(core)
    notes = (tmp_path / "self_notes.md").read_text(encoding="utf-8")
    assert "土星环" in notes, "反复要点应晋升为人格特质笔记"
