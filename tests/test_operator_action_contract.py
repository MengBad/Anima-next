from __future__ import annotations

import pytest

from anima.observer_contract import OfflineObserver, SyntheticEvidence
from anima.operator_action_contract import (
    ActionContractError,
    ActionStatus,
    OfflineOperatorActionPlanner,
    SyntheticAuthorization,
)
from anima.report_contract import OfflineReportBuilder, verify_report


NOW = "2026-08-11T00:00:00Z"
FUTURE = "2026-08-11T01:00:00Z"


def _evidence():
    return SyntheticEvidence("e-1", "synthetic", "observed active", NOW)


def _chain():
    evidence = (_evidence(),)
    observation = OfflineObserver().observe(
        observer_id="observer-synthetic",
        observed_at=NOW,
        subject="synthetic-anima",
        runtime_state={"active": True},
        evidence=evidence,
    )
    report = OfflineReportBuilder().build(
        report_id="report-1",
        observation=observation,
        evidence=evidence,
        findings=("service observed active",),
        generated_at=NOW,
    )
    return report, verify_report(report, observation, evidence)


def _authorization(report, *, subject=None, expires_at=FUTURE):
    return SyntheticAuthorization(
        authorization_id="auth-1",
        operator_id="operator-synthetic",
        role="operator",
        action_type="review_only",
        subject=subject or report.subject,
        report_digest=report.report_digest,
        issued_at=NOW,
        expires_at=expires_at,
    )


def test_valid_report_and_authorization_create_unexecuted_action_record():
    report, verification = _chain()
    result = OfflineOperatorActionPlanner().plan(
        action_id="action-1",
        report=report,
        report_verification=verification,
        authorization=_authorization(report),
        created_at=NOW,
        now=NOW,
    )

    assert result.allowed is True
    assert result.action is not None
    assert result.action.status == ActionStatus.NOT_EXECUTED
    assert result.action.report_digest == report.report_digest


def test_missing_authorization_is_denied_by_default():
    report, verification = _chain()
    result = OfflineOperatorActionPlanner().plan(
        action_id="action-1",
        report=report,
        report_verification=verification,
        authorization=None,
        created_at=NOW,
        now=NOW,
    )

    assert result.allowed is False
    assert result.action is None
    assert "authorization is required" in result.errors


def test_expired_authorization_is_denied():
    report, verification = _chain()
    with pytest.raises(ActionContractError, match="expires_at"):
        _authorization(report, expires_at=NOW)

    authorization = _authorization(report, expires_at="2026-08-11T00:30:00Z")
    result = OfflineOperatorActionPlanner().plan(
        action_id="action-1",
        report=report,
        report_verification=verification,
        authorization=authorization,
        created_at=NOW,
        now="2026-08-11T00:30:00Z",
    )
    assert result.allowed is False
    assert "authorization is expired" in result.errors


def test_report_or_scope_mismatch_is_denied():
    report, verification = _chain()
    authorization = _authorization(report, subject="other-subject")
    result = OfflineOperatorActionPlanner().plan(
        action_id="action-1",
        report=report,
        report_verification=verification,
        authorization=authorization,
        created_at=NOW,
        now=NOW,
    )

    assert result.allowed is False
    assert "authorization subject does not match report" in result.errors


def test_invalid_report_verification_is_denied():
    report, verification = _chain()
    invalid = verification.__class__(False, False, False, False, ("tampered",))
    result = OfflineOperatorActionPlanner().plan(
        action_id="action-1",
        report=report,
        report_verification=invalid,
        authorization=_authorization(report),
        created_at=NOW,
        now=NOW,
    )

    assert result.allowed is False
    assert "report verification did not pass" in result.errors


def test_planner_has_no_execution_api():
    planner = OfflineOperatorActionPlanner()
    assert not hasattr(planner, "execute")
    assert not hasattr(planner, "dispatch")
    assert not hasattr(planner, "persist")
