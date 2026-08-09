"""F8.2b —— Metadata Integrity Enforcement。

F8.2a 建了只报告的校验框架；本 slice 把它接进**能力注册链路**并按等级 hard fail。
这是整个 F8 序列里唯一真正触碰 F5.5 热路径的一刀，所以测试的重心不在"能拦住坏
契约"（那是容易的部分），而在**证明它没有顺手改坏别的东西**：

- 核心不变式「**只罚说错话，不罚没法说话**」——ERROR 是"声明自相矛盾"，必须拦；
  WARNING 多半是"这个来源面结构上就无法声明该字段"（F8.1 ``unverified_metadata``），
  拦它等于每次 B 面注册都失败，最后一定被人关掉；
- F7.1 注册一致性门语义**零变化**，且优先级在 F8.2b 之前；
- 热路径三不变：``contract_for`` 返回的仍是同一个 B 面对象、``execution_key``
  逐字透传、fallback/degradation 语义不动；
- enforcement 只在**注册期**跑一次，``resolve()`` 每次调用的成本为零。
"""
from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

from anima.capability.adapters import from_legacy_contract, from_runtime_contract
from anima.capability.canonical import (
    CanonicalCapabilityContract,
    ContractSource,
    UNDECLARED_BY_SOURCE,
)
from anima.capability.contract import CapabilityContract as LegacyCapabilityContract
from anima.capability.governance import Finding, Severity, ValidationReport, validate_contract
from anima.capability.governance import enforcement as enforcement_mod
from anima.capability.governance.enforcement import (
    DEFAULT_ENFORCEMENT_LEVEL,
    PROJECTION_FAILURE_RULE_ID,
    EnforcementLevel,
    MetadataIntegrityError,
    blocking_findings,
    blocking_severity,
    enforce_metadata_integrity,
    enforce_runtime_contract,
)
from anima.runtime.capability_resolver import (
    CapabilityContract,
    CapabilityNeed,
    CapabilityRegistry,
    CapabilityResolver,
    CapabilityUnavailable,
    RegistrationIntegrityError,
    ResolutionError,
)
from anima.runtime.providers.base import (
    LogicalError,
    Provider,
    ProviderResult,
    TransientError,
)


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------
class _Prov(Provider):
    """最小 provider：记录每次 invoke 的 execution_key，可注入行为。"""

    def __init__(
        self,
        capability_id: str = "search.web",
        *,
        side_effect: bool = False,
        idempotent: bool = True,
        healthy: bool = True,
        behavior=None,
        provider_id: str = "p",
    ) -> None:
        self._capability_id = capability_id
        self._side_effect = side_effect
        self._idempotent = idempotent
        self._healthy = healthy
        self._behavior = behavior
        self._provider_id = provider_id
        self.calls: list[tuple] = []

    @property
    def capability_id(self) -> str:
        return self._capability_id

    @property
    def healthy(self) -> bool:
        return self._healthy

    @property
    def side_effect(self) -> bool:
        return self._side_effect

    @property
    def idempotent(self) -> bool:
        return self._idempotent

    def invoke(self, capability, arguments, execution_key) -> ProviderResult:
        self.calls.append(execution_key)
        if self._behavior is not None:
            return self._behavior()
        return ProviderResult(success=True, data={"ok": True},
                              metadata={"provider_id": self._provider_id})


def _boom(contract, provenance):
    raise RuntimeError("validator exploded")


def _contradictory() -> CanonicalCapabilityContract:
    """一份**说错话**的契约：cacheable 却非 deterministic（M011 ERROR）。"""
    return CanonicalCapabilityContract(id="bad.cap", cacheable=True, deterministic=False)


# ===========================================================================
# A. EnforcementLevel —— 三档语义
# ===========================================================================
def test_three_levels_exist_and_are_ordered():
    assert EnforcementLevel.REPORT < EnforcementLevel.PERMISSIVE < EnforcementLevel.STRICT


def test_default_level_is_permissive():
    """默认必须是 PERMISSIVE。

    默认 STRICT 会让每次 B 面注册都失败（B 面结构上无法声明 deterministic /
    permission_scopes）；默认 REPORT 又等于没有执法。
    """
    assert DEFAULT_ENFORCEMENT_LEVEL is EnforcementLevel.PERMISSIVE


@pytest.mark.parametrize(
    "level,expected",
    [
        (EnforcementLevel.REPORT, None),
        (EnforcementLevel.PERMISSIVE, Severity.ERROR),
        (EnforcementLevel.STRICT, Severity.WARNING),
    ],
)
def test_blocking_severity_threshold(level, expected):
    assert blocking_severity(level) is expected


# ===========================================================================
# B. blocking_findings —— 阈值切分
# ===========================================================================
def _report_with_all_severities() -> ValidationReport:
    return ValidationReport(
        identity="x@1",
        findings=(
            Finding("I", Severity.INFO, "a", "info"),
            Finding("W", Severity.WARNING, "b", "warn"),
            Finding("E", Severity.ERROR, "c", "err"),
        ),
    )


def test_report_level_blocks_nothing():
    assert blocking_findings(_report_with_all_severities(), EnforcementLevel.REPORT) == ()


def test_permissive_blocks_only_errors():
    blocked = blocking_findings(_report_with_all_severities(), EnforcementLevel.PERMISSIVE)
    assert [f.rule_id for f in blocked] == ["E"]


def test_strict_blocks_errors_and_warnings_but_not_info():
    blocked = blocking_findings(_report_with_all_severities(), EnforcementLevel.STRICT)
    assert sorted(f.rule_id for f in blocked) == ["E", "W"]


# ===========================================================================
# C. enforce_metadata_integrity —— 纯函数执法
# ===========================================================================
def test_clean_contract_passes_and_returns_report():
    contract = CanonicalCapabilityContract(id="search.web", description="d")
    report = enforce_metadata_integrity(contract)
    assert isinstance(report, ValidationReport)
    assert report.ok


def test_contradictory_contract_is_refused():
    with pytest.raises(MetadataIntegrityError):
        enforce_metadata_integrity(_contradictory())


def test_error_payload_carries_actionable_detail():
    """报错必须能让人直接定位：身份、等级、以及每条 blocking finding 的四元组。"""
    with pytest.raises(MetadataIntegrityError) as ei:
        enforce_metadata_integrity(_contradictory())

    err = ei.value
    assert err.identity == "bad.cap@1"
    assert err.level is EnforcementLevel.PERMISSIVE
    assert isinstance(err.report, ValidationReport)
    assert err.blocking, "blocking findings must be attached, not just stringified"
    assert all(f.severity is Severity.ERROR for f in err.blocking)
    assert "M011" in {f.rule_id for f in err.blocking}
    # 每条 finding 的四要素都要在消息里露出来，否则运维只能去翻源码
    text = str(err)
    assert "M011" in text and "cacheable" in text and "bad.cap" in text


def test_report_level_never_raises_even_on_error():
    """REPORT 是应急阀门：出了误报也能先把系统跑起来，同时保留证据。"""
    report = enforce_metadata_integrity(_contradictory(), level=EnforcementLevel.REPORT)
    assert not report.ok
    assert report.has("M011")


def test_strict_refuses_warning_only_contract():
    contract = CanonicalCapabilityContract(id="a", side_effect=True, idempotent=True)
    assert enforce_metadata_integrity(contract).ok  # PERMISSIVE 放行
    with pytest.raises(MetadataIntegrityError):
        enforce_metadata_integrity(contract, level=EnforcementLevel.STRICT)


def test_permissive_does_not_punish_warnings():
    contract = CanonicalCapabilityContract(id="a", side_effect=True, idempotent=True)
    report = enforce_metadata_integrity(contract, level=EnforcementLevel.PERMISSIVE)
    assert report.has("M010")  # 看见了
    assert report.ok            # 但没拦


def test_enforcement_does_not_mutate_contract():
    contract = CanonicalCapabilityContract(id="a", retry=3, domain="knowledge")
    before = (contract.id, contract.retry, contract.domain, contract.source)
    enforce_metadata_integrity(contract)
    assert (contract.id, contract.retry, contract.domain, contract.source) == before


def test_custom_validators_participate_in_enforcement():
    def _reject(contract, provenance):
        yield Finding("X999", Severity.ERROR, "id", "custom rejection")

    with pytest.raises(MetadataIntegrityError) as ei:
        enforce_metadata_integrity(CanonicalCapabilityContract(id="a"), validators=[_reject])
    assert "X999" in {f.rule_id for f in ei.value.blocking}


def test_crashing_validator_fails_closed():
    """M900：校验器自己炸了要按 ERROR 处理（fail-closed），不能当作通过。"""
    with pytest.raises(MetadataIntegrityError) as ei:
        enforce_metadata_integrity(CanonicalCapabilityContract(id="a"), validators=[_boom])
    assert "M900" in {f.rule_id for f in ei.value.blocking}


# ===========================================================================
# D. 核心不变式 —— 只罚说错话，不罚没法说话
# ===========================================================================
def test_runtime_face_contract_passes_by_default():
    """B 面契约（生产注册面）在默认等级下必须原样通过。

    这是整个 F8.2b 能不能上线的前提：B 面结构上没有 version / deterministic /
    cacheable / streaming，如果把"没法声明"也判成 ERROR，等于给注册链路装了一个
    永远闭合的熔断器。
    """
    report = enforce_runtime_contract(CapabilityContract(capability_id="memory.read"))
    assert report.ok
    assert report.errors == ()


def test_runtime_face_absent_fields_are_seen_but_not_punished():
    """"没法说话"要被**看见**（WARNING）而不是被**忽略**——否则治理层在自欺。"""
    report = enforce_runtime_contract(CapabilityContract(capability_id="memory.read"))
    seen = {(f.rule_id, f.field_name) for f in report.warnings}
    assert ("M004", "deterministic") in seen
    assert ("M005", "permission_scopes") in seen
    assert report.ok


@pytest.mark.parametrize(
    "canonical",
    [
        from_runtime_contract(CapabilityContract(capability_id="memory.read")),
        from_legacy_contract(LegacyCapabilityContract(id="filesystem.read")),
    ],
    ids=["runtime_face", "legacy_face"],
)
def test_structural_gaps_never_produce_errors(canonical):
    """两个既有面的 ABSENT 字段，任何一个都不许升级成 ERROR。"""
    report = enforce_metadata_integrity(canonical, level=EnforcementLevel.REPORT)
    absent = UNDECLARED_BY_SOURCE[canonical.source]
    assert absent, "该来源面本就应存在结构性缺口，否则这个用例失去意义"
    assert not [f for f in report.errors if f.field_name in absent]


def test_strict_would_block_existing_faces_which_is_why_it_is_opt_in():
    """把 STRICT 会拦住既有面这件事**写进测试**，防止有人顺手把默认改成 STRICT。"""
    canonical = from_runtime_contract(CapabilityContract(capability_id="memory.read"))
    with pytest.raises(MetadataIntegrityError):
        enforce_metadata_integrity(canonical, level=EnforcementLevel.STRICT)


def test_strict_blocking_set_excludes_info():
    canonical = from_runtime_contract(CapabilityContract(capability_id="memory.read"))
    report = validate_contract(canonical)
    assert report.has("M003")  # description 空 → INFO
    blocked = blocking_findings(report, EnforcementLevel.STRICT)
    assert "M003" not in {f.rule_id for f in blocked}


def test_malformed_contract_object_is_governed_not_crashed():
    """投影失败要变成一条 ERROR finding（M901），而不是漏出 AttributeError。"""
    class _NotAContract:
        capability_id = "x"

    with pytest.raises(MetadataIntegrityError) as ei:
        enforce_runtime_contract(_NotAContract())
    assert PROJECTION_FAILURE_RULE_ID in {f.rule_id for f in ei.value.blocking}


# ===========================================================================
# E. 注册链路接线
# ===========================================================================
def test_default_registry_accepts_runtime_contract():
    reg = CapabilityRegistry()
    reg.register(CapabilityContract(capability_id="search.web"), _Prov("search.web"))
    assert reg.has("search.web")


def test_registry_exposes_metadata_report():
    """WARNING 不能凭空蒸发——注册完还得能查到当时的报告。"""
    reg = CapabilityRegistry()
    reg.register(CapabilityContract(capability_id="search.web"), _Prov("search.web"))
    report = reg.metadata_report_for("search.web")
    assert isinstance(report, ValidationReport)
    assert report.identity == "search.web@1"
    assert report.has("M004")


def test_metadata_report_absent_for_unknown_capability():
    assert CapabilityRegistry().metadata_report_for("nope") is None


def test_strict_registry_refuses_runtime_contract():
    reg = CapabilityRegistry(metadata_enforcement=EnforcementLevel.STRICT)
    with pytest.raises(MetadataIntegrityError):
        reg.register(CapabilityContract(capability_id="search.web"), _Prov("search.web"))


def test_rejected_registration_leaves_registry_untouched():
    """原子性：先校验后写入，拒绝时 registry 一个字节都不能变。"""
    reg = CapabilityRegistry(metadata_enforcement=EnforcementLevel.STRICT)
    with pytest.raises(MetadataIntegrityError):
        reg.register(CapabilityContract(capability_id="search.web"), _Prov("search.web"))
    assert not reg.has("search.web")
    assert reg.providers_for("search.web") == []
    assert reg.metadata_report_for("search.web") is None


def test_rejected_reregistration_preserves_previous_contract():
    """重复注册被拒时，既有 contract / provider / 报告都必须原封不动。"""
    reg = CapabilityRegistry()
    good = CapabilityContract(capability_id="search.web", retry=1)
    reg.register(good, _Prov("search.web"))
    first_report = reg.metadata_report_for("search.web")

    with pytest.raises(MetadataIntegrityError):
        reg.register(
            CapabilityContract(capability_id="search.web", timeout=0.0), _Prov("search.web")
        )

    assert reg.contract_for("search.web") is good
    assert len(reg.providers_for("search.web")) == 1
    assert reg.metadata_report_for("search.web") is first_report


def test_report_level_registry_admits_contradictory_contract_with_evidence():
    reg = CapabilityRegistry(metadata_enforcement=EnforcementLevel.REPORT)
    reg.register(CapabilityContract(capability_id="search.web", timeout=-1.0), _Prov("search.web"))
    assert reg.has("search.web")
    report = reg.metadata_report_for("search.web")
    assert report is not None and report.has("M013")


def test_negative_timeout_is_refused_at_default_level():
    """timeout ≤ 0 是"按字面执行一定出错"，属于说错话，默认就该拦。"""
    reg = CapabilityRegistry()
    with pytest.raises(MetadataIntegrityError):
        reg.register(CapabilityContract(capability_id="search.web", timeout=0.0), _Prov("search.web"))


# --- F7.1 语义零变化 --------------------------------------------------------
def test_f71_guard_still_rejects_profile_mismatch():
    reg = CapabilityRegistry()
    with pytest.raises(RegistrationIntegrityError):
        reg.register(
            CapabilityContract(capability_id="http.post", side_effect=True, idempotent=False),
            _Prov("http.post", side_effect=False, idempotent=True),
        )


def test_f71_guard_takes_priority_over_metadata_enforcement():
    """两个门同时不满足时，先报 F7.1 —— 安全一致性比 metadata 完整性更靠前。"""
    reg = CapabilityRegistry(metadata_enforcement=EnforcementLevel.STRICT)
    with pytest.raises(RegistrationIntegrityError):
        reg.register(
            CapabilityContract(capability_id="http.post", side_effect=True, idempotent=False),
            _Prov("http.post", side_effect=False, idempotent=True),
        )


def test_f71_matching_profile_still_registers():
    reg = CapabilityRegistry()
    reg.register(
        CapabilityContract(capability_id="http.post", side_effect=True, idempotent=False),
        _Prov("http.post", side_effect=True, idempotent=False),
    )
    assert reg.has("http.post")


def test_empty_provider_list_still_raises_value_error_first():
    reg = CapabilityRegistry(metadata_enforcement=EnforcementLevel.STRICT)
    with pytest.raises(ValueError):
        reg.register(CapabilityContract(capability_id="search.web"))


def test_metadata_error_is_a_configuration_guard_not_a_resolution_error():
    """与 RegistrationIntegrityError 同类：配置期守卫，不该被 resolve 的兜底吞掉。"""
    assert not issubclass(MetadataIntegrityError, ResolutionError)


# ===========================================================================
# F. 热路径不变量
# ===========================================================================
def _reg_with(provider: _Prov, **contract_kw) -> CapabilityRegistry:
    reg = CapabilityRegistry()
    reg.register(
        CapabilityContract(capability_id=provider.capability_id, **contract_kw), provider
    )
    return reg


def test_contract_for_returns_the_same_runtime_object():
    """I：registry 存的仍是 B 面对象本身，没有被 canonical 悄悄替换掉。"""
    reg = CapabilityRegistry()
    contract = CapabilityContract(capability_id="search.web")
    reg.register(contract, _Prov("search.web"))
    got = reg.contract_for("search.web")
    assert got is contract
    assert isinstance(got, CapabilityContract)


def test_execution_key_is_passed_through_verbatim():
    """I3：(run_id, task_id, attempt) 逐字送达 provider。"""
    prov = _Prov("search.web")
    resolver = CapabilityResolver(_reg_with(prov))
    key = ("run-7", "task-3", 2)
    resolver.resolve(CapabilityNeed("search.web"), key)
    assert prov.calls == [key]


def test_transient_retry_budget_unchanged():
    calls = {"n": 0}

    def _flaky():
        calls["n"] += 1
        raise TransientError("nope")

    prov = _Prov("search.web", behavior=_flaky)
    resolver = CapabilityResolver(_reg_with(prov, retry=2))
    with pytest.raises(CapabilityUnavailable):
        resolver.resolve(CapabilityNeed("search.web"), ("r", "t", 0))
    assert calls["n"] == 3  # 1 + retry


def test_logical_error_still_skips_to_next_candidate():
    def _logical():
        raise LogicalError("bad args")

    bad = _Prov("search.web", behavior=_logical, provider_id="bad")
    good = _Prov("search.web", provider_id="good")
    reg = CapabilityRegistry()
    reg.register(CapabilityContract(capability_id="search.web", retry=5), bad, good)
    result = CapabilityResolver(reg).resolve(CapabilityNeed("search.web"), ("r", "t", 0))
    assert result.success
    assert len(bad.calls) == 1  # logical 不吃重试预算


def test_soft_failure_still_degrades_to_next_candidate():
    soft = _Prov("search.web", behavior=lambda: ProviderResult(success=False, error="soft"))
    good = _Prov("search.web", provider_id="good")
    reg = CapabilityRegistry()
    reg.register(CapabilityContract(capability_id="search.web"), soft, good)
    assert CapabilityResolver(reg).resolve(CapabilityNeed("search.web"), ("r", "t", 0)).success


def test_all_candidates_failing_still_raises_capability_unavailable():
    soft = _Prov("search.web", behavior=lambda: ProviderResult(success=False, error="soft"))
    resolver = CapabilityResolver(_reg_with(soft))
    with pytest.raises(CapabilityUnavailable):
        resolver.resolve(CapabilityNeed("search.web"), ("r", "t", 0))


def test_enforcement_runs_at_registration_only_never_per_resolve(monkeypatch):
    """R4 的核心保证：执法是**配置期**成本，每次 resolve 一次校验都不许多跑。"""
    seen = {"n": 0}
    original = enforcement_mod.enforce_runtime_contract

    def _counting(contract, **kwargs):
        seen["n"] += 1
        return original(contract, **kwargs)

    monkeypatch.setattr(enforcement_mod, "enforce_runtime_contract", _counting)

    prov = _Prov("search.web")
    reg = CapabilityRegistry()
    reg.register(CapabilityContract(capability_id="search.web"), prov)
    assert seen["n"] == 1

    resolver = CapabilityResolver(reg)
    for i in range(5):
        resolver.resolve(CapabilityNeed("search.web"), ("r", "t", i))
    assert seen["n"] == 1, "resolve() 不得触发 metadata 校验"


# ===========================================================================
# G. 依赖隔离
# ===========================================================================
def test_enforcement_module_does_not_import_runtime_or_constitution():
    """治理层不许反向依赖 runtime —— 否则 import 环迟早出现。"""
    script = textwrap.dedent(
        """
        import sys
        import anima.capability.governance.enforcement  # noqa: F401
        leaked = [m for m in sys.modules
                  if m.startswith(("anima.runtime", "anima.constitution"))]
        print("LEAKED:", sorted(leaked))
        assert not leaked, leaked
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_enforcement_source_has_no_runtime_import():
    """机械守门：连惰性 import 都不许有（文档里提到模块名不算）。"""
    import inspect

    src = inspect.getsource(enforcement_mod)
    import_lines = [
        line.strip()
        for line in src.splitlines()
        if line.strip().startswith(("import ", "from "))
    ]
    offenders = [
        line for line in import_lines
        if "anima.runtime" in line or "anima.constitution" in line
    ]
    assert not offenders, offenders
