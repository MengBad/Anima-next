"""Relationship Engine (M1.11) — the projection act (RFC-0111).

Relationship = Derived projection of an external relation. ``derive_relationship(memory, identity,
external_ref) -> RelationshipProjection`` is a pure read→emit: it reads ``Memory.relations``
(filtered by ``external_ref``) and ``Identity`` read-only, emits a RelationshipProjection (the held
model of an external agent), writes nothing, mutates nothing, owns no store, holds no Life authority.

The Relationship risks (user's brief; RFC-0111 central risk) are a derived relational layer
masquerading as: (a) a **second Memory** (``RelationshipStore`` / ``SocialMemory`` — writing around
the single Memory Authority), (b) a **motivation source** (``relationship.set_goal()`` — an optimizer
at the relational seam), or (c) a **second Identity system** (``relationship.rewrite_identity()`` —
reverse-defining the self). The projection contract forbids that mechanically: RelationshipProjection
carries no ``save`` / ``store`` / ``update_identity`` / ``modify_goal`` API, and references Memory +
Identity only by immutable refs (never mutable handles), so it cannot reach back and rewrite the root
(Identity) or the sink (Memory). This module deliberately imports NO Goal / Planning / Need / Drive /
Memory writer — there is no handle to leak (the projection X-firewall, RFC-0111 C1/C2/C3).
"""
from __future__ import annotations

import hashlib

from anima.constitution.state import Identity, Memory, MemoryRecord, RelationshipProjection


def _memory_ref(memory: Memory, external_ref: str) -> str:
    """Deterministic, handle-free ref of the Memory.relations slice read.

    Relationship may carry this string but never the Memory object, so the projection cannot
    mutate the sink it was derived from (RFC-0100B line 167 single-Memory rule; RFC-0111 C3).
    """
    slice_records = [
        (r.kind, tuple(sorted(r.content.items())), round(r.salience, 4))
        for r in memory.relations
        if r.kind == "relation" and r.content.get("external_ref") == external_ref
    ]
    payload = repr((external_ref, tuple(sorted(slice_records))))
    return "memory#relation#" + hashlib.sha1(payload.encode()).hexdigest()[:16]


def _identity_ref(identity: Identity) -> str:
    """Deterministic, handle-free ref of an Identity.

    Relationship may carry this string but never the Identity object, so the projection cannot
    mutate the root it was derived from (RFC-0100B line 162 single-Identity-root; RFC-0111 C1).
    """
    payload = repr(
        (
            tuple(sorted(identity.traits.items())),
            tuple(identity.values),
            tuple(sorted(identity.self_model.items())),
        )
    )
    return "identity#" + hashlib.sha1(payload.encode()).hexdigest()[:16]


def _derive_trust(records: list[MemoryRecord]) -> float:
    """Modeled trust in the external agent — salience-weighted mean of relation trust (0..1)."""
    if not records:
        return 0.5  # neutral default when no relation record exists
    weighted = sum(r.content.get("trust", 0.5) * r.salience for r in records)
    weight = sum(r.salience for r in records) or 1.0
    return round(min(1.0, max(0.0, weighted / weight)), 4)


def _derive_role(records: list[MemoryRecord]) -> str:
    """Modeled role of the external agent — most frequent role label in the relation records."""
    roles = [r.content.get("role", "unknown") for r in records]
    if not roles:
        return "unknown"
    return max(set(roles), key=roles.count)


def _derive_history(records: list[MemoryRecord]) -> list:
    """A read-only summary view of the relation records (no mutation of the source)."""
    return [
        {
            "trust": r.content.get("trust"),
            "role": r.content.get("role"),
            "content": dict(r.content),
        }
        for r in records
    ]


def derive_relationship(memory: Memory, identity: Identity, external_ref: str) -> RelationshipProjection:
    """Project Memory.relations ∩ Identity into a held model of an external agent.

    Pure, deterministic, read-only on Memory + Identity:
    - same (Memory, Identity, external_ref) ⇒ same RelationshipProjection (no hidden state / no timestamp);
    - reads ``Memory.relations`` (filtered by ``external_ref``) + Identity, emits a RelationshipProjection,
      writes nothing, mutates nothing;
    - references Memory + Identity only by immutable refs (no root / sink mutation).
    """
    records = [
        r for r in memory.relations
        if r.kind == "relation" and r.content.get("external_ref") == external_ref
    ]
    return RelationshipProjection(
        external_ref=external_ref,
        trust=_derive_trust(records),
        role=_derive_role(records),
        history=_derive_history(records),
        memory_ref=_memory_ref(memory, external_ref),
        identity_ref=_identity_ref(identity),
    )


class RelationshipProjector:
    """Owns the projection act — parallel to ``Reflector`` / ``PersonaProjector`` / ``Planner``.

    The projector is the *behavior owner*; it exposes NO storage / authority-leak method
    (RFC-0111 C1/C2/C3 projection X-firewall): no ``save``, ``persist``, ``write_memory``,
    ``update_identity``, ``rewrite_identity``, ``create_goal``, ``modify_goal``, ``create_memory``.
    Relationship is *modeled*, never stored; it holds no Life authority (it cannot mutate
    Identity/Need/Drive/Goal, and it does not own a Memory store).
    """

    def project(self, memory: Memory, identity: Identity, external_ref: str) -> RelationshipProjection:
        return derive_relationship(memory, identity, external_ref)
