"""M1 smoke test — validates the Constitution Layer root against the Foundation.

These assertions ARE the validation: they prove the code honors the contracts the
RFC-0100 trilogy froze (RFC-0105 §14). Failures here mean implementation drifted
from the Foundation — fix the code, not the contract (unless via Foundation
Evolution F1–F4).
"""
from anima.constitution.identity import derive_identity
from anima.constitution.need import derive_need
from anima.constitution.affect import derive_affect
from anima.constitution.drive import derive_drive
from anima.constitution.invariants import check_constitution_invariants
from anima.constitution.state import Affect, Drive, Perception
from anima.constitution.memory_engine import MemoryEngine


def _engine_with(semantic: dict, salience: float = 0.8) -> MemoryEngine:
    e = MemoryEngine()
    e.ingest_semantic(semantic, salience=salience)
    return e


def _seed(engine: MemoryEngine) -> None:
    engine.ingest_semantic({"curiosity": 0.9, "value": "understanding"}, salience=0.8)
    engine.ingest_episode({"event": "solved X", "outcome": "success"}, salience=0.6)
    engine.ingest_relation({"bond": "user_abu", "strength": 0.7}, salience=0.5)


def test_identity_is_pure_function():
    e1 = MemoryEngine()
    _seed(e1)
    e2 = MemoryEngine()
    _seed(e2)
    assert derive_identity(e1.snapshot()) == derive_identity(e2.snapshot())


def test_identity_derivation_does_not_mutate_memory():
    e = MemoryEngine()
    _seed(e)
    snap = e.snapshot()
    before = len(snap.semantic)
    derive_identity(snap)
    # derivation must not add/remove records (no hidden Memory mutation)
    assert len(snap.semantic) == before
    assert len(snap.episodes) == before


def test_identity_inherits_persistence_via_memory():
    # Two engines with identical Memory ⇒ identical Identity (canonical Hybrid:
    # Identity has no store of its own; it is re-derived from Memory).
    e1 = MemoryEngine()
    _seed(e1)
    e2 = MemoryEngine()
    _seed(e2)
    i1 = derive_identity(e1.snapshot())
    i2 = derive_identity(e2.snapshot())
    assert i1.traits == i2.traits
    assert i1.values == i2.values


# --------------------------------------------------------------------------
# M1.2 — Need Engine g(Identity)
# --------------------------------------------------------------------------
def test_need_is_pure_function():
    i = derive_identity(
        _engine_with({"curiosity": 0.9, "value": "understanding"}).snapshot()
    )
    assert derive_need(i) == derive_need(i)


def test_need_depends_on_identity():
    curious = derive_identity(
        _engine_with({"curiosity": 0.9, "value": "understanding"}).snapshot()
    )
    passive = derive_identity(
        _engine_with({"curiosity": 0.1, "value": "rest"}).snapshot()
    )
    assert derive_need(curious) != derive_need(passive)


# --------------------------------------------------------------------------
# M1.3 — Affect Engine h(Need, Perception)
# --------------------------------------------------------------------------
def _identity_for_affect() -> "Identity":
    return derive_identity(
        _engine_with({"curiosity": 0.9, "value": "understanding"}).snapshot()
    )


def test_affect_does_not_mutate_need():
    need = derive_need(_identity_for_affect())
    before = (dict(need.vectors), need.urgency)
    perception = Perception(signals={"novelty": 0.7, "threat": 0.0})
    derive_affect(need, perception)
    assert (dict(need.vectors), need.urgency) == before


def test_affect_is_pure_function():
    need = derive_need(_identity_for_affect())
    p = Perception(signals={"novelty": 0.7, "threat": 0.0})
    assert derive_affect(need, p) == derive_affect(need, p)


# --------------------------------------------------------------------------
# M1.4 — Drive Engine i(Affect)
# --------------------------------------------------------------------------
def test_drive_is_pure_function():
    a = Affect(valence=0.4, arousal=0.6, curiosity=0.5, confidence=0.7, urgency=0.6)
    assert derive_drive(a) == derive_drive(a)


def test_drive_has_no_goal_authority_edge():
    # Drive must not expose planner-like methods; it only produces orientation.
    assert not hasattr(Drive, "create_goal")
    assert not hasattr(Drive, "plan")
    # and cannot be constructed with a goal parameter (no Goal field)
    import inspect

    params = inspect.signature(Drive.__init__).parameters
    assert "goal" not in params


# --------------------------------------------------------------------------
# M1.5 — Invariant Guard
# --------------------------------------------------------------------------
def test_constitution_invariants_hold():
    # Runs every mechanical guard (no setter / no storage authority / no upstream
    # mutation) over the real engines. This IS the guard against future drift.
    assert check_constitution_invariants() is True


if __name__ == "__main__":
    test_identity_is_pure_function()
    test_identity_derivation_does_not_mutate_memory()
    test_identity_inherits_persistence_via_memory()
    test_need_is_pure_function()
    test_need_depends_on_identity()
    test_affect_does_not_mutate_need()
    test_affect_is_pure_function()
    test_drive_is_pure_function()
    test_drive_has_no_goal_authority_edge()
    test_constitution_invariants_hold()
    print("ALL M1 SMOKE TESTS PASSED")
