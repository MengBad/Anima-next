"""cognition_stage 的 data_dir 必须与装配同源（消 split-brain）。

🔴 缺陷（2026-10-12 生产实证）：
    `anima_core._init_cognitive_state` 用 `str(paths.data_dir())` = **绝对**路径
    （如 `/opt/anima/data`）⇒ `set_relation_data_dir(绝对)` ⇒ `_default_dir` 绝对；
    而 `cognition.py` 用 `config.persistence.data_dir` = **相对** `'data'`
    ⇒ `get_relation('data')` 与 `get_relation()` **命中两个缓存键、两个实例**
    （生产实测 `get_relation() is get_relation('data')` → False）。

    后果：`touch()` 走相对键实例（只登记自己的 deferred 队列），
    心跳 `autonomy.py` 的 `get_relation().flush_deferred_if_any()` 走绝对键实例
    （队列恒空）⇒ **永不落盘**。重启后 relation/desire/time_sense/learning/
    world_model 的全部变更回退 ⇒「bot 忘了和你的关系」。

本测试锁死：cognition_stage 传给 `get_relation()` 的 data_dir 必须等于
`str(paths.data_dir())`（装配同源），从而与心跳/装配拿到**同一个实例**。
"""
from __future__ import annotations

import asyncio
import types

import pytest

from anima.pipeline import PipelineContext
from anima.pipeline.stages import cognition_stage


class _RelStub:
    """relation.touch() 的返回值：需提供 R1 质量闸门要读的数值字段。"""

    familiarity = 0.5
    trust = 0.5
    valence = 0.0
    stage = "ACQUAINTANCE"
    interaction_count = 1
    positive_days = 0
    bond_tension = 0.0
    relational_depth = 0.05

    def __getattr__(self, name):
        return ""


class _Stub:
    """轻量桩：避免认知单例的真实持久化。"""

    def touch(self, *a, **k):
        return _RelStub()

    def add(self, *a, **k):
        pass

    def trust_penalty(self, *a, **k):
        return 0.0

    def prompt_fragment(self, *a, **k):
        return ""

    def get_active(self, *a, **k):
        return []

    def get(self, *a, **k):
        return _RelStub()

    def fragment(self, *a, **k):
        return ""

    def capture(self, *a, **k):
        return None

    def pending(self, *a, **k):
        return []

    def days_since_first(self, *a, **k):
        return 0

    def record(self, *a, **k):
        return None

    def get_state(self, *a, **k):
        return types.SimpleNamespace(valence=0.0)

    def __getattr__(self, name):
        def _noop(*a, **k):
            return None
        return _noop


class _FakeCore:
    def __init__(self, persistence_data_dir: str = "data"):
        from anima.core.config import AnimaConfig
        cfg = AnimaConfig()
        cfg.persistence.data_dir = persistence_data_dir   # 生产现值：相对 'data'
        cfg.crossgroup.enabled = False
        self.config = cfg


@pytest.mark.asyncio
async def test_cognition_data_dir_matches_assembly(monkeypatch, tmp_path):
    """🔴 核心判据：cognition 的 data_dir 必须 == str(paths.data_dir())。"""
    from anima.core import paths

    # 模拟装配：anima_core._init_cognitive_state 把 paths 门面设成绝对目录
    abs_dir = str(tmp_path / "adata")
    paths.set_data_dir(abs_dir)

    captured: dict = {}

    def spy_get_relation(data_dir=None, autonomy=None):
        captured["data_dir"] = data_dir
        return _Stub()

    monkeypatch.setattr("anima.cognitive.time_sense.get_time_context", lambda user_id=None: "")
    monkeypatch.setattr("anima.cognitive.desire.get_desire_system", lambda *a, **k: _Stub())
    monkeypatch.setattr("anima.cognitive.learning.get_learning", lambda *a, **k: _Stub())
    monkeypatch.setattr("anima.cognitive.world_model.get_world_model", lambda *a, **k: _Stub())
    monkeypatch.setattr("anima.cognitive.scar.get_scar_system", lambda *a, **k: _Stub())
    monkeypatch.setattr("anima.cognitive.relation.get_relation", spy_get_relation)

    ctx = PipelineContext(
        event={}, text="你好", user_id=1, group_id=2, detail_type="group",
        sender_name="测试",
        meta={"core": _FakeCore(persistence_data_dir="data")},   # ← 生产：相对
    )
    await cognition_stage(ctx)

    assert captured.get("data_dir") is not None, "cognition_stage 未调用 get_relation"
    assert captured["data_dir"] == str(paths.data_dir()), (
        "split-brain：cognition 用 %r 而装配用 %r ⇒ 两个 RelationEngine 实例，"
        "touch 的 deferred 队列永不 flush"
        % (captured["data_dir"], str(paths.data_dir()))
    )


def test_relation_engine_single_instance_for_same_paths(tmp_path):
    """同源路径必须拿到同一实例；相对路径必然分裂（缺陷的文档化证据）。"""
    from anima.core import paths
    from anima.cognitive import relation as rm

    abs_dir = str(tmp_path / "bdata")
    paths.set_data_dir(abs_dir)
    rm.set_relation_data_dir(abs_dir)              # 装配路径（绝对）

    a = rm.get_relation()                           # 心跳/装配路径
    b = rm.get_relation(str(paths.data_dir()))      # cognition 修复后路径
    assert a is b, "同源路径却拿到不同实例 ⇒ split-brain"

    c = rm.get_relation("data")                     # 修复前的 cognition 路径（相对）
    assert a is not c, (
        "相对路径未分裂？说明 data_dir 已被归一化——请复核本测试的前提"
    )
