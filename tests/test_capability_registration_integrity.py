"""F7.1 — Capability Registration Integrity guard.

Freeze裁定 (§50): ``CapabilityContract`` is the capability *policy* (declaration
layer); ``Provider`` is the runtime *truth* (implementation layer). The registry
must refuse to register when the two disagree on the side-effect / idempotency
profile, so a "declares safe but actually mutates" provider cannot slip past the
executor's ledger side-effect gate (C2 / C4).

Tests-first: these cases fail (RED) until ``CapabilityRegistry.register`` grows
the F7.1 guard. Scope is intentionally narrow — only ``CapabilityRegistry`` is
touched; no workflow / recovery / constitution / core module is involved.
"""
from __future__ import annotations

import pytest

from anima.runtime.capability_resolver import (
    CapabilityContract,
    CapabilityRegistry,
    RegistrationIntegrityError,
    ResolutionError,
)
from anima.runtime.providers.base import Provider, ProviderResult


class _StubProvider(Provider):
    """Minimal provider with overridable side-effect / idempotency profile."""

    def __init__(
        self, capability_id: str = "stub", *, side_effect: bool = False, idempotent: bool = True
    ) -> None:
        self._capability_id = capability_id
        self._side_effect = side_effect
        self._idempotent = idempotent

    @property
    def capability_id(self) -> str:
        return self._capability_id

    @property
    def side_effect(self) -> bool:
        return self._side_effect

    @property
    def idempotent(self) -> bool:
        return self._idempotent

    def invoke(self, capability, arguments, execution_key) -> ProviderResult:
        return ProviderResult(success=True, data={})


def test_register_consistent_policy_and_truth_succeeds():
    reg = CapabilityRegistry()
    contract = CapabilityContract(capability_id="c", side_effect=True, idempotent=False)
    prov = _StubProvider("c", side_effect=True, idempotent=False)
    reg.register(contract, prov)  # must not raise
    assert reg.has("c")
    assert reg.providers_for("c") == [prov]


def test_register_side_effect_mismatch_rejected():
    # policy declares safe (no side effect) but provider actually mutates
    reg = CapabilityRegistry()
    contract = CapabilityContract(capability_id="c", side_effect=False)
    prov = _StubProvider("c", side_effect=True)
    with pytest.raises(RegistrationIntegrityError):
        reg.register(contract, prov)
    # rejection is atomic: nothing was registered
    assert not reg.has("c")


def test_register_idempotent_mismatch_rejected():
    reg = CapabilityRegistry()
    contract = CapabilityContract(capability_id="c", idempotent=True)
    prov = _StubProvider("c", idempotent=False)
    with pytest.raises(RegistrationIntegrityError):
        reg.register(contract, prov)
    assert not reg.has("c")


def test_register_contract_overestimating_side_effect_rejected():
    # reverse direction: policy claims a side effect the provider does not have
    reg = CapabilityRegistry()
    contract = CapabilityContract(capability_id="c", side_effect=True)
    prov = _StubProvider("c", side_effect=False)
    with pytest.raises(RegistrationIntegrityError):
        reg.register(contract, prov)


def test_register_rejects_if_any_provider_in_list_mismatches():
    reg = CapabilityRegistry()
    contract = CapabilityContract(capability_id="c", side_effect=False, idempotent=True)
    good = _StubProvider("c", side_effect=False, idempotent=True)
    bad = _StubProvider("c", side_effect=True, idempotent=True)
    with pytest.raises(RegistrationIntegrityError):
        reg.register(contract, good, bad)
    assert not reg.has("c")


def test_integrity_error_is_distinct_exception_type():
    assert issubclass(RegistrationIntegrityError, Exception)
    # a registration-time guard, NOT a resolution-time error
    assert not issubclass(RegistrationIntegrityError, ResolutionError)
