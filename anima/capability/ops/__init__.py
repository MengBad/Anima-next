"""F8.8 —— Governance Ops（Provider Health Observation → Lifecycle Recording）。

用户 F8.8 Implementation Authorization 的范围裁定：**Health Observation →
Lifecycle Recording**，不实现健康控制系统（不做 timer/daemon、不做自动 recovery、
不做 override）。

## 本包做什么

1. :class:`~anima.capability.ops.observer.HealthObserver` —— 最小 health signal
   adapter：外部（或测试）把「某个健康事实」报告进来，它把事实沉淀为
   ``CapabilityLifecycle`` 的 QUARANTINED transition，并同步写入持久化 store。
   只做 **QUARANTINED 方向**；恢复（ACTIVE）属于未来的 recovery slice，不在这里。
2. :class:`~anima.capability.ops.transition_store.TransitionStore` ——
   ``LifecycleTransition`` 的 JSONL append-only 持久化（复用 F8.7 的 envelope /
   retention 模式，独立实现，零触碰 ``capability/audit``）。

## 红线（R1/R2，测试钉死）

- **R1**：不允许 selection → health feedback loop。观察者**只写 lifecycle**，
  绝不读写 resolver selection；parity 测试证明 QUARANTINED 后 ``resolve()`` 输出
  逐字节不变（resolver 本就不读 lifecycle——F5.4b 冻结语义保持）。
- **R2**：transition 幂等——重复 ``report_unhealthy`` 只产生 1 条 QUARANTINED
  transition（第二次起返回 ``False``，不抛、不重复写）。

## 依赖方向纪律

本包**不 import** ``anima.runtime`` / ``anima.tools``：lifecycle 引用经
``lifecycle_accessor`` 注入（生产接线传 ``registry.lifecycle_for``），store 独立。
governance 包 / 冻结文件零改动。
"""
from __future__ import annotations

from .observer import HealthObserver
from .transition_store import CURRENT_SCHEMA_VERSION, TransitionStore

__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "HealthObserver",
    "TransitionStore",
]
