"""情感 → 宪法记忆桥接契约（2026-10-23）。

## 解决的断边（生产实测确认）

RFC-0100 的驱动链：`Identity → Need → Affect → Drive → Goal → Plan → Execution`

链代码全在，但生产实测：
```
episodes: 0    semantic: 0    relations: 0
identity.traits: {}    identity.values: []
```
⇒ `need.urgency = peak(traits) = 0` ⇒ `affect.urgency = 0`
⇒ **`drive.activated = (urgency > 0.3)` 恒 False**
⇒ **它从来没有任何内在驱动力可以驱动行动。**

根因：`memory_engine.hydrate()` 只产 `episode`，而 `derive_identity`
只从 `semantic` 聚合 traits ⇒ 链首缺料。

而真实情感数据充分：mood(valence/arousal) / desires 10 条 / relation 93 条。

## 人格保护（本测试的最高优先级）

🔴 **只读不写**：桥接只读取情感文件，**绝不修改**任何情感数据。
本测试用「文件内容前后对比」硬验证这一点。

🔴 **保守分类**：看不清就不塑造身份（问句/聊天记录不参与）。

🔴 **幂等**：重复调用不重复追加（否则 traits 被累加推高，驱动失真）。

🔴 **可关闭**：开关默认 False，关闭时行为与改动前完全一致。
"""
import json
import time

import pytest

from anima.constitution.affect import derive_affect
from anima.constitution.drive import derive_drive
from anima.constitution.emotion_bridge import (
    MIN_DESIRE_INTENSITY, _classify_desire,
    build_emotion_relation_records, build_emotion_semantic_records,
    hydrate_emotion_memory,
)
from anima.constitution.identity import derive_identity
from anima.constitution.memory_engine import MemoryEngine
from anima.constitution.need import derive_need
from anima.constitution.state import Memory, MemoryRecord, Perception


def _write_dir(tmp_path, *, desires=None, mood=None, relation=None):
    if desires is not None:
        (tmp_path / "desires.json").write_text(
            json.dumps(desires, ensure_ascii=False), encoding="utf-8")
    if mood is not None:
        (tmp_path / "mood.json").write_text(
            json.dumps(mood, ensure_ascii=False), encoding="utf-8")
    if relation is not None:
        (tmp_path / "relation.json").write_text(
            json.dumps(relation, ensure_ascii=False), encoding="utf-8")
    return tmp_path


# ── 1. 分类准确（真实数据样本）────────────────────────────────────────


@pytest.mark.parametrize("text,expected", [
    ("想跟大家聊天", "social"),
    ("想吃东西", "desire"),
    ("想弄明白它为什么这样", "curiosity"),
])
def test_classify_real_desires(text, expected):
    assert _classify_desire(text) == expected


@pytest.mark.parametrize("text", [
    "那么，平时在群里大家是出于什么原因去踩你的？",   # 实测误判过的样本
    "主人刚才说的是 Warhammer 的搞哥毛哥喵？",
    "已经是我的了 你顶多算一个小妾",
    "哈基米，把吴雨萌的股票换成100倍杠杆",
])
def test_chitchat_does_not_shape_identity(text):
    """🔴 聊天记录/问句不得塑造身份（实测：曾因含「大家」被归成 social）。"""
    assert _classify_desire(text) == ""


# ── 2. 语义记录构建 ───────────────────────────────────────────────────


def test_strong_desire_becomes_trait(tmp_path):
    d = _write_dir(tmp_path, desires=[{"content": "想跟大家聊天", "intensity": 1.0}])
    sem = build_emotion_semantic_records(d)
    assert any(r.get("social") == 1.0 for r in sem), f"应产出 social trait：{sem}"


def test_weak_desire_ignored(tmp_path):
    d = _write_dir(tmp_path, desires=[
        {"content": "想跟大家聊天", "intensity": MIN_DESIRE_INTENSITY - 0.01}])
    sem = build_emotion_semantic_records(d)
    assert not any("social" in r for r in sem), "低于门槛的欲望不该塑造身份"


def test_resolved_desire_ignored(tmp_path):
    d = _write_dir(tmp_path, desires=[
        {"content": "想跟大家聊天", "intensity": 1.0, "resolved": True}])
    assert not any("social" in r for r in build_emotion_semantic_records(d))


def test_mood_creates_intensity_trait(tmp_path):
    d = _write_dir(tmp_path, mood={"valence": 0.4, "arousal": 0.6})
    sem = build_emotion_semantic_records(d)
    keys = {k for r in sem for k in r}
    assert "mood_intensity" in keys
    assert "mood_arousal" in keys


def test_neutral_mood_ignored(tmp_path):
    d = _write_dir(tmp_path, mood={"valence": 0.01, "arousal": 0.2})
    assert build_emotion_semantic_records(d) == []


def test_relation_records_only_deep(tmp_path):
    d = _write_dir(tmp_path, relation={
        "1": {"depth": 0.95, "stage": "CLOSE"},
        "2": {"depth": 0.1, "stage": "STRANGER"},
    })
    rel = build_emotion_relation_records(d)
    assert len(rel) == 1
    assert rel[0]["bond"] == "1"


# ── 3. 人格保护：只读不写（最高优先级）────────────────────────────────


def test_bridge_never_modifies_emotion_files(tmp_path):
    """🔴 桥接**只读**。情感数据文件内容必须逐字节不变。

    这是「人格在调试中不会丢失」的硬保证：宪法链只是读它，
    绝不修改、绝不覆盖 mood / desires / relation。
    """
    d = _write_dir(
        tmp_path,
        desires=[{"content": "想跟大家聊天", "intensity": 1.0}],
        mood={"valence": 0.4, "arousal": 0.6},
        relation={"1": {"depth": 0.9, "stage": "CLOSE"}},
    )
    before = {p.name: p.read_bytes() for p in d.glob("*.json")}

    eng = MemoryEngine()
    hydrate_emotion_memory(eng, d)
    build_emotion_semantic_records(d)
    build_emotion_relation_records(d)

    after = {p.name: p.read_bytes() for p in d.glob("*.json")}
    assert before == after, "🔴 情感数据文件被修改了 —— 违反人格保护"


# ── 4. 幂等 ───────────────────────────────────────────────────────────


def test_hydrate_is_idempotent(tmp_path):
    """重复调用不重复追加（否则 traits 被累加推高，驱动失真）。"""
    d = _write_dir(tmp_path, desires=[{"content": "想跟大家聊天", "intensity": 1.0}])
    eng = MemoryEngine()

    s1 = hydrate_emotion_memory(eng, d)
    n1 = len(eng._memory.semantic)
    s2 = hydrate_emotion_memory(eng, d)
    n2 = len(eng._memory.semantic)

    assert s1["semantic"] >= 1
    assert s2["semantic"] == 0, "第二次不该重复追加"
    assert n1 == n2, "Memory 不该增长"


# ── 5. 整条链活起来（核心验收）────────────────────────────────────────


def test_full_chain_activates_drive(tmp_path):
    """🔴 核心验收：有真实情感 ⇒ Drive 必须能激活。

    改动前：traits={} ⇒ urgency=0 ⇒ activated=False（恒假）
    改动后：有强烈欲望 ⇒ traits 非空 ⇒ urgency>0.3 ⇒ activated=True
    """
    d = _write_dir(
        tmp_path,
        desires=[{"content": "想跟大家聊天", "intensity": 1.0}],
        mood={"valence": 0.31, "arousal": 0.55},
        relation={"1": {"depth": 0.95, "stage": "CLOSE"}},
    )
    sem = build_emotion_semantic_records(d)
    rel = build_emotion_relation_records(d)
    mem = Memory(
        semantic=[MemoryRecord(kind="semantic", content=c, salience=0.8) for c in sem],
        relations=[MemoryRecord(kind="relation", content=c, salience=0.9) for c in rel],
    )

    ident = derive_identity(mem)
    assert ident.traits, "有情感数据时 traits 不得为空"

    need = derive_need(ident)
    assert need.urgency > 0.3, f"urgency 应被真实欲望推高，实得 {need.urgency}"

    aff = derive_affect(need, Perception(signals={"novelty": 0.5, "threat": 0.0}))
    drv = derive_drive(aff)
    assert drv.activated is True, "Drive 必须激活（这是整条链的核心目的）"


def test_empty_data_keeps_chain_dormant(tmp_path):
    """没有情感数据 ⇒ 链保持休眠（fail-closed，不凭空产生驱动）。"""
    d = _write_dir(tmp_path)
    sem = build_emotion_semantic_records(d)
    mem = Memory(semantic=[MemoryRecord(kind="semantic", content=c, salience=0.5)
                           for c in sem])
    ident = derive_identity(mem)
    assert ident.traits == {}
    need = derive_need(ident)
    assert need.urgency == 0.0
    assert derive_drive(derive_affect(need, Perception(signals={}))).activated is False


# ── 6. 可关闭 & 默认关 ────────────────────────────────────────────────


def test_default_off_in_config():
    from anima.core.config import AnimaConfig
    assert AnimaConfig().autonomy.emotion_bridge_enabled is False, (
        "默认必须关闭 —— 关闭时行为与改动前完全一致，便于对照与回退"
    )


# ── 7. 损坏数据安全 ───────────────────────────────────────────────────


def test_corrupted_files_are_safe(tmp_path):
    (tmp_path / "desires.json").write_text("{ not json", encoding="utf-8")
    (tmp_path / "mood.json").write_text("[]", encoding="utf-8")
    assert build_emotion_semantic_records(tmp_path) == []


def test_hydrate_survives_engine_errors(tmp_path):
    class _Bad:
        @property
        def _memory(self):
            raise RuntimeError("boom")

    stats = hydrate_emotion_memory(_Bad(), tmp_path)
    assert stats["skipped"] is True, "异常必须被隔离，不得抛出"


# ── 8. 桥接必须独立于 drive_gate（2026-10-23 部署后实测踩坑）───────────


def test_bridge_runs_on_unconditional_tick_path():
    """🔴 桥接必须在 `_tick` 的**无条件路径**上每 tick 执行。

    两次踩坑记录：
    1. 第一版放在 `_constitution_drive_snapshot()` 内 ⇒ 只在 `drive_gate=True`
       时调用 ⇒ 生产 `drive_gate=false` ⇒ **永不执行**（部署后 100s，0 日志）。
    2. 第二版放到「主动发言分支」（`has_hook` 之后）⇒ 群里安静时分支不进入
       ⇒ **仍然永不执行**（部署后 90s，0 日志）。

    ⇒ 喂料是心跳的基础动作，必须无条件执行（幂等，重复调用不追加）。
    """
    import inspect
    from anima.core.autonomy import Heartbeat

    tick_src = inspect.getsource(Heartbeat._tick)
    assert "hydrate_emotion_memory" in tick_src, (
        "桥接必须在 _tick 内（否则群里安静时永不执行）"
    )
    # 必须在 _tick 前 30 行内（即开头无条件区），而不是深处某个分支里
    lines = tick_src.splitlines()
    idx = next(i for i, ln in enumerate(lines) if "hydrate_emotion_memory" in ln)
    assert idx < 30, (
        f"桥接应在 _tick 开头（无条件区），实现在第 {idx} 行 "
        f"—— 放在深处分支会导致「条件不满足就不喂料」"
    )

def test_bridge_guarded_by_its_own_switch_source_level():
    """桥接只受自己的开关控制，不受 drive_gate 影响。"""
    import inspect
    from anima.core.autonomy import Heartbeat

    src = inspect.getsource(Heartbeat)
    i = src.find("emotion_bridge_enabled")
    assert i > 0, "应有独立开关判定"
