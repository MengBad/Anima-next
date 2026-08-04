"""Constitution Layer (M1) — the validated derivation chain f/g/h/i + Planning Authority.

Implements the RFC-0100 trilogy as real, reviewable engines. Every state is a
*derived* (or, for Goal, *owned*) contract: no setters, no in-place mutation.
The chain is strictly:

    Memory → Identity=f(Memory) → Need=g(Identity) → Affect=h(Need,Perception)
           → Drive=i(Affect) → Planning Authority reads Drive → Goal (owned)
           → Planning Authority constructs Plan=p(Goal,Drive,Context) → Execution

This package is the implementation half of Phase 2A. The Foundation it consumes
is FROZEN (RFC-0105 §14, re-confirmed at RFC-0106/0107); changes only via Foundation
Evolution F1–F4.

Package layout:
    state.py          — typed vocabulary (state classes)
    memory_engine.py  — M1.1 canonical Memory store (root of the chain)
    identity.py       — M1.1 f(Memory)
    need.py           — M1.2 g(Identity)
    affect.py         — M1.3 h(Need, Perception)
    drive.py          — M1.4 i(Affect)
    invariants.py     — M1.5 mechanical Constitution guards
    planner.py        — M1.6 Planning Authority: derive_plan (p) + Planner lifecycle
    execution.py      — M1.7 Execution Runtime: execute_plan (consumption act) + carrier
    memory_authority.py — M1.8 Memory Authority: ingest (single-writer sink) + MemoryAuthority
"""

__all__ = [
    "state",
    "memory_engine",
    "identity",
    "need",
    "affect",
    "drive",
    "invariants",
    "planner",
    "execution",
    "memory_authority",
]
