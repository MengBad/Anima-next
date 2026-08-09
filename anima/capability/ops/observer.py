"""F8.8 / F8.12 —— HealthObserver：lifecycle 变更的执行通道。

把一条治理结论沉淀为 ``CapabilityLifecycle`` 的 transition，并同步写入持久化
store。两个方向：

- **QUARANTINED**（F8.8，``report_unhealthy``）：外部 push 一个健康事实；
- **ACTIVE**（F8.12，``recover``）：策略层给出 ``RecoveryDecision`` 后执行恢复。

本类是**执行者，不是决策者**。恢复该不该发生由
:class:`~anima.capability.ops.health_policy_evaluator.HealthPolicyEvaluator`
判断，本类只负责「决策已授权」之后那一步——并在这一步上再加一层状态机守卫。
仍然没有 timer / daemon / 轮询：两个方向都由外部显式驱动。

## 恢复只从隔离态出发（F8.12）

``recover`` 只接受 ``QUARANTINED → ACTIVE``。``VALIDATED → ACTIVE`` 虽然也在
``LEGAL_TRANSITIONS`` 里，但那是**正常上线**路径，不是恢复；若让 recovery 借这
条道走，一个从未通过 metadata 校验的 capability 就能靠「最近没失败」直接上线，
F8.2b 那道门等于白设。

## 依赖注入

- ``lifecycle_accessor: Callable[[str], CapabilityLifecycle | None]`` —— 生产接线
  传 ``registry.lifecycle_for``（公开只读 seam）；本类不 import runtime。
- ``store: TransitionStore`` —— transition 持久化出口。

## 幂等（R2）

- ``report_unhealthy``：已处于 QUARANTINED → 返回 ``False``，不重复 transition；
- ``recover``：已处于 ACTIVE → 返回 ``False``，不重复 transition。

两者都不抛、不重复写 store。

## fail-open（红线延续）

store 写失败 → ``store_failures += 1``，但治理事实**仍生效**（lifecycle 已转
成目标状态，返回 ``True``）——持久化失败 ≠ 治理失败。
"""
from __future__ import annotations

from typing import Callable

from anima.capability.governance.lifecycle import (
    LifecycleState,
    LifecycleTransition,
    LifecycleTransitionError,
)
from anima.capability.ops.decision import RecoveryAction, RecoveryDecision
from anima.capability.ops.transition_store import TransitionStore


class HealthObserver:
    """把治理结论沉淀为 lifecycle 记录（QUARANTINED / ACTIVE），并持久化。"""

    def __init__(
        self,
        lifecycle_accessor: "Callable[[str], object | None]",
        store: TransitionStore,
    ) -> None:
        self._accessor = lifecycle_accessor
        self._store = store
        #: store 写失败次数（fail-open 可观测；绝不阻断治理事实生效）。
        self.store_failures = 0

    def report_unhealthy(self, capability_id: str, *, reason: str) -> bool:
        """报告一个健康事实：把 capability 沉淀为 QUARANTINED。

        :returns: ``True`` = 本报告应用了一次 transition（或仅 store 失败但治理
            事实生效）；``False`` = 未应用（未注册 / 已隔离 / 终态 / 不可达）。
        """
        lifecycle = self._accessor(capability_id)
        if lifecycle is None:
            return False  # 未注册的 capability 没有治理记录，无从隔离
        if lifecycle.state is LifecycleState.QUARANTINED:
            return False  # R2 幂等：已隔离，重复报告不产生第二次 transition
        if lifecycle.state is LifecycleState.REMOVED:
            return False  # 终态不唤醒（REMOVED 无合法出边，也不该被 health 复活）
        try:
            transition = lifecycle.transition_to(
                LifecycleState.QUARANTINED, reason=str(reason)
            )
        except LifecycleTransitionError:
            return False  # 状态机拒绝（如 DEPRECATED→QUARANTINED 以外的不可达边）
        self._persist(capability_id, transition)
        return True

    def recover(self, capability_id: str, decision: RecoveryDecision) -> bool:
        """执行一条已授权的恢复决策：``QUARANTINED → ACTIVE``（F8.12）。

        本方法**不重新判断**恢复该不该发生——那是策略层的事；它只校验这一步在
        状态机上成立，并把决策原样写进 transition 的 ``reason``（P3：决策不单开
        store，靠 reason 随 transition 落盘）。

        :param decision: 策略层产出的决策。``action`` 非 ``RECOVER`` 一律不执行
            ——执行通道不替决策层改主意。
        :raises ValueError: ``decision.capability_id`` 与 ``capability_id`` 不符
            （接错线属于结构性错误，静默返回 ``False`` 只会把它藏起来）。
        :returns: ``True`` = 本次应用了一次恢复 transition（或仅 store 失败但治理
            事实生效）；``False`` = 未应用（决策非 RECOVER / 未注册 / 非隔离态 /
            状态机拒绝）。
        """
        if decision.capability_id != capability_id:
            raise ValueError(
                f"recover: decision is for {decision.capability_id!r} "
                f"but was applied to {capability_id!r}"
            )
        if decision.action is not RecoveryAction.RECOVER:
            return False  # 执行通道只落 RECOVER；其余动作各有各的通道
        lifecycle = self._accessor(capability_id)
        if lifecycle is None:
            return False  # 未注册的 capability 没有治理记录，无从恢复
        if lifecycle.state is not LifecycleState.QUARANTINED:
            # 已 ACTIVE → 幂等返回；REGISTERED/VALIDATED → 那是上线不是恢复；
            # DEPRECATED/REMOVED → 产品决策与终态，不由健康信号翻案。
            return False
        try:
            transition = lifecycle.transition_to(
                LifecycleState.ACTIVE, reason=decision.as_transition_reason()
            )
        except LifecycleTransitionError:
            return False
        self._persist(capability_id, transition)
        return True

    # -- 持久化（fail-open）-------------------------------------------------
    def _persist(self, capability_id: str, transition: LifecycleTransition) -> None:
        try:
            self._store.append(capability_id, transition)
        except Exception:  # noqa: BLE001 - 持久化失败 ≠ 治理失败（fail-open）
            self.store_failures += 1
