"""宪法记忆闭环的**断层事实**守卫（D-13，2026-09-18）。

为什么需要这组测试：
    上一轮修复 B1 时发现「接上真记忆 ≠ 链变活，而是链变诚实变弱」。本文件把那条
    结论从**叙事**变成**可执行的断言**，逐环节钉住断层位置：

        hydrate()            → 只产 episode（content = {group_id, role, text}）
        derive_identity()    → traits 只从 **semantic 的数值键** 累加（identity.py:29-41）
        derive_need()        → urgency = max(traits.values(), default=0.0)（need.py:50-51）
        reflect()            → 产出 {"lesson": <str>}（reflection.py:67）——**非数值**，永不进 traits

    ⇒ 生产形状的 Memory 走到 Need 时 urgency 恒为 0.0；B1 之前那个恒为 1.0 的值
    来自 `_constitution_drive` 现场合成的 `{"valence": x, "arousal": y}` —— **伪造的**。

    这项守卫的作用是：任何将来想翻 `autonomy.drive_gate=True` 的人，都会先撞到这里，
    看到 traits 为空的事实，而不是上线后才发现主动表达被静默压死。

另附一条 meta 守卫：把 `invariants.py` 的自称钉在真实调用点上（该句曾自称
"in CI / at boot"，但 boot 处无调用点 —— 文字与事实脱节正是本项目的债源）。
"""

from __future__ import annotations

import re
from pathlib import Path

from anima.constitution.identity import derive_identity
from anima.constitution.invariants import check_constitution_invariants
from anima.constitution.memory_authority import MemoryAuthority
from anima.constitution.memory_engine import MemoryEngine
from anima.constitution.need import derive_need
from anima.constitution.reflection import reflect
from anima.constitution.state import ExecutionResult, ExecutionStatus, MemoryRecord

_REPO_ROOT = Path(__file__).resolve().parents[1]


class _FakeChatMemory:
    """最小 chat_memory 形状：hydrate() 真正读的两个方法。"""

    def group_recent_ids(self) -> list[int]:
        return [111]

    def get_recent_group_context(self, gid: int, limit: int = 8) -> list[dict]:
        return [
            {"role": "user", "text": "今天天气不错"},
            {"role": "assistant", "text": "嗯呢，要出去玩吗"},
        ]


# --------------------------------------------------------------------------
# 1) 水合只产 episode
# --------------------------------------------------------------------------
def test_hydrate_produces_only_episodes():
    """水合的形状事实：semantic / relations 恒空（traits 的物质基础因此缺失）。"""
    auth = MemoryAuthority()
    added = auth.hydrate_from_chat(cm=_FakeChatMemory(), force=True)
    assert added > 0, "水合未产出任何记录，测试夹具失效"
    mem = auth.snapshot_memory()
    assert mem.episodes, "水合应产 episode"
    assert mem.semantic == [], "水合不应产 semantic（若改了，本守卫需同步更新 D-13 结论）"
    assert mem.relations == [], "水合不应产 relation"


# --------------------------------------------------------------------------
# 2) 生产形状的 Memory ⇒ traits 空 ⇒ urgency 0
# --------------------------------------------------------------------------
def test_production_shaped_memory_yields_empty_traits_and_zero_urgency():
    """端到端后果：B1 接上真记忆后，urgency 恒为 0（这正是修复「变弱」的机制）。"""
    auth = MemoryAuthority()
    auth.hydrate_from_chat(cm=_FakeChatMemory(), force=True)

    ident = derive_identity(auth.snapshot_memory())
    assert ident.traits == {}, f"生产形状不应产 traits，实得 {ident.traits}"
    assert ident.values == [], "episode content 无 'value' 键 ⇒ values 为空"

    need = derive_need(ident)
    assert need.urgency == 0.0, f"traits 空 ⇒ urgency 必须为 0，实得 {need.urgency}"


# --------------------------------------------------------------------------
# 3) Reflection 的产出是文本，不是数值 ⇒ 永不进 traits
# --------------------------------------------------------------------------
def test_reflection_lessons_are_textual_and_never_become_traits():
    """reflect() 产 {"lesson": str}；derive_identity 只认数值键 ⇒ 该边仍然断。"""
    result = ExecutionResult(
        plan_ref="plan#x", status=ExecutionStatus.SUCCESS,
        outcome="did the thing", lessons=["下次先备份", "别在深夜改配置"],
    )
    refl = reflect(result)
    semantic = [r for r in refl.records if r["kind"] == "semantic"]
    assert semantic, "reflect 应产 semantic 记录"
    assert all(isinstance(r["content"].get("lesson"), str) for r in semantic)

    engine = MemoryEngine()
    MemoryAuthority(engine).ingest(refl)
    ident = derive_identity(engine.snapshot())
    assert "lesson" not in ident.traits, "lesson 是文本，不应变成数值 trait"
    assert ident.traits == {}, f"反射路径不应产生 traits，实得 {ident.traits}"
    assert derive_need(ident).urgency == 0.0


# --------------------------------------------------------------------------
# 4) 载荷证明：traits 只认数值 semantic（说明「数值通道存在，但没人喂」）
# --------------------------------------------------------------------------
def test_numeric_semantic_records_are_the_only_trait_source():
    """对照实验：给数值 semantic → traits 非空、urgency>0。证明断层在**输入端**，非算法。"""
    engine = MemoryEngine()
    engine.ingest_semantic({"valence": 0.8, "arousal": 0.6}, salience=1.0)
    ident = derive_identity(engine.snapshot())
    assert set(ident.traits) == {"valence", "arousal"}, ident.traits
    assert derive_need(ident).urgency > 0.0

    # 而 B1 修复前 `_constitution_drive` 现场合成的正是这种记录（含常量 relation_depth）
    synthetic_engine = MemoryEngine()
    synthetic_engine.ingest_semantic({"valence": 0.1, "arousal": 0.2}, salience=0.1)
    synthetic_engine.ingest_relation({"depth": 0.2}, salience=0.2)
    synth_ident = derive_identity(synthetic_engine.snapshot())
    assert set(synth_ident.traits) == {"valence", "arousal"}, \
        "关系记录的 'depth' 不是语义 trait（relation 走 self_model）"


# --------------------------------------------------------------------------
# 5) meta 守卫：invariants 的「在 CI 跑」必须真有调用点
# --------------------------------------------------------------------------
def test_invariants_claim_has_a_real_ci_call_site():
    """把 invariants.py 的自称钉在事实上（它曾自称 at boot，但那里没有调用点）。"""
    assert check_constitution_invariants() is True

    doc = (_REPO_ROOT / "anima" / "constitution" / "invariants.py").read_text(encoding="utf-8")
    docstring = doc.split('"""')[1]
    assert "in CI / at boot so any regression" not in docstring, \
        "旧的、不可核查的自称（CI / at boot）仍在 docstring 里当指令用"
    assert "no boot call site" in docstring, \
        "缺少对 at boot 自称的澄清（应显式说明不存在该调用点）"

    ci = (_REPO_ROOT / "tests" / "test_constitution_m1.py").read_text(encoding="utf-8")
    assert "check_constitution_invariants()" in ci, \
        "docstring 声称的唯一 CI 调用点消失 —— 必须同步修改自称"
    assert re.search(r"assert check_constitution_invariants\(\) is True", ci), \
        "CI 调用点存在但未被断言"
