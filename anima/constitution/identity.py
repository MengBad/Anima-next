"""Identity Engine (M1.2) — f(Memory): the first derivation.

RFC-0101: ``Identity = f(Memory)``, a pure function with no setter. The life
re-derives who it has become every session from Memory; there is no separate
Identity store (canonical Hybrid — persistence is *inherited* via Memory).

This is the first link of the chain; everything downstream (Need, Affect, Drive,
Goal) depends on it. It is implemented as a pure function: same Memory ⇒ same
Identity.
"""
from __future__ import annotations

from anima.constitution.state import Identity, Memory


def derive_identity(memory: Memory) -> Identity:
    """``f(Memory)`` — pure derivation of Identity from sedimented experience.

    Heuristic (M1): aggregate traits/values from salient MemoryRecords. A later
    RFC or Reflection loop may refine the *policy*; the contract (pure function,
    no setter) is fixed by RFC-0101.

    The function reads ``memory`` but must not mutate it — callers pass a snapshot.
    """
    traits: dict[str, float] = {}
    values: list[str] = []
    self_model: dict = {}

    for rec in (*memory.episodes, *memory.semantic, *memory.relations):
        if rec.kind == "semantic":
            for k, v in rec.content.items():
                if k == "value":
                    if v not in values:
                        values.append(v)
                elif isinstance(v, (int, float)):
                    traits[str(k)] = traits.get(str(k), 0.0) + float(v) * rec.salience
        elif rec.kind == "episode":
            # episodes shape the self-model narrative, not scalar traits
            self_model.setdefault("episodes", []).append(rec.content)
        elif rec.kind == "relation":
            self_model.setdefault("relations", []).append(rec.content)

    # normalize trait weights to a 0..1 peak so Identity is comparable across sessions
    if traits:
        peak = max(traits.values()) or 1.0
        traits = {k: round(v / peak, 3) for k, v in traits.items()}

    return Identity(traits=traits, values=values, self_model=self_model)
