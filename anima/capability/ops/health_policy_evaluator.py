"""F8.12 —— ``HealthPolicyEvaluator``：把执行事实解释成一条治理决策。

这是四层链路里的第三层，也是此前唯一缺席的一层：

.. code-block:: text

    HealthEvidence              事实      「发生了什么」   health/model.py
    NormalizedExecutionOutcome  解释      「算成功还是失败」 health/normalizer.py
    HealthPolicyEvaluator       策略      「该不该动状态」  ← 本模块
    Lifecycle 状态变更          执行      「动了状态」      governance/lifecycle.py

本模块**只产出决策，不执行决策**。它拿不到、也不去拿 lifecycle 引用——恢复要真
的发生，得由执行通道（``HealthObserver.recover``）再走一遍它自己的守卫。这条分
界不是形式主义：决策可以基于统计做得很激进，而状态机的合法性、幂等、终态保护
是另一套约束，两者混在一起时，任何一方出错都会直接落成脏状态。

## 时间窗，不是全局计数器（RFC §6）

统计约束在「同一 capability、同一时段」。全局累计计数会把「search.web 超时一次
+ filesystem.read 失败两次」算成三连败，然后隔离一个毫无关系的 capability。外部
依赖的故障本来就是**时段性**的，窗口才是它的自然刻画。

## UNKNOWN 稀释（RFC §7）

``UNKNOWN`` 进分母、不进分子，并按占比抬高有效阈值：

.. code-block:: text

    effective_threshold = failure_threshold * (1 + k * unknown_ratio)

意思是「越看不清，越要更硬的证据才动手」。把 UNKNOWN 归 FAILURE 会过度惩罚偶发
无法分类的 provider，归 SUCCESS 又是自欺——两头都错，所以取稀释。

## 查询形态（P2-A）

``store.query(capability_id=, start=, end=)`` 拉时间窗，再在内存里按
``metadata["normalized_outcome"]`` 过滤。**不给 store 加 metadata 查询参数**
（F8.10 schema 冻结）。F8 pilot 只有 4 个 capability、低流量，内存过滤成本可忽略；
真到成本不可接受那天，再单独裁定给 store 加 predicate。

## 红线

- **R1**：不 import ``anima.runtime`` / ``anima.tools``——策略层与 resolver 选路
  没有任何耦合，``LifecycleState`` 也不参与候选过滤。
- **R2**：只读 evidence，绝不回写或变异。
- **R3**：没有 timer / daemon / 轮询。评估由调用方（evidence 写入事件或测试）
  显式驱动，本模块不持有任何后台执行体。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from anima.capability.health.normalizer import NormalizedExecutionOutcome
from anima.capability.ops.decision import RecoveryAction, RecoveryDecision
from anima.capability.ops.policy import (
    CONDITION_FAILURE_ABSENT_AND_SUCCESS_MAJORITY,
    HealthPolicy,
    UnknownHandling,
)

#: evidence 里承载归一化结论的 metadata 键（F8.11 投影约定）。
OUTCOME_METADATA_KEY = "normalized_outcome"


@dataclass(frozen=True)
class WindowStats:
    """一个评估窗口内的三态计数（纯数据，不含策略）。"""

    success: int
    failure: int
    unknown: int
    #: 窗口起止（``observed_at`` 基准），与 ``RecoveryDecision.evidence_range`` 对齐。
    start: float
    end: float

    @property
    def observed(self) -> int:
        """窗口内实际读到的 evidence 条数（不受 ``unknown_handling`` 影响）。"""
        return self.success + self.failure + self.unknown

    @property
    def range(self) -> tuple[float, float]:
        return (self.start, self.end)


class HealthPolicyEvaluator:
    """读 evidence 窗口 + policy，产出 :class:`RecoveryDecision`。"""

    def __init__(
        self,
        store: Any,
        *,
        policies: "dict[str, HealthPolicy] | None" = None,
        observer: Any | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """
        :param store: 提供 ``query(capability_id=, start=, end=)`` 的 evidence 源
            （鸭子类型：生产传 ``HealthEvidenceStore``）。
        :param policies: policy registry。``None`` 时退回全局 ``POLICIES``——注意
            它出厂为空，未注册的 capability 一律 ``NO_ACTION``。
        :param observer: 可选执行通道（``HealthObserver``）。只有 :meth:`record`
            会用到；不传则本类**纯只读**。
        :param clock: 时间源（与 lifecycle / store 一致用 ``time.time``）。
        """
        self._store = store
        self._policies = policies
        self._observer = observer
        self._clock = clock or time.time

    # -- registry -----------------------------------------------------------
    def policy_for(self, capability_id: str) -> HealthPolicy | None:
        """取该 capability 的策略；未注册返回 ``None``（不造默认策略）。"""
        if self._policies is not None:
            return self._policies.get(capability_id)
        from anima.capability.ops.policy import get_policy

        return get_policy(capability_id)

    # -- L1/L2 → 窗口统计 ---------------------------------------------------
    def collect(self, capability_id: str, policy: HealthPolicy) -> WindowStats:
        """拉取窗口内 evidence 并按 ``normalized_outcome`` 归类（P2-A）。"""
        end = float(self._clock())
        start = end - float(policy.failure_window)
        records = self._store.query(
            capability_id=capability_id, start=start, end=end
        )
        success = failure = unknown = 0
        for evidence in records:
            match self._outcome_of(evidence):
                case NormalizedExecutionOutcome.SUCCESS:
                    success += 1
                case NormalizedExecutionOutcome.FAILURE:
                    failure += 1
                case _:
                    unknown += 1
        return WindowStats(
            success=success, failure=failure, unknown=unknown, start=start, end=end
        )

    @staticmethod
    def _outcome_of(evidence: Any) -> NormalizedExecutionOutcome:
        """读 evidence 的归一化结论。

        缺失或无法识别 → ``UNKNOWN``。刻意**不**回退到 ``evidence.success``：那是
        F8.11 已降级的 surface 字段（混了「handler 正常返回」和「执行成功」两种语
        义），拿它兜底等于把归一化白做了。
        """
        metadata = getattr(evidence, "metadata", None) or {}
        raw = metadata.get(OUTCOME_METADATA_KEY)
        try:
            return NormalizedExecutionOutcome(raw)
        except (ValueError, TypeError):
            return NormalizedExecutionOutcome.UNKNOWN

    # -- 统计口径（受 unknown_handling 影响）--------------------------------
    @staticmethod
    def sample_count(stats: WindowStats, policy: HealthPolicy) -> int:
        """参与判定的样本数：DILUTE 含 UNKNOWN，IGNORE 排除。"""
        if policy.unknown_handling is UnknownHandling.IGNORE:
            return stats.success + stats.failure
        return stats.success + stats.failure + stats.unknown

    @classmethod
    def failure_rate(cls, stats: WindowStats, policy: HealthPolicy) -> float:
        """``FAILURE / sample_count``。UNKNOWN 永远不进分子。"""
        total = cls.sample_count(stats, policy)
        return (stats.failure / total) if total else 0.0

    @classmethod
    def success_ratio(cls, stats: WindowStats, policy: HealthPolicy) -> float:
        total = cls.sample_count(stats, policy)
        return (stats.success / total) if total else 0.0

    @classmethod
    def effective_threshold(cls, stats: WindowStats, policy: HealthPolicy) -> float:
        """按 UNKNOWN 占比抬高的隔离门槛（IGNORE 模式下即基础阈值）。"""
        if policy.unknown_handling is UnknownHandling.IGNORE:
            return float(policy.failure_threshold)
        total = cls.sample_count(stats, policy)
        unknown_ratio = (stats.unknown / total) if total else 0.0
        return float(policy.failure_threshold) * (
            1.0 + float(policy.unknown_dilution_k) * unknown_ratio
        )

    # -- L3：决策 -----------------------------------------------------------
    def decide(
        self, capability_id: str, stats: WindowStats, policy: HealthPolicy
    ) -> RecoveryDecision:
        """纯函数：给定窗口统计与策略，产出决策。不读外部状态、不写任何东西。"""
        total = self.sample_count(stats, policy)
        if total < int(policy.minimum_samples):
            return self._decision(
                capability_id,
                RecoveryAction.NO_ACTION,
                f"insufficient samples: {total} < minimum_samples="
                f"{policy.minimum_samples} in window",
                stats,
            )

        rate = self.failure_rate(stats, policy)
        threshold = self.effective_threshold(stats, policy)
        if rate > threshold:
            return self._decision(
                capability_id,
                RecoveryAction.QUARANTINE,
                f"failure_rate={rate:.3f} > effective_threshold={threshold:.3f} "
                f"(n={total}, unknown={stats.unknown}, "
                f"handling={policy.unknown_handling.value})",
                stats,
            )

        condition = policy.recovery_condition
        if condition.kind != CONDITION_FAILURE_ABSENT_AND_SUCCESS_MAJORITY:
            return self._decision(
                capability_id,
                RecoveryAction.NO_ACTION,
                f"unsupported recovery condition kind={condition.kind!r}; "
                f"an unreadable condition is never treated as satisfied",
                stats,
            )

        ratio = self.success_ratio(stats, policy)
        if (
            rate <= float(condition.max_failure_rate)
            and ratio >= float(condition.min_success_ratio)
        ):
            return self._decision(
                capability_id,
                RecoveryAction.RECOVER,
                f"failure_rate={rate:.3f} <= {condition.max_failure_rate:.3f} and "
                f"success_ratio={ratio:.3f} >= {condition.min_success_ratio:.3f} "
                f"(n={total}, condition={condition.kind})",
                stats,
            )

        return self._decision(
            capability_id,
            RecoveryAction.NO_ACTION,
            f"recovery condition unmet: failure_rate={rate:.3f}, "
            f"success_ratio={ratio:.3f}, n={total} (condition={condition.kind})",
            stats,
        )

    def evaluate(self, capability_id: str) -> RecoveryDecision:
        """完整评估：取策略 → 拉窗口 → 决策。**不改变任何状态。**"""
        policy = self.policy_for(capability_id)
        if policy is None:
            now = float(self._clock())
            return RecoveryDecision(
                capability_id=capability_id,
                action=RecoveryAction.NO_ACTION,
                reason=(
                    "no health policy registered for this capability; "
                    "governance stays out of it"
                ),
                evidence_range=(now, now),
            )
        stats = self.collect(capability_id, policy)
        return self.decide(capability_id, stats, policy)

    # -- L3 → L4：交给执行通道（P4/P5）--------------------------------------
    def record(self, capability_id: str) -> tuple[RecoveryDecision, bool]:
        """事件驱动入口：评估一次，并把决策交给执行通道。

        由 evidence 写入方（或测试）在写入后**显式**调用——push 模型，没有轮询。

        :returns: ``(decision, applied)``。``applied`` 表示执行通道是否真的落了
            一次状态变更；未注入 observer 时恒为 ``False``（本类只出决策）。
        """
        decision = self.evaluate(capability_id)
        if self._observer is None:
            return decision, False
        if decision.action is RecoveryAction.RECOVER:
            return decision, bool(self._observer.recover(capability_id, decision))
        if decision.action is RecoveryAction.QUARANTINE:
            # 隔离方向复用 F8.8 既有通道，不新开第二个写入口。
            return decision, bool(
                self._observer.report_unhealthy(
                    capability_id, reason=decision.as_transition_reason()
                )
            )
        return decision, False

    # -- 内部 ---------------------------------------------------------------
    @staticmethod
    def _decision(
        capability_id: str,
        action: RecoveryAction,
        reason: str,
        stats: WindowStats,
    ) -> RecoveryDecision:
        return RecoveryDecision(
            capability_id=capability_id,
            action=action,
            reason=reason,
            evidence_range=stats.range,
        )
