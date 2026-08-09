"""F8.8 —— HealthObserver：最小 health signal adapter。

把「某个健康事实」沉淀为 ``CapabilityLifecycle`` 的 QUARANTINED transition，并
同步写入持久化 store。**只做 QUARANTINED 方向**（用户裁定：不做自动 recovery、
不做 override、不做 timer/daemon——恢复策略属于未来的独立 slice）。

## 依赖注入

- ``lifecycle_accessor: Callable[[str], CapabilityLifecycle | None]`` —— 生产接线
  传 ``registry.lifecycle_for``（公开只读 seam）；本类不 import runtime。
- ``store: TransitionStore`` —— transition 持久化出口。

## 幂等（R2）

``report_unhealthy`` 对同一 capability 重复报告：已处于 QUARANTINED → 返回
``False``，不重复 transition、不重复写 store、不抛。

## fail-open（红线延续）

store 写失败 → ``store_failures += 1``，但治理事实**仍生效**（lifecycle 已转
QUARANTINED，返回 ``True``）——持久化失败 ≠ 治理失败。
"""
from __future__ import annotations

from typing import Callable

from anima.capability.governance.lifecycle import (
    LifecycleState,
    LifecycleTransition,
    LifecycleTransitionError,
)
from anima.capability.ops.transition_store import TransitionStore


class HealthObserver:
    """把健康观察报告沉淀为 lifecycle 记录（QUARANTINED），并持久化。"""

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
        try:
            self._store.append(capability_id, transition)
        except Exception:  # noqa: BLE001 - 持久化失败 ≠ 治理失败（fail-open）
            self.store_failures += 1
        return True
