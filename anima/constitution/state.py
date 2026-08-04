"""Typed constitution vocabulary — the state classes of the 0100 trilogy.

Grounded in:
- RFC-0100 §1.4 (Identity), §1.6 (Affect), §1.7 (Drive), §1.8 (Goal), §1.9 (Task)
- RFC-0100B §3 (state classes: Hybrid / Transient / Persistent), §4 (Ownership)

These dataclasses are *vocabulary*, not behavior. Derivation lives in the engine
modules (identity.py, need.py, ...). A state has NO setter method by design:
the only way to obtain a state is to derive it (or, for Goal, to have it adopted
by the Planning Authority). This is the single most important invariant the code
must not violate — it is what keeps the life a *life* and not an agent that edits
its own wants in place (RFC-0100 §1.2).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import time


# --------------------------------------------------------------------------
# Memory — the canonical root store (RFC-0101: Identity = f(Memory))
# --------------------------------------------------------------------------
@dataclass
class MemoryRecord:
    """One experience / fact sedimented into Memory."""

    kind: str  # "episode" | "semantic" | "relation" | "skill"
    content: dict
    timestamp: float = field(default_factory=time.time)
    salience: float = 0.5  # how strongly it shapes Identity (0..1)


@dataclass
class Memory:
    """The canonical Memory store — root of the derivation chain.

    Owned by the **Memory Authority** (RFC-0100B). It is the ONLY direct writer
    besides the Planning Authority's Goal. Reflection writes HERE, never to Goal.
    """

    episodes: list[MemoryRecord] = field(default_factory=list)
    semantic: list[MemoryRecord] = field(default_factory=list)
    relations: list[MemoryRecord] = field(default_factory=list)

    def snapshot(self) -> "Memory":
        """Return a copy for derivation. f(Memory) must never mutate the source."""
        return Memory(
            episodes=list(self.episodes),
            semantic=list(self.semantic),
            relations=list(self.relations),
        )


# --------------------------------------------------------------------------
# Identity — canonical Hybrid, derived f(Memory) (RFC-0101)
# --------------------------------------------------------------------------
@dataclass
class Identity:
    """Who the life has become. Derived pure-function f(Memory); no setter.

    Cross-session persistence is *inherited* via Memory (RFC-0101): there is no
    separate Identity store. Re-derive each session from Memory.
    """

    traits: dict[str, float] = field(default_factory=dict)  # "curiosity": 0.8 ...
    values: list[str] = field(default_factory=list)  # what matters
    self_model: dict = field(default_factory=dict)  # narrative self


# --------------------------------------------------------------------------
# Need — Transient-Derived, g(Identity) (RFC-0102)
# --------------------------------------------------------------------------
class WantVector(Enum):
    DESIRE = "desire"
    REQUIREMENT = "requirement"
    AVERSION = "aversion"
    CURIOSITY = "curiosity"
    SOCIAL = "social"


@dataclass
class Need:
    """The life's current want-vector. Derived g(Identity); no setter, no storage."""

    vectors: dict[WantVector, list[str]] = field(default_factory=dict)
    urgency: float = 0.0


# --------------------------------------------------------------------------
# Perception — instantaneous input to h(Need, Perception) (RFC-0103)
# --------------------------------------------------------------------------
@dataclass
class Perception:
    """The life's read of the current environment / utterance. Instantaneous input to
    ``h(Need, Perception)`` — NOT a stored state. Produced by the perception layer each
    tick; it is read-only for the derivation chain. Affect interprets it; it does not
    react to it (the ``Environment → Emotion → Action`` collapse is forbidden, RFC-0103
    Challenge C1).
    """

    signals: dict[str, float] = field(default_factory=dict)  # e.g. {"novelty": 0.7, "threat": 0.1}
    raw: str = ""


# --------------------------------------------------------------------------
# Affect — Transient-Derived, h(Need, Perception) (RFC-0103)
# --------------------------------------------------------------------------
@dataclass
class Affect:
    """Interpretation of how the life evaluates its need vs. current perception.

    Five dimensions (RFC-0100 §1.6). Derived h(Need, Perception); no setter; no
    persistence authority (long-term patterns sediment into Memory, not Affect).
    """

    valence: float = 0.0  # -1..1 pleasant/unpleasant
    arousal: float = 0.0  # 0..1 activation
    curiosity: float = 0.0  # 0..1
    confidence: float = 0.0  # 0..1
    urgency: float = 0.0  # 0..1 — feeds Drive activation


# --------------------------------------------------------------------------
# Drive — Transient-Derived, i(Affect) (RFC-0104)
# --------------------------------------------------------------------------
class OrientationAxis(Enum):
    APPROACH = "approach"
    AVOID = "avoid"
    INTENSITY = "intensity"
    FOCUS = "focus"


@dataclass
class Drive:
    """Operational orientation (not motivational content). Derived i(Affect).

    Exists only during one reasoning cycle (RFC-0104 D2); never restored.
    """

    orientation: dict[OrientationAxis, float] = field(default_factory=dict)
    activated: bool = False  # Need stays latent without Affect urgency


# --------------------------------------------------------------------------
# Goal — canonical Hybrid / Owned (RFC-0105)
# --------------------------------------------------------------------------
class GoalLifecycle(Enum):
    ADOPTED = "adopted"
    ACTIVE = "active"
    DONE = "done"
    ABANDONED = "abandoned"


@dataclass
class Goal:
    """The life's current committed target. Owned + lifecycle-maintained by the
    Planning Authority (RFC-0105). NOT derived; NOT a Drive; NOT a Task.
    """

    target: str
    success_criterion: str
    horizon: str  # "weeks" / "months"
    priority: float = 0.0
    lifecycle: GoalLifecycle = GoalLifecycle.ADOPTED


# --------------------------------------------------------------------------
# PlannerContext — read-only situational snapshot for Plan construction (RFC-0106)
# --------------------------------------------------------------------------
@dataclass
class PlannerContext:
    """Read-only situational snapshot the Planning Authority reads when constructing Plan.

    Available capabilities, current environment, resources. Parallel to how Affect reads
    Perception (RFC-0103): it is an *input signal*, never a stored state. The Planning
    Authority reads it but never mutates it (RFC-0106 §5.1 C2).
    """

    capabilities: list[str] = field(default_factory=list)
    environment: dict = field(default_factory=dict)
    resources: dict = field(default_factory=dict)


# --------------------------------------------------------------------------
# Plan — Transient-Derived, p(Goal, Drive, Context) (RFC-0106)
# --------------------------------------------------------------------------
@dataclass
class Plan:
    """The life's operational structure for reaching its committed Goal (RFC-0106).

    ``Plan = p(Goal, Drive, Context)`` — **Transient-Derived**, *not* Owned (RFC-0106 §4 P1).
    Constructed by the Planning Authority for the duration of a Goal's pursuit; consumed by
    Execution; **never stored**. No setter, no storage authority (RFC-0106 P3). Plan is the
    *graph of Tasks* (RFC-0100 §1.15), not a single Task and not a Goal.
    """

    goal_target: str = ""          # the Goal this Plan pursues (content source, read-only)
    task_graph: list = field(default_factory=list)   # decomposition: list of task descriptors
    ordering: list = field(default_factory=list)     # sequence / dependencies among Tasks
    branching: dict = field(default_factory=dict)    # conditional / fallback paths (Workflow shape)
    rollback: dict = field(default_factory=dict)     # unwind-on-failure (not a Plan lifecycle)
    context_ref: str = ""           # deterministic ref of the Context read (audit only)
    emphasis: str = ""              # "approach" | "avoid" — Drive orientation, read-only influence
    focus: float = 0.0              # Drive focus, read-only influence


# --------------------------------------------------------------------------
# ExecutionContext — read-only runtime snapshot for carrying a Plan (RFC-0107)
# --------------------------------------------------------------------------
@dataclass
class ExecutionContext:
    """Read-only situational snapshot the Execution Runtime reads when carrying a Plan.

    Available capabilities, current environment, resources at execution time. Parallel to
    PlannerContext / Perception (RFC-0107 §5.1): it is an *input signal* to the carrying act,
    never a stored Constitution state. The Runtime reads it but never mutates it (RFC-0107 C2).
    """

    capabilities: list[str] = field(default_factory=list)
    environment: dict = field(default_factory=dict)
    resources: dict = field(default_factory=dict)


# --------------------------------------------------------------------------
# ExecutionResult — Transient event output of carrying a Plan (RFC-0107)
# --------------------------------------------------------------------------
class ExecutionStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    PARTIAL = "partial"


@dataclass
class ExecutionResult:
    """The event output of carrying a Plan into the Universe (RFC-0107).

    **Transient** — produced by Execution, consumed by Reflection → Memory, **never stored** as a
    Constitution state (RFC-0107 C3). It references the *Plan it consumed* (``plan_ref``), **not** the
    Goal that produced the Plan (RFC-0107 C1) — so it cannot reach back and mutate Goal ownership.
    No setter, no storage authority (RFC-0107 P1/V4). ``lessons`` is what sediments into Memory
    next cycle; ``status`` / ``outcome`` are the carrying record only.
    """

    plan_ref: str = ""             # deterministic ref of the consumed Plan (NOT a Goal reference)
    status: ExecutionStatus = ExecutionStatus.PENDING
    outcome: str = ""
    lessons: list = field(default_factory=list)   # what was learned — feeds Reflection → Memory
    timestamp: float = field(default_factory=time.time)


# --------------------------------------------------------------------------
# ReflectionResult — Transient output of Reflection's transform act (RFC-0108)
# --------------------------------------------------------------------------
@dataclass
class ReflectionResult:
    """The records Reflection decides to sediment after interpreting an ExecutionResult.

    **Transient** — produced by Reflection (Reflection Authority), consumed by the
    **Memory Authority** (the *only* legitimate sink), **never stored** as a Constitution
    state (RFC-0108 C3/V4). It references the *ExecutionResult it consumed* (``result_ref``),
    **not** the Goal that produced the Plan (RFC-0108 C1 — so Reflection cannot reach back and
    mutate Goal ownership), and **not** the Plan (RFC-0107 C1). No setter, no storage authority.
    ``records`` is the *learned* shape that enters Memory; the transform act itself is not persisted.
    """

    result_ref: str = ""           # deterministic ref of the consumed ExecutionResult (NOT Goal/Plan)
    records: list = field(default_factory=list)   # list of {"kind","content","salience"} candidates
    timestamp: float = field(default_factory=time.time)


# --------------------------------------------------------------------------
# Persona — Derived projection of Identity (RFC-0110)
# --------------------------------------------------------------------------
@dataclass
class Persona:
    """The presentation mask — a Derived projection of Identity (RFC-0110).

    Persona is the *expression face* of the self, NOT a second Identity root and NOT a
    store. Derived pure-function projection of Identity; no setter, no storage authority
    (RFC-0110 P1/C3). It references the Identity it projected (``identity_ref``), never
    holds a mutable handle to Identity, so it cannot reach back and rewrite the root
    (RFC-0100B single-Identity-root rule, line 162; RFC-0110 C1/C2). Re-projected each
    Recall (RFC-0100 §1.4); never stored, never restored. Deterministic: same Identity
    ⇒ same Persona (no ``timestamp`` / no hidden state) — it is a *reading*, like Need.
    """

    identity_ref: str = ""        # deterministic ref of the projected Identity (NOT an Identity handle)
    voice: str = ""               # how the self presents (derived from traits/values)
    presentation: dict = field(default_factory=dict)   # the expression projection (read-only view)

