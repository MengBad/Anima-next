"""P8.3 — Identity Correlation (RED contract).

qq identity binds to F5 execution identity WITHOUT merging:
    qq_group_id -> qq_user_id -> request_id -> intent_id -> authz_id
                -> execution_key -> evidence_id

Hard rules:
    * qq identity != execution identity
    * qq_user_id is NEVER authz_id and NEVER an execution_key component
    * authz_id derives from the pilot enablement scope
    * evidence_id deterministic from execution_key

Acceptance:
    A  full chain built; all 8 components present
    B  qq_user_id != authz_id; qq_user_id not in execution_key
    C  authz_id stable for the same pilot scope + group (scope binding)
    D  execution_key is a 3-tuple (run_id, task_id, attempt); run_id embeds
       request_id (traceable) but the shape stays F5
    E  evidence_id == sha256(execution_key); deterministic
    F  qq_user_hash is a digest, never the raw qq id (privacy)
    G  intent_id unique per request_id
    H  static boundary: no System B / gateway / executor import
"""
from __future__ import annotations

import hashlib
import pathlib

from anima.pilot.identity_correlator import (
    IdentityChain,
    IdentityCorrelator,
    new_request_id,
)


def _chain(**overrides):
    args = dict(
        group_id="123456", user_id="10001", request_id="req-abc",
        task_id="t-1", attempt=0,
    )
    args.update(overrides)
    return IdentityCorrelator().bind(**args)


# ---------------------------------------------------------------------------
# A - full chain, all components
# ---------------------------------------------------------------------------
def test_a_full_chain():
    ch = _chain()
    assert isinstance(ch, IdentityChain)
    d = ch.to_dict()
    for key in ("qq_group_id", "qq_user_hash", "request_id", "intent_id",
                "authz_id", "execution_key", "evidence_id"):
        assert key in d


# ---------------------------------------------------------------------------
# B - qq identity != execution identity
# ---------------------------------------------------------------------------
def test_b_qq_never_execution():
    ch = _chain()
    assert ch.qq_user_id != ch.authz_id
    assert ch.qq_user_id not in ch.execution_key
    assert "10001" not in str(ch.execution_key)
    # authz is a digest, not a qq id, not a request id
    assert ch.authz_id != ch.request_id
    assert ch.authz_id != ch.qq_group_id


# ---------------------------------------------------------------------------
# C - authz stable per scope+group (pilot-scope binding)
# ---------------------------------------------------------------------------
def test_c_authz_scope_stable():
    a = IdentityCorrelator(pilot_enablement_id="pe-X").bind(
        group_id="123456", user_id="u1", request_id="r1")
    b = IdentityCorrelator(pilot_enablement_id="pe-X").bind(
        group_id="123456", user_id="u2", request_id="r2")
    assert a.authz_id == b.authz_id  # same scope + group -> same authz
    c = IdentityCorrelator(pilot_enablement_id="pe-Y").bind(
        group_id="123456", user_id="u1", request_id="r3")
    assert a.authz_id != c.authz_id  # different scope -> different authz


# ---------------------------------------------------------------------------
# D - execution_key is an F5 triple
# ---------------------------------------------------------------------------
def test_d_execution_key_shape():
    ch = _chain()
    key = ch.execution_key
    assert isinstance(key, tuple) and len(key) == 3
    run_id, task_id, attempt = key
    assert run_id.startswith("run-")
    assert "req-abc" in run_id  # traceable to the request
    assert attempt == 0


# ---------------------------------------------------------------------------
# E - evidence_id deterministic from execution_key
# ---------------------------------------------------------------------------
def test_e_evidence_deterministic():
    ch = _chain()
    expected = hashlib.sha256(repr(ch.execution_key).encode("utf-8")).hexdigest()
    assert ch.evidence_id == expected
    ch2 = _chain(attempt=1)
    assert ch2.evidence_id != ch.evidence_id


# ---------------------------------------------------------------------------
# F - qq_user_hash is a digest (privacy)
# ---------------------------------------------------------------------------
def test_f_qq_user_hash_privacy():
    ch = _chain()
    assert ch.qq_user_hash != ch.qq_user_id
    assert "10001" not in ch.qq_user_hash
    assert len(ch.qq_user_hash) == 16


# ---------------------------------------------------------------------------
# G - intent unique per request
# ---------------------------------------------------------------------------
def test_g_intent_unique():
    a = _chain(request_id="req-1")
    b = _chain(request_id="req-2")
    assert a.intent_id != b.intent_id
    assert a.intent_id == f"intent-req-1"


# ---------------------------------------------------------------------------
# H - static boundary
# ---------------------------------------------------------------------------
def test_h_static_boundary():
    src = pathlib.Path("anima/pilot/identity_correlator.py").read_text(encoding="utf-8")
    parts = src.split('"""')
    code_body = parts[2] if len(parts) >= 3 else src
    forbidden = [
        "executor", "Executor", "PlanOrchestrator", "pilot_rollout",
        "CapabilityGateway", "gateway", "tools", "approve_and_execute",
        "capability_mode", "webui", "napcat", "subprocess", "os.environ",
        "getenv", "requests", "httpx", "ssh", "paramiko",
    ]
    for needle in forbidden:
        assert needle not in code_body, f"forbidden token: {needle}"
    allowed_prefixes = ("anima.pilot.", "anima.runtime.", "anima.constitution.")
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith(("from ", "import ")) and "anima." in stripped:
            target = stripped.split(" import ")[0].replace("from ", "").strip()
            if target.startswith("anima."):
                assert target.startswith(allowed_prefixes), f"import escapes: {target!r}"
