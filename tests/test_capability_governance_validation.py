"""F8.2a —— Metadata Provenance + Validation Framework 的机械边界测试。

本 slice 只建**框架**，不做 enforcement：

- ``anima/capability/governance/provenance.py``——字段级三态 provenance
  （DECLARED / DEFAULTED / ABSENT），把 F8.1 的"来源面能不能声明"细化成
  "这个字段到底有没有被真正填过"；
- ``anima/capability/governance/validation.py``——只**报告**不抛异常、不接线
  registry 的校验框架（Severity / Finding / ValidationReport / Validator）。

因此测试要钉死的边界是：

1. provenance 必须能区分 **ABSENT（结构上无法声明）** 与 **DEFAULTED（能声明
   但和默认值不可区分）**——F8.1 的 :meth:`declares` 把这两种混为一谈；
2. provenance 与 F8.1 的 ``unverified_metadata()`` **向后兼容**，不得另立真源；
3. 校验框架 **永不抛异常**，连第三方 validator 炸了也要降级成 Finding；
4. 校验 **不修改** 任何输入；
5. ``anima.capability.governance`` **不得**拉入 ``anima.runtime`` /
   ``anima.constitution``（子进程隔离守门）；
6. 内置 9 条规则（M001–M005 completeness、M010–M013 consistency）分级正确。

明确**不**在本 slice 测试的（留给 F8.2b）：registry 接线、hard fail、
QUARANTINED 状态流转。
"""
from __future__ import annotations

import subprocess
import sys
from dataclasses import FrozenInstanceError, fields

import pytest

from anima.capability.adapters import from_legacy_contract, from_runtime_contract
from anima.capability.canonical import (
    CanonicalCapabilityContract,
    ContractSource,
)
from anima.capability.contract import CapabilityContract as LegacyContract
from anima.capability.governance import (
    Finding,
    Provenance,
    Severity,
    ValidationReport,
    contract_provenance,
    provenance_of,
    validate_contract,
)
from anima.capability.scopes import PermissionScopes
from anima.contracts.capability import Stability


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class _RuntimeContractStub:
    """B 面 Contract 的鸭子类型替身（避免为构造样本拉入 anima.runtime）。"""

    def __init__(
        self,
        capability_id: str = "search.web",
        permission: str = "capability:invoke",
        side_effect: bool = False,
        idempotent: bool = True,
        timeout: float = 30.0,
        retry: int = 0,
    ) -> None:
        self.capability_id = capability_id
        self.permission = permission
        self.side_effect = side_effect
        self.idempotent = idempotent
        self.timeout = timeout
        self.retry = retry


def _rule_ids(report: ValidationReport) -> list[str]:
    return [f.rule_id for f in report.findings]


def _find(report: ValidationReport, rule_id: str, field_name: str | None = None) -> Finding:
    for finding in report.findings:
        if finding.rule_id == rule_id and (field_name is None or finding.field_name == field_name):
            return finding
    raise AssertionError(
        f"expected finding {rule_id}/{field_name}; got {[(f.rule_id, f.field_name) for f in report.findings]}"
    )


# ---------------------------------------------------------------------------
# 1. provenance —— 字段级三态
# ---------------------------------------------------------------------------


def test_provenance_has_exactly_three_states() -> None:
    """三态而非二态：ABSENT ≠ DEFAULTED 是本 slice 存在的全部理由。"""
    assert {p.value for p in Provenance} == {"declared", "defaulted", "absent"}


def test_explicitly_set_field_is_declared() -> None:
    contract = CanonicalCapabilityContract(id="search.web", domain="knowledge", retry=2)
    prov = contract_provenance(contract)

    assert prov["domain"] is Provenance.DECLARED
    assert prov["retry"] is Provenance.DECLARED


def test_field_left_at_its_default_is_defaulted_not_declared() -> None:
    """值等于 dataclass 默认值时无法证明"被声明过"——保守判 DEFAULTED。"""
    contract = CanonicalCapabilityContract(id="search.web")
    prov = contract_provenance(contract)

    assert prov["domain"] is Provenance.DEFAULTED
    assert prov["retry"] is Provenance.DEFAULTED
    assert prov["side_effect"] is Provenance.DEFAULTED
    assert prov["stability"] is Provenance.DEFAULTED


def test_required_field_without_default_is_always_declared() -> None:
    """``id`` 没有默认值，构造时必须给——永远是 DECLARED。"""
    contract = CanonicalCapabilityContract(id="search.web")
    assert provenance_of(contract, "id") is Provenance.DECLARED


def test_default_factory_field_is_handled_as_defaulted() -> None:
    """``permission_scopes`` 用 default_factory，不能因此误判成 DECLARED。"""
    contract = CanonicalCapabilityContract(id="search.web")
    assert provenance_of(contract, "permission_scopes") is Provenance.DEFAULTED

    scoped = CanonicalCapabilityContract(
        id="search.web", permission_scopes=PermissionScopes(network=True)
    )
    assert provenance_of(scoped, "permission_scopes") is Provenance.DECLARED


def test_legacy_projection_marks_execution_semantics_absent_not_defaulted() -> None:
    """A 面结构上就没有 side_effect —— 必须是 ABSENT，不能是 DEFAULTED。

    这是 F8.2a 最核心的一条：把"没这个字段"和"填了默认值"混为一谈，等于
    默默把未知声明成安全。
    """
    canonical = from_legacy_contract(
        LegacyContract(id="search.web", version="1.0.0", domain="knowledge")
    )
    prov = contract_provenance(canonical)

    assert prov["side_effect"] is Provenance.ABSENT
    assert prov["idempotent"] is Provenance.ABSENT
    assert prov["permission_token"] is Provenance.ABSENT
    # A 面能声明的那些不受影响
    assert prov["version"] is not Provenance.ABSENT
    assert prov["domain"] is Provenance.DECLARED


def test_runtime_projection_marks_declaration_metadata_absent() -> None:
    canonical = from_runtime_contract(_RuntimeContractStub(capability_id="search.web"))
    prov = contract_provenance(canonical)

    for name in ("version", "domain", "description", "stability", "permission_scopes"):
        assert prov[name] is Provenance.ABSENT, name
    # B 面能声明的执行语义不受影响
    assert prov["side_effect"] is not Provenance.ABSENT
    assert prov["idempotent"] is not Provenance.ABSENT


def test_f8_new_metadata_is_absent_on_both_legacy_surfaces() -> None:
    """deterministic / cacheable / streaming 两个既有面都无法声明。"""
    legacy = from_legacy_contract(LegacyContract(id="a"))
    runtime = from_runtime_contract(_RuntimeContractStub(capability_id="a"))

    for canonical in (legacy, runtime):
        prov = contract_provenance(canonical)
        assert prov["deterministic"] is Provenance.ABSENT
        assert prov["cacheable"] is Provenance.ABSENT
        assert prov["streaming"] is Provenance.ABSENT


@pytest.mark.parametrize(
    "canonical",
    [
        CanonicalCapabilityContract(id="a"),
        from_legacy_contract(LegacyContract(id="a", domain="knowledge")),
        from_runtime_contract(_RuntimeContractStub(capability_id="a")),
    ],
    ids=["canonical", "legacy", "runtime"],
)
def test_provenance_is_backward_compatible_with_f81_unverified_metadata(
    canonical: CanonicalCapabilityContract,
) -> None:
    """ABSENT 集合必须**恰好**等于 F8.1 的 ``unverified_metadata()``。

    F8.2a 是对 F8.1 的细化，不是第二个真源。
    """
    absent = {
        name for name, prov in contract_provenance(canonical).items() if prov is Provenance.ABSENT
    }
    assert absent == set(canonical.unverified_metadata())


def test_f81_declares_is_coarser_than_field_level_provenance() -> None:
    """F8.1 的 ``declares()`` 对 DEFAULTED 也返回 True —— 正是要细化的地方。"""
    contract = CanonicalCapabilityContract(id="a")

    assert contract.declares("domain") is True
    assert provenance_of(contract, "domain") is Provenance.DEFAULTED


def test_provenance_covers_every_governed_field_and_excludes_source() -> None:
    """``source`` 是 provenance 的载体本身，不是被治理的字段。"""
    contract = CanonicalCapabilityContract(id="a")
    prov = contract_provenance(contract)

    expected = {f.name for f in fields(CanonicalCapabilityContract)} - {"source"}
    assert set(prov) == expected


def test_provenance_of_rejects_unknown_field() -> None:
    contract = CanonicalCapabilityContract(id="a")
    with pytest.raises(KeyError):
        provenance_of(contract, "not_a_field")


def test_provenance_does_not_mutate_the_contract() -> None:
    contract = CanonicalCapabilityContract(id="a", domain="knowledge", retry=3)
    before = (contract.id, contract.domain, contract.retry, contract.source)

    contract_provenance(contract)

    assert (contract.id, contract.domain, contract.retry, contract.source) == before


# ---------------------------------------------------------------------------
# 2. validation framework —— 结构与不变式
# ---------------------------------------------------------------------------


def test_severity_is_ordered_info_lt_warning_lt_error() -> None:
    assert Severity.INFO < Severity.WARNING < Severity.ERROR


def test_finding_and_report_are_immutable() -> None:
    report = validate_contract(CanonicalCapabilityContract(id="a"))

    assert isinstance(report.findings, tuple)
    with pytest.raises(FrozenInstanceError):
        report.findings = ()  # type: ignore[misc]
    if report.findings:
        with pytest.raises(FrozenInstanceError):
            report.findings[0].severity = Severity.INFO  # type: ignore[misc]


def test_report_carries_governance_identity_not_raw_id() -> None:
    """报告按治理身份 ``<id>@<major>`` 归档（对齐 F8.1 identity_key）。"""
    contract = CanonicalCapabilityContract(id="search.web", version="2.3.1")
    report = validate_contract(contract)

    assert report.identity == "search.web@2"


def test_clean_canonical_contract_passes_with_at_most_informational_findings() -> None:
    contract = CanonicalCapabilityContract(
        id="search.web",
        version="1.0.0",
        domain="knowledge",
        description="Web search",
        stability=Stability.STABLE,
    )
    report = validate_contract(contract)

    assert report.ok is True
    assert report.errors == ()
    assert report.warnings == ()


def test_validation_never_raises_even_when_a_validator_explodes() -> None:
    """框架层契约：任何 validator 抛异常都降级为 Finding，绝不向上冒泡。"""

    def _boom(contract, provenance):  # noqa: ANN001, ARG001
        raise RuntimeError("validator exploded")

    report = validate_contract(CanonicalCapabilityContract(id="a"), validators=[_boom])

    assert "M900" in _rule_ids(report)
    crash = _find(report, "M900")
    assert crash.severity is Severity.ERROR
    assert "validator exploded" in crash.message


def test_validation_does_not_mutate_contract_or_scopes() -> None:
    scopes = PermissionScopes(network=True, exec=True)
    contract = CanonicalCapabilityContract(
        id="code.execute", permission_scopes=scopes, side_effect=True, cacheable=True
    )
    snapshot = contract.permission_scopes.as_dict()

    validate_contract(contract)

    assert contract.permission_scopes.as_dict() == snapshot
    assert contract.permission_scopes is scopes


def test_findings_are_deterministically_ordered() -> None:
    contract = CanonicalCapabilityContract(
        id="code.execute", side_effect=True, cacheable=True, retry=-1
    )
    first = validate_contract(contract)
    second = validate_contract(contract)

    keys = [(f.rule_id, f.field_name) for f in first.findings]
    assert keys == sorted(keys, key=lambda kv: (kv[0], kv[1] or ""))
    assert keys == [(f.rule_id, f.field_name) for f in second.findings]


def test_custom_validators_extend_rather_than_replace_builtins() -> None:
    def _extra(contract, provenance):  # noqa: ANN001, ARG001
        return [Finding("X001", Severity.INFO, None, "custom")]

    contract = CanonicalCapabilityContract(id="a", cacheable=True)
    report = validate_contract(contract, validators=[_extra])

    assert "X001" in _rule_ids(report)
    assert "M011" in _rule_ids(report)  # 内置规则仍然跑


def test_validation_framework_is_not_wired_into_any_registry() -> None:
    """F8.2a 只报告。接线 registry / hard fail 属于 F8.2b。"""
    import anima.capability.governance.validation as validation

    source = validation.__file__
    with open(source, encoding="utf-8") as handle:
        text = handle.read()

    for forbidden in ("CapabilityRegistry", "register(", "raise RegistrationIntegrityError"):
        assert forbidden not in text, forbidden


# ---------------------------------------------------------------------------
# 3. 内置规则 —— completeness M001–M005
# ---------------------------------------------------------------------------


def test_m001_blank_identity_is_an_error() -> None:
    report = validate_contract(CanonicalCapabilityContract(id="   "))
    assert _find(report, "M001", "id").severity is Severity.ERROR
    assert report.ok is False


def test_m002_unparseable_version_is_a_warning() -> None:
    report = validate_contract(CanonicalCapabilityContract(id="a", version="latest"))
    finding = _find(report, "M002", "version")

    assert finding.severity is Severity.WARNING
    assert report.ok is True  # 降级到 major=0，可用但需知会


def test_m003_missing_description_is_informational_only() -> None:
    report = validate_contract(CanonicalCapabilityContract(id="a", description=""))
    assert _find(report, "M003", "description").severity is Severity.INFO


def test_m004_absent_execution_semantics_is_a_warning_per_field() -> None:
    canonical = from_legacy_contract(LegacyContract(id="a", domain="knowledge"))
    report = validate_contract(canonical)

    flagged = {f.field_name for f in report.findings if f.rule_id == "M004"}
    assert flagged == {"side_effect", "idempotent", "deterministic"}
    for finding in report.findings:
        if finding.rule_id == "M004":
            assert finding.severity is Severity.WARNING


def test_m005_absent_permission_surface_is_a_warning() -> None:
    legacy = validate_contract(from_legacy_contract(LegacyContract(id="a")))
    runtime = validate_contract(from_runtime_contract(_RuntimeContractStub(capability_id="a")))

    assert _find(legacy, "M005", "permission_token").severity is Severity.WARNING
    assert _find(runtime, "M005", "permission_scopes").severity is Severity.WARNING


def test_completeness_rules_are_silent_on_a_fully_declared_contract() -> None:
    contract = CanonicalCapabilityContract(
        id="a",
        version="1.2.3",
        domain="knowledge",
        description="d",
        stability=Stability.STABLE,
        permission_scopes=PermissionScopes(network=True),
        permission_token="capability:invoke:web",
        side_effect=True,
        idempotent=False,
        deterministic=False,
    )
    report = validate_contract(contract)

    completeness = {f.rule_id for f in report.findings if f.rule_id.startswith("M00")}
    assert completeness == set()


# ---------------------------------------------------------------------------
# 4. 内置规则 —— consistency M010–M013
# ---------------------------------------------------------------------------


def test_m010_side_effecting_capability_claiming_idempotency_is_flagged() -> None:
    contract = CanonicalCapabilityContract(id="a", side_effect=True, idempotent=True)
    finding = _find(validate_contract(contract), "M010")

    assert finding.severity is Severity.WARNING


def test_m011_cacheable_requires_determinism() -> None:
    contract = CanonicalCapabilityContract(id="a", cacheable=True, deterministic=False)
    report = validate_contract(contract)

    assert _find(report, "M011", "cacheable").severity is Severity.ERROR
    assert report.ok is False


def test_m012_cacheable_forbids_side_effect() -> None:
    contract = CanonicalCapabilityContract(
        id="a", cacheable=True, deterministic=True, side_effect=True
    )
    report = validate_contract(contract)

    assert _find(report, "M012", "cacheable").severity is Severity.ERROR


def test_m011_and_m012_stay_silent_when_cacheable_is_false() -> None:
    contract = CanonicalCapabilityContract(id="a", side_effect=True, idempotent=False)
    ids = _rule_ids(validate_contract(contract))

    assert "M011" not in ids
    assert "M012" not in ids


@pytest.mark.parametrize("timeout", [0.0, -1.0])
def test_m013_non_positive_timeout_is_an_error(timeout: float) -> None:
    report = validate_contract(CanonicalCapabilityContract(id="a", timeout=timeout))
    assert _find(report, "M013", "timeout").severity is Severity.ERROR


def test_m013_negative_retry_is_an_error() -> None:
    report = validate_contract(CanonicalCapabilityContract(id="a", retry=-1))
    assert _find(report, "M013", "retry").severity is Severity.ERROR


def test_m013_retry_on_non_idempotent_capability_is_a_warning() -> None:
    contract = CanonicalCapabilityContract(id="a", retry=2, idempotent=False)
    finding = _find(validate_contract(contract), "M013", "retry")

    assert finding.severity is Severity.WARNING


def test_m013_accepts_absent_timeout() -> None:
    """``timeout=None`` 是 A 面合法状态，不该被当成非法预算。"""
    ids = _rule_ids(validate_contract(CanonicalCapabilityContract(id="a", timeout=None)))
    assert "M013" not in ids


def test_builtin_rule_set_is_exactly_the_nine_documented_rules() -> None:
    from anima.capability.governance.validation import BUILTIN_RULE_IDS

    assert BUILTIN_RULE_IDS == (
        "M001",
        "M002",
        "M003",
        "M004",
        "M005",
        "M010",
        "M011",
        "M012",
        "M013",
    )


# ---------------------------------------------------------------------------
# 5. 依赖边界 —— governance 不得拉入 runtime / constitution
# ---------------------------------------------------------------------------


def test_governance_package_does_not_drag_in_runtime_or_constitution() -> None:
    """治理层是纯策略层：import 它不应把执行运行时一起拽起来。

    子进程隔离，避免被同一 pytest 会话里其它测试预先 import 的模块污染。
    """
    script = (
        "import sys\n"
        "import anima.capability.governance as g\n"
        "leaked = sorted(\n"
        "    m for m in sys.modules\n"
        "    if m.startswith('anima.runtime') or m.startswith('anima.constitution')\n"
        ")\n"
        "assert not leaked, leaked\n"
        "print('CLEAN')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert proc.returncode == 0, proc.stderr
    assert "CLEAN" in proc.stdout
