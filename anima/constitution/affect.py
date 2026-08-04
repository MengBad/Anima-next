"""Affect Engine (M1.3) — h(Need, Perception): the third derivation.

RFC-0103: ``Affect = h(Need, Perception)``. Affect is an *interpretation* layer — how
the life evaluates its need against the current perception — NOT an event-reaction
module (the ``Environment → Emotion → Action`` collapse is explicitly forbidden by
RFC-0103 Challenge C1). Perception is the read-only instantaneous input; Need is the
attributed upstream input. Affect is Transient-Derived with no storage authority.

Contract: ``h`` reads Need but MUST NOT mutate it (Affect is explained BY Need, not
the other way). Same (Need, Perception) ⇒ same Affect.
"""
from __future__ import annotations

from anima.constitution.state import Affect, Need, Perception


def derive_affect(need: Need, perception: Perception) -> Affect:
    """``h(Need, Perception)`` — interpret the life's stance toward its want, now.

    Heuristic (M1): valence from perceived novelty minus threat; arousal/urgency from
    perception novelty and Need urgency; curiosity/confidence from the perception
    signal. The function reads ``need`` (e.g. ``need.urgency``) but never assigns to it.
    """
    sig = perception.signals
    novelty = float(sig.get("novelty", 0.3))
    threat = float(sig.get("threat", 0.0))
    urgency_in = float(need.urgency)

    valence = round(0.5 * novelty - 0.8 * threat, 3)
    arousal = round(min(1.0, 0.4 + 0.4 * novelty + 0.3 * urgency_in), 3)
    curiosity = round(min(1.0, 0.4 + 0.5 * novelty), 3)
    confidence = round(min(1.0, 0.6 + 0.3 * (1.0 - threat)), 3)
    urgency = round(min(1.0, 0.5 * urgency_in + 0.4 * novelty), 3)

    return Affect(
        valence=valence,
        arousal=arousal,
        curiosity=curiosity,
        confidence=confidence,
        urgency=urgency,
    )
