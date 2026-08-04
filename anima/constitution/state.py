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
