"""P6.12 — Intent Identity Closure (RED contract).

Closes the identity chain:

    intent_id -> authz_id -> execution_key -> evidence_id

Acceptance (all GREEN):
    A  Intent isolation          - intent != authorization (no authz semantics
                                   in IntentFact; intent_id != authz_id)
    B  Identity continuity       - intent_id propagates through authz -> exec
                                   -> evidence without loss
    C  Replay safety             - same intent never creates a second chain
    D  Non-escalation            - intent never auto-upgrades to approved /
                                   executed / production
    E  Cross-sink consistency    - IntentFact + AuthorizationFact +
                                   ExecutionFact + HealthEvidence link coherently
Boundary (fail-closed):
    F  Static boundary           - transport / forbidden callables / frozen
                                   imports = 0
    G  Backward compatibility    - P6.10-A/B facts without intent_id still work
"""
from __future__ import annotations

import pathlib
import re

import pytest

NOW = "2026-08-11T16:00:00+00:00"


# ---------------------------------------------------------------------------
# A - Intent isolation (intent != authorization)
# ---------------------------------------------------------------------------
def test_a_intent_isolation(tmp_path):
    from anima.capability_intent_fact import (
        IntentFactReadError,
        IntentFactStore,
        verify_intent_fact,
    )

    store = IntentFactStore(str(tmp_path))
    fact = store.record(
        intent_id="intent-p612-1",
        principal="owner-123",
        capability_id="code.execute",
        request_digest="ab" * 32,
        requested_at=NOW,
    )
    # intent fact carries NO authorization semantics
    for banned in ("authz_id", "scope", "granted_at", "executed", "approved"):
        assert banned not in fact.__slots__, f"IntentFact leaks {banned!r}"
    # intent_id must never equal authz_id by construction
    assert fact.intent_id != "authz-p612-1"
    verification = verify_intent_fact(fact)
    assert verification.valid is True
    assert verification.digest_intact is True


# ---------------------------------------------------------------------------
# B - Identity continuity (intent -> authz -> exec -> evidence)
# ---------------------------------------------------------------------------
def test_b_identity_continuity(tmp_path):
    from anima.capability_authorization_fact import AuthorizationFactStore
    from anima.capability_execution_dispatch import DispatchFlow
    from anima.capability_intent_fact import (
        IntentFactStore,
        verify_identity_continuity,
    )

    intent_store = IntentFactStore(str(tmp_path))
    intent = intent_store.record(
        intent_id="intent-p612-cont",
        principal="owner-123",
        capability_id="code.execute",
        request_digest="cd" * 32,
        requested_at=NOW,
    )
    authz_store = AuthorizationFactStore(str(tmp_path))
    authz = authz_store.record(
        authz_id="authz-p612-cont",
        principal="owner-123",
        capability_id="code.execute",
        scope="pilot-allowlist",
        granted_at=NOW,
        intent_id=intent.intent_id,
    )
    flow = DispatchFlow(str(tmp_path))
    draft = flow.dispatch(authz.authz_id)
    fact = flow.record(draft)

    # intent_id must propagate: intent -> authz -> execution fact
    assert authz.intent_id == intent.intent_id
    assert fact.intent_id == authz.intent_id
    # execution identity still derived from authz (key shape unchanged)
    assert fact.execution_key == (authz.authz_id, "code.execute", 0)
    assert fact.evidence_id

    verification = verify_identity_continuity(
        intent, authz, fact, intent_id=intent.intent_id
    )
    assert verification.valid is True
    assert verification.intent_continuity is True


# ---------------------------------------------------------------------------
# C - Replay safety (same intent -> same chain, no second chain)
# ---------------------------------------------------------------------------
def test_c_replay_safe(tmp_path):
    from anima.capability_intent_fact import IntentFactStore

    store = IntentFactStore(str(tmp_path))
    first = store.record(
        intent_id="intent-p612-replay",
        principal="owner-123",
        capability_id="code.execute",
        request_digest="ef" * 32,
        requested_at=NOW,
    )
    second = store.record(
        intent_id="intent-p612-replay",
        principal="owner-123",
        capability_id="code.execute",
        request_digest="ef" * 32,
        requested_at=NOW,
    )
    # replay-safe: identical fact, never overwritten, no second chain
    assert second.intent_digest == first.intent_digest
    assert store.load("intent-p612-replay").intent_digest == first.intent_digest
    files = list((pathlib.Path(tmp_path) / "intent_facts").glob("*.json"))
    assert len(files) == 1


# ---------------------------------------------------------------------------
# D - Non-escalation (intent never upgrades to approved/executed/production)
# ---------------------------------------------------------------------------
def test_d_non_escalation(tmp_path):
    from anima.capability_authorization_fact import AuthorizationFactStore
    from anima.capability_execution_dispatch import DispatchFlow
    from anima.capability_intent_fact import (
        IntentFactStore,
        verify_identity_continuity,
    )

    intent = IntentFactStore(str(tmp_path)).record(
        intent_id="intent-p612-esc",
        principal="owner-123",
        capability_id="code.execute",
        request_digest="10" * 32,
        requested_at=NOW,
    )
    authz = AuthorizationFactStore(str(tmp_path)).record(
        authz_id="authz-p612-esc",
        principal="owner-123",
        capability_id="code.execute",
        scope="pilot-allowlist",
        granted_at=NOW,
        intent_id=intent.intent_id,
    )
    flow = DispatchFlow(str(tmp_path))
    fact = flow.record(flow.dispatch(authz.authz_id))

    verification = verify_identity_continuity(intent, authz, fact, intent_id=intent.intent_id)
    assert verification.valid is True
    assert verification.non_escalated is True
    # a continuity PASS is NOT an execution authorization
    text = " ".join(
        (intent.capability_id, authz.scope, str(fact.execution_key))
    ).lower()
    for token in ("authorized_running", "deployed", "production"):
        assert token not in text


# ---------------------------------------------------------------------------
# E - Cross-sink consistency (intent + authz + exec + health evidence)
# ---------------------------------------------------------------------------
def test_e_cross_sink_consistency(tmp_path):
    from anima.capability_authorization_fact import AuthorizationFactStore
    from anima.capability_execution_dispatch import DispatchFlow
    from anima.capability_health_evidence_linker import HealthEvidenceLinker
    from anima.capability_intent_fact import (
        IntentFactStore,
        verify_identity_continuity,
    )

    intent = IntentFactStore(str(tmp_path)).record(
        intent_id="intent-p612-x",
        principal="owner-123",
        capability_id="search.web",
        request_digest="20" * 32,
        requested_at=NOW,
    )
    authz = AuthorizationFactStore(str(tmp_path)).record(
        authz_id="authz-p612-x",
        principal="owner-123",
        capability_id="search.web",
        scope="pilot-allowlist",
        granted_at=NOW,
        intent_id=intent.intent_id,
    )
    flow = DispatchFlow(str(tmp_path))
    fact = flow.record(flow.dispatch(authz.authz_id))

    # health evidence (real upstream sink) for the same capability
    from anima.capability.health.model import HealthEvidence
    from anima.capability.health.store import HealthEvidenceStore

    ev = HealthEvidence(
        evidence_id="he-p612-0000000000000000000000000000",
        capability_id="search.web",
        provider_id="provider.a",
        execution_id="('authz-p612-x', 'search.web', 0)",
        observed_at=1723360000.0,
        selected=True,
        executed=True,
        success=True,
        error_type="",
        duration_ms=8.0,
        metadata={"task_id": "t1"},
    )
    health_path = tmp_path / "health_evidence.jsonl"
    HealthEvidenceStore(str(health_path)).append(ev)

    # continuity PASS
    cont = verify_identity_continuity(intent, authz, fact, intent_id=intent.intent_id)
    assert cont.valid is True
    # execution fact links to health evidence through the P6.10-C linker
    linkage = HealthEvidenceLinker().link(
        pathlib.Path(tmp_path) / "execution_facts" / f"{fact.evidence_id}.json",
        health_path,
    )
    assert linkage.linkage_status == "CONSISTENT"
    assert linkage.capability_matched is True
    assert linkage.executed_matched is True


# ---------------------------------------------------------------------------
# F - Static boundary (doubles as the scan)
# ---------------------------------------------------------------------------
def test_f_static_boundary():
    src = pathlib.Path("anima/capability_intent_fact.py").read_text(encoding="utf-8")
    transport = [
        "subprocess", r"\bssh\b", "paramiko", "docker", "systemctl",
        r"\brequests\b", "httpx", "daemon", r"\btimer\b", r"\bpoll\b",
        r"\bschedule\b",
    ]
    forbidden = [
        r"\.execute\(", r"\.invoke\(", r"\.activate\(", r"\.dispatch\(",
        r"\.put\(", r"\.replay\(",
        r"\bdef (apply|execute|activate|mutate|repair|dispatch|write|delete|"
        r"update|patch|start|stop|restart|reload|deploy|save|run|recover|"
        r"put|invoke|replay)\b",
    ]
    frozen = re.compile(
        r"^\s*(?:from|import)\s+anima\.(runtime|constitution|core|tools|"
        r"watchdog|capability\.governance|capability\.health)\b", re.M,
    )
    for needle in transport:
        assert not re.search(needle, src), f"transport token: {needle}"
    for needle in forbidden:
        assert not re.search(needle, src), f"forbidden callable: {needle}"
    assert frozen.findall(src) == [], f"frozen imports: {frozen.findall(src)}"


# ---------------------------------------------------------------------------
# G - Backward compatibility (P6.10-A/B facts without intent_id still work)
# ---------------------------------------------------------------------------
def test_g_backward_compat(tmp_path):
    from anima.capability_authorization_fact import (
        AuthorizationFact,
        AuthorizationFactStore,
        verify_authorization_fact,
    )
    from anima.capability_execution_dispatch import DispatchFlow

    # AuthorizationFact without intent_id -> default "" (absence marker)
    authz = AuthorizationFactStore(str(tmp_path)).record(
        authz_id="authz-legacy-1",
        principal="owner-123",
        capability_id="code.execute",
        scope="pilot-allowlist",
        granted_at=NOW,
    )
    assert authz.intent_id == ""
    assert verify_authorization_fact(authz).valid is True
    assert AuthorizationFactStore(str(tmp_path)).load("authz-legacy-1").intent_id == ""

    # direct construction without intent_id still valid
    legacy = AuthorizationFact(
        authz_id="authz-legacy-2",
        principal="owner-123",
        capability_id="code.execute",
        scope="pilot-allowlist",
        granted_at=NOW,
        source="local://authorization_facts/authz-legacy-2.json",
        authz_digest="0" * 64,
    )
    assert legacy.intent_id == ""

    # ExecutionFact without intent binding still works end-to-end
    flow = DispatchFlow(str(tmp_path))
    draft = flow.dispatch("authz-legacy-1")
    fact = flow.record(draft)
    assert fact.intent_id == ""
    assert fact.execution_key == ("authz-legacy-1", "code.execute", 0)
