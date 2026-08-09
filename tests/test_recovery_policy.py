"""F8.12 —— Recovery Policy Decision Layer 的四层单向链路证明。

本测试是 F8.12 Implementation 的验收面。它证明的**不是「恢复发生了」**，而是：

.. code-block:: text

    HealthEvidence          (Fact)           health/model.py
          |  只读
          v
    NormalizedExecutionOutcome (Interpretation)  health/normalizer.py
          |  只读（metadata["normalized_outcome"]）
          v
    HealthPolicyEvaluator → RecoveryDecision (Policy)  ops/health_policy_evaluator.py
          |  授权对象，非直接写
          v
    HealthObserver.recover → transition_to(ACTIVE)  (State Mutation)
                                       governance/lifecycle.py:167

四层**单向**、逐层只消费自己的上游、不许跳级；``transition_to()`` 保持
**唯一状态写入口**。

红线（AST 级钉死，不靠 docstring 文本匹配）：

- **R1**：evaluator / policy / decision 不 import runtime / tools（无 resolver 耦合）；
- **R2**：evaluator 只读 evidence，绝不变异；store schema 零改动（P2-A：时间窗
  拉取 + 内存过滤，query 只用顶层参数）；
- **R3**：ops 包无 timer / daemon / polling（AST 检查 banned import + ``while True``）。
"""
from __future__ import annotations

import ast
import uuid
from pathlib import Path

import pytest

from anima.capability.governance.lifecycle import (
    CapabilityLifecycle,
    LifecycleState,
)
from anima.capability.health.model import HealthEvidence
from anima.capability.health.normalizer import NormalizedExecutionOutcome
from anima.capability.health.store import HealthEvidenceStore
from anima.capability.ops.observer import HealthObserver
from anima.capability.ops.transition_store import TransitionStore

# -- F8.12 新增面（RED：实现前这些 import 必然失败）------------------------
from anima.capability.ops.decision import RecoveryAction, RecoveryDecision
from anima.capability.ops.health_policy_evaluator import (
    HealthPolicyEvaluator,
    WindowStats,
)
from anima.capability.ops.policy import (
    POLICIES,
    HealthPolicy,
    RecoveryCondition,
    UnknownHandling,
    get_policy,
    register_policy,
)

CAP = "search.web"
NOW = 1_000_000.0


# =========================================================================
# helpers
# =========================================================================
def _ev(
    outcome: NormalizedExecutionOutcome,
    at: float,
    *,
    capability_id: str = CAP,
    with_metadata: bool = True,
) -> HealthEvidence:
    """构造一条 evidence（``normalized_outcome`` 走 metadata，同 F8.11 投影）。"""
    metadata = {"normalized_outcome": outcome.value} if with_metadata else {}
    return HealthEvidence(
        evidence_id=uuid.uuid4().hex,
        capability_id=capability_id,
        provider_id="provider.a",
        execution_id="('run-1', 'task-1', 1)",
        observed_at=at,
        selected=True,
        executed=True,
        success=outcome is NormalizedExecutionOutcome.SUCCESS,
        error_type="" if outcome is NormalizedExecutionOutcome.SUCCESS else "Boom",
        duration_ms=1.5,
        metadata=metadata,
    )


def _policy(
    *,
    capability_id: str = CAP,
    failure_window: float = 60.0,
    minimum_samples: int = 3,
    failure_threshold: float = 0.5,
    unknown_handling: UnknownHandling = UnknownHandling.DILUTE,
    unknown_dilution_k: float = 1.0,
    max_failure_rate: float = 0.0,
    min_success_ratio: float = 0.6,
    kind: str = "failure_absent_and_success_majority",
) -> HealthPolicy:
    return HealthPolicy(
        capability_id=capability_id,
        failure_window=failure_window,
        minimum_samples=minimum_samples,
        failure_threshold=failure_threshold,
        unknown_handling=unknown_handling,
        unknown_dilution_k=unknown_dilution_k,
        recovery_condition=RecoveryCondition(
            kind=kind,
            max_failure_rate=max_failure_rate,
            min_success_ratio=min_success_ratio,
        ),
    )


class _FakeEvidenceStore:
    """只暴露 ``query``——用于钉死 evaluator 的查询形态（P2-A）。"""

    def __init__(self, evidence: list[HealthEvidence]) -> None:
        self._evidence = list(evidence)
        self.calls: list[dict] = []

    def query(self, **kwargs) -> list[HealthEvidence]:
        self.calls.append(dict(kwargs))
        cap = kwargs.get("capability_id")
        start = kwargs.get("start")
        end = kwargs.get("end")
        out = []
        for ev in self._evidence:
            if cap is not None and ev.capability_id != cap:
                continue
            if start is not None and ev.observed_at < start:
                continue
            if end is not None and ev.observed_at > end:
                continue
            out.append(ev)
        return out


def _quarantined(capability_id: str = CAP) -> CapabilityLifecycle:
    lc = CapabilityLifecycle(capability_id)
    lc.transition_to(LifecycleState.QUARANTINED, reason="test fixture: isolated")
    return lc


def _active(capability_id: str = CAP) -> CapabilityLifecycle:
    lc = CapabilityLifecycle(capability_id)
    lc.transition_to(LifecycleState.VALIDATED, reason="test fixture")
    lc.transition_to(LifecycleState.ACTIVE, reason="test fixture")
    return lc


def _accessor(lifecycle):
    return lambda cid: lifecycle


def _module_ast(module) -> ast.Module:
    return ast.parse(Path(module.__file__).read_text(encoding="utf-8"))


def _imported_roots(tree: ast.Module) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _imported_modules(tree: ast.Module) -> set[str]:
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
    return mods


def _attribute_names(tree: ast.Module) -> set[str]:
    return {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}


@pytest.fixture(autouse=True)
def _keep_global_registry_clean():
    """任何测试都不许污染全局 ``POLICIES``（每个用例自带 registry）。"""
    snapshot = dict(POLICIES)
    yield
    POLICIES.clear()
    POLICIES.update(snapshot)


# =========================================================================
# L3a —— HealthPolicy registry（P1：ops/ 落点，schema only 不带默认值）
# =========================================================================
def test_policy_registry_ships_no_default_values():
    """RFC §4.3：schema only，值不钉。F8 无生产数据 → 全局注册表**必须为空**。

    定错阈值比不定更危险（会误隔离真实可用 capability）。
    """
    assert dict(POLICIES) == {}, "F8.12 不得内置任何猜测阈值"


def test_register_and_get_policy_uses_capability_id_as_key():
    registry: dict[str, HealthPolicy] = {}
    policy = _policy()
    register_policy(policy, registry=registry)
    assert registry == {CAP: policy}
    assert get_policy(CAP, registry=registry) is policy
    assert get_policy("nope", registry=registry) is None


def test_health_policy_is_frozen_and_carries_rfc_fields():
    policy = _policy()
    for field_name in (
        "capability_id",
        "failure_window",
        "minimum_samples",
        "failure_threshold",
        "unknown_handling",
        "recovery_condition",
    ):
        assert hasattr(policy, field_name), field_name
    with pytest.raises(Exception):
        policy.failure_threshold = 0.9  # type: ignore[misc]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"capability_id": "  "},
        {"failure_window": 0.0},
        {"failure_window": -1.0},
        {"minimum_samples": 0},
        {"failure_threshold": -0.1},
        {"failure_threshold": 1.1},
        {"unknown_dilution_k": -0.5},
        {"min_success_ratio": 1.5},
        {"max_failure_rate": -0.2},
    ],
)
def test_health_policy_rejects_structurally_invalid_values(kwargs):
    """schema 校验属于契约（不是「默认值」）——非法结构必须当场拒绝。"""
    with pytest.raises(ValueError):
        _policy(**kwargs)


# =========================================================================
# L3b —— 窗口统计（RFC §6：时间窗为主模型，拒绝全局计数器）
# =========================================================================
def test_window_query_is_time_bounded_and_capability_scoped():
    store = _FakeEvidenceStore(
        [
            _ev(NormalizedExecutionOutcome.FAILURE, NOW - 10),   # 窗口内
            _ev(NormalizedExecutionOutcome.FAILURE, NOW - 9999),  # 窗口外
            _ev(NormalizedExecutionOutcome.FAILURE, NOW - 5, capability_id="other.cap"),
        ]
    )
    ev = HealthPolicyEvaluator(store, policies={CAP: _policy()}, clock=lambda: NOW)
    stats = ev.collect(CAP, _policy())
    assert isinstance(stats, WindowStats)
    assert (stats.success, stats.failure, stats.unknown) == (0, 1, 0)
    assert stats.start == pytest.approx(NOW - 60.0)
    assert stats.end == pytest.approx(NOW)


def test_evaluator_queries_store_with_top_level_params_only():
    """P2-A：时间窗拉取 + 内存过滤。**不得**给 store 传 metadata 查询参数
    （F8.10 store schema 冻结）。"""
    store = _FakeEvidenceStore([_ev(NormalizedExecutionOutcome.SUCCESS, NOW - 1)])
    ev = HealthPolicyEvaluator(store, policies={CAP: _policy()}, clock=lambda: NOW)
    ev.evaluate(CAP)
    assert store.calls, "evaluator 必须真的查 store"
    for call in store.calls:
        assert set(call) <= {"capability_id", "start", "end"}, call


def test_missing_or_unparsable_outcome_counts_as_unknown():
    """R3（F8.11 延续）：无法诚实分类 → UNKNOWN，绝不强行归 FAILURE。"""
    store = _FakeEvidenceStore(
        [
            _ev(NormalizedExecutionOutcome.SUCCESS, NOW - 1, with_metadata=False),
            _ev(NormalizedExecutionOutcome.UNKNOWN, NOW - 2),
        ]
    )
    ev = HealthPolicyEvaluator(store, clock=lambda: NOW)
    stats = ev.collect(CAP, _policy())
    assert (stats.success, stats.failure, stats.unknown) == (0, 0, 2)


# =========================================================================
# L3c —— 决策语义（RFC §6/§7：UNKNOWN 进分母不进分子）
# =========================================================================
def test_unknown_enters_denominator_not_numerator():
    stats = WindowStats(success=1, failure=1, unknown=2, start=NOW - 60, end=NOW)
    policy = _policy(unknown_handling=UnknownHandling.DILUTE, unknown_dilution_k=1.0)
    ev = HealthPolicyEvaluator(_FakeEvidenceStore([]), clock=lambda: NOW)
    assert ev.failure_rate(stats, policy) == pytest.approx(0.25)  # 1 / 4
    assert ev.sample_count(stats, policy) == 4


def test_unknown_handling_ignore_excludes_unknown_from_both_sides():
    stats = WindowStats(success=1, failure=1, unknown=2, start=NOW - 60, end=NOW)
    policy = _policy(unknown_handling=UnknownHandling.IGNORE)
    ev = HealthPolicyEvaluator(_FakeEvidenceStore([]), clock=lambda: NOW)
    assert ev.failure_rate(stats, policy) == pytest.approx(0.5)  # 1 / 2
    assert ev.sample_count(stats, policy) == 2


def test_unknown_dilution_raises_effective_threshold():
    """§7.2：UNKNOWN 占比越高，触发隔离所需的真实失败率越高。"""
    policy = _policy(failure_threshold=0.5, unknown_dilution_k=1.0)
    ev = HealthPolicyEvaluator(_FakeEvidenceStore([]), clock=lambda: NOW)
    clean = WindowStats(success=1, failure=1, unknown=0, start=NOW - 60, end=NOW)
    murky = WindowStats(success=1, failure=1, unknown=2, start=NOW - 60, end=NOW)
    assert ev.effective_threshold(clean, policy) == pytest.approx(0.5)
    assert ev.effective_threshold(murky, policy) == pytest.approx(0.75)  # 0.5*(1+0.5)


def test_no_policy_registered_yields_no_action():
    store = _FakeEvidenceStore([_ev(NormalizedExecutionOutcome.SUCCESS, NOW - 1)])
    ev = HealthPolicyEvaluator(store, policies={}, clock=lambda: NOW)
    decision = ev.evaluate(CAP)
    assert decision.action is RecoveryAction.NO_ACTION
    assert "policy" in decision.reason.lower()


def test_insufficient_samples_yields_no_action():
    store = _FakeEvidenceStore([_ev(NormalizedExecutionOutcome.FAILURE, NOW - 1)])
    ev = HealthPolicyEvaluator(
        store, policies={CAP: _policy(minimum_samples=3)}, clock=lambda: NOW
    )
    decision = ev.evaluate(CAP)
    assert decision.action is RecoveryAction.NO_ACTION
    assert "sample" in decision.reason.lower()


def test_failure_rate_above_threshold_yields_quarantine():
    store = _FakeEvidenceStore(
        [_ev(NormalizedExecutionOutcome.FAILURE, NOW - i) for i in range(1, 5)]
    )
    ev = HealthPolicyEvaluator(
        store, policies={CAP: _policy(failure_threshold=0.5)}, clock=lambda: NOW
    )
    decision = ev.evaluate(CAP)
    assert decision.action is RecoveryAction.QUARANTINE
    assert decision.evidence_range == (pytest.approx(NOW - 60.0), pytest.approx(NOW))


def test_healthy_window_yields_recover():
    store = _FakeEvidenceStore(
        [_ev(NormalizedExecutionOutcome.SUCCESS, NOW - i) for i in range(1, 5)]
    )
    ev = HealthPolicyEvaluator(store, policies={CAP: _policy()}, clock=lambda: NOW)
    decision = ev.evaluate(CAP)
    assert decision.action is RecoveryAction.RECOVER
    assert decision.capability_id == CAP
    assert decision.reason.strip()


def test_success_below_required_ratio_yields_no_action():
    store = _FakeEvidenceStore(
        [_ev(NormalizedExecutionOutcome.SUCCESS, NOW - 1)]
        + [_ev(NormalizedExecutionOutcome.UNKNOWN, NOW - i) for i in range(2, 5)]
    )
    ev = HealthPolicyEvaluator(
        store, policies={CAP: _policy(min_success_ratio=0.6)}, clock=lambda: NOW
    )
    assert ev.evaluate(CAP).action is RecoveryAction.NO_ACTION


def test_unsupported_recovery_condition_kind_never_recovers():
    """未知谓词 → 不恢复（fail-safe：看不懂的条件不构成恢复授权）。"""
    store = _FakeEvidenceStore(
        [_ev(NormalizedExecutionOutcome.SUCCESS, NOW - i) for i in range(1, 5)]
    )
    ev = HealthPolicyEvaluator(
        store, policies={CAP: _policy(kind="quantum_vibes")}, clock=lambda: NOW
    )
    decision = ev.evaluate(CAP)
    assert decision.action is RecoveryAction.NO_ACTION
    assert "condition" in decision.reason.lower()


def test_decide_is_pure_and_takes_precomputed_stats():
    """``decide`` 是纯函数：给同样的 stats + policy 必得同样的 decision。"""
    ev = HealthPolicyEvaluator(_FakeEvidenceStore([]), clock=lambda: NOW)
    stats = WindowStats(success=4, failure=0, unknown=0, start=NOW - 60, end=NOW)
    policy = _policy()
    first = ev.decide(CAP, stats, policy)
    second = ev.decide(CAP, stats, policy)
    assert first == second
    assert first.action is RecoveryAction.RECOVER


# =========================================================================
# L3 → L4 边界：evaluator 绝不写状态
# =========================================================================
def test_evaluate_does_not_mutate_lifecycle():
    lifecycle = _quarantined()
    store = _FakeEvidenceStore(
        [_ev(NormalizedExecutionOutcome.SUCCESS, NOW - i) for i in range(1, 5)]
    )
    ev = HealthPolicyEvaluator(store, policies={CAP: _policy()}, clock=lambda: NOW)
    decision = ev.evaluate(CAP)
    assert decision.action is RecoveryAction.RECOVER
    assert lifecycle.state is LifecycleState.QUARANTINED
    assert len(lifecycle.history) == 2  # 初始 + fixture 隔离，evaluate 没写


def test_evaluator_never_calls_transition_to():
    """AST 级：evaluator / policy / decision 模块内不得出现 ``transition_to``。"""
    from anima.capability.ops import decision as decision_mod
    from anima.capability.ops import health_policy_evaluator as eval_mod
    from anima.capability.ops import policy as policy_mod

    for module in (eval_mod, policy_mod, decision_mod):
        assert "transition_to" not in _attribute_names(_module_ast(module)), module.__name__


def test_evaluator_does_not_mutate_evidence():
    """R2：evidence 是历史事实，policy 只解释不改写。"""
    evidence = [
        _ev(NormalizedExecutionOutcome.SUCCESS, NOW - 1),
        _ev(NormalizedExecutionOutcome.FAILURE, NOW - 2),
    ]
    before = [(e.evidence_id, dict(e.metadata), e.success) for e in evidence]
    ev = HealthPolicyEvaluator(
        _FakeEvidenceStore(evidence), policies={CAP: _policy()}, clock=lambda: NOW
    )
    ev.evaluate(CAP)
    after = [(e.evidence_id, dict(e.metadata), e.success) for e in evidence]
    assert before == after


# =========================================================================
# L4 —— HealthObserver.recover（唯一状态写入通道）
# =========================================================================
def _recover_decision(capability_id: str = CAP) -> RecoveryDecision:
    return RecoveryDecision(
        capability_id=capability_id,
        action=RecoveryAction.RECOVER,
        reason="failure_rate=0.000 over healthy window",
        evidence_range=(NOW - 60.0, NOW),
    )


def test_recover_moves_quarantined_to_active(tmp_path):
    lifecycle = _quarantined()
    store = TransitionStore(str(tmp_path / "transitions.jsonl"))
    observer = HealthObserver(_accessor(lifecycle), store)

    assert observer.recover(CAP, _recover_decision()) is True
    assert lifecycle.state is LifecycleState.ACTIVE

    last = lifecycle.history[-1]
    assert last.source is LifecycleState.QUARANTINED
    assert last.target is LifecycleState.ACTIVE


def test_recover_reason_carries_the_decision(tmp_path):
    """P3：不建 RecoveryDecisionStore——decision 以结构化 reason 落 TransitionStore。"""
    lifecycle = _quarantined()
    store = TransitionStore(str(tmp_path / "transitions.jsonl"))
    observer = HealthObserver(_accessor(lifecycle), store)
    decision = _recover_decision()

    observer.recover(CAP, decision)
    reason = lifecycle.history[-1].reason

    assert "RECOVER" in reason
    assert "failure_rate=0.000" in reason
    assert str(NOW - 60.0) in reason or f"{NOW - 60.0:.3f}" in reason
    assert reason == decision.as_transition_reason()

    persisted = store.query(capability_id=CAP, target=LifecycleState.ACTIVE)
    assert len(persisted) == 1
    assert persisted[0].reason == reason


def test_recover_is_idempotent_on_already_active(tmp_path):
    lifecycle = _active()
    observer = HealthObserver(
        _accessor(lifecycle), TransitionStore(str(tmp_path / "t.jsonl"))
    )
    before = len(lifecycle.history)
    assert observer.recover(CAP, _recover_decision()) is False
    assert lifecycle.state is LifecycleState.ACTIVE
    assert len(lifecycle.history) == before


@pytest.mark.parametrize(
    "factory,expected_state",
    [
        (lambda: CapabilityLifecycle(CAP), LifecycleState.REGISTERED),
    ],
)
def test_recover_refuses_non_quarantined_states(tmp_path, factory, expected_state):
    """恢复只从**隔离态**出发。REGISTERED/VALIDATED → ACTIVE 是正常上线路径，
    不能被 recovery 借道绕过 F8.2b 校验门。"""
    lifecycle = factory()
    observer = HealthObserver(
        _accessor(lifecycle), TransitionStore(str(tmp_path / "t.jsonl"))
    )
    assert observer.recover(CAP, _recover_decision()) is False
    assert lifecycle.state is expected_state


def test_recover_refuses_deprecated_and_removed(tmp_path):
    for target in (LifecycleState.DEPRECATED, LifecycleState.REMOVED):
        lifecycle = _quarantined()
        lifecycle.transition_to(target, reason="test fixture")
        observer = HealthObserver(
            _accessor(lifecycle), TransitionStore(str(tmp_path / f"t-{target.value}.jsonl"))
        )
        assert observer.recover(CAP, _recover_decision()) is False
        assert lifecycle.state is target


@pytest.mark.parametrize("action", [RecoveryAction.NO_ACTION, RecoveryAction.QUARANTINE])
def test_recover_refuses_non_recover_decisions(tmp_path, action):
    lifecycle = _quarantined()
    observer = HealthObserver(
        _accessor(lifecycle), TransitionStore(str(tmp_path / "t.jsonl"))
    )
    decision = RecoveryDecision(
        capability_id=CAP, action=action, reason="r", evidence_range=(NOW - 1, NOW)
    )
    assert observer.recover(CAP, decision) is False
    assert lifecycle.state is LifecycleState.QUARANTINED


def test_recover_rejects_capability_id_mismatch(tmp_path):
    lifecycle = _quarantined()
    observer = HealthObserver(
        _accessor(lifecycle), TransitionStore(str(tmp_path / "t.jsonl"))
    )
    with pytest.raises(ValueError):
        observer.recover("other.cap", _recover_decision(CAP))
    assert lifecycle.state is LifecycleState.QUARANTINED


def test_recover_returns_false_for_unregistered_capability(tmp_path):
    observer = HealthObserver(
        lambda cid: None, TransitionStore(str(tmp_path / "t.jsonl"))
    )
    assert observer.recover(CAP, _recover_decision()) is False


def test_recover_is_fail_open_on_store_failure():
    """持久化失败 ≠ 治理失败（F8.8 纪律延续）。"""

    class _Boom:
        def append(self, *_args, **_kwargs):
            raise OSError("disk on fire")

    lifecycle = _quarantined()
    observer = HealthObserver(_accessor(lifecycle), _Boom())
    assert observer.recover(CAP, _recover_decision()) is True
    assert lifecycle.state is LifecycleState.ACTIVE
    assert observer.store_failures == 1


def test_report_unhealthy_semantics_unchanged(tmp_path):
    """冻结面回归：F8.8 的 QUARANTINED 方向语义一字不改。"""
    lifecycle = _active()
    observer = HealthObserver(
        _accessor(lifecycle), TransitionStore(str(tmp_path / "t.jsonl"))
    )
    assert observer.report_unhealthy(CAP, reason="probe failed") is True
    assert lifecycle.state is LifecycleState.QUARANTINED
    assert observer.report_unhealthy(CAP, reason="probe failed again") is False


# =========================================================================
# 端到端：四层链路（真实 store + 真实 lifecycle）
# =========================================================================
def test_end_to_end_four_layer_chain(tmp_path):
    """Fact → Interpretation → Policy → State Mutation，一次跑通且**只跑一次**。"""
    evidence_store = HealthEvidenceStore(str(tmp_path / "evidence.jsonl"))
    for i in range(1, 5):
        evidence_store.append(_ev(NormalizedExecutionOutcome.SUCCESS, NOW - i))

    lifecycle = _quarantined()
    transition_store = TransitionStore(str(tmp_path / "transitions.jsonl"))
    observer = HealthObserver(_accessor(lifecycle), transition_store)
    evaluator = HealthPolicyEvaluator(
        evidence_store,
        policies={CAP: _policy()},
        observer=observer,
        clock=lambda: NOW,
    )

    decision, applied = evaluator.record(CAP)

    assert decision.action is RecoveryAction.RECOVER
    assert applied is True
    assert lifecycle.state is LifecycleState.ACTIVE

    recoveries = [
        t for t in lifecycle.history
        if t.source is LifecycleState.QUARANTINED and t.target is LifecycleState.ACTIVE
    ]
    assert len(recoveries) == 1

    # 二次触发幂等：已 ACTIVE，不再产生第二条 transition
    decision2, applied2 = evaluator.record(CAP)
    assert decision2.action is RecoveryAction.RECOVER  # policy 层仍这么判
    assert applied2 is False                            # 执行通道拒绝重复
    assert len(lifecycle.history) == 3


def test_record_without_observer_is_decision_only(tmp_path):
    """没接执行通道时，``record`` 只出决策，绝不偷偷改状态。"""
    evidence_store = HealthEvidenceStore(str(tmp_path / "evidence.jsonl"))
    for i in range(1, 5):
        evidence_store.append(_ev(NormalizedExecutionOutcome.SUCCESS, NOW - i))
    evaluator = HealthPolicyEvaluator(
        evidence_store, policies={CAP: _policy()}, clock=lambda: NOW
    )
    decision, applied = evaluator.record(CAP)
    assert decision.action is RecoveryAction.RECOVER
    assert applied is False


def test_record_routes_quarantine_through_existing_channel(tmp_path):
    """QUARANTINE 方向复用 F8.8 既有 ``report_unhealthy``，不新开写入口。"""
    evidence_store = HealthEvidenceStore(str(tmp_path / "evidence.jsonl"))
    for i in range(1, 5):
        evidence_store.append(_ev(NormalizedExecutionOutcome.FAILURE, NOW - i))

    lifecycle = _active()
    observer = HealthObserver(
        _accessor(lifecycle), TransitionStore(str(tmp_path / "t.jsonl"))
    )
    evaluator = HealthPolicyEvaluator(
        evidence_store,
        policies={CAP: _policy(failure_threshold=0.5)},
        observer=observer,
        clock=lambda: NOW,
    )
    decision, applied = evaluator.record(CAP)
    assert decision.action is RecoveryAction.QUARANTINE
    assert applied is True
    assert lifecycle.state is LifecycleState.QUARANTINED


# =========================================================================
# 红线：R1 依赖面 / R3 无后台
# =========================================================================
def test_ops_package_does_not_import_runtime_or_tools():
    """R1：policy 决策层与 resolver / runtime 物理解耦。"""
    from anima.capability.ops import decision as decision_mod
    from anima.capability.ops import health_policy_evaluator as eval_mod
    from anima.capability.ops import observer as observer_mod
    from anima.capability.ops import policy as policy_mod

    for module in (eval_mod, policy_mod, decision_mod, observer_mod):
        mods = _imported_modules(_module_ast(module))
        offenders = [
            m for m in mods
            if m.startswith("anima.runtime") or m.startswith("anima.tools")
        ]
        assert not offenders, f"{module.__name__} imports {offenders}"


def test_ops_package_has_no_timer_daemon_or_polling():
    """R3：事件驱动，无 timer / daemon / polling。"""
    from anima.capability.ops import decision as decision_mod
    from anima.capability.ops import health_policy_evaluator as eval_mod
    from anima.capability.ops import observer as observer_mod
    from anima.capability.ops import policy as policy_mod

    banned = {"threading", "sched", "asyncio", "multiprocessing", "signal", "concurrent"}
    for module in (eval_mod, policy_mod, decision_mod, observer_mod):
        tree = _module_ast(module)
        assert not (_imported_roots(tree) & banned), module.__name__
        for node in ast.walk(tree):
            if isinstance(node, ast.While):
                assert not (
                    isinstance(node.test, ast.Constant) and node.test.value is True
                ), f"{module.__name__} 含 while True 轮询"


def test_transition_to_remains_the_sole_mutation_point():
    """L4 唯一写入口：整个 ops 包里只有 observer 调 ``transition_to``。"""
    import anima.capability.ops as ops_pkg

    pkg_dir = Path(ops_pkg.__file__).parent
    callers = []
    for path in sorted(pkg_dir.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if "transition_to" in _attribute_names(tree):
            callers.append(path.name)
    assert callers == ["observer.py"], callers
