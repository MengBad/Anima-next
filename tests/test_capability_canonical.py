"""F8.1 — Capability Governance Foundation: canonical contract + adapter layer.

Freeze 裁定 (§58): the codebase carries **two** distinct ``CapabilityContract``
definitions whose field sets are almost mutually exclusive —

- ``anima/capability/contract.py:24``  (A / legacy declaration surface)
  ``id, version, domain, description, stability, permission: PermissionScopes,
  timeout: float | None, retry``
- ``anima/runtime/capability_resolver.py:129`` (B / runtime registration surface)
  ``capability_id, permission: str, side_effect, idempotent, timeout: float, retry``

F8.1 does **not** delete either of them. It introduces a canonical model plus a
pure-function adapter layer so both surfaces can be reasoned about as one
governed object, while each keeps serving its existing callers untouched.

Two governance rules are mechanised here:

1. **No deletion.** Both legacy classes must still exist and stay distinct.
2. **No invented truth (F7.1).** A surface that *cannot* declare a field must
   not have its conservative default presented as declared policy. The
   canonical model carries provenance (``source`` / ``unverified_metadata()``)
   so the Provider stays the runtime truth.

Tests-first: RED until ``anima/capability/canonical.py`` and
``anima/capability/adapters/`` exist.
"""
from __future__ import annotations

import dataclasses

import pytest

from anima.capability.adapters import (
    from_legacy_contract,
    from_runtime_contract,
    to_legacy_contract,
    to_runtime_contract,
)
from anima.capability.canonical import (
    DEFAULT_PERMISSION_TOKEN,
    DEFAULT_RUNTIME_TIMEOUT,
    CanonicalCapabilityContract,
    ContractSource,
)
from anima.capability.contract import CapabilityContract as LegacyCapabilityContract
from anima.capability.scopes import NO_SCOPES, PermissionScopes
from anima.contracts.capability import Stability
from anima.runtime.capability_resolver import (
    CapabilityContract as RuntimeCapabilityContract,
)


# ---------------------------------------------------------------------------
# Rule 1 — F8.1 must NOT delete either contract
# ---------------------------------------------------------------------------
def test_both_contract_definitions_still_exist_and_stay_distinct():
    """§58 constraint: build a canonical model + adapters, do not rip out the
    two live definitions (that would explode imports/tests and hide regressions)."""
    assert LegacyCapabilityContract is not RuntimeCapabilityContract

    legacy_fields = {f.name for f in dataclasses.fields(LegacyCapabilityContract)}
    runtime_fields = {f.name for f in dataclasses.fields(RuntimeCapabilityContract)}

    # the identity field is spelled differently on each surface (root of R1)
    assert "id" in legacy_fields and "capability_id" not in legacy_fields
    assert "capability_id" in runtime_fields and "id" not in runtime_fields
    # ...and the overlap really is only these three names
    assert legacy_fields & runtime_fields == {"permission", "timeout", "retry"}


# ---------------------------------------------------------------------------
# Canonical model shape
# ---------------------------------------------------------------------------
def test_canonical_defaults_grant_nothing():
    c = CanonicalCapabilityContract(id="search.web")

    assert c.version == "1.0.0"
    assert c.domain == ""
    assert c.description == ""
    assert c.stability is Stability.EXPERIMENTAL

    # two different permission models coexist; neither is collapsed into the other
    assert c.permission_scopes == NO_SCOPES
    assert c.permission_token == DEFAULT_PERMISSION_TOKEN == "capability:invoke"

    # existing runtime defaults (must match capability_resolver.py:140-141 so a
    # round-trip through canonical cannot silently change registry behaviour)
    assert c.side_effect is False
    assert c.idempotent is True

    # NEW F8 metadata: the conservative value is the one that grants no
    # optimisation (no dedup/replay assumption, no caching, no streaming)
    assert c.deterministic is False
    assert c.cacheable is False
    assert c.streaming is False

    assert c.timeout is None
    assert c.retry == 0
    assert c.source is ContractSource.CANONICAL


def test_canonical_is_immutable():
    c = CanonicalCapabilityContract(id="x")
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.side_effect = True  # type: ignore[misc]


def test_canonical_does_not_auto_derive_domain():
    """Canonical is a pure carrier. Deriving ``domain`` from the id prefix is the
    legacy surface's job (``contract.py:64 normalize_contract``); doing it here
    would make canonical a second source of declaration semantics."""
    assert CanonicalCapabilityContract(id="search.web").domain == ""


def test_capability_id_alias_matches_id():
    """B-side callers speak ``capability_id``; expose it without duplicating state."""
    assert CanonicalCapabilityContract(id="memory.read").capability_id == "memory.read"


@pytest.mark.parametrize(
    "version,expected",
    [("1.0.0", 1), ("2.3.4", 2), ("10.0.0", 10), ("0.9.9", 0), ("", 0), ("x.y", 0)],
)
def test_major_parsing_matches_legacy_semantics(version, expected):
    assert CanonicalCapabilityContract(id="c", version=version).major == expected


def test_identity_key_is_id_at_major():
    """Governance identity (RFC-0001 §9): the Resolver only matches same major,
    so a major bump is a *different* governed object."""
    assert (
        CanonicalCapabilityContract(id="search.web", version="2.3.4").identity_key()
        == "search.web@2"
    )


def test_identity_key_stable_within_major_and_splits_across_major():
    a = CanonicalCapabilityContract(id="search.web", version="2.0.0")
    b = CanonicalCapabilityContract(id="search.web", version="2.9.1")
    c = CanonicalCapabilityContract(id="search.web", version="3.0.0")
    assert a.identity_key() == b.identity_key()
    assert a.identity_key() != c.identity_key()


def test_execution_semantics_exposes_only_the_execution_profile():
    c = CanonicalCapabilityContract(
        id="http.post", side_effect=True, idempotent=False, deterministic=False
    )
    assert c.execution_semantics() == {
        "side_effect": True,
        "idempotent": False,
        "deterministic": False,
    }


# ---------------------------------------------------------------------------
# Rule 2 — provenance: no surface may claim what it cannot declare
# ---------------------------------------------------------------------------
def test_from_legacy_records_provenance_and_undeclared_metadata():
    canonical = from_legacy_contract(LegacyCapabilityContract(id="filesystem.read"))
    assert canonical.source is ContractSource.LEGACY_DECLARATION
    assert canonical.unverified_metadata() == frozenset(
        {
            "permission_token",
            "side_effect",
            "idempotent",
            "deterministic",
            "cacheable",
            "streaming",
        }
    )


def test_from_runtime_records_provenance_and_undeclared_metadata():
    canonical = from_runtime_contract(RuntimeCapabilityContract(capability_id="memory.read"))
    assert canonical.source is ContractSource.RUNTIME_REGISTRATION
    assert canonical.unverified_metadata() == frozenset(
        {
            "version",
            "domain",
            "description",
            "stability",
            "permission_scopes",
            "deterministic",
            "cacheable",
            "streaming",
        }
    )


def test_canonical_source_declares_everything():
    assert CanonicalCapabilityContract(id="c").unverified_metadata() == frozenset()


def test_legacy_projection_does_not_claim_verified_execution_semantics():
    """The A surface has no ``side_effect``/``idempotent`` field at all
    (``contract.py:24-35``). The projection must carry the conservative default
    *and* flag it unverified — otherwise F8.2 would treat "absent" as
    "declared safe", which is exactly the hole F7.1 closed."""
    canonical = from_legacy_contract(LegacyCapabilityContract(id="code.execute"))
    assert canonical.side_effect is False
    assert canonical.idempotent is True
    assert not canonical.declares("side_effect")
    assert not canonical.declares("idempotent")
    assert canonical.declares("id")
    assert canonical.declares("stability")


def test_runtime_projection_does_not_claim_verified_declaration_metadata():
    canonical = from_runtime_contract(RuntimeCapabilityContract(capability_id="http.post"))
    assert canonical.stability is Stability.EXPERIMENTAL
    assert not canonical.declares("stability")
    assert not canonical.declares("version")
    assert canonical.declares("side_effect")
    assert canonical.declares("permission_token")


# ---------------------------------------------------------------------------
# Adapter round-trips
# ---------------------------------------------------------------------------
def test_legacy_round_trip_is_lossless():
    legacy = LegacyCapabilityContract(
        id="filesystem.read",
        version="2.1.0",
        domain="filesystem",
        description="read a file",
        stability=Stability.BETA,
        permission=PermissionScopes(filesystem=True),
        timeout=12.5,
        retry=3,
    )
    assert to_legacy_contract(from_legacy_contract(legacy)) == legacy


def test_legacy_round_trip_preserves_defaults():
    legacy = LegacyCapabilityContract(id="memory.read")
    assert to_legacy_contract(from_legacy_contract(legacy)) == legacy
    assert legacy.timeout is None


def test_runtime_round_trip_is_lossless():
    runtime = RuntimeCapabilityContract(
        capability_id="http.post",
        permission="capability:mutate",
        side_effect=True,
        idempotent=False,
        timeout=45.0,
        retry=2,
    )
    assert to_runtime_contract(from_runtime_contract(runtime)) == runtime


def test_runtime_round_trip_preserves_defaults():
    runtime = RuntimeCapabilityContract(capability_id="memory.read")
    assert runtime.timeout == DEFAULT_RUNTIME_TIMEOUT
    assert to_runtime_contract(from_runtime_contract(runtime)) == runtime


def test_adapters_do_not_mutate_their_input():
    """The B contract is a *mutable* dataclass (``@dataclass`` at
    capability_resolver.py:129). Adapters must be pure projections."""
    runtime = RuntimeCapabilityContract(
        capability_id="c", side_effect=True, idempotent=False
    )
    snapshot = dataclasses.asdict(runtime)
    from_runtime_contract(runtime)
    assert dataclasses.asdict(runtime) == snapshot


# ---------------------------------------------------------------------------
# Cross-surface projection must not smuggle one permission model into the other
# ---------------------------------------------------------------------------
def test_legacy_to_runtime_does_not_smuggle_scopes_into_the_token_gate():
    """A declares structured ``PermissionScopes``; B gates on a single scope
    token checked against ``Provider.granted_scopes: set[str]``
    (``providers/base.py:66``). Crossing over must not fabricate a token."""
    legacy = LegacyCapabilityContract(
        id="filesystem.read", permission=PermissionScopes(filesystem=True)
    )
    runtime = to_runtime_contract(from_legacy_contract(legacy))
    assert runtime.capability_id == "filesystem.read"
    assert runtime.permission == DEFAULT_PERMISSION_TOKEN


def test_runtime_to_legacy_does_not_invent_scopes_or_stability():
    runtime = RuntimeCapabilityContract(
        capability_id="http.post",
        permission="capability:mutate",
        side_effect=True,
        idempotent=False,
    )
    legacy = to_legacy_contract(from_runtime_contract(runtime))
    assert legacy.id == "http.post"
    assert legacy.permission == NO_SCOPES
    assert legacy.stability is Stability.EXPERIMENTAL


def test_canonical_without_timeout_fills_the_runtime_default():
    """A allows ``timeout=None``; B requires a float. The fill happens at the
    boundary and is the only documented lossy direction."""
    canonical = CanonicalCapabilityContract(id="c")
    assert canonical.timeout is None
    assert to_runtime_contract(canonical).timeout == DEFAULT_RUNTIME_TIMEOUT


def test_canonical_explicit_timeout_survives_runtime_projection():
    assert to_runtime_contract(CanonicalCapabilityContract(id="c", timeout=7.5)).timeout == 7.5


def test_canonical_none_timeout_survives_legacy_projection():
    assert to_legacy_contract(CanonicalCapabilityContract(id="c")).timeout is None


# ---------------------------------------------------------------------------
# F5.5 / F7.1 compatibility — projected contracts stay drop-in usable
# ---------------------------------------------------------------------------
class _StubProvider:
    """Duck-typed provider (mirrors ``providers/base.py`` surface used by the
    F7.1 registration gate)."""

    def __init__(self, capability_id: str, *, side_effect: bool, idempotent: bool) -> None:
        self.capability_id = capability_id
        self.side_effect = side_effect
        self.idempotent = idempotent


def test_runtime_projection_preserves_the_f71_gate_fields():
    canonical = CanonicalCapabilityContract(id="http.post", side_effect=True, idempotent=False)
    runtime = to_runtime_contract(canonical)
    assert (runtime.side_effect, runtime.idempotent) == (True, False)


def test_projected_contract_is_accepted_by_the_real_registry():
    from anima.runtime.capability_resolver import CapabilityRegistry

    registry = CapabilityRegistry()
    contract = to_runtime_contract(
        CanonicalCapabilityContract(id="http.post", side_effect=True, idempotent=False)
    )
    provider = _StubProvider("http.post", side_effect=True, idempotent=False)
    registry.register(contract, provider)  # must not raise
    assert registry.has("http.post")


def test_projected_contract_still_trips_the_f71_gate_on_mismatch():
    from anima.runtime.capability_resolver import (
        CapabilityRegistry,
        RegistrationIntegrityError,
    )

    registry = CapabilityRegistry()
    contract = to_runtime_contract(
        CanonicalCapabilityContract(id="http.post", side_effect=False, idempotent=True)
    )
    provider = _StubProvider("http.post", side_effect=True, idempotent=True)
    with pytest.raises(RegistrationIntegrityError):
        registry.register(contract, provider)
    assert not registry.has("http.post")
