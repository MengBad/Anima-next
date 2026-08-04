"""Reflection Engine (M1.9) — Reflection Authority's transform act.

RFC-0109: ``Reflection`` is a **Transform Act** (a Mind-layer process), *not* a Constitution
State, *not* an Owned object, *not* a Memory container, *not* a cognitive authority root.
The Reflection Authority owns the *interpretation* (what to sediment); the Memory Authority
owns *storage* (RFC-0109 C2). They are separate — this is the 9th symmetric Authority boundary.

- ``reflect(execution_result)`` is the transform ``reflect``: it *reads* an ExecutionResult
  read-only and *emits* a ``ReflectionResult`` (transient transform output). It is deterministic
  given the same ExecutionResult and **mutates nothing** — not the ExecutionResult, not Goal,
  not Drive, not Memory (RFC-0109 C1/C2/C3). A real Reflection would invoke an LLM to distill
  lessons; M1.9 *heuristically simulates* the transform to prove the boundary without building
  cognition infra (RFC-0109 NG1: no self-model writer, no Goal editor, no memory store).
- ``Reflector`` is the Reflection Authority's transform owner: it holds the *act* of interpreting
  an ExecutionResult, never a Constitution state, and never edits Goal/Drive/Identity/Memory
  (RFC-0109 C1/C2). It exposes no ``patch_goal`` / ``update_identity`` / ``modify_drive`` /
  ``write_memory`` / ``create_persona`` method (the transform X-firewall, §6.2).

**0100C watchpoint (RFC-0109 C4):** transaction / conflict semantics were validated at Memory
Ingestion (M1.8) with no observed contradiction. Reflection is pure read→emit, so it adds no new
transaction need. RFC-0100C stays deferred (9th). If a future *real* Reflection needs conflict
resolution the Foundation cannot express, that routes via Foundation Evolution F1 — not a silent
expansion here.
"""
from __future__ import annotations

import hashlib
import json

from anima.constitution.errors import ConstitutionViolation
from anima.constitution.state import ExecutionResult, ExecutionStatus, ReflectionResult


def reflect(execution_result: ExecutionResult) -> ReflectionResult:
    """Interpret an ExecutionResult into Memory-ready records — the transform act.

    Reads the ExecutionResult read-only; emits a ``ReflectionResult``. Must NOT mutate the
    ``execution_result``, Goal, Drive, or any Memory (RFC-0109 C1/C2/C3).

    M1.9 simulates the cognitive transform: each ``lesson`` sediments as a semantic/skill
    record, and a successful carrying sediments an episode capturing the outcome. A real
    Reflection would distill these via an LLM; the boundary is what M1.9 proves (RFC-0109 NG1).

    Purity: the emitted ``ReflectionResult.timestamp`` echoes the consumed ExecutionResult's
    timestamp, so the same input object yields the same output object (deterministic transform,
    no hidden state) — satisfying the ``same input ⇒ same output`` contract of every M1 engine.
    """
    records = []

    # The carrying episode (if the Plan was actually carried into the Universe). This is the
    # *experience* record; it references the outcome, not a Goal handle (RFC-0108 C1 / C3).
    if execution_result.status in (ExecutionStatus.SUCCESS, ExecutionStatus.PARTIAL):
        records.append(
            {
                "kind": "episode",
                "content": {
                    "event": execution_result.outcome,
                    "status": execution_result.status.value,
                },
                "salience": 0.7 if execution_result.status is ExecutionStatus.SUCCESS else 0.4,
            }
        )

    # Each learned lesson sediments as a semantic record (the *learned* shape,
    # RFC-0109 §5: Learning is the *outcome* of Reflection, not a second store).
    for lesson in execution_result.lessons:
        records.append({"kind": "semantic", "content": {"lesson": lesson}, "salience": 0.6})

    # result_ref references THIS ExecutionResult via a content fingerprint — it leaks no
    # Goal/Plan handle (RFC-0108 C1: not the Goal; here, not even a Plan object).
    result_ref = _fingerprint(execution_result)

    # timestamp echoes the input so the transform is pure (same input ⇒ same output).
    return ReflectionResult(
        result_ref=result_ref, records=records, timestamp=execution_result.timestamp
    )


def _fingerprint(result: ExecutionResult) -> str:
    """Deterministic content fingerprint of an ExecutionResult (references it, not upstream)."""
    payload = json.dumps(
        [
            result.plan_ref,
            result.status.value,
            result.outcome,
            list(result.lessons),
        ],
        sort_keys=True,
        default=str,
    )
    return "exec#" + hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


class Reflector:
    """Reflection Authority's transform owner (RFC-0109 §6.1).

    Owns the *act* of interpreting an ExecutionResult into a ReflectionResult; owns NO
    Constitution state, and never edits Goal/Drive/Identity/Need/Affect or Memory (RFC-0109
    C1/C2). The transform X-firewall: it exposes no upstream/downstream mutation method, and
    no storage API (it is a *producer*, not a second store — RFC-0109 C3). The 0100C transaction
    question lives at ingestion (M1.8); Reflection is pure read→emit and adds no transaction need.
    """

    def interpret(self, execution_result: ExecutionResult) -> ReflectionResult:
        """Transform an ExecutionResult into a ReflectionResult. Does not mutate the input."""
        return reflect(execution_result)

    # Intentionally ABSENT (transform X-firewall, RFC-0109 C1/C2/C3):
    #   patch_goal / modify_goal / set_goal
    #   modify_drive / update_identity / apply_to_need / set_affect
    #   save / persist / write_memory / create_persona / store_memory
    # Reflection interprets experience into Memory records; it never edits a life state or
    # owns a store. The Memory Authority (RFC-0108) is the only sink; Reflection is its producer.
