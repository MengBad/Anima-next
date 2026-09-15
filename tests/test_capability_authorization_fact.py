"""P6.10-A Authorization Fact Recording Slice - authoritative contract.

Establishes an independent ``AuthorizationFact`` data structure and its
verification contract — the isolation layer between "authorization to
execute" (authorization fact) and "execution that happened" (execution fact /
HealthEvidence / ExecutionLedger).

Hard boundary (this contract enforces it):

  * FACT ONLY        - AuthorizationFact records the *intent to authorize*; it
                       carries NO execution status, NO result, NO execution_key
                       semantics beyond identity.  approved != executed,
                       authorized != running, resolution != authorization,
                       authorization fact != execution fact.
  * NO EXECUTION     - the slice never calls an executor, never invokes a
                       provider, never dispatches a capability, never touches
                       approve_and_execute, never writes an execution ledger.
  * IMMUTABLE        - AuthorizationFact is frozen + slots + digest-bound;
                       stored facts are never overwritten (replay-safe).
  * NO FROZEN IMPORT - the module imports NO anima.runtime / anima.constitution /
                       anima.core / anima.tools / anima.capability.governance;
                       atomic storage uses stdlib only.
  * NON-ESCALATION   - an authorization fact never upgrades to executed /
                       deployed / running / production semantics.

Acceptance (P6.9 contract):
  A. Authorization isolation  - approved/authorized produces no execution fact
  D. Replay safety            - same authz_id verifies repeatedly, never re-records
  F. Non-escalation           - authz evidence never upgrades to execution
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import tempfile
from dataclasses import FrozenInstanceError

import pytest

NOW = "2026-08-11T14:40:00+08:00"


def _digest(payload) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------
def _fact_kwargs(**overrides):
    defaults = dict(
        authz_id="authz-p610-1",
        principal="owner-123",
        capability_id="code.execute",
        scope="pilot-allowlist",
        granted_at=NOW,
        source="local://authorization_facts/authz-p610-1.json",
    )
    defaults.update(overrides)
    return defaults


# --------------------------------------------------------------------------
# A. Authorization isolation: approved/authorized != executed
# --------------------------------------------------------------------------
def test_a_authorization_fact_has_no_execution_semantics(tmp_path):
    from anima.capability.authorization_fact import (  # RED
        AuthorizationFact,
        AuthorizationFactStore,
    )

    store = AuthorizationFactStore(str(tmp_path))
    fact = store.record(**_fact_kwargs())
    assert isinstance(fact, AuthorizationFact)
    # an authorization fact carries NO execution field
    fields = set(fact.__dataclass_fields__.keys())
    assert "execution_status" not in fields
    assert "result" not in fields
    assert "success" not in fields
    # authorized != executed: the fact's semantics are authorization-only
    for banned in ("executed", "running", "deployed"):
        assert banned not in fact.scope.lower()
    assert fact.authz_id  # identity present, but no execution carrier


def test_a2_authorization_does_not_create_execution_fact(tmp_path):
    from anima.capability.authorization_fact import AuthorizationFactStore

    store = AuthorizationFactStore(str(tmp_path))
    store.record(**_fact_kwargs())
    # recording an authorization produces exactly ONE file: the authz fact itself
    # and NOTHING in any execution ledger / health evidence domain
    authz_files = list((pathlib.Path(tmp_path) / "authorization_facts").glob("*.json"))
    assert len(authz_files) == 1
    assert not (pathlib.Path(tmp_path) / "execution_ledger").exists()
    assert not (pathlib.Path(tmp_path) / "health_evidence").exists()


# --------------------------------------------------------------------------
# D. Replay safety: same authz_id verifies repeatedly, never re-records
# --------------------------------------------------------------------------
def test_d_same_authz_id_is_replay_safe(tmp_path):
    from anima.capability.authorization_fact import (
        AuthorizationFactStore,
        verify_authorization_fact,
    )

    store = AuthorizationFactStore(str(tmp_path))
    first = store.record(**_fact_kwargs())
    # a second record of the SAME authz_id is a no-op: fact unchanged, file not overwritten
    before = (pathlib.Path(tmp_path) / "authorization_facts" / f"{first.authz_id}.json").read_bytes()
    second = store.record(**_fact_kwargs())
    after = (pathlib.Path(tmp_path) / "authorization_facts" / f"{first.authz_id}.json").read_bytes()
    assert second.authz_id == first.authz_id
    assert before == after, "replay must not overwrite the recorded fact"

    # repeated verification succeeds and never triggers execution
    ver1 = verify_authorization_fact(first)
    ver2 = verify_authorization_fact(first)
    assert ver1.valid is True
    assert ver2.valid is True
    assert ver1.digest_intact is True
    assert ver2.non_escalation is True
    assert ver1.non_escalation is True


# --------------------------------------------------------------------------
# F. Non-escalation: authz evidence never upgrades to execution
# --------------------------------------------------------------------------
def test_f_authz_never_escalates(tmp_path):
    from anima.capability.authorization_fact import (
        AuthorizationFactStore,
        verify_authorization_fact,
    )

    store = AuthorizationFactStore(str(tmp_path))
    fact = store.record(**_fact_kwargs())
    verification = verify_authorization_fact(fact)
    assert verification.non_escalation is True
    assert verification.valid is True
    # the fact's serialized payload contains no execution vocabulary
    payload = json.dumps(
        {k: getattr(fact, k) for k in fact.__dataclass_fields__},
        ensure_ascii=False, sort_keys=True,
    )
    for banned in ("executed", "deployed", "authorized_running", "production"):
        assert banned not in payload.lower()


def test_f2_tampered_authz_digest_fails_closed(tmp_path):
    from anima.capability.authorization_fact import (
        AuthorizationFact,
        AuthorizationFactStore,
        verify_authorization_fact,
    )

    store = AuthorizationFactStore(str(tmp_path))
    fact = store.record(**_fact_kwargs())
    tampered = object.__new__(AuthorizationFact)
    for field in AuthorizationFact.__slots__:
        object.__setattr__(tampered, field, getattr(fact, field))
    object.__setattr__(tampered, "authz_digest", "0" * 64)
    verification = verify_authorization_fact(tampered)
    assert verification.valid is False
    assert verification.digest_intact is False


# --------------------------------------------------------------------------
# storage integrity: missing/corrupt fail closed, immutable
# --------------------------------------------------------------------------
def test_g_missing_fact_returns_none(tmp_path):
    from anima.capability.authorization_fact import AuthorizationFactStore

    store = AuthorizationFactStore(str(tmp_path))
    assert store.load("ghost-authz") is None
    assert store.has("ghost-authz") is False


def test_g2_corrupt_fact_fails_closed(tmp_path):
    from anima.capability.authorization_fact import (
        AuthorizationFactStore,
        AuthorizationFactReadError,
    )

    p = pathlib.Path(tmp_path) / "authorization_facts" / "broken.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not valid json", encoding="utf-8")
    store = AuthorizationFactStore(str(tmp_path))
    with pytest.raises(AuthorizationFactReadError):
        store.load("broken")
    assert p.read_text(encoding="utf-8") == "{not valid json"  # nothing repaired


def test_h_fact_is_immutable(tmp_path):
    from anima.capability.authorization_fact import AuthorizationFactStore

    fact = AuthorizationFactStore(str(tmp_path)).record(**_fact_kwargs())
    with pytest.raises(FrozenInstanceError):
        fact.principal = "attacker"  # frozen


# --------------------------------------------------------------------------
# static boundary: no execution surface, no frozen imports
# --------------------------------------------------------------------------
_MODULE_PATH = "anima/capability/authorization_fact.py"


def test_i_no_execution_surface(tmp_path):
    from anima.capability.authorization_fact import (
        AuthorizationFactStore,
    )

    store = AuthorizationFactStore(str(tmp_path))
    banned = {
        "execute", "run", "dispatch", "activate", "apply", "repair",
        "invoke", "replay", "recover", "put", "write", "update", "delete",
        "patch", "deploy", "restart", "start", "stop", "commit",
    }
    surface = {name for name in dir(store) if not name.startswith("_")}
    assert surface & banned == set()
    assert surface == {"record", "load", "has"}


def test_j1_no_forbidden_transport_tokens():
    source = pathlib.Path(_MODULE_PATH).read_text(encoding="utf-8")
    transport = (
        "sub" + "process", r"\bssh\b", "para" + "miko", "doc" + "ker",
        "system" + "ctl", r"\brequests\b", "htt" + "px", "dae" + "mon",
        "ti" + "mer", r"\bpoll\b", r"\bschedule\b",
    )
    hits = [n for n in transport if re.search(n, source)]
    assert hits == [], f"transport tokens present: {hits}"


def test_j2_no_mutation_callables():
    source = pathlib.Path(_MODULE_PATH).read_text(encoding="utf-8")
    mutation = (
        r"def (apply|execute|activate|mutate|repair|dispatch|write|delete|update|patch|"
        r"start|stop|restart|reload|deploy|save|run|recover|put|invoke|replay)\b",
        r"\." + "execute" + r"\(",
        r"\." + "activate" + r"\(",
        r"\." + "dispatch" + r"\(",
        r"\." + "invoke" + r"\(",
        r"\." + "put" + r"\(",
    )
    hits = [n for n in mutation if re.search(n, source)]
    assert hits == [], f"mutation callables present: {hits}"


def test_k_no_frozen_subsystem_imports():
    source = pathlib.Path(_MODULE_PATH).read_text(encoding="utf-8")
    frozen = re.compile(
        r"^\s*(?:from|import)\s+anima\.(runtime|constitution|core|tools|watchdog|"
        r"lifecycle|foundation|memory|identity|capability_contract|"
        r"capability\.governance)\b",
        re.M,
    )
    hits = frozen.findall(source)
    assert hits == [], f"frozen/governance imports present: {hits}"
