from __future__ import annotations

import copy

import pytest

from anima.observer_contract import (
    ObservationRecord,
    ObserverContractError,
    OfflineObserver,
    SyntheticEvidence,
    verify_observation,
)


NOW = "2026-08-11T00:00:00Z"


def _evidence(evidence_id="e-1"):
    return SyntheticEvidence(
        evidence_id=evidence_id,
        evidence_type="synthetic-runtime-state",
        content="service active; no action taken",
        observed_at=NOW,
    )


def _observe(state=None):
    return OfflineObserver().observe(
        observer_id="observer-synthetic",
        observed_at=NOW,
        subject="synthetic-anima",
        runtime_state=state or {"active": True, "restart_count": 0},
        evidence=(_evidence(),),
    )


def test_valid_synthetic_observation_is_verifiable():
    record = _observe()

    result = verify_observation(record, (_evidence(),))

    assert isinstance(record, ObservationRecord)
    assert result.valid is True
    assert result.digest_match is True
    assert result.evidence_complete is True


def test_observation_copies_input_and_freezes_runtime_state():
    state = {"active": True, "resources": {"cpu": 1}}
    before = copy.deepcopy(state)
    record = _observe(state)
    state["active"] = False
    state["resources"]["cpu"] = 99

    assert state != before
    assert record.runtime_state["active"] is True
    assert record.runtime_state["resources"]["cpu"] == 1
    with pytest.raises(TypeError):
        record.runtime_state["active"] = False  # type: ignore[index]


def test_missing_evidence_fails_closed():
    record = _observe()

    result = verify_observation(record, ())

    assert result.valid is False
    assert result.evidence_complete is False
    assert result.digest_match is False
    assert "missing evidence_refs" in result.errors[0]


def test_tampered_runtime_state_fails_digest_verification():
    record = _observe()
    tampered = ObservationRecord(
        observer_id=record.observer_id,
        observed_at=record.observed_at,
        subject=record.subject,
        evidence_refs=record.evidence_refs,
        runtime_state={"active": False, "restart_count": 0},
        digest=record.digest,
    )

    result = verify_observation(tampered, (_evidence(),))

    assert result.valid is False
    assert result.digest_match is False


def test_observation_requires_subject_and_evidence():
    with pytest.raises(ObserverContractError, match="subject"):
        OfflineObserver().observe(
            observer_id="observer-synthetic",
            observed_at=NOW,
            subject="",
            runtime_state={"active": True},
            evidence=(_evidence(),),
        )

    with pytest.raises(ObserverContractError, match="evidence_refs"):
        OfflineObserver().observe(
            observer_id="observer-synthetic",
            observed_at=NOW,
            subject="synthetic-anima",
            runtime_state={"active": True},
            evidence=(),
        )


def test_observer_has_no_action_or_mutation_api():
    observer = OfflineObserver()

    assert not hasattr(observer, "execute")
    assert not hasattr(observer, "repair")
    assert not hasattr(observer, "persist")
