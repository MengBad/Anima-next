"""Memory Authority (M1.8) — the single-writer sink of the life (RFC-0108).

Implements the *consumption* act at the persistence joint: the Memory Authority takes a
``ReflectionResult`` (produced by Reflection under the Reflection Authority) and ingests its
records into the one Memory store. This is the **closing of the loop**:
``ExecutionResult → Reflection → Memory``.

Design constraints (RFC-0108 Challenge C1/C2/C3):
- The Memory Authority is the **store owner** (ingestion + retention). It is **NOT** a
  Constitution Authority over derived states — it exposes no Identity/Need/Affect/Drive/Goal
  mutation method (the sink X-firewall; parallel to RFC-0107's Runtime firewall).
- It consumes a ``ReflectionResult``; it does **NOT** provide ``ExecutionResult.save()`` or
  ``ReflectionMemory.write()`` — those would be a *second* Memory (O1 violation, C3).
- It must not mutate the ``ReflectionResult`` it ingests (Authority-leak guard).
- It does **NOT** rewrite ``memory_engine.py`` (M1.1); it *uses* the existing engine's
  ``ingest_*`` surface as the store internals (parallel to how M1.6 added ``derive_plan``
  without rewriting the Memory engine).

There is **exactly one** Memory Authority. This module is where RFC-0100C is *truly* tested
(bound here from M1.7 per C4): if building it surfaces a need for transaction/conflict
semantics the Foundation cannot express, that is an *observed contradiction* → Foundation
Evolution F1, not a silent expansion.
"""
from __future__ import annotations

from anima.constitution.errors import ConstitutionViolation
from anima.constitution.memory_engine import MemoryEngine
from anima.constitution.state import ReflectionResult


def ingest(reflection_result: ReflectionResult, engine: MemoryEngine | None = None) -> MemoryEngine:
    """The Memory Authority's single-writer ingest act.

    Consumes a ``ReflectionResult`` and sediments each record into the one Memory store.
    Returns the (possibly same) ``MemoryEngine`` so callers can chain reads. The
    ``reflection_result`` is read-only — this function must not mutate it (Authority-leak
    guard, RFC-0108 C1/C2).

    Args:
        reflection_result: the transform output Reflection submit under its own authority.
        engine: the Memory store to write into; created if not supplied (single store).

    Returns:
        The MemoryEngine that now holds the sedimented records.
    """
    if not isinstance(reflection_result, ReflectionResult):
        raise ConstitutionViolation(
            f"Memory Authority may only ingest a ReflectionResult, got {type(reflection_result).__name__} "
            f"(ExecutionResult.save() / ReflectionMemory.write() are forbidden — second Memory = O1, RFC-0108 C3)"
        )
    if engine is None:
        engine = MemoryEngine()

    # Read-only consumption: capture the record count BEFORE, assert unchanged AFTER.
    consumed_count = len(reflection_result.records)
    for rec in reflection_result.records:
        kind = rec.get("kind", "semantic")
        content = rec.get("content", {})
        salience = rec.get("salience", 0.5)
        if kind == "episode":
            engine.ingest_episode(content, salience=salience)
        elif kind == "relation":
            engine.ingest_relation(content, salience=salience)
        else:  # "semantic" | "skill" | anything else sediments as semantic
            engine.ingest_semantic(content, salience=salience)

    # Authority-leak guard: the consumed ReflectionResult must be untouched.
    if len(reflection_result.records) != consumed_count:
        raise ConstitutionViolation(
            "Memory Authority mutated the ReflectionResult it ingested "
            "(cross-authority mutation forbidden by RFC-0108 C1/C2)"
        )
    return engine


class MemoryAuthority:
    """The single-writer owner of the persistence sink (RFC-0108).

    Owns ingestion + retention of the one Memory. It is **infrastructure-adjacent but inside
    the Constitution contract**: it may not mutate any derived state (Identity/Need/Affect/
    Drive) or the owned Goal. The loop closes *through* it, not *by* it editing the chain.
    """

    def __init__(self, engine: MemoryEngine | None = None) -> None:
        self.engine = engine or MemoryEngine()

    def ingest(self, reflection_result: ReflectionResult) -> "MemoryAuthority":
        """Ingest a ReflectionResult into the one Memory. Returns self for chaining."""
        ingest(reflection_result, self.engine)
        return self

    def snapshot_memory(self):
        """Read surface for the derivation chain (``f(Memory)``). Does not expose writers."""
        return self.engine.snapshot()
