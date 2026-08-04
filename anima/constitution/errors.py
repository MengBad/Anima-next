"""Constitution exceptions (M1).

Kept in its own module so ``invariants`` and ``planner`` can both raise
``ConstitutionViolation`` without creating an import cycle (planner is imported
by invariants for the ``derive_plan`` guard; it must not import invariants back).
"""
from __future__ import annotations


class ConstitutionViolation(Exception):
    """Raised when code violates a frozen Constitution invariant."""
