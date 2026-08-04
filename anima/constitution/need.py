"""Need Engine (M1.2) — g(Identity): the second derivation.

RFC-0102: ``Need = g(Identity)``, a pure function with no setter. Memory influences
Need ONLY transitively via ``f(Memory)`` — there is NO ``Need(identity, memory)``
bypass (anti-bypass, validated in RFC-0102 Challenge C2). Need is Transient-Derived;
it has no storage authority: ``need.update()`` / ``need.save()`` / ``need.load()`` are
forbidden by contract.

Contract (frozen): same Identity ⇒ same Need.
"""
from __future__ import annotations

from anima.constitution.state import Identity, Need, WantVector


def derive_need(identity: Identity) -> Need:
    """``g(Identity)`` — pure derivation of the want-vector from who the life is.

    Heuristic (M1): map Identity's values / traits / relations onto the five
    WantVectors. The *policy* may be refined later (Reflection, RFC-0107); the
    contract (pure function, no setter, no storage) is fixed by RFC-0102.

    The function reads ``identity`` but must not mutate it.
    """
    vectors: dict[WantVector, list[str]] = {}

    # DESIRE — what the life values becomes what it wants to pursue
    vectors[WantVector.DESIRE] = [f"pursue: {v}" for v in identity.values]

    # CURIOSITY — strongly-held traits become things to explore
    vectors[WantVector.CURIOSITY] = [
        f"explore: {k}" for k, v in identity.traits.items() if v > 0.5
    ]

    # REQUIREMENT — baseline conditions for continued coherence
    vectors[WantVector.REQUIREMENT] = ["maintain coherence", "respond to user"]

    # AVERSION — traits that are absent/weak become things to avoid losing
    aversions = [f"avoid losing: {k}" for k, v in identity.traits.items() if v < 0.1]
    vectors[WantVector.AVERSION] = aversions or ["avoid incoherence"]

    # SOCIAL — bonds in the self-model become relational wants
    social: list[str] = []
    for rel in identity.self_model.get("relations", []):
        if isinstance(rel, dict) and "bond" in rel:
            social.append(f"nurture: {rel['bond']}")
    vectors[WantVector.SOCIAL] = social or ["be present for the user"]

    # urgency peaks with the strongest trait (a strongly-shaped identity wants more now)
    peak = max(identity.traits.values(), default=0.0)
    urgency = round(min(1.0, peak), 3)

    return Need(vectors=vectors, urgency=urgency)
