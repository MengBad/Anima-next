"""P6.10-C — Execution Fact <-> HealthEvidence reconciliation (read-only).

Contract (authorization): execution fact and health evidence stay separate
sinks; a read-only consistency view links them via capability identity.

Acceptance (all GREEN):
    A  Cross-sink consistency   - matching fact + evidence -> CONSISTENT
    B  Evidence immutability    - reconcile never mutates either file
    C  Replay safety            - same input -> same linkage
    D  Non-escalation           - execution fact never becomes authorization
Additional (boundary, fail-closed):
    E  Missing health evidence  - MISSING_HEALTH_EVIDENCE, no fabrication
    F  Corrupt health evidence  - SCHEMA_FAILURE, no auto repair
    G  Cross-sink separation    - execution_ledger untouched / not read
    H  Static boundary          - transport / forbidden / frozen imports = 0
"""
from __future__ import annotations

import pathlib
import re
import sys

import pytest

NOW = "2026-08-11T15:00:00+00:00"


# ---------------------------------------------------------------------------
# fixtures built through the REAL upstream vocabularies (schema-mirror pins)
# ---------------------------------------------------------------------------
def _make_execution_fact(tmp_path, capability_id: str = "code.execute"):
    """Build a REAL ExecutionFact through DispatchFlow (P6.10-B)."""
    from anima.capability_execution_dispatch import DispatchFlow

    flow = DispatchFlow(str(tmp_path))
    authz = flow.approve(
        authz_id="authz-p610c-1",
        principal="governance-agent",
        capability_id=capability_id,
        scope="sandbox:read",
        granted_at=NOW,
    )
    draft = flow.dispatch(authz.authz_id)
    fact = flow.record(draft)
    fact_path = tmp_path / "execution_facts" / f"{fact.evidence_id}.json"
    return fact, fact_path


def _make_health_evidence(
    tmp_path,
    capability_id: str = "code.execute",
    executed: bool = True,
    success: bool = True,
    evidence_id: str | None = None,
):
    """Build a REAL HealthEvidence through the upstream store (F8.10)."""
    from anima.capability.health.model import HealthEvidence
    from anima.capability.health.store import HealthEvidenceStore

    ev = HealthEvidence(
        evidence_id=evidence_id or "he-00000000000000000000000000000001",
        capability_id=capability_id,
        provider_id="sandbox.tier3",
        execution_id="('run-1', 't1', 0)",
        observed_at=1723359600.0,
        selected=True,
        executed=executed,
        success=success,
        error_type="",
        duration_ms=12.5,
        metadata={"task_id": "t1"},
    )
    path = tmp_path / "health_evidence.jsonl"
    HealthEvidenceStore(str(path)).append(ev)
    return path


# ---------------------------------------------------------------------------
# A - Cross-sink consistency
# ---------------------------------------------------------------------------
def test_a_cross_sink_consistent(tmp_path):
    from anima.capability_health_evidence_linker import (
        CONSISTENT,
        HealthEvidenceLinker,
    )

    _fact, fact_path = _make_execution_fact(tmp_path, capability_id="code.execute")
    health_path = _make_health_evidence(tmp_path, capability_id="code.execute")

    linkage = HealthEvidenceLinker().link(fact_path, health_path)
    assert linkage.linkage_status == CONSISTENT
    assert linkage.capability_matched is True
    assert linkage.executed_matched is True
    assert len(linkage.linked_health_evidence_ids) == 1


# ---------------------------------------------------------------------------
# B - Evidence immutability (both files byte-identical after reconcile)
# ---------------------------------------------------------------------------
def test_b_evidence_immutable(tmp_path):
    from anima.capability_health_evidence_linker import HealthEvidenceLinker

    _fact, fact_path = _make_execution_fact(tmp_path)
    health_path = _make_health_evidence(tmp_path)
    fact_before = fact_path.read_bytes()
    health_before = health_path.read_bytes()

    linker = HealthEvidenceLinker()
    linkage = linker.link(fact_path, health_path)
    linker.verify(linkage, fact_path, health_path)
    linker.inspect(linkage)

    assert fact_path.read_bytes() == fact_before
    assert health_path.read_bytes() == health_before


# ---------------------------------------------------------------------------
# C - Replay safety (same input -> same linkage digest)
# ---------------------------------------------------------------------------
def test_c_replay_safe_same_linkage(tmp_path):
    from anima.capability_health_evidence_linker import HealthEvidenceLinker

    _fact, fact_path = _make_execution_fact(tmp_path)
    health_path = _make_health_evidence(tmp_path)

    linker = HealthEvidenceLinker()
    l1 = linker.link(fact_path, health_path)
    l2 = linker.link(fact_path, health_path)
    assert l1.linkage_digest == l2.linkage_digest
    assert l1.linked_health_evidence_ids == l2.linked_health_evidence_ids


# ---------------------------------------------------------------------------
# D - Non-escalation (execution fact never becomes authorization)
# ---------------------------------------------------------------------------
def test_d_non_escalation(tmp_path):
    from anima.capability_health_evidence_linker import HealthEvidenceLinker

    _fact, fact_path = _make_execution_fact(tmp_path)
    health_path = _make_health_evidence(
        tmp_path, capability_id="code.execute", executed=True, success=True
    )

    linkage = HealthEvidenceLinker().link(fact_path, health_path)
    verification = HealthEvidenceLinker().verify(linkage, fact_path, health_path)
    assert verification.valid is True
    assert verification.non_escalated is True
    # the linkage itself carries no escalation tokens
    text = " ".join(
        str(linkage.capability_id)
        + " "
        + linkage.linkage_status
        + " "
        + str(linkage.linked_health_evidence_ids)
    ).lower()
    for token in ("authorized_running", "activated", "deployed", "production"):
        assert token not in text


# ---------------------------------------------------------------------------
# E - Missing health evidence (audit state, no fabrication)
# ---------------------------------------------------------------------------
def test_e_missing_health_evidence(tmp_path):
    from anima.capability_health_evidence_linker import (
        MISSING_HEALTH_EVIDENCE,
        HealthEvidenceLinker,
    )

    _fact, fact_path = _make_execution_fact(tmp_path, capability_id="code.execute")
    # no health_evidence.jsonl at all
    missing = tmp_path / "health_evidence.jsonl"

    linkage = HealthEvidenceLinker().link(fact_path, missing)
    assert linkage.linkage_status == MISSING_HEALTH_EVIDENCE
    assert linkage.capability_matched is False
    verification = HealthEvidenceLinker().verify(linkage, fact_path, missing)
    assert verification.valid is False  # fail closed
    # the missing file was NOT created
    assert missing.exists() is False


# ---------------------------------------------------------------------------
# F - Corrupt health evidence (fail closed, no auto repair)
# ---------------------------------------------------------------------------
def test_f_corrupt_health_evidence(tmp_path):
    from anima.capability_health_evidence_linker import (
        SCHEMA_FAILURE,
        HealthEvidenceLinker,
    )

    _fact, fact_path = _make_execution_fact(tmp_path)
    health_path = tmp_path / "health_evidence.jsonl"
    health_path.write_text("not-json\n{broken\n", encoding="utf-8")
    before = health_path.read_bytes()

    linkage = HealthEvidenceLinker().link(fact_path, health_path)
    assert linkage.linkage_status == SCHEMA_FAILURE
    verification = HealthEvidenceLinker().verify(linkage, fact_path, health_path)
    assert verification.valid is False
    assert health_path.read_bytes() == before  # no auto repair


# ---------------------------------------------------------------------------
# G - Cross-sink separation (execution_ledger untouched / not read)
# ---------------------------------------------------------------------------
def test_g_cross_sink_separation(tmp_path):
    from anima.capability_health_evidence_linker import HealthEvidenceLinker

    _fact, fact_path = _make_execution_fact(tmp_path)
    health_path = _make_health_evidence(tmp_path)
    # a decoy ledger dir must remain untouched and unread
    ledger = tmp_path / "execution_ledger"
    ledger.mkdir()
    decoy = ledger / "run__t1__0__deadbeef.json"
    decoy.write_text("{}", encoding="utf-8")

    HealthEvidenceLinker().link(fact_path, health_path)
    assert decoy.read_text(encoding="utf-8") == "{}"


# ---------------------------------------------------------------------------
# H - Static boundary (this test doubles as the scan)
# ---------------------------------------------------------------------------
def test_h_static_boundary():
    src = pathlib.Path(
        "anima/capability_health_evidence_linker.py"
    ).read_text(encoding="utf-8")
    transport = [
        "subprocess",
        r"\bssh\b",
        "paramiko",
        "docker",
        "systemctl",
        r"\brequests\b",
        "httpx",
        "daemon",
        r"\btimer\b",
        r"\bpoll\b",
        r"\bschedule\b",
    ]
    forbidden = [
        r"\.execute\(",
        r"\.invoke\(",
        r"\.activate\(",
        r"\.dispatch\(",
        r"\.put\(",
        r"\.replay\(",
        r"\bdef (apply|execute|activate|mutate|repair|dispatch|write|delete|"
        r"update|patch|start|stop|restart|reload|deploy|save|run|recover|"
        r"put|invoke|replay)\b",
    ]
    frozen = re.compile(
        r"^\s*(?:from|import)\s+anima\.(runtime|constitution|core|tools|"
        r"watchdog|capability\.governance|capability\.health)\b",
        re.M,
    )
    for needle in transport:
        assert not re.search(needle, src), f"transport token: {needle}"
    for needle in forbidden:
        assert not re.search(needle, src), f"forbidden callable: {needle}"
    assert frozen.findall(src) == [], f"frozen imports: {frozen.findall(src)}"
