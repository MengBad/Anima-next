"""Persona Engine (M1.10) — the projection act (RFC-0110).

Persona = Derived projection of Identity. ``derive_persona(identity) -> Persona`` is a
pure read→emit: it reads Identity, emits a Persona (the presentation mask), writes
nothing, mutates nothing, owns no store, holds no Life authority. It is the *expression
layer* of the self — parallel to how Reflection is the *interpretation* layer and
Execution is the *carrying* layer.

The Persona risk (RFC-0100 §1.4; RFC-0110 central risk) is a derived identity layer
masquerading as a **second Identity root** (``Memory → Persona → Identity`` or a
``Persona Authority`` that writes Identity/Need/Drive/Goal). The projection contract
forbids that mechanically: Persona carries no ``update_identity`` / ``create_goal`` /
``save`` API, and references Identity only by an immutable ref (never a mutable handle),
so it cannot reach back and rewrite the root. This module deliberately imports NO Goal /
Planning / Memory writer — there is no handle to leak (the transform X-firewall, RFC-0110
C2/C3).
"""
from __future__ import annotations

import hashlib

from anima.constitution.state import Identity, Persona


def _identity_ref(identity: Identity) -> str:
    """Deterministic, handle-free ref of an Identity.

    Persona may carry this string but never the Identity object, so the projection cannot
    mutate the root it was derived from (RFC-0110 C1 / RFC-0100B line 162 single-Identity-root).
    """
    payload = repr(
        (
            tuple(sorted(identity.traits.items())),
            tuple(identity.values),
            tuple(sorted(identity.self_model.items())),
        )
    )
    return "identity#" + hashlib.sha1(payload.encode()).hexdigest()[:16]


def _derive_voice(identity: Identity) -> str:
    """A short presentable label derived from traits/values (no state mutation)."""
    top_trait = max(identity.traits.items(), key=lambda kv: kv[1], default=("curious", 0.5))
    top_values = ", ".join(identity.values[:2]) or "open"
    return f"{top_trait[0]} and {top_values}"


def _derive_presentation(identity: Identity) -> dict:
    """The expression projection: a read-only view shaped for presentation."""
    return {
        "traits": dict(identity.traits),
        "values": list(identity.values),
        "self_model": dict(identity.self_model),
    }


def derive_persona(identity: Identity) -> Persona:
    """Project Identity into its presentation mask (Persona).

    Pure, deterministic, read-only on Identity:
    - same Identity ⇒ same Persona (no hidden state / no timestamp);
    - reads Identity, emits a Persona, writes nothing, mutates nothing;
    - references Identity only by immutable ``identity_ref`` (no root mutation).
    """
    return Persona(
        identity_ref=_identity_ref(identity),
        voice=_derive_voice(identity),
        presentation=_derive_presentation(identity),
    )


class PersonaProjector:
    """Owns the projection act — parallel to ``Reflector`` / ``Planner`` / ``ExecutionRuntime``.

    The projector is the *behavior owner*; it exposes NO storage / authority-leak method
    (RFC-0110 C2/C3 transform X-firewall): no ``save``, ``persist``, ``write_memory``,
    ``update_identity``, ``create_goal``, ``modify_identity``. Persona is *expressed*,
    never stored; it holds no Life authority (it cannot mutate Identity/Need/Drive/Goal).
    """

    def project(self, identity: Identity) -> Persona:
        return derive_persona(identity)
