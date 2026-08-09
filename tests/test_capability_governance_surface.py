"""F8.4 —— Capability Governance Reachability（只读治理查询面）。

本套件的头号任务不是验证 view「能显示已治理的能力」，而是验证它 **能诚实地显示
「未接入治理」**。

F8.4 最大的失败模式不是代码错误，是**治理幻觉**：一个好看的读面把所有能力都显示成
governed，而它其实只是在展示 runtime registry 这座孤岛。所以本文件里权重最高的是
第 B / C / E 组：

- ``provider_count`` 在未治理时必须是 ``None`` 而不是 ``0``；
- ``validation_status`` 在没有报告时必须是 ``UNAVAILABLE`` 而不是 ``PASSED``；
- ``audit_available`` 在 source 不暴露审计面时必须是 ``None`` 而不是 ``False``；
- ``inventory()`` 在 source 无法枚举时必须**抛异常**而不是返回空列表
  （空列表在 dashboard 上长得和「一切正常」一模一样）。
"""
from __future__ import annotations

import inspect
import subprocess
import sys
import textwrap

import pytest

from anima.capability.governance import surface as surface_mod
from anima.capability.governance.surface import (
    CapabilityGovernanceSnapshot,
    CapabilityGovernanceView,
    EnumerableGovernanceSource,
    GovernanceEnumerationUnavailable,
    GovernanceReachability,
    GovernanceSource,
    ProvenanceSummary,
    ValidationStatus,
)
from anima.capability.governance.enforcement import EnforcementLevel
from anima.capability.governance.lifecycle import LifecycleState
from anima.capability.governance.provenance import Provenance


# ===========================================================================
# 固件：真实 runtime registry（唯一被治理的真源）
# ===========================================================================
@pytest.fixture
def registry_factory():
    """构造真实 CapabilityRegistry —— 治理读面必须对真货成立，不能只对 fake 成立。"""
    from anima.runtime.capability_resolver import CapabilityContract, CapabilityRegistry
    from anima.runtime.providers.base import Provider, ProviderResult

    class _Provider(Provider):
        def __init__(self, capability_id, *, side_effect=False, idempotent=True):
            # Provider.capability_id 是抽象 property，不能当构造参数传 —— 存私有字段后重写属性。
            self._capability_id = capability_id
            # 基类的 side_effect / idempotent 是只读 property（data descriptor），
            # 直接 self.side_effect = x 会 AttributeError；用私有字段 + 重写属性绕过。
            self._side_effect = side_effect
            self._idempotent = idempotent

        @property
        def capability_id(self):
            return self._capability_id

        @property
        def side_effect(self):
            return self._side_effect

        @property
        def idempotent(self):
            return self._idempotent

        def invoke(self, capability, arguments, execution_key):  # pragma: no cover
            return ProviderResult(data={"ok": True}, success=True)

    def _make(level=None):
        registry = CapabilityRegistry(metadata_enforcement=level)
        return registry, CapabilityContract, _Provider

    return _make


@pytest.fixture
def clean_registry(registry_factory):
    """一个注册成功、metadata 干净的能力。"""
    registry, Contract, Provider = registry_factory()
    registry.register(
        Contract(capability_id="search.web", timeout=5.0, retry=1),
        Provider("search.web"),
    )
    return registry


class _RecordingSource:
    """记录被调方法的 spy —— 用来证明读面不产生执行副作用（E 条）。"""

    def __init__(self, inner):
        self._inner = inner
        self.calls: list[str] = []

    def has(self, capability_id):
        self.calls.append("has")
        return self._inner.has(capability_id)

    def contract_for(self, capability_id):
        self.calls.append("contract_for")
        return self._inner.contract_for(capability_id)

    def providers_for(self, capability_id):
        self.calls.append("providers_for")
        return self._inner.providers_for(capability_id)

    def metadata_report_for(self, capability_id):
        self.calls.append("metadata_report_for")
        return self._inner.metadata_report_for(capability_id)

    def lifecycle_for(self, capability_id):
        self.calls.append("lifecycle_for")
        return self._inner.lifecycle_for(capability_id)


class _EnumerableSource(_RecordingSource):
    """额外实现枚举面的 source。"""

    def __init__(self, inner, ids):
        super().__init__(inner)
        self._ids = tuple(ids)

    def capabilities(self):
        self.calls.append("capabilities")
        return self._ids


class _FullDisclosureSource(_EnumerableSource):
    """连 enforcement / audit 都愿意如实交代的 source（可选扩展面）。"""

    def __init__(self, inner, ids, *, level, audit):
        super().__init__(inner, ids)
        self._level = level
        self._audit = audit

    def enforcement_level(self):
        self.calls.append("enforcement_level")
        return self._level

    def audit_available(self):
        self.calls.append("audit_available")
        return self._audit


# ===========================================================================
# A. Read model —— 快照结构
# ===========================================================================
REQUIRED_SNAPSHOT_FIELDS = (
    "capability_id",
    "contract_identity",
    "provider_count",
    "validation_status",
    "enforcement_level",
    "lifecycle_state",
    "audit_available",
    "provenance_summary",
)


def test_snapshot_exposes_every_required_field():
    """验收标准 A 冻结的字段一个都不能少。"""
    from dataclasses import fields

    names = {f.name for f in fields(CapabilityGovernanceSnapshot)}
    missing = [n for n in REQUIRED_SNAPSHOT_FIELDS if n not in names]
    assert not missing, f"snapshot 缺字段: {missing}"


def test_snapshot_is_immutable(clean_registry):
    view = CapabilityGovernanceView(clean_registry)
    snap = view.describe("search.web")
    with pytest.raises(Exception):
        snap.capability_id = "mutated"  # type: ignore[misc]


def test_snapshot_carries_reachability(clean_registry):
    """reachability 是本 slice 的主题字段：治理到底够不够得着这个能力。"""
    view = CapabilityGovernanceView(clean_registry)
    assert view.describe("search.web").reachability is GovernanceReachability.GOVERNED


# ===========================================================================
# B. 反治理幻觉 —— 未接入治理必须能被诚实说出来（本套件权重最高）
# ===========================================================================
def test_unknown_capability_is_reported_as_ungoverned(clean_registry):
    """describe 一个 registry 不认识的能力：不抛 KeyError，也不假装它被治理。"""
    view = CapabilityGovernanceView(clean_registry)
    snap = view.describe("tools.legacy.web_search")
    assert snap.reachability is GovernanceReachability.UNGOVERNED
    assert snap.capability_id == "tools.legacy.web_search"


def test_ungoverned_provider_count_is_none_not_zero(clean_registry):
    """``0`` 意思是「治理知道它，且它没有 provider」；未治理必须是 ``None``。

    这两者在 dashboard 上会被读成完全不同的结论，绝不可合并。
    """
    snap = CapabilityGovernanceView(clean_registry).describe("nope")
    assert snap.provider_count is None
    assert snap.provider_count is not 0  # noqa: F632 —— 刻意断言身份而非相等


def test_ungoverned_validation_status_is_unavailable_not_passed(clean_registry):
    """没有校验报告 ≠ 校验通过。把缺席读成通过是最危险的一种治理幻觉。"""
    snap = CapabilityGovernanceView(clean_registry).describe("nope")
    assert snap.validation_status is ValidationStatus.UNAVAILABLE
    assert snap.validation_status is not ValidationStatus.PASSED


def test_ungoverned_optional_fields_are_all_none(clean_registry):
    snap = CapabilityGovernanceView(clean_registry).describe("nope")
    assert snap.contract_identity is None
    assert snap.lifecycle_state is None
    assert snap.provenance_summary is None


def test_audit_availability_is_unknown_not_false_for_plain_registry(clean_registry):
    """registry 不暴露审计面 → 只能说「不知道」。

    ``False`` 是一个断言（「确定没有审计」），而 view 没有资格做这个断言：
    audit sink 挂在 resolver 上，registry 根本看不见。
    """
    snap = CapabilityGovernanceView(clean_registry).describe("search.web")
    assert snap.audit_available is None
    assert snap.audit_available is not False


def test_enforcement_level_is_unknown_not_defaulted_for_plain_registry(clean_registry):
    """registry 的 enforcement 级别是私有状态；读面不许猜一个默认值填上。"""
    snap = CapabilityGovernanceView(clean_registry).describe("search.web")
    assert snap.enforcement_level is None
    assert snap.enforcement_level is not EnforcementLevel.PERMISSIVE


def test_source_module_has_no_absence_laundering_patterns():
    """源码扫描：禁止把「缺席」洗成「默认值」的惯用写法。"""
    src = inspect.getsource(surface_mod)
    banned = (
        " or 0",
        ' or ""',
        " or []",
        " or ()",
        ' or "unknown"',
        ' or "UNKNOWN"',
        " or False",
    )
    offenders = [b for b in banned if b in src]
    assert not offenders, f"发现审计污染写法: {offenders}"


# ===========================================================================
# C. 已治理能力 —— 正向读取
# ===========================================================================
def test_governed_snapshot_reports_real_identity(clean_registry):
    snap = CapabilityGovernanceView(clean_registry).describe("search.web")
    assert snap.contract_identity == "search.web@1"


def test_governed_snapshot_counts_providers(clean_registry, registry_factory):
    snap = CapabilityGovernanceView(clean_registry).describe("search.web")
    assert snap.provider_count == 1


def test_governed_snapshot_counts_multiple_providers(registry_factory):
    registry, Contract, Provider = registry_factory()
    registry.register(
        Contract(capability_id="search.web"),
        Provider("search.web"),
        Provider("search.web"),
    )
    snap = CapabilityGovernanceView(registry).describe("search.web")
    assert snap.provider_count == 2


def test_governed_clean_contract_validation_passed(clean_registry):
    snap = CapabilityGovernanceView(clean_registry).describe("search.web")
    assert snap.validation_status is ValidationStatus.PASSED


def test_governed_snapshot_reports_active_lifecycle(clean_registry):
    snap = CapabilityGovernanceView(clean_registry).describe("search.web")
    assert snap.lifecycle_state is LifecycleState.ACTIVE


def test_governed_snapshot_exposes_provenance_gaps(clean_registry):
    """B 面结构上无法声明 version/domain/deterministic… —— 读面必须让这些缺口可见。"""
    snap = CapabilityGovernanceView(clean_registry).describe("search.web")
    assert snap.provenance_summary is not None
    assert "deterministic" in snap.provenance_summary.absent
    assert "version" in snap.provenance_summary.absent
    assert "id" in snap.provenance_summary.declared


def test_provenance_summary_partitions_every_governed_field(clean_registry):
    """三态互斥且穷尽：不能有字段被漏掉或重复归类。"""
    from anima.capability.governance.provenance import governed_field_names

    s = CapabilityGovernanceView(clean_registry).describe("search.web").provenance_summary
    assert s is not None
    buckets = (set(s.declared), set(s.defaulted), set(s.absent))
    union = buckets[0] | buckets[1] | buckets[2]
    assert union == set(governed_field_names())
    assert len(union) == sum(len(b) for b in buckets), "字段被重复归类"


def test_provenance_summary_is_sorted_for_stable_diffing():
    s = ProvenanceSummary(declared=("b", "a"), defaulted=(), absent=())
    assert s.declared == ("a", "b")


# ===========================================================================
# D. 治理失败 ≠ 未治理 —— 两者必须可区分
# ===========================================================================
def test_quarantined_capability_is_still_governed(registry_factory):
    """REPORT 级放行的坏 contract：它**被治理了**，只是没通过。

    如果读面把它显示成 UNGOVERNED，就等于把「治理拦下了问题」错报成「治理没看到」。
    """
    registry, Contract, Provider = registry_factory(level=EnforcementLevel.REPORT)
    registry.register(
        Contract(capability_id="bad.cap", timeout=-1.0),
        Provider("bad.cap"),
    )
    snap = CapabilityGovernanceView(registry).describe("bad.cap")
    assert snap.reachability is GovernanceReachability.GOVERNED
    assert snap.validation_status is ValidationStatus.FAILED
    assert snap.lifecycle_state is LifecycleState.QUARANTINED


def test_refused_registration_is_ungoverned(registry_factory):
    """被默认门拒掉的能力从未被接纳 → 读面必须说 UNGOVERNED，而不是编一个状态。"""
    from anima.capability.governance.enforcement import MetadataIntegrityError

    registry, Contract, Provider = registry_factory()
    with pytest.raises(MetadataIntegrityError):
        registry.register(
            Contract(capability_id="bad.cap", timeout=-1.0),
            Provider("bad.cap"),
        )
    snap = CapabilityGovernanceView(registry).describe("bad.cap")
    assert snap.reachability is GovernanceReachability.UNGOVERNED
    assert snap.lifecycle_state is None


def test_validation_failed_still_reports_findings_count(registry_factory):
    registry, Contract, Provider = registry_factory(level=EnforcementLevel.REPORT)
    registry.register(Contract(capability_id="bad.cap", timeout=-1.0), Provider("bad.cap"))
    snap = CapabilityGovernanceView(registry).describe("bad.cap")
    assert snap.error_count >= 1
    assert snap.warning_count >= 1


# ===========================================================================
# E. 枚举面 —— source 够不着的东西不许假装看得见
# ===========================================================================
def test_plain_registry_is_not_enumerable(clean_registry):
    """事实钉死：CapabilityRegistry 没有 capabilities()，而 runtime 已冻结。"""
    assert not hasattr(clean_registry, "capabilities")
    assert not isinstance(clean_registry, EnumerableGovernanceSource)


def test_inventory_refuses_to_fake_an_empty_world(clean_registry):
    """source 无法枚举时必须抛异常。

    返回空列表会在 dashboard 上呈现为「系统里没有任何能力」——一个看起来
    很干净的谎。宁可拒绝回答。
    """
    view = CapabilityGovernanceView(clean_registry)
    with pytest.raises(GovernanceEnumerationUnavailable):
        view.inventory()


def test_inventory_error_names_the_source(clean_registry):
    view = CapabilityGovernanceView(clean_registry)
    with pytest.raises(GovernanceEnumerationUnavailable) as exc:
        view.inventory()
    assert "CapabilityRegistry" in str(exc.value)


def test_inventory_accepts_explicit_ids(clean_registry):
    """显式给定 id 集合时，不需要 source 支持枚举。"""
    view = CapabilityGovernanceView(clean_registry)
    snaps = view.inventory(["search.web", "not.there"])
    assert [s.capability_id for s in snaps] == ["search.web", "not.there"]
    assert snaps[0].reachability is GovernanceReachability.GOVERNED
    assert snaps[1].reachability is GovernanceReachability.UNGOVERNED


def test_inventory_uses_source_enumeration_when_available(clean_registry):
    src = _EnumerableSource(clean_registry, ["search.web"])
    snaps = CapabilityGovernanceView(src).inventory()
    assert [s.capability_id for s in snaps] == ["search.web"]
    assert "capabilities" in src.calls


def test_inventory_preserves_caller_order(clean_registry):
    view = CapabilityGovernanceView(clean_registry)
    ids = ["z", "a", "search.web"]
    assert [s.capability_id for s in view.inventory(ids)] == ids


def test_inventory_returns_tuple_snapshot(clean_registry):
    snaps = CapabilityGovernanceView(clean_registry).inventory(["search.web"])
    assert isinstance(snaps, tuple)


def test_empty_explicit_ids_is_not_an_error(clean_registry):
    """显式传空集合是调用方的选择，与「source 答不上来」不同。"""
    assert CapabilityGovernanceView(clean_registry).inventory([]) == ()


# ===========================================================================
# F. 可选扩展面 —— source 愿意交代时如实采纳
# ===========================================================================
def test_full_disclosure_source_reports_enforcement_and_audit(clean_registry):
    src = _FullDisclosureSource(
        clean_registry, ["search.web"], level=EnforcementLevel.STRICT, audit=True
    )
    snap = CapabilityGovernanceView(src).describe("search.web")
    assert snap.enforcement_level is EnforcementLevel.STRICT
    assert snap.audit_available is True


def test_disclosed_audit_false_is_preserved(clean_registry):
    """source 明确说「没有审计」时，False 必须原样保留，不得退化成 None。"""
    src = _FullDisclosureSource(
        clean_registry, ["search.web"], level=EnforcementLevel.REPORT, audit=False
    )
    snap = CapabilityGovernanceView(src).describe("search.web")
    assert snap.audit_available is False
    assert snap.enforcement_level is EnforcementLevel.REPORT


# ===========================================================================
# G. 观测 fail-open —— 读面自己不许成为故障源
# ===========================================================================
class _BrokenSource:
    def __init__(self, inner, broken):
        self._inner = inner
        self._broken = broken

    def _guard(self, name):
        if name in self._broken:
            raise RuntimeError(f"{name} exploded")

    def has(self, capability_id):
        self._guard("has")
        return self._inner.has(capability_id)

    def contract_for(self, capability_id):
        self._guard("contract_for")
        return self._inner.contract_for(capability_id)

    def providers_for(self, capability_id):
        self._guard("providers_for")
        return self._inner.providers_for(capability_id)

    def metadata_report_for(self, capability_id):
        self._guard("metadata_report_for")
        return self._inner.metadata_report_for(capability_id)

    def lifecycle_for(self, capability_id):
        self._guard("lifecycle_for")
        return self._inner.lifecycle_for(capability_id)


def test_broken_has_degrades_to_unknown(clean_registry):
    src = _BrokenSource(clean_registry, {"has"})
    snap = CapabilityGovernanceView(src).describe("search.web")
    assert snap.reachability is GovernanceReachability.UNKNOWN


def test_unknown_reachability_does_not_claim_ungoverned(clean_registry):
    """「读不到」不等于「没治理」——UNKNOWN 与 UNGOVERNED 是两个结论。"""
    src = _BrokenSource(clean_registry, {"has"})
    snap = CapabilityGovernanceView(src).describe("search.web")
    assert snap.reachability is not GovernanceReachability.UNGOVERNED


def test_broken_field_source_degrades_that_field_only(clean_registry):
    """一个查询炸了只影响它自己的字段，其余照常读出来。"""
    src = _BrokenSource(clean_registry, {"providers_for"})
    snap = CapabilityGovernanceView(src).describe("search.web")
    assert snap.reachability is GovernanceReachability.GOVERNED
    assert snap.provider_count is None          # 读不到 → None，不是 0
    assert snap.lifecycle_state is LifecycleState.ACTIVE
    assert snap.contract_identity == "search.web@1"


def test_broken_lifecycle_source_degrades_to_none(clean_registry):
    src = _BrokenSource(clean_registry, {"lifecycle_for"})
    snap = CapabilityGovernanceView(src).describe("search.web")
    assert snap.lifecycle_state is None
    assert snap.reachability is GovernanceReachability.GOVERNED


def test_broken_report_source_is_unavailable_not_passed(clean_registry):
    src = _BrokenSource(clean_registry, {"metadata_report_for"})
    snap = CapabilityGovernanceView(src).describe("search.web")
    assert snap.validation_status is ValidationStatus.UNAVAILABLE


def test_unprojectable_contract_degrades_provenance_to_none(clean_registry):
    """contract 投影不成 canonical → provenance 未知，但不崩、也不编。"""

    class _Junk:
        def __init__(self, inner):
            self._inner = inner

        def has(self, cid):
            return self._inner.has(cid)

        def contract_for(self, cid):
            return object()          # 不是任何已知 contract 面

        def providers_for(self, cid):
            return self._inner.providers_for(cid)

        def metadata_report_for(self, cid):
            return None

        def lifecycle_for(self, cid):
            return self._inner.lifecycle_for(cid)

    snap = CapabilityGovernanceView(_Junk(clean_registry)).describe("search.web")
    assert snap.provenance_summary is None
    assert snap.contract_identity is None
    assert snap.reachability is GovernanceReachability.GOVERNED


# ===========================================================================
# H. 纯读 —— 不 resolve / 不 invoke / 不 register / 不 mutate
# ===========================================================================
def test_describe_only_touches_read_methods(clean_registry):
    src = _RecordingSource(clean_registry)
    CapabilityGovernanceView(src).describe("search.web")
    allowed = {"has", "contract_for", "providers_for", "metadata_report_for", "lifecycle_for"}
    assert set(src.calls) <= allowed, f"读面调用了非查询方法: {set(src.calls) - allowed}"


def test_describe_does_not_mutate_lifecycle(clean_registry):
    lc = clean_registry.lifecycle_for("search.web")
    before_state, before_len = lc.state, len(lc.history)
    CapabilityGovernanceView(clean_registry).describe("search.web")
    assert lc.state is before_state
    assert len(lc.history) == before_len


def test_describe_is_repeatable(clean_registry):
    view = CapabilityGovernanceView(clean_registry)
    assert view.describe("search.web") == view.describe("search.web")


def test_view_never_registers(clean_registry):
    before = clean_registry.providers_for("search.web")
    CapabilityGovernanceView(clean_registry).inventory(["search.web", "brand.new"])
    assert clean_registry.providers_for("search.web") == before
    assert not clean_registry.has("brand.new")


def test_view_module_declares_no_execution_verbs():
    """源码扫描：读面不许出现解析/调用/注册动作。"""
    src = inspect.getsource(surface_mod)
    banned = (".resolve(", ".invoke(", ".register(", ".transition_to(")
    offenders = [b for b in banned if b in src]
    assert not offenders, f"读面出现执行动作: {offenders}"


# ===========================================================================
# I. Protocol 注入 —— 不反向依赖 runtime
# ===========================================================================
def test_registry_structurally_satisfies_governance_source(clean_registry):
    """runtime registry 未经修改即满足协议 —— 这正是协议注入的验收点。"""
    assert isinstance(clean_registry, GovernanceSource)


def test_view_accepts_any_conforming_source(clean_registry):
    src = _RecordingSource(clean_registry)
    assert isinstance(src, GovernanceSource)
    assert CapabilityGovernanceView(src).describe("search.web").provider_count == 1


def test_governance_surface_does_not_import_runtime():
    script = textwrap.dedent(
        """
        import sys
        import anima.capability.governance.surface  # noqa: F401
        leaked = [m for m in sys.modules
                  if m.startswith(("anima.runtime", "anima.tools", "anima.workflow",
                                   "anima.constitution"))]
        print("LEAKED:", sorted(leaked))
        assert not leaked, leaked
        """
    )
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_governance_package_import_does_not_pull_runtime():
    """包级 re-export 也不能把 runtime 拖进来。"""
    script = textwrap.dedent(
        """
        import sys
        import anima.capability.governance  # noqa: F401
        leaked = [m for m in sys.modules
                  if m.startswith(("anima.runtime", "anima.tools", "anima.workflow"))]
        assert not leaked, leaked
        """
    )
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_surface_has_no_forbidden_import_statements():
    offenders = [
        line.strip()
        for line in inspect.getsource(surface_mod).splitlines()
        if line.strip().startswith(("import ", "from "))
        and any(p in line for p in ("anima.runtime", "anima.tools", "anima.workflow",
                                    "anima.constitution"))
    ]
    assert not offenders, offenders


def test_surface_is_exported_from_governance_package():
    import anima.capability.governance as gov

    for name in (
        "CapabilityGovernanceSnapshot",
        "CapabilityGovernanceView",
        "GovernanceReachability",
        "GovernanceSource",
        "ValidationStatus",
    ):
        assert hasattr(gov, name), name
        assert name in gov.__all__, name


# ===========================================================================
# J. 枚举协议自身
# ===========================================================================
def test_enumerable_source_satisfies_both_protocols(clean_registry):
    src = _EnumerableSource(clean_registry, ["search.web"])
    assert isinstance(src, GovernanceSource)
    assert isinstance(src, EnumerableGovernanceSource)


def test_enumeration_failure_surfaces_as_governance_error(clean_registry):
    """source 自称可枚举但枚举时炸了 → 仍走治理错误，不外泄原始异常类型。"""

    class _BadEnum(_RecordingSource):
        def capabilities(self):
            raise RuntimeError("boom")

    with pytest.raises(GovernanceEnumerationUnavailable):
        CapabilityGovernanceView(_BadEnum(clean_registry)).inventory()


def test_validation_status_values_are_explicit():
    assert {s.name for s in ValidationStatus} == {"PASSED", "FAILED", "UNAVAILABLE"}


def test_reachability_values_are_explicit():
    assert {r.name for r in GovernanceReachability} == {"GOVERNED", "UNGOVERNED", "UNKNOWN"}
