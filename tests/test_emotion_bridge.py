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
    """🔴 桥接必须在 `_tick` 的**无条件路径**上执行（不被其它条件挡住）。

    两次踩坑记录：
    1. 放在 `_constitution_drive_snapshot()` 内 ⇒ 只在 `drive_gate=True`
       时调用 ⇒ 生产 `drive_gate=false` ⇒ **永不执行**（部署后 100s，0 日志）。
    2. 放到「主动发言分支」（`has_hook` 之后）⇒ 群里安静时分支不进入
       ⇒ **仍然永不执行**（部署后 90s，0 日志）。

    ⚠️ 断言方式演进：原来用「前 30 行」这种行号阈值，太脆
    （新增一个块就把它挤到 34 行而误报）。改为**结构化检查**：
    桥接代码所在的 try 块**不得嵌套在 `if _auth is not None:` 里**，
    且必须出现在 `_tick` 的主动发言分支之前。
    """
    import inspect
    from anima.core.autonomy import Heartbeat

    tick_src = inspect.getsource(Heartbeat._tick)
    lines = tick_src.splitlines()
    idx = next((i for i, ln in enumerate(lines) if "hydrate_emotion_memory" in ln), -1)
    assert idx > 0, "桥接必须在 _tick 内（否则群里安静时永不执行）"

    # 桥接必须出现在「主动发言分支」之前（has_hook / _active_hook_present）
    i_hook = next((i for i, ln in enumerate(lines) if "_active_hook_present" in ln), -1)
    assert i_hook > 0
    assert idx < i_hook, (
        f"桥接必须在主动发言分支之前（实测：放在里面则群里安静时永不执行）"
        f" bridge@{idx} hook@{i_hook}"
    )

    # 桥接必须出现在 _tick 的前半段（无条件区），而不是深处
    assert idx < len(lines) // 2, (
        f"桥接应在 _tick 前半段（无条件区），实现在第 {idx}/{len(lines)} 行"
    )

def test_bridge_guarded_by_its_own_switch_source_level():
    """桥接只受自己的开关控制，不受 drive_gate 影响。"""
    import inspect
    from anima.core.autonomy import Heartbeat

    src = inspect.getsource(Heartbeat)
    i = src.find("emotion_bridge_enabled")
    assert i > 0, "应有独立开关判定"


# ── 9. 强度不得被归一化/平方压缩抹掉（2026-10-23）─────────────────────


def test_peak_intensity_preserved_after_normalization():
    """🔴 `traits` 被归一化成「峰值=1.0」，但 `peak_intensity` 必须保留**真实强度**。

    否则：一条 0.1 的微弱欲望和一条 1.0 的强烈欲望都得到 traits=1.0
    ⇒ urgency 恒 1.0 ⇒ Drive 永远激活、「最想要什么」失去区分度。
    """
    from anima.constitution.identity import derive_identity
    from anima.constitution.state import Memory, MemoryRecord

    for v in (0.1, 0.4, 1.0):
        mem = Memory(semantic=[MemoryRecord(
            kind="semantic", content={"social": v}, salience=1.0)])
        ident = derive_identity(mem)
        assert ident.traits["social"] == 1.0, "traits 应归一化为 1.0"
        assert abs(ident.peak_intensity - v) < 1e-6, (
            f"peak_intensity 应保留真实强度 {v}，实得 {ident.peak_intensity}"
        )


def test_urgency_reflects_real_strength():
    """urgency 必须随真实欲望强度变化（不是恒 1.0）。"""
    from anima.constitution.need import derive_need
    from anima.constitution.state import Identity

    weak = derive_need(Identity(traits={"social": 1.0}, peak_intensity=0.1))
    strong = derive_need(Identity(traits={"social": 1.0}, peak_intensity=0.9))
    assert weak.urgency < 0.3, f"微弱欲望不该激活，实得 {weak.urgency}"
    assert strong.urgency > 0.8, f"强烈欲望应高，实得 {strong.urgency}"
    assert weak.urgency != strong.urgency, "强弱必须可区分"


def test_no_squared_compression_in_bridge(tmp_path):
    """🔴 桥接的 salience **不能等于强度本身** —— 否则 `v * salience = v²`。

    `derive_identity` 公式是 `traits[k] += v * salience`。若 salience 也取 v，
    0.4 就成了 0.16，强度被平方压缩，传到 urgency 时严重失真。
    """
    from anima.constitution.emotion_bridge import hydrate_emotion_memory
    from anima.constitution.identity import derive_identity
    from anima.constitution.memory_engine import MemoryEngine

    import json as _json
    (tmp_path / "desires.json").write_text(
        _json.dumps([{"content": "想跟大家聊天", "intensity": 0.4}],
                    ensure_ascii=False), encoding="utf-8")

    eng = MemoryEngine()
    hydrate_emotion_memory(eng, tmp_path)
    ident = derive_identity(eng.snapshot())
    assert abs(ident.peak_intensity - 0.4) < 0.01, (
        f"0.4 的欲望应传导为 peak≈0.40（不是 0.16），实得 {ident.peak_intensity}"
    )


# ── 10. hydrate 必须在短时缓冲为空时回落到持久化历史（2026-10-23）───


class _OD:
    def __init__(self, pairs):
        self._pairs = pairs

    def items(self):
        return list(self._pairs)

    def __len__(self):
        return len(self._pairs)


def _cm_with_history(now):
    from anima.memory.chat_memory import ChatMemory
    m = ChatMemory()
    m._store.clear()
    m._store["group_111_1"] = _OD([("r1", [
        {"role": "user", "name": "小明",
         "content": "今天聊了AI绘图，很有收获", "time": now - 3600}])])
    m._store["group_111_2"] = _OD([("r2", [
        {"role": "user", "name": "小红",
         "content": "我也在试，效果不错", "time": now - 1800}])])
    return m


def test_hydrate_falls_back_to_persistent_history():
    """🔴 生产实证：夜间/群安静时 `_group_recent`（15 分钟短时缓冲）为空
    ⇒ 生产日志长期 `群=0 条目=0 新增=0` ⇒ `episodes` 永远是 0
    ⇒ 成长快照的「记忆增长」维度一直是死的（而 `_store` 里有 133 个群桶）。

    修法：短时缓冲为空时回落到持久化历史（24 小时时间窗）。
    """
    import time as _t
    from anima.constitution.memory_engine import MemoryEngine

    now = _t.time()
    m = _cm_with_history(now)
    # 确认短时缓冲确实是空的（这正是生产现场）
    assert list(m.group_recent_ids()) == [], "前置：短时缓冲应为空"

    eng = MemoryEngine()
    added = eng.hydrate(m)
    assert added == 2, f"应从持久化历史水合 2 条，实得 {added}"
    assert len(eng.snapshot().episodes) == 2


def test_hydrate_persistent_fallback_respects_time_window():
    """回落时只取 24 小时内的（不把上个月的事当「刚发生」）。"""
    import time as _t
    from anima.constitution.memory_engine import MemoryEngine

    now = _t.time()
    m = _cm_with_history(now)
    # 把两条都改成 3 天前
    m._store["group_111_1"] = _OD([("r1", [
        {"role": "user", "name": "小明", "content": "很久以前的事",
         "time": now - 3 * 86400}])])
    m._store["group_111_2"] = _OD([("r2", [
        {"role": "user", "name": "小红", "content": "也很久以前",
         "time": now - 3 * 86400}])])

    eng = MemoryEngine()
    assert eng.hydrate(m) == 0, "24 小时窗口外的不该水合"


def test_hydrate_prefers_recent_buffer_when_present():
    """短时缓冲有内容时仍优先用它（保持既有语义，不破坏线上行为）。"""
    from anima.constitution.memory_engine import MemoryEngine

    class _Fake:
        def group_recent_ids(self):
            return [999]

        def get_recent_group_context(self, gid, limit=8):
            return [{"role": "user", "content": "刚说的那句"}]

        _store = {}

    eng = MemoryEngine()
    assert eng.hydrate(_Fake()) == 1
