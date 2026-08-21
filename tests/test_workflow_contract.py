from __future__ import annotations

import pytest

from anima.observer_contract import OfflineObserver, SyntheticEvidence
from anima.operator_action_contract import OfflineOperatorActionPlanner, SyntheticAuthorization
from anima.report_contract import OfflineReportBuilder, verify_report
from anima.workflow_contract import (
    OfflineWorkflowBuilder,
    VerificationEvidence,
    WorkflowStatus,
    verify_workflow,
)


NOW = "2026-08-11T00:00:00Z"
FUTURE = "2026-08-11T01:00:00Z"


def _chain():
    evidence = (SyntheticEvidence("e-1", "synthetic", "active", NOW),)
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
        findings=("active observed",),
        generated_at=NOW,
    )
    report_verification = verify_report(report, observation, evidence)
    authorization = SyntheticAuthorization(
        authorization_id="auth-1",
        operator_id="operator-synthetic",
        role="operator",
        action_type="review_only",
        subject=report.subject,
        report_digest=report.report_digest,
        issued_at=NOW,
        expires_at=FUTURE,
    )
    action_result = OfflineOperatorActionPlanner().plan(
        action_id="action-1",
        report=report,
        report_verification=report_verification,
        authorization=authorization,
        created_at=NOW,
        now=NOW,
    )
    assert action_result.action is not None
    verification = VerificationEvidence.create(
        verification_id="verification-1",
        workflow_id="workflow-1",
        action_id=action_result.action.action_id,
        result="action record verified; execution absent",
        observed_at=NOW,
    )
    return observation, report, report_verification, authorization, action_result.action, verification


def test_complete_chain_builds_and_verifies():
    observation, report, report_verification, authorization, action, verification = _chain()
    workflow = OfflineWorkflowBuilder().build(
        workflow_id="workflow-1",
        observations=(observation,),
        report=report,
        report_verification=report_verification,
        authorization=authorization,
        action=action,
        verification_evidence=(verification,),
        created_at=NOW,
        now=NOW,
    )

    result = verify_workflow(
        workflow,
        observations=(observation,),
        report=report,
        authorization=authorization,
        action=action,
        verification_evidence=(verification,),
        now=NOW,
    )

    assert workflow.status == WorkflowStatus.VERIFIED
    assert result.workflow_valid is True
    assert result.reference_binding is True
    assert result.integrity_valid is True
    assert result.verification_chain_valid is True


@pytest.mark.parametrize(
    "override",
    [
        {"observations": ()},
        {"report": None},
        {"authorization": None},
        {"action": None},
        {"verification_evidence": ()},
    ],
)
def test_missing_workflow_stage_fails_closed(override):
    observation, report, report_verification, authorization, action, verification = _chain()
    values = {
        "observations": (observation,),
        "report": report,
        "report_verification": report_verification,
        "authorization": authorization,
        "action": action,
        "verification_evidence": (verification,),
    }
    values.update(override)
    workflow = OfflineWorkflowBuilder().build(
        workflow_id="workflow-1",
        created_at=NOW,
        now=NOW,
        **values,
    )

    assert workflow.status == WorkflowStatus.FAILED
    assert workflow.errors


def test_tampered_workflow_digest_is_rejected():
    observation, report, report_verification, authorization, action, verification = _chain()
    workflow = OfflineWorkflowBuilder().build(
        workflow_id="workflow-1",
        observations=(observation,),
        report=report,
        report_verification=report_verification,
        authorization=authorization,
        action=action,
        verification_evidence=(verification,),
        created_at=NOW,
        now=NOW,
    )
    tampered = workflow.__class__(
        workflow_id=workflow.workflow_id,
        observation_refs=workflow.observation_refs,
        report_ref=workflow.report_ref,
        authorization_ref=workflow.authorization_ref,
        action_ref=workflow.action_ref,
        verification_refs=workflow.verification_refs,
        status=workflow.status,
        workflow_digest="0" * 64,
        errors=workflow.errors,
    )

    result = verify_workflow(
        tampered,
        observations=(observation,),
        report=report,
        authorization=authorization,
        action=action,
        verification_evidence=(verification,),
        now=NOW,
    )
    assert result.workflow_valid is False
    assert result.integrity_valid is False
    assert "workflow digest mismatch" in result.errors


def test_reference_mismatch_and_expired_authorization_fail():
    observation, report, report_verification, authorization, action, verification = _chain()
    wrong_verification = VerificationEvidence.create(
        verification_id="verification-2",
        workflow_id="other-workflow",
        action_id=action.action_id,
        result="wrong workflow",
        observed_at=NOW,
    )
    workflow = OfflineWorkflowBuilder().build(
        workflow_id="workflow-1",
        observations=(observation,),
        report=report,
        report_verification=report_verification,
        authorization=authorization,
        action=action,
        verification_evidence=(wrong_verification,),
        created_at=NOW,
        now=NOW,
    )

    result = verify_workflow(
        workflow,
        observations=(observation,),
        report=report,
        authorization=authorization,
        action=action,
        verification_evidence=(wrong_verification,),
        now=NOW,
    )
    assert result.workflow_valid is False
    assert "verification workflow mismatch" in result.errors


def test_workflow_has_no_execution_api():
    assert not hasattr(OfflineWorkflowBuilder(), "execute")
    assert not hasattr(OfflineWorkflowBuilder(), "repair")
    assert not hasattr(OfflineWorkflowBuilder(), "persist")
