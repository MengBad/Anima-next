"""F8.3 —— Capability Lifecycle + Resolution Audit。

F8.2b 让 capability 能被验证、拒绝、解释。本 slice 回答另外两个问题：

- **能追责**：这次执行为什么用了这个 provider？——resolution audit record；
- **能演进**：一个不达标的 capability 如何退场、如何回来？——lifecycle 状态机。

两条设计主线，各自的关键张力都被测试钉死：

1. **QUARANTINED ≠ DEPRECATED**。前者是故障隔离，**可康复**；后者是主动弃用，
   **不可撤销**。这个不对称不是实现细节，是两个状态存在的全部理由；
2. **audit fail-open，enforcement fail-closed**。守门失灵必须拒绝（F8.2b M900），
   观测失灵必须放行——否则"加审计"变成"加故障"；
3. **audit 不许说谎**：记录下来的候选集必须与实际参与选择的候选集完全一致；
4. **默认零成本**：不挂 sink 时 ``resolve()`` 与 F8.2b 逐字节等价（R4=HIGH）。

本 slice 的边界：lifecycle 是**记录层**，`resolve()` 不读它。让 QUARANTINED 真的
影响候选过滤会改变 fallback semantics，那是另一次授权的事——同一个 slice 里既
接审计又改选择语义，正是 §58 拒绝"大一统重构"的那种叠加风险。
"""
from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

from anima.capability.governance import (
    EnforcementLevel,
    MetadataIntegrityError,
)
from anima.capability.governance import audit as audit_mod
from anima.capability.governance import lifecycle as lifecycle_mod
from anima.capability.governance.audit import (
    REASON_LOGICAL_ERROR,
    REASON_NOT_ATTEMPTED,
    REASON_SCOPE_GAP,
    REASON_SOFT_FAILURE,
    REASON_TRANSIENT_EXHAUSTED,
    REASON_UNHEALTHY,
    CandidateRecord,
    InMemoryAuditSink,
    ResolutionEvent,
    ResolutionOutcome,
    provider_identity,
)
from anima.capability.governance.lifecycle import (
    CapabilityLifecycle,
    LifecycleState,
    LifecycleTransition,
    LifecycleTransitionError,
)
from anima.runtime.capability_resolver import (
    CapabilityContract,
    CapabilityNeed,
    CapabilityNotFound,
    CapabilityRegistry,
    CapabilityResolver,
    CapabilityUnavailable,
    NoProvider,
    PermissionDenied,
    RegistrationIntegrityError,
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
    def __init__(
        self,
        capability_id: str = "search.web",
        *,
        granted_scopes: set[str] | None = None,
        healthy: bool = True,
        latency_ms: float = 0.0,
        side_effect: bool = False,
        idempotent: bool = True,
        behavior=None,
        provider_id: str | None = None,
    ) -> None:
        self._capability_id = capability_id
        self._granted_scopes = granted_scopes or {"capability:invoke"}
        self._healthy = healthy
        self._latency_ms = latency_ms
        self._side_effect = side_effect
        self._idempotent = idempotent
        self._behavior = behavior
        if provider_id is not None:
            self.provider_id = provider_id
        self.calls: list[tuple] = []

    @property
    def capability_id(self) -> str:
        return self._capability_id

    @property
    def granted_scopes(self) -> set[str]:
        return self._granted_scopes

    @property
    def healthy(self) -> bool:
        return self._healthy

    @property
    def latency_ms(self) -> float:
        return self._latency_ms

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
        return ProviderResult(success=True, data={"ok": True})


def _sunk_resolver(*providers: Provider, **contract_kw):
    """建一个挂了内存 sink 的 resolver，返回 (resolver, registry, sink)。"""
    reg = CapabilityRegistry()
    reg.register(
        CapabilityContract(capability_id=providers[0].capability_id, **contract_kw),
        *providers,
    )
    sink = InMemoryAuditSink()
    return CapabilityResolver(reg, audit_sink=sink), reg, sink


# ===========================================================================
# A. Lifecycle 状态机
# ===========================================================================
def test_six_states_exist():
    assert {s.value for s in LifecycleState} == {
        "registered",
        "validated",
        "active",
        "deprecated",
        "quarantined",
        "removed",
    }


def test_lifecycle_starts_registered_with_recorded_origin():
    """初始状态也要有一条历史——历史必须能解释当前状态是怎么来的。"""
    lc = CapabilityLifecycle("search.web")
    assert lc.state is LifecycleState.REGISTERED
    assert len(lc.history) == 1
    assert lc.history[0].source is None
    assert lc.history[0].target is LifecycleState.REGISTERED


@pytest.mark.parametrize(
    "source,target",
    [
        (LifecycleState.REGISTERED, LifecycleState.VALIDATED),
        (LifecycleState.REGISTERED, LifecycleState.QUARANTINED),
        (LifecycleState.REGISTERED, LifecycleState.REMOVED),
        (LifecycleState.VALIDATED, LifecycleState.ACTIVE),
        (LifecycleState.VALIDATED, LifecycleState.QUARANTINED),
        (LifecycleState.ACTIVE, LifecycleState.DEPRECATED),
        (LifecycleState.ACTIVE, LifecycleState.QUARANTINED),
        (LifecycleState.ACTIVE, LifecycleState.REMOVED),
        (LifecycleState.DEPRECATED, LifecycleState.QUARANTINED),
        (LifecycleState.DEPRECATED, LifecycleState.REMOVED),
        (LifecycleState.QUARANTINED, LifecycleState.ACTIVE),
        (LifecycleState.QUARANTINED, LifecycleState.DEPRECATED),
        (LifecycleState.QUARANTINED, LifecycleState.REMOVED),
    ],
)
def test_legal_transitions(source, target):
    lc = _lifecycle_at(source)
    lc.transition_to(target, reason="test")
    assert lc.state is target


@pytest.mark.parametrize(
    "source,target",
    [
        (LifecycleState.REGISTERED, LifecycleState.ACTIVE),      # 不许跳过校验
        (LifecycleState.VALIDATED, LifecycleState.DEPRECATED),   # 没上线过谈何弃用
        (LifecycleState.ACTIVE, LifecycleState.REGISTERED),      # 不能倒流
        (LifecycleState.DEPRECATED, LifecycleState.ACTIVE),      # 弃用不可撤销
        (LifecycleState.REMOVED, LifecycleState.ACTIVE),         # 终态
        (LifecycleState.REMOVED, LifecycleState.QUARANTINED),
    ],
)
def test_illegal_transitions_are_refused(source, target):
    lc = _lifecycle_at(source)
    with pytest.raises(LifecycleTransitionError):
        lc.transition_to(target, reason="test")
    assert lc.state is source  # 拒绝后状态不动


def _lifecycle_at(state: LifecycleState) -> CapabilityLifecycle:
    """把一个 lifecycle 推到指定状态（只走合法路径）。"""
    lc = CapabilityLifecycle("c")
    path = {
        LifecycleState.REGISTERED: [],
        LifecycleState.VALIDATED: [LifecycleState.VALIDATED],
        LifecycleState.ACTIVE: [LifecycleState.VALIDATED, LifecycleState.ACTIVE],
        LifecycleState.DEPRECATED: [
            LifecycleState.VALIDATED,
            LifecycleState.ACTIVE,
            LifecycleState.DEPRECATED,
        ],
        LifecycleState.QUARANTINED: [LifecycleState.QUARANTINED],
        LifecycleState.REMOVED: [LifecycleState.REMOVED],
    }[state]
    for step in path:
        lc.transition_to(step, reason="setup")
    return lc


def test_quarantine_is_recoverable_but_deprecation_is_not():
    """两个状态的**全部**存在理由：一个是故障隔离，一个是产品决策。"""
    quarantined = _lifecycle_at(LifecycleState.QUARANTINED)
    quarantined.transition_to(LifecycleState.ACTIVE, reason="health restored")
    assert quarantined.state is LifecycleState.ACTIVE

    deprecated = _lifecycle_at(LifecycleState.DEPRECATED)
    with pytest.raises(LifecycleTransitionError):
        deprecated.transition_to(LifecycleState.ACTIVE, reason="changed our mind")


def test_removed_is_terminal_for_every_target():
    for target in LifecycleState:
        lc = _lifecycle_at(LifecycleState.REMOVED)
        with pytest.raises(LifecycleTransitionError):
            lc.transition_to(target, reason="test")


def test_self_transition_is_refused():
    lc = CapabilityLifecycle("c")
    with pytest.raises(LifecycleTransitionError):
        lc.transition_to(LifecycleState.REGISTERED, reason="noop")


def test_reason_is_mandatory():
    """无理由的状态变更等于没有审计——追责链在这里断掉。"""
    lc = CapabilityLifecycle("c")
    with pytest.raises(ValueError):
        lc.transition_to(LifecycleState.VALIDATED, reason="   ")


def test_history_is_append_only_and_ordered():
    lc = CapabilityLifecycle("c")
    lc.transition_to(LifecycleState.VALIDATED, reason="metadata ok")
    lc.transition_to(LifecycleState.ACTIVE, reason="provider available")

    assert [t.target for t in lc.history] == [
        LifecycleState.REGISTERED,
        LifecycleState.VALIDATED,
        LifecycleState.ACTIVE,
    ]
    assert [t.reason for t in lc.history][1:] == ["metadata ok", "provider available"]
    assert isinstance(lc.history, tuple)


def test_history_cannot_be_mutated_through_the_accessor():
    lc = CapabilityLifecycle("c")
    snapshot = lc.history
    lc.transition_to(LifecycleState.VALIDATED, reason="r")
    assert len(snapshot) == 1  # 旧快照不受后续变更影响


def test_transition_record_is_immutable_and_timestamped():
    lc = CapabilityLifecycle("c", clock=lambda: 1234.5)
    t = lc.transition_to(LifecycleState.VALIDATED, reason="r")
    assert isinstance(t, LifecycleTransition)
    assert t.at == 1234.5
    with pytest.raises(Exception):
        t.reason = "tampered"  # type: ignore[misc]


def test_can_transition_to_agrees_with_actual_behaviour():
    for source in LifecycleState:
        for target in LifecycleState:
            lc = _lifecycle_at(source)
            allowed = lc.can_transition_to(target)
            if allowed:
                lc.transition_to(target, reason="r")
                assert lc.state is target
            else:
                with pytest.raises(LifecycleTransitionError):
                    lc.transition_to(target, reason="r")


def test_state_has_no_public_setter():
    lc = CapabilityLifecycle("c")
    with pytest.raises(AttributeError):
        lc.state = LifecycleState.ACTIVE  # type: ignore[misc]


def test_single_write_point_for_state():
    """状态只能有一个写入点，否则转换表随时可以被绕过。"""
    import inspect

    src = inspect.getsource(lifecycle_mod)
    assert src.count("self._state = ") == 1


# ===========================================================================
# B. Registry 集成 —— lifecycle 不是空壳
# ===========================================================================
def test_successful_registration_reaches_active_with_full_history():
    reg = CapabilityRegistry()
    reg.register(CapabilityContract(capability_id="search.web"), _Prov("search.web"))

    lc = reg.lifecycle_for("search.web")
    assert lc is not None
    assert lc.state is LifecycleState.ACTIVE
    assert [t.target for t in lc.history] == [
        LifecycleState.REGISTERED,
        LifecycleState.VALIDATED,
        LifecycleState.ACTIVE,
    ]


def test_report_level_admits_but_quarantines_contradictory_contract():
    """本 slice 的核心设计点：REPORT 级不再是"完全放行"，而是"放行但隔离"。

    应急阀门若只是静默放行，就成了黑洞；带上 QUARANTINED 之后它变成一个**留痕的**
    例外——这正是 §58 说的"注册成功但校验不达标，不直接 REMOVED"。
    """
    reg = CapabilityRegistry(metadata_enforcement=EnforcementLevel.REPORT)
    reg.register(
        CapabilityContract(capability_id="search.web", timeout=-1.0), _Prov("search.web")
    )

    lc = reg.lifecycle_for("search.web")
    assert lc is not None
    assert lc.state is LifecycleState.QUARANTINED
    assert "M013" in lc.history[-1].reason


def test_clean_contract_is_not_quarantined_under_report_level():
    reg = CapabilityRegistry(metadata_enforcement=EnforcementLevel.REPORT)
    reg.register(CapabilityContract(capability_id="search.web"), _Prov("search.web"))
    assert reg.lifecycle_for("search.web").state is LifecycleState.ACTIVE


def test_metadata_rejected_registration_leaves_no_lifecycle():
    reg = CapabilityRegistry()
    with pytest.raises(MetadataIntegrityError):
        reg.register(
            CapabilityContract(capability_id="search.web", timeout=0.0), _Prov("search.web")
        )
    assert reg.lifecycle_for("search.web") is None


def test_f71_rejected_registration_leaves_no_lifecycle():
    reg = CapabilityRegistry()
    with pytest.raises(RegistrationIntegrityError):
        reg.register(
            CapabilityContract(capability_id="http.post", side_effect=True, idempotent=False),
            _Prov("http.post", side_effect=False, idempotent=True),
        )
    assert reg.lifecycle_for("http.post") is None


def test_lifecycle_for_unknown_capability_is_none():
    assert CapabilityRegistry().lifecycle_for("nope") is None


def test_reregistration_keeps_one_lifecycle_and_extends_history():
    reg = CapabilityRegistry()
    reg.register(CapabilityContract(capability_id="search.web"), _Prov("search.web"))
    first = reg.lifecycle_for("search.web")
    reg.register(CapabilityContract(capability_id="search.web"), _Prov("search.web"))

    assert reg.lifecycle_for("search.web") is first  # 同一个受治理对象
    assert first.state is LifecycleState.ACTIVE


def test_quarantined_capability_is_still_resolvable_in_this_slice():
    """边界声明（刻意）：本 slice 的 lifecycle 是记录层，不参与候选过滤。

    让 QUARANTINED 真的挡住 resolve 会改变 fallback semantics（R4=HIGH）。那需要
    单独一次授权，而不是搭本 slice 的便车。
    """
    reg = CapabilityRegistry(metadata_enforcement=EnforcementLevel.REPORT)
    prov = _Prov("search.web")
    reg.register(CapabilityContract(capability_id="search.web", timeout=-1.0), prov)
    assert reg.lifecycle_for("search.web").state is LifecycleState.QUARANTINED

    result = CapabilityResolver(reg).resolve(CapabilityNeed("search.web"), ("r", "t", 0))
    assert result.success


# ===========================================================================
# C. Resolution Audit —— 默认零成本
# ===========================================================================
def test_no_sink_means_no_audit_work(monkeypatch):
    """R4：不挂 sink 时，resolve 不得为审计付出任何代价。"""
    calls = {"n": 0}
    original = CapabilityResolver._emit_audit

    def _counting(self, *a, **kw):
        calls["n"] += 1
        return original(self, *a, **kw)

    monkeypatch.setattr(CapabilityResolver, "_emit_audit", _counting)

    reg = CapabilityRegistry()
    reg.register(CapabilityContract(capability_id="search.web"), _Prov("search.web"))
    resolver = CapabilityResolver(reg)
    for i in range(3):
        resolver.resolve(CapabilityNeed("search.web"), ("r", "t", i))
    assert calls["n"] == 0


def test_sink_receives_one_event_per_resolve():
    resolver, _, sink = _sunk_resolver(_Prov("search.web"))
    resolver.resolve(CapabilityNeed("search.web"), ("r", "t", 0))
    resolver.resolve(CapabilityNeed("search.web"), ("r", "t", 1))
    assert len(sink.events) == 2


def test_event_carries_the_frozen_minimal_shape():
    """§58 冻结的最小结构必须逐字段存在。"""
    resolver, _, sink = _sunk_resolver(_Prov("search.web"))
    resolver.resolve(CapabilityNeed("search.web"), ("r", "t", 0))
    e = sink.events[0]

    assert isinstance(e, ResolutionEvent)
    assert e.timestamp > 0
    assert e.capability_id == "search.web"
    assert e.requested_version is None
    assert isinstance(e.candidates, tuple) and e.candidates
    assert e.selected_provider is not None
    assert isinstance(e.metadata_snapshot, dict)


def test_requested_version_is_absent_not_invented():
    """``CapabilityNeed`` 结构上没有 version，就诚实记 None（续 F8.1 no-invented-truth）。"""
    resolver, _, sink = _sunk_resolver(_Prov("search.web"))
    resolver.resolve(CapabilityNeed("search.web"), ("r", "t", 0))
    assert sink.events[0].requested_version is None


def test_execution_key_is_recorded_for_ledger_reconciliation():
    """追责的关键：audit record 必须能和 ledger 对上同一次执行（I3 身份）。"""
    resolver, _, sink = _sunk_resolver(_Prov("search.web"))
    resolver.resolve(CapabilityNeed("search.web"), ("run-9", "task-2", 3))
    assert sink.events[0].execution_key == ("run-9", "task-2", 3)


def test_provider_identity_falls_back_to_class_name():
    """``Provider`` ABC 没有 provider_id；不伪造，退化到类名。"""
    plain = _Prov("search.web")
    assert provider_identity(plain) == "_Prov"


def test_provider_identity_prefers_declared_id():
    named = _Prov("search.web", provider_id="search.web.bing")
    assert provider_identity(named) == "search.web.bing"


def test_metadata_snapshot_captures_decision_time_contract():
    resolver, _, sink = _sunk_resolver(_Prov("search.web"), retry=2, timeout=9.0)
    resolver.resolve(CapabilityNeed("search.web"), ("r", "t", 0))
    snap = sink.events[0].metadata_snapshot
    assert snap["retry"] == 2
    assert snap["timeout"] == 9.0
    assert snap["permission"] == "capability:invoke"


def test_event_is_immutable():
    resolver, _, sink = _sunk_resolver(_Prov("search.web"))
    resolver.resolve(CapabilityNeed("search.web"), ("r", "t", 0))
    with pytest.raises(Exception):
        sink.events[0].capability_id = "tampered"  # type: ignore[misc]


# --- 候选记录 ---------------------------------------------------------------
def test_selected_candidate_has_no_rejection_reason():
    resolver, _, sink = _sunk_resolver(_Prov("search.web"))
    resolver.resolve(CapabilityNeed("search.web"), ("r", "t", 0))
    selected = [c for c in sink.events[0].candidates if c.rejected_reason is None]
    assert len(selected) == 1
    assert selected[0].provider == sink.events[0].selected_provider


def test_unhealthy_provider_is_recorded_with_reason():
    reg = CapabilityRegistry()
    sick = _Prov("search.web", healthy=False)
    good = _Prov("search.web")
    reg.register(CapabilityContract(capability_id="search.web"), sick, good)
    sink = InMemoryAuditSink()
    CapabilityResolver(reg, audit_sink=sink).resolve(CapabilityNeed("search.web"), ("r", "t", 0))

    reasons = {c.rejected_reason for c in sink.events[0].candidates}
    assert REASON_UNHEALTHY in reasons


def test_scope_gap_is_recorded_with_reason():
    reg = CapabilityRegistry()
    narrow = _Prov("search.web", granted_scopes={"something:else"})
    good = _Prov("search.web")
    reg.register(CapabilityContract(capability_id="search.web"), narrow, good)
    sink = InMemoryAuditSink()
    CapabilityResolver(reg, audit_sink=sink).resolve(CapabilityNeed("search.web"), ("r", "t", 0))

    reasons = {c.rejected_reason for c in sink.events[0].candidates}
    assert REASON_SCOPE_GAP in reasons


def test_filtered_candidates_carry_no_score():
    """过滤期就出局的候选根本没被评分——记 0.0 会让报告看起来像"评过分但很低"。"""
    reg = CapabilityRegistry()
    reg.register(
        CapabilityContract(capability_id="search.web"),
        _Prov("search.web", healthy=False),
        _Prov("search.web"),
    )
    sink = InMemoryAuditSink()
    CapabilityResolver(reg, audit_sink=sink).resolve(CapabilityNeed("search.web"), ("r", "t", 0))

    for c in sink.events[0].candidates:
        if c.rejected_reason == REASON_UNHEALTHY:
            assert c.score is None
        else:
            assert isinstance(c.score, float)


def test_unattempted_candidate_is_distinguished_from_failed_one():
    """"没轮到它"和"它失败了"是两件事，混为一谈会误导事后复盘。"""
    reg = CapabilityRegistry()
    fast = _Prov("search.web", latency_ms=0.0, provider_id="fast")
    slow = _Prov("search.web", latency_ms=900.0, provider_id="slow")
    reg.register(CapabilityContract(capability_id="search.web"), fast, slow)
    sink = InMemoryAuditSink()
    CapabilityResolver(reg, audit_sink=sink).resolve(CapabilityNeed("search.web"), ("r", "t", 0))

    by_id = {c.provider: c for c in sink.events[0].candidates}
    assert by_id["fast"].rejected_reason is None
    assert by_id["slow"].rejected_reason == REASON_NOT_ATTEMPTED


def test_transient_exhaustion_is_recorded():
    def _flaky():
        raise TransientError("boom")

    resolver, _, sink = _sunk_resolver(_Prov("search.web", behavior=_flaky), retry=1)
    with pytest.raises(CapabilityUnavailable):
        resolver.resolve(CapabilityNeed("search.web"), ("r", "t", 0))
    assert sink.events[0].candidates[0].rejected_reason == REASON_TRANSIENT_EXHAUSTED


def test_logical_error_is_recorded():
    def _logical():
        raise LogicalError("bad args")

    resolver, _, sink = _sunk_resolver(_Prov("search.web", behavior=_logical))
    with pytest.raises(CapabilityUnavailable):
        resolver.resolve(CapabilityNeed("search.web"), ("r", "t", 0))
    assert sink.events[0].candidates[0].rejected_reason == REASON_LOGICAL_ERROR


def test_soft_failure_is_recorded():
    resolver, _, sink = _sunk_resolver(
        _Prov("search.web", behavior=lambda: ProviderResult(success=False, error="nope"))
    )
    with pytest.raises(CapabilityUnavailable):
        resolver.resolve(CapabilityNeed("search.web"), ("r", "t", 0))
    assert sink.events[0].candidates[0].rejected_reason == REASON_SOFT_FAILURE


def test_audit_candidate_set_matches_the_real_one():
    """audit 不许说谎：记录的候选集必须与实际参与选择的完全一致。"""
    reg = CapabilityRegistry()
    provs = [
        _Prov("search.web", provider_id="a"),
        _Prov("search.web", provider_id="b", healthy=False),
        _Prov("search.web", provider_id="c", granted_scopes={"other"}),
    ]
    reg.register(CapabilityContract(capability_id="search.web"), *provs)
    sink = InMemoryAuditSink()
    CapabilityResolver(reg, audit_sink=sink).resolve(CapabilityNeed("search.web"), ("r", "t", 0))

    recorded = {c.provider for c in sink.events[0].candidates}
    assert recorded == {"a", "b", "c"}


# --- 失败路径也必须留痕 ------------------------------------------------------
def test_capability_not_found_is_audited():
    """§58 的最小结构只描述了成功字段，但审计恰恰在全盘失败时最有价值。"""
    sink = InMemoryAuditSink()
    resolver = CapabilityResolver(CapabilityRegistry(), audit_sink=sink)
    with pytest.raises(CapabilityNotFound):
        resolver.resolve(CapabilityNeed("nope"), ("r", "t", 0))

    e = sink.events[0]
    assert e.outcome is ResolutionOutcome.CAPABILITY_NOT_FOUND
    assert e.selected_provider is None
    assert e.candidates == ()
    assert e.error


def test_no_provider_is_audited():
    reg = CapabilityRegistry()
    reg.register(CapabilityContract(capability_id="search.web"), _Prov("search.web"))
    reg._providers["search.web"] = []  # 模拟 provider 全部撤走
    sink = InMemoryAuditSink()
    with pytest.raises(NoProvider):
        CapabilityResolver(reg, audit_sink=sink).resolve(
            CapabilityNeed("search.web"), ("r", "t", 0)
        )
    assert sink.events[0].outcome is ResolutionOutcome.NO_PROVIDER


def test_permission_denied_is_audited_with_candidate_detail():
    reg = CapabilityRegistry()
    reg.register(
        CapabilityContract(capability_id="search.web"),
        _Prov("search.web", granted_scopes={"other"}, provider_id="narrow"),
    )
    sink = InMemoryAuditSink()
    with pytest.raises(PermissionDenied):
        CapabilityResolver(reg, audit_sink=sink).resolve(
            CapabilityNeed("search.web"), ("r", "t", 0)
        )

    e = sink.events[0]
    assert e.outcome is ResolutionOutcome.PERMISSION_DENIED
    assert e.candidates[0].provider == "narrow"
    assert e.candidates[0].rejected_reason == REASON_SCOPE_GAP


def test_capability_unavailable_is_audited():
    resolver, _, sink = _sunk_resolver(
        _Prov("search.web", behavior=lambda: ProviderResult(success=False, error="nope"))
    )
    with pytest.raises(CapabilityUnavailable):
        resolver.resolve(CapabilityNeed("search.web"), ("r", "t", 0))
    assert sink.events[0].outcome is ResolutionOutcome.UNAVAILABLE


def test_successful_outcome_is_labelled():
    resolver, _, sink = _sunk_resolver(_Prov("search.web"))
    resolver.resolve(CapabilityNeed("search.web"), ("r", "t", 0))
    assert sink.events[0].outcome is ResolutionOutcome.SELECTED
    assert sink.events[0].error is None


# --- fail-open --------------------------------------------------------------
def test_exploding_sink_does_not_break_resolution():
    """audit fail-**open**。观测失灵不该让能力不可用——与 enforcement 正好相反。"""

    def _explode(event):
        raise RuntimeError("sink is down")

    reg = CapabilityRegistry()
    reg.register(CapabilityContract(capability_id="search.web"), _Prov("search.web"))
    result = CapabilityResolver(reg, audit_sink=_explode).resolve(
        CapabilityNeed("search.web"), ("r", "t", 0)
    )
    assert result.success


def test_exploding_sink_does_not_mask_a_real_failure():
    """sink 炸掉时，原本该抛的 ResolutionError 仍要原样抛出，不能被替换。"""

    def _explode(event):
        raise RuntimeError("sink is down")

    resolver = CapabilityResolver(CapabilityRegistry(), audit_sink=_explode)
    with pytest.raises(CapabilityNotFound):
        resolver.resolve(CapabilityNeed("nope"), ("r", "t", 0))


def test_plain_callable_works_as_a_sink():
    seen = []
    reg = CapabilityRegistry()
    reg.register(CapabilityContract(capability_id="search.web"), _Prov("search.web"))
    CapabilityResolver(reg, audit_sink=seen.append).resolve(
        CapabilityNeed("search.web"), ("r", "t", 0)
    )
    assert len(seen) == 1


# ===========================================================================
# D. 热路径不变量（sink 启用时同样成立）
# ===========================================================================
def test_execution_key_still_passed_verbatim_with_audit_on():
    prov = _Prov("search.web")
    resolver, _, _ = _sunk_resolver(prov)
    key = ("run-1", "task-1", 5)
    resolver.resolve(CapabilityNeed("search.web"), key)
    assert prov.calls == [key]


def test_transient_budget_unchanged_with_audit_on():
    calls = {"n": 0}

    def _flaky():
        calls["n"] += 1
        raise TransientError("nope")

    resolver, _, _ = _sunk_resolver(_Prov("search.web", behavior=_flaky), retry=2)
    with pytest.raises(CapabilityUnavailable):
        resolver.resolve(CapabilityNeed("search.web"), ("r", "t", 0))
    assert calls["n"] == 3


def test_logical_error_still_skips_candidate_with_audit_on():
    def _logical():
        raise LogicalError("bad")

    bad = _Prov("search.web", behavior=_logical, provider_id="bad")
    good = _Prov("search.web", provider_id="good")
    reg = CapabilityRegistry()
    reg.register(CapabilityContract(capability_id="search.web", retry=5), bad, good)
    sink = InMemoryAuditSink()
    result = CapabilityResolver(reg, audit_sink=sink).resolve(
        CapabilityNeed("search.web"), ("r", "t", 0)
    )
    assert result.success
    assert len(bad.calls) == 1  # logical 不吃重试预算


def test_contract_for_still_returns_runtime_object():
    reg = CapabilityRegistry()
    contract = CapabilityContract(capability_id="search.web")
    reg.register(contract, _Prov("search.web"))
    assert reg.contract_for("search.web") is contract


def test_result_object_is_returned_untouched():
    sentinel = ProviderResult(success=True, data={"marker": 1})
    resolver, _, _ = _sunk_resolver(_Prov("search.web", behavior=lambda: sentinel))
    assert resolver.resolve(CapabilityNeed("search.web"), ("r", "t", 0)) is sentinel


# ===========================================================================
# E. InMemoryAuditSink
# ===========================================================================
def test_in_memory_sink_collects_and_clears():
    sink = InMemoryAuditSink()
    assert sink.events == ()
    reg = CapabilityRegistry()
    reg.register(CapabilityContract(capability_id="search.web"), _Prov("search.web"))
    CapabilityResolver(reg, audit_sink=sink).resolve(CapabilityNeed("search.web"), ("r", "t", 0))
    assert len(sink.events) == 1
    sink.clear()
    assert sink.events == ()


def test_in_memory_sink_events_are_a_snapshot():
    sink = InMemoryAuditSink()
    reg = CapabilityRegistry()
    reg.register(CapabilityContract(capability_id="search.web"), _Prov("search.web"))
    resolver = CapabilityResolver(reg, audit_sink=sink)
    resolver.resolve(CapabilityNeed("search.web"), ("r", "t", 0))
    snapshot = sink.events
    resolver.resolve(CapabilityNeed("search.web"), ("r", "t", 1))
    assert len(snapshot) == 1


def test_candidate_record_is_immutable():
    rec = CandidateRecord(provider="p", score=1.0, rejected_reason=None)
    with pytest.raises(Exception):
        rec.score = 2.0  # type: ignore[misc]


# ===========================================================================
# F. 依赖隔离
# ===========================================================================
@pytest.mark.parametrize("module", ["lifecycle", "audit"])
def test_governance_modules_do_not_import_runtime(module):
    script = textwrap.dedent(
        f"""
        import sys
        import anima.capability.governance.{module}  # noqa: F401
        leaked = [m for m in sys.modules
                  if m.startswith(("anima.runtime", "anima.constitution"))]
        print("LEAKED:", sorted(leaked))
        assert not leaked, leaked
        """
    )
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.parametrize("module", [lifecycle_mod, audit_mod])
def test_governance_modules_have_no_runtime_import_statements(module):
    import inspect

    offenders = [
        line.strip()
        for line in inspect.getsource(module).splitlines()
        if line.strip().startswith(("import ", "from "))
        and ("anima.runtime" in line or "anima.constitution" in line)
    ]
    assert not offenders, offenders
