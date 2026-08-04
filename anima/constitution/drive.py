"""Drive Engine (M1.4) — i(Affect): the fourth derivation.

RFC-0104: ``Drive = i(Affect)``. Drive is operational *orientation* (approach/avoid/
intensity/focus), NOT motivational content and NOT a goal (``Need == Drive`` forbidden;
``drive.create_goal()`` / ``drive.plan()`` forbidden — Planner-pollution guard,
RFC-0104 D1/D3). Drive exists only during one reasoning cycle and has no storage
authority.

Contract: ``i`` reads Affect; same Affect ⇒ same Drive. Drive has NO authority edge to
Goal — only ``Drive → Planner reads → Goal`` (RFC-0104 Boundary B).
"""
from __future__ import annotations

from anima.constitution.state import Affect, Drive, OrientationAxis


def derive_drive(affect: Affect) -> Drive:
    """``i(Affect)`` — crystallize Affect into approach/avoid orientation.

    Heuristic (M1): approach from positive valence × urgency; avoid from negative
    valence × urgency; intensity from arousal; focus from confidence. The life's Need
    stays latent (``activated = False``) when Affect urgency is below the activation
    gate — the Need↔Drive bridge only fires when Affect actually charges it (RFC-0100
    §1.7 activation condition).
    """
    urgency = float(affect.urgency)
    approach = max(0.0, float(affect.valence)) * urgency
    avoid = max(0.0, -float(affect.valence)) * urgency
    orientation = {
        OrientationAxis.APPROACH: round(approach, 3),
        OrientationAxis.AVOID: round(avoid, 3),
        OrientationAxis.INTENSITY: round(float(affect.arousal), 3),
        OrientationAxis.FOCUS: round(float(affect.confidence), 3),
    }
    activated = urgency > 0.3
    return Drive(orientation=orientation, activated=activated)
