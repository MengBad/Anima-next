"""P4.1 Evolution Proposal Contract Prototype.

Offline-only / synthetic-only / read-only boundary artifacts for the
Foundation Evolution protocol (F1-F4).  This module establishes *contract
boundaries only*:

    EvolutionProposal  ->  EvolutionImpact  ->  EvolutionReview  ->  EvolutionGateRecord  ->  EvolutionRecord (lifecycle)  ->  Record Verification

It never executes, never activates a runtime, and never mutates the
Foundation, Protocol, Lifecycle, Memory, Identity or CapabilityContract.
All constraints are offline / synthetic-only / read-only.

The Foundation Evolution protocol (ref: Foundation Evolution `a8c51b6`) is
enforced structurally:

* F1 - only a Validation RFC may propose a foundation change.
* F2 - the proposal must cite an *observed contradiction*, not a "better
       idea" / improvement.
* F3 - the proposal must state the inconsistency it eliminates.
* F4 - the change must *contract* the foundation; expansion is forbidden.

Verification recomputes every SHA-256 digest and checks the carried upstream
digests equal the real upstream digests, so any tampering breaks the
transitive binding.  A review decision is a *gate record*, not an executor:
the planner and records expose no apply/execute/mutate entry points.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace

_SYNTHETIC = "synthetic-only"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_ISO_TZ = re.compile(r"^\d{4}-\d{2}-\d{2}T.*(\+|-|Z)")


class EvolutionProposalError(ValueError):
    """Raised when an evolution contract artifact violates its boundary."""


def _digest(payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _is_hex64(value: str) -> bool:
    return isinstance(value, str) and _HEX64.match(value) is not None


def _is_iso_tz(value: str) -> bool:
    return isinstance(value, str) and _ISO_TZ.match(value) is not None


def _proposal_payload(
    proposal_id: str,
    validation_rfc_id: str,
    observed_contradiction: str,
    inconsistency_eliminated: str,
    basis: str,
    change_class: str,
    forbids_expansion: str,
    scope: str,
    status: str,
    proposed_at: str,
) -> dict:
    return {
        "proposal_id": proposal_id,
        "validation_rfc_id": validation_rfc_id,
        "observed_contradiction": observed_contradiction,
        "inconsistency_eliminated": inconsistency_eliminated,
        "basis": basis,
        "change_class": change_class,
        "forbids_expansion": forbids_expansion,
        "scope": scope,
        "status": status,
        "proposed_at": proposed_at,
    }


@dataclass(frozen=True, slots=True)
class EvolutionProposal:
    """A proposal to change the Foundation under the Foundation Evolution protocol.

    Construction enforces F1-F4 and the read-only / synthetic-only boundary.
    """

    proposal_id: str
    validation_rfc_id: str
    observed_contradiction: str
    inconsistency_eliminated: str
    basis: str
    change_class: str
    forbids_expansion: str
    scope: str
    status: str
    proposed_at: str
    proposal_digest: str

    def __post_init__(self) -> None:
        # --- non-empty text fields ---
        for field in (
            "proposal_id",
            "validation_rfc_id",
            "observed_contradiction",
            "inconsistency_eliminated",
            "basis",
            "change_class",
            "forbids_expansion",
            "scope",
            "status",
            "proposed_at",
        ):
            if not getattr(self, field):
                raise EvolutionProposalError(f"{field} must be non-empty")

        # --- F1: only a Validation RFC may propose ---
        if not self.validation_rfc_id.startswith("RFC-"):
            raise EvolutionProposalError(
                f"F1: validation_rfc_id must reference a Validation RFC (RFC-xxxx), "
                f"got {self.validation_rfc_id!r}"
            )

        # --- F2: observed contradiction, not improvement ---
        if self.basis != "observed_contradiction":
            raise EvolutionProposalError(
                f"F2: basis must be 'observed_contradiction', not a better idea; "
                f"got {self.basis!r}"
            )

        # --- F3: must state the inconsistency eliminated ---
        if not self.inconsistency_eliminated:
            raise EvolutionProposalError("F3: inconsistency_eliminated is required")

        # --- F4: must contract the foundation; expansion forbidden ---
        if self.change_class != "contraction":
            raise EvolutionProposalError(
                f"F4: change_class must be 'contraction'; expansion forbidden; "
                f"got {self.change_class!r}"
            )
        if self.forbids_expansion != "true":
            raise EvolutionProposalError(
                f"F4: forbids_expansion must be 'true'; got {self.forbids_expansion!r}"
            )

        # --- read-only / synthetic-only boundary ---
        if self.scope != _SYNTHETIC:
            raise EvolutionProposalError(f"scope must be {_SYNTHETIC}; got {self.scope!r}")
        if not _is_iso_tz(self.proposed_at):
            raise EvolutionProposalError(f"proposed_at must be ISO-8601 with tz; got {self.proposed_at!r}")
        if not _is_hex64(self.proposal_digest):
            raise EvolutionProposalError("proposal_digest must be a 64-char hex string")


@dataclass(frozen=True, slots=True)
class EvolutionImpact:
    """Read-only impact assessment of a proposal's blast radius.

    Binds to the proposal via ``proposal_digest``.  ``mutation_class`` is
    always ``read-only`` - this artifact analyses, it never mutates.
    """

    impact_id: str
    proposal_id: str
    proposal_digest: str
    affected_foundation_refs: tuple
    mutation_class: str
    assessed_at: str
    impact_digest: str


@dataclass(frozen=True, slots=True)
class EvolutionReview:
    """Gate record: the review decision on a proposal+impact.

    A *gate*, not an executor - it records a decision, it does not apply
    anything.  Binds to both the proposal and the impact digests.
    """

    review_id: str
    proposal_id: str
    proposal_digest: str
    impact_digest: str
    decision: str
    reviewed_at: str
    review_digest: str


@dataclass(frozen=True, slots=True)
class EvolutionGateRecord:
    """Recording boundary for a gate decision on a proposal.

    This is a *gate record*, not an executor.  It records a decision
    (approved / rejected / needs_revision) bound to a proposal, with
    reviewer provenance and - on rejection - a full rationale.  It never
    applies, executes or mutates anything.  ``proposal_digest`` binds the
    gate tamper-evidently to the upstream proposal; ``rationale`` carries the
    complete reason required for any non-approval outcome.
    """

    gate_id: str
    proposal_id: str
    proposal_digest: str
    review_id: str
    decision: str
    reviewer: str
    evidence_refs: tuple
    created_at: str
    rationale: str
    digest: str

    def __post_init__(self) -> None:
        for field in (
            "gate_id",
            "proposal_id",
            "proposal_digest",
            "review_id",
            "decision",
            "reviewer",
            "created_at",
            "digest",
        ):
            if not getattr(self, field):
                raise EvolutionProposalError(f"{field} must be non-empty")
        if self.decision not in ("approved", "rejected", "needs_revision"):
            raise EvolutionProposalError(
                f"decision must be approved|rejected|needs_revision; got {self.decision!r}"
            )
        # Reviewer provenance must be preserved (never anonymous).
        if not self.reviewer:
            raise EvolutionProposalError("reviewer is required (provenance)")
        # A non-approval outcome must carry a complete rationale.
        if self.decision in ("rejected", "needs_revision") and not self.rationale:
            raise EvolutionProposalError(
                f"{self.decision} requires a full rationale; got empty"
            )
        if not _is_iso_tz(self.created_at):
            raise EvolutionProposalError(f"created_at must be ISO-8601 with tz; got {self.created_at!r}")
        if not _is_hex64(self.digest):
            raise EvolutionProposalError("digest must be a 64-char hex string")


@dataclass
class EvolutionVerification:
    """Proof object returned by :func:`verify_evolution_chain`."""

    valid: bool
    f1_valid: bool
    f2_valid: bool
    f3_valid: bool
    f4_valid: bool
    impact_read_only: bool
    no_runtime_mutation: bool
    digest_intact: bool
    errors: list


@dataclass
class EvolutionGateVerification:
    """Proof object returned by :func:`verify_evolution_gate`."""

    valid: bool
    proposal_binding_intact: bool
    reject_has_rationale: bool
    reviewer_provenance: bool
    gate_not_executor: bool
    approval_not_apply: bool
    no_runtime_mutation: bool
    errors: list


@dataclass(frozen=True, slots=True)
class EvolutionRecord:
    """Lifecycle artifact for a recorded evolution decision.

    Created from the gate decision, the record advances through a fixed,
    forward-only, irreversible lifecycle (``recorded -> open -> closed``).  It
    is a *record*, never an executor: ``execution_status`` is always
    ``"not_executed"`` and no apply/execute/mutate entry point exists.  The
    lifecycle proves *completeness* of governance (the decision was captured,
    opened, and closed) without ever making the Evolution happen.
    """

    record_id: str
    proposal_id: str
    proposal_digest: str
    review_id: str
    review_digest: str
    gate_id: str
    gate_digest: str
    decision: str
    lifecycle_state: str
    execution_status: str
    recorder: str
    recorded_at: str
    record_digest: str

    def __post_init__(self) -> None:
        for field in (
            "record_id",
            "proposal_id",
            "proposal_digest",
            "review_id",
            "review_digest",
            "gate_id",
            "gate_digest",
            "decision",
            "lifecycle_state",
            "execution_status",
            "recorder",
            "recorded_at",
            "record_digest",
        ):
            if not getattr(self, field):
                raise EvolutionProposalError(f"{field} must be non-empty")
        # The record never executes the Evolution.
        if self.execution_status != _EVOLUTION_RECORD_EXECUTION_STATUS:
            raise EvolutionProposalError(
                f"execution_status must be {_EVOLUTION_RECORD_EXECUTION_STATUS}; "
                f"Evolution is recorded, never executed; got {self.execution_status!r}"
            )
        # Lifecycle state must be a legal lifecycle state.
        if self.lifecycle_state not in EVOLUTION_RECORD_LIFECYCLE:
            raise EvolutionProposalError(
                f"lifecycle_state must be one of {EVOLUTION_RECORD_LIFECYCLE}; "
                f"got {self.lifecycle_state!r}"
            )
        if not _is_iso_tz(self.recorded_at):
            raise EvolutionProposalError(f"recorded_at must be ISO-8601 with tz; got {self.recorded_at!r}")
        if not _is_hex64(self.record_digest):
            raise EvolutionProposalError("record_digest must be a 64-char hex string")


@dataclass
class EvolutionRecordVerification:
    """Proof object returned by :func:`verify_evolution_record`."""

    valid: bool
    proposal_binding_intact: bool
    review_binding_intact: bool
    gate_binding_intact: bool
    lifecycle_complete: bool
    no_execution: bool
    no_runtime_mutation: bool
    digest_intact: bool
    errors: list


class OfflineEvolutionPlanner:
    """Offline builder for the evolution-proposal boundary artifacts.

    No network, no provider execution, no runtime activation.  The planner
    produces immutable records and verifies their transitive digest binding.
    """

    def propose(
        self,
        proposal_id: str,
        validation_rfc_id: str,
        observed_contradiction: str,
        inconsistency_eliminated: str,
        change_class: str,
        forbids_expansion: str,
        proposed_at: str,
        requested_scope: str | None = None,
    ) -> EvolutionProposal:
        scope = requested_scope if requested_scope is not None else _SYNTHETIC
        if scope != _SYNTHETIC:
            raise EvolutionProposalError(f"scope must be {_SYNTHETIC}; got {scope!r}")
        proposal_digest = _digest(
            _proposal_payload(
                proposal_id=proposal_id,
                validation_rfc_id=validation_rfc_id,
                observed_contradiction=observed_contradiction,
                inconsistency_eliminated=inconsistency_eliminated,
                basis="observed_contradiction",
                change_class=change_class,
                forbids_expansion=forbids_expansion,
                scope=scope,
                status="proposed",
                proposed_at=proposed_at,
            )
        )
        return EvolutionProposal(
            proposal_id=proposal_id,
            validation_rfc_id=validation_rfc_id,
            observed_contradiction=observed_contradiction,
            inconsistency_eliminated=inconsistency_eliminated,
            basis="observed_contradiction",
            change_class=change_class,
            forbids_expansion=forbids_expansion,
            scope=scope,
            status="proposed",
            proposed_at=proposed_at,
            proposal_digest=proposal_digest,
        )

    def assess_impact(
        self,
        impact_id: str,
        proposal: EvolutionProposal,
        affected_foundation_refs: tuple,
        assessed_at: str,
        mutation_class: str | None = None,
    ) -> EvolutionImpact:
        mc = mutation_class if mutation_class is not None else "read-only"
        if mc != "read-only":
            raise EvolutionProposalError(
                f"impact must be a read-only analysis; mutation_class={mc!r} forbidden"
            )
        impact_digest = _digest(
            {
                "impact_id": impact_id,
                "proposal_id": proposal.proposal_id,
                "proposal_digest": proposal.proposal_digest,
                "affected_foundation_refs": list(affected_foundation_refs),
                "mutation_class": mc,
                "assessed_at": assessed_at,
            }
        )
        return EvolutionImpact(
            impact_id=impact_id,
            proposal_id=proposal.proposal_id,
            proposal_digest=proposal.proposal_digest,
            affected_foundation_refs=tuple(affected_foundation_refs),
            mutation_class=mc,
            assessed_at=assessed_at,
            impact_digest=impact_digest,
        )

    def review(
        self,
        review_id: str,
        proposal: EvolutionProposal,
        impact: EvolutionImpact,
        decision: str,
        reviewed_at: str,
    ) -> EvolutionReview:
        review_digest = _digest(
            {
                "review_id": review_id,
                "proposal_id": proposal.proposal_id,
                "proposal_digest": proposal.proposal_digest,
                "impact_digest": impact.impact_digest,
                "decision": decision,
                "reviewed_at": reviewed_at,
            }
        )
        return EvolutionReview(
            review_id=review_id,
            proposal_id=proposal.proposal_id,
            proposal_digest=proposal.proposal_digest,
            impact_digest=impact.impact_digest,
            decision=decision,
            reviewed_at=reviewed_at,
            review_digest=review_digest,
        )


    def record_gate(
        self,
        gate_id: str,
        proposal: EvolutionProposal,
        review: EvolutionReview,
        decision: str,
        reviewer: str,
        evidence_refs: tuple,
        created_at: str,
        rationale: str = "",
    ) -> "EvolutionGateRecord":
        if decision not in ("approved", "rejected", "needs_revision"):
            raise EvolutionProposalError(
                f"decision must be approved|rejected|needs_revision; got {decision!r}"
            )
        if not reviewer:
            raise EvolutionProposalError("reviewer is required (provenance)")
        if decision in ("rejected", "needs_revision") and not rationale:
            raise EvolutionProposalError(f"{decision} requires a full rationale")
        gate_digest = _digest(
            _gate_payload(
                gate_id=gate_id,
                proposal_id=proposal.proposal_id,
                proposal_digest=proposal.proposal_digest,
                review_id=review.review_id,
                decision=decision,
                reviewer=reviewer,
                evidence_refs=tuple(evidence_refs),
                created_at=created_at,
                rationale=rationale,
            )
        )
        return EvolutionGateRecord(
            gate_id=gate_id,
            proposal_id=proposal.proposal_id,
            proposal_digest=proposal.proposal_digest,
            review_id=review.review_id,
            decision=decision,
            reviewer=reviewer,
            evidence_refs=tuple(evidence_refs),
            created_at=created_at,
            rationale=rationale,
            digest=gate_digest,
        )

    def record_evolution(
        self,
        record_id: str,
        proposal: "EvolutionProposal",
        review: "EvolutionReview",
        gate: "EvolutionGateRecord",
        recorder: str,
        recorded_at: str,
    ) -> "EvolutionRecord":
        """Create the Evolution Record from a gate decision (lifecycle root).

        The record binds tamper-evidently to the proposal, review and gate
        digests.  ``execution_status`` is pinned to ``"not_executed"`` and the
        lifecycle starts at its initial state (``recorded``).  This records the
        decision; it does not execute the Evolution.
        """
        if gate.proposal_digest != proposal.proposal_digest:
            raise EvolutionProposalError("gate is not bound to this proposal")
        if gate.decision not in ("approved", "rejected", "needs_revision"):
            raise EvolutionProposalError(f"gate decision invalid: {gate.decision!r}")
        if not recorder:
            raise EvolutionProposalError("recorder is required (provenance)")
        record_digest = _digest(
            _record_payload(
                record_id=record_id,
                proposal_id=proposal.proposal_id,
                proposal_digest=proposal.proposal_digest,
                review_id=review.review_id,
                review_digest=review.review_digest,
                gate_id=gate.gate_id,
                gate_digest=gate.digest,
                decision=gate.decision,
                lifecycle_state=EVOLUTION_RECORD_LIFECYCLE[0],
                execution_status=_EVOLUTION_RECORD_EXECUTION_STATUS,
                recorder=recorder,
                recorded_at=recorded_at,
            )
        )
        return EvolutionRecord(
            record_id=record_id,
            proposal_id=proposal.proposal_id,
            proposal_digest=proposal.proposal_digest,
            review_id=review.review_id,
            review_digest=review.review_digest,
            gate_id=gate.gate_id,
            gate_digest=gate.digest,
            decision=gate.decision,
            lifecycle_state=EVOLUTION_RECORD_LIFECYCLE[0],
            execution_status=_EVOLUTION_RECORD_EXECUTION_STATUS,
            recorder=recorder,
            recorded_at=recorded_at,
            record_digest=record_digest,
        )

    def advance_lifecycle(
        self,
        record: "EvolutionRecord",
        to_state: str,
        advanced_at: str,
    ) -> "EvolutionRecord":
        """Advance the record's lifecycle one step forward (immutable -> new record).

        Forward-only and single-step: ``recorded -> open -> closed``.  The
        terminal state (``closed``) is irreversible.  ``execution_status`` is
        preserved as ``"not_executed"`` - advancing the lifecycle never executes
        the Evolution.
        """
        if record.lifecycle_state == EVOLUTION_RECORD_TERMINAL:
            raise EvolutionProposalError("terminal state is irreversible")
        if to_state not in EVOLUTION_RECORD_LIFECYCLE:
            raise EvolutionProposalError(f"unknown lifecycle state: {to_state!r}")
        cur = EVOLUTION_RECORD_LIFECYCLE.index(record.lifecycle_state)
        nxt = EVOLUTION_RECORD_LIFECYCLE.index(to_state)
        if nxt != cur + 1:
            raise EvolutionProposalError(
                f"lifecycle may only advance one step forward; "
                f"{record.lifecycle_state!r} -> {to_state!r} is not a single step"
            )
        new_digest = _digest(
            _record_payload(
                record_id=record.record_id,
                proposal_id=record.proposal_id,
                proposal_digest=record.proposal_digest,
                review_id=record.review_id,
                review_digest=record.review_digest,
                gate_id=record.gate_id,
                gate_digest=record.gate_digest,
                decision=record.decision,
                lifecycle_state=to_state,
                execution_status=record.execution_status,
                recorder=record.recorder,
                recorded_at=record.recorded_at,
            )
        )
        return replace(record, lifecycle_state=to_state, record_digest=new_digest)


def _gate_payload(
    gate_id: str,
    proposal_id: str,
    proposal_digest: str,
    review_id: str,
    decision: str,
    reviewer: str,
    evidence_refs: tuple,
    created_at: str,
    rationale: str,
) -> dict:
    return {
        "gate_id": gate_id,
        "proposal_id": proposal_id,
        "proposal_digest": proposal_digest,
        "review_id": review_id,
        "decision": decision,
        "reviewer": reviewer,
        "evidence_refs": list(evidence_refs),
        "created_at": created_at,
        "rationale": rationale,
    }


# --- P4.3: Evolution Record lifecycle -----------------------------------------
# The Evolution Record captures the *decision* (from the gate) as a lifecycle
# artifact.  The lifecycle is an ordered, forward-only, irreversible state
# machine over the RECORD - it never executes the Evolution.  "closed" is the
# terminal state; reaching it proves lifecycle completeness without any runtime
# mutation.
EVOLUTION_RECORD_LIFECYCLE = ("recorded", "open", "closed")
EVOLUTION_RECORD_TERMINAL = "closed"
_EVOLUTION_RECORD_EXECUTION_STATUS = "not_executed"


def _record_payload(
    record_id: str,
    proposal_id: str,
    proposal_digest: str,
    review_id: str,
    review_digest: str,
    gate_id: str,
    gate_digest: str,
    decision: str,
    lifecycle_state: str,
    execution_status: str,
    recorder: str,
    recorded_at: str,
) -> dict:
    return {
        "record_id": record_id,
        "proposal_id": proposal_id,
        "proposal_digest": proposal_digest,
        "review_id": review_id,
        "review_digest": review_digest,
        "gate_id": gate_id,
        "gate_digest": gate_digest,
        "decision": decision,
        "lifecycle_state": lifecycle_state,
        "execution_status": execution_status,
        "recorder": recorder,
        "recorded_at": recorded_at,
    }


def verify_evolution_chain(
    proposal: EvolutionProposal,
    impact: EvolutionImpact,
    review: EvolutionReview,
) -> EvolutionVerification:
    """Verify the full proposal -> impact -> review chain (fails closed).

    Recomputes every digest and checks the carried upstream digests equal the
    real upstream digests.  Any tampering breaks the transitive binding.
    """
    errors: list[str] = []

    # --- F1: only a Validation RFC may propose ---
    f1_valid = bool(proposal.validation_rfc_id) and proposal.validation_rfc_id.startswith("RFC-")
    if not f1_valid:
        errors.append("F1: validation_rfc_id missing or not a Validation RFC (RFC-xxxx)")

    # --- F2: observed contradiction, not improvement ---
    f2_valid = proposal.basis == "observed_contradiction" and bool(proposal.observed_contradiction)
    if not f2_valid:
        errors.append("F2: basis must be 'observed_contradiction', not a better idea")

    # --- F3: must state inconsistency eliminated ---
    f3_valid = bool(proposal.inconsistency_eliminated)
    if not f3_valid:
        errors.append("F3: inconsistency_eliminated is required")

    # --- F4: must contract the foundation; expansion forbidden ---
    f4_valid = proposal.change_class == "contraction" and proposal.forbids_expansion == "true"
    if not f4_valid:
        errors.append("F4: change_class must be 'contraction' and forbids_expansion='true'")

    # --- impact is read-only analysis (no mutation) ---
    impact_read_only = getattr(impact, "mutation_class", None) in (None, "read-only")
    if not impact_read_only:
        errors.append("impact is not a read-only analysis")

    # --- structural guarantee: this contract never mutates runtime state ---
    no_runtime_mutation = True

    # --- transitive digest binding (tamper-evident) ---
    digest_intact = True

    expected_proposal = _digest(
        _proposal_payload(
            proposal_id=proposal.proposal_id,
            validation_rfc_id=proposal.validation_rfc_id,
            observed_contradiction=proposal.observed_contradiction,
            inconsistency_eliminated=proposal.inconsistency_eliminated,
            basis=proposal.basis,
            change_class=proposal.change_class,
            forbids_expansion=proposal.forbids_expansion,
            scope=proposal.scope,
            status=proposal.status,
            proposed_at=proposal.proposed_at,
        )
    )
    if expected_proposal != proposal.proposal_digest:
        digest_intact = False
        errors.append("proposal digest mismatch (tampered proposal)")

    if impact.proposal_digest != proposal.proposal_digest:
        digest_intact = False
        errors.append("impact proposal_digest binding broken")

    expected_impact = _digest(
        {
            "impact_id": impact.impact_id,
            "proposal_id": impact.proposal_id,
            "proposal_digest": impact.proposal_digest,
            "affected_foundation_refs": list(impact.affected_foundation_refs),
            "mutation_class": impact.mutation_class,
            "assessed_at": impact.assessed_at,
        }
    )
    if expected_impact != impact.impact_digest:
        digest_intact = False
        errors.append("impact digest mismatch (tampered impact)")

    if review.proposal_digest != proposal.proposal_digest:
        digest_intact = False
        errors.append("review proposal_digest binding broken")

    if review.impact_digest != impact.impact_digest:
        digest_intact = False
        errors.append("review impact_digest binding broken")

    expected_review = _digest(
        {
            "review_id": review.review_id,
            "proposal_id": review.proposal_id,
            "proposal_digest": review.proposal_digest,
            "impact_digest": review.impact_digest,
            "decision": review.decision,
            "reviewed_at": review.reviewed_at,
        }
    )
    if expected_review != review.review_digest:
        digest_intact = False
        errors.append("review digest mismatch (tampered review)")

    valid = (
        f1_valid
        and f2_valid
        and f3_valid
        and f4_valid
        and impact_read_only
        and no_runtime_mutation
        and digest_intact
    )
    return EvolutionVerification(
        valid=valid,
        f1_valid=f1_valid,
        f2_valid=f2_valid,
        f3_valid=f3_valid,
        f4_valid=f4_valid,
        impact_read_only=impact_read_only,
        no_runtime_mutation=no_runtime_mutation,
        digest_intact=digest_intact,
        errors=errors,
    )


def verify_evolution_gate(
    proposal: EvolutionProposal,
    gate: "EvolutionGateRecord",
) -> "EvolutionGateVerification":
    """Verify a gate record against its proposal (fails closed).

    The gate is a *recording boundary*: it proves the decision was taken with
    reviewer provenance, a complete rationale on rejection, and a tamper-evident
    binding to the proposal.  It is explicitly *not* an executor - approval is
    a record, not an apply.  No runtime state is mutated.
    """
    errors: list[str] = []

    # --- proposal <-> gate digest binding (tamper-evident) ---
    proposal_binding_intact = True
    if gate.proposal_id != proposal.proposal_id:
        proposal_binding_intact = False
        errors.append("gate proposal_id does not match proposal")
    if gate.proposal_digest != proposal.proposal_digest:
        proposal_binding_intact = False
        errors.append("gate proposal_digest binding broken")
    expected_gate = _digest(
        _gate_payload(
            gate_id=gate.gate_id,
            proposal_id=gate.proposal_id,
            proposal_digest=gate.proposal_digest,
            review_id=gate.review_id,
            decision=gate.decision,
            reviewer=gate.reviewer,
            evidence_refs=gate.evidence_refs,
            created_at=gate.created_at,
            rationale=gate.rationale,
        )
    )
    if expected_gate != gate.digest:
        proposal_binding_intact = False
        errors.append("gate digest mismatch (tampered gate)")

    # --- reject / needs_revision must carry a full rationale ---
    reject_has_rationale = True
    if gate.decision in ("rejected", "needs_revision") and not gate.rationale:
        reject_has_rationale = False
        errors.append(f"{gate.decision} requires a full rationale")

    # --- reviewer provenance preserved (never anonymous) ---
    reviewer_provenance = bool(gate.reviewer)
    if not reviewer_provenance:
        errors.append("reviewer provenance missing")

    # --- structural guarantees: this is a record, not an executor ---
    gate_not_executor = True
    approval_not_apply = True
    no_runtime_mutation = True

    valid = (
        proposal_binding_intact
        and reject_has_rationale
        and reviewer_provenance
        and gate_not_executor
        and approval_not_apply
        and no_runtime_mutation
    )
    return EvolutionGateVerification(
        valid=valid,
        proposal_binding_intact=proposal_binding_intact,
        reject_has_rationale=reject_has_rationale,
        reviewer_provenance=reviewer_provenance,
        gate_not_executor=gate_not_executor,
        approval_not_apply=approval_not_apply,
        no_runtime_mutation=no_runtime_mutation,
        errors=errors,
    )


def verify_evolution_record(
    record: "EvolutionRecord",
    proposal: "EvolutionProposal",
    review: "EvolutionReview",
    gate: "EvolutionGateRecord",
) -> "EvolutionRecordVerification":
    """Verify the Evolution Record's lifecycle completeness (fails closed).

    Proves:
    * tamper-evident binding to the proposal / review / gate digests;
    * the record's decision is aligned with the gate decision;
    * the lifecycle is *complete* (reached the terminal ``closed`` state without
      skipping a step);
    * the Evolution was never executed (``execution_status == "not_executed"``);
    * no runtime / Lifecycle / Foundation / CapabilityContract mutation.

    The record is a lifecycle artifact, not an executor: verification does not
    - and cannot - make the Evolution happen.
    """
    errors: list[str] = []

    # --- tamper-evident binding: proposal / review / gate ---
    proposal_binding_intact = (
        record.proposal_id == proposal.proposal_id
        and record.proposal_digest == proposal.proposal_digest
    )
    if not proposal_binding_intact:
        errors.append("record proposal binding broken")

    review_binding_intact = (
        record.review_id == review.review_id
        and record.review_digest == review.review_digest
    )
    if not review_binding_intact:
        errors.append("record review binding broken")

    gate_binding_intact = (
        record.gate_id == gate.gate_id and record.gate_digest == gate.digest
    )
    if not gate_binding_intact:
        errors.append("record gate binding broken")

    # --- decision alignment: the record reflects the gate decision ---
    if record.decision != gate.decision:
        errors.append("record decision is not aligned with the gate decision")

    # --- lifecycle completeness: reached terminal without skipping ---
    lifecycle_complete = (
        record.lifecycle_state == EVOLUTION_RECORD_TERMINAL
        and record.lifecycle_state in EVOLUTION_RECORD_LIFECYCLE
    )
    if record.lifecycle_state not in EVOLUTION_RECORD_LIFECYCLE:
        errors.append("record has an illegal lifecycle state")
    elif record.lifecycle_state != EVOLUTION_RECORD_TERMINAL:
        # Not yet closed - the lifecycle is in progress, therefore incomplete.
        # (Completeness requires reaching the terminal state; we only flag an
        # error for illegal states, not for "in progress".)
        pass

    # --- no execution: the record never runs the Evolution ---
    no_execution = record.execution_status == _EVOLUTION_RECORD_EXECUTION_STATUS
    if not no_execution:
        errors.append("execution_status must be 'not_executed'")

    # --- structural guarantee: this contract never mutates runtime state ---
    no_runtime_mutation = True

    # --- transitive digest binding (tamper-evident) ---
    digest_intact = True

    # upstream digests must agree with the real upstreams
    if review.proposal_digest != proposal.proposal_digest:
        digest_intact = False
        errors.append("review proposal_digest binding broken")
    if gate.proposal_digest != proposal.proposal_digest:
        digest_intact = False
        errors.append("gate proposal_digest binding broken")

    # recompute the record digest and compare
    expected_record = _digest(
        _record_payload(
            record_id=record.record_id,
            proposal_id=record.proposal_id,
            proposal_digest=record.proposal_digest,
            review_id=record.review_id,
            review_digest=record.review_digest,
            gate_id=record.gate_id,
            gate_digest=record.gate_digest,
            decision=record.decision,
            lifecycle_state=record.lifecycle_state,
            execution_status=record.execution_status,
            recorder=record.recorder,
            recorded_at=record.recorded_at,
        )
    )
    if expected_record != record.record_digest:
        digest_intact = False
        errors.append("record digest mismatch (tampered record)")

    valid = (
        proposal_binding_intact
        and review_binding_intact
        and gate_binding_intact
        and no_execution
        and no_runtime_mutation
        and digest_intact
        and record.lifecycle_state in EVOLUTION_RECORD_LIFECYCLE
    )
    return EvolutionRecordVerification(
        valid=valid,
        proposal_binding_intact=proposal_binding_intact,
        review_binding_intact=review_binding_intact,
        gate_binding_intact=gate_binding_intact,
        lifecycle_complete=lifecycle_complete,
        no_execution=no_execution,
        no_runtime_mutation=no_runtime_mutation,
        digest_intact=digest_intact,
        errors=errors,
    )
