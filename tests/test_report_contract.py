from __future__ import annotations

import copy

import pytest

from anima.observer_contract import OfflineObserver, SyntheticEvidence
from anima.report_contract import (
    OfflineReportBuilder,
    ReportContractError,
    ReportRecord,
    ReportStatus,
    verify_report,
)


NOW = "2026-08-11T00:00:00Z"


def _evidence(evidence_id="e-1"):
    return SyntheticEvidence(
        evidence_id=evidence_id,
        evidence_type="synthetic-runtime-state",
        content="service active; no action taken",
        observed_at=NOW,
    )


def _observation():
    return OfflineObserver().observe(
        observer_id="observer-synthetic",
        observed_at=NOW,
        subject="synthetic-anima",
        runtime_state={"active": True, "restart_count": 0},
        evidence=(_evidence(),),
    )


def _report():
    observation = _observation()
    evidence = (_evidence(),)
    report = OfflineReportBuilder().build(
        report_id="report-1",
        observation=observation,
        evidence=evidence,
        findings=("service observed active",),
        generated_at=NOW,
    )
    return report, observation, evidence


def test_valid_observation_evidence_report_chain_verifies():
    report, observation, evidence = _report()

    result = verify_report(report, observation, evidence)

    assert isinstance(report, ReportRecord)
    assert report.status == ReportStatus.OBSERVED
    assert result.valid is True
    assert result.observation_valid is True
    assert result.report_digest_match is True
    assert result.evidence_complete is True


def test_report_preserves_observation_reference_and_input_immutability():
    observation = _observation()
    evidence = (_evidence(),)
    findings = ["observed active"]
    before = copy.deepcopy(findings)
    report = OfflineReportBuilder().build(
        report_id="report-1",
        observation=observation,
        evidence=evidence,
        findings=tuple(findings),
        generated_at=NOW,
    )
    findings.append("mutated")

    assert findings != before
    assert report.findings == ("observed active",)
    assert report.observation_digest == observation.digest


def test_tampered_finding_fails_report_digest():
    report, observation, evidence = _report()
    tampered = ReportRecord(
        report_id=report.report_id,
        observation_digest=report.observation_digest,
        subject=report.subject,
        generated_at=report.generated_at,
        evidence_refs=report.evidence_refs,
        findings=("changed finding",),
        status=report.status,
        report_digest=report.report_digest,
    )

    result = verify_report(tampered, observation, evidence)

    assert result.valid is False
    assert result.report_digest_match is False


def test_missing_evidence_produces_incomplete_report_and_failed_verification():
    observation = _observation()
    report = OfflineReportBuilder().build(
        report_id="report-1",
        observation=observation,
        evidence=(),
        findings=("evidence unavailable",),
        generated_at=NOW,
    )

    result = verify_report(report, observation, ())

    assert report.status == ReportStatus.INCOMPLETE
    assert result.valid is False
    assert result.observation_valid is False
    assert result.evidence_complete is False


def test_report_requires_references_and_has_no_action_field():
    with pytest.raises(ReportContractError, match="evidence_refs"):
        ReportRecord(
            report_id="report-1",
            observation_digest="0" * 64,
            subject="synthetic",
            generated_at=NOW,
            evidence_refs=(),
            findings=("finding",),
            status=ReportStatus.OBSERVED,
            report_digest="0" * 64,
        )

    assert not hasattr(OfflineReportBuilder(), "execute")
    assert not hasattr(OfflineReportBuilder(), "decide")
