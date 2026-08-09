"""F8.8 / F8.12 —— Governance Ops（Health Observation + Recovery Policy）。

本包装的是治理里「**该不该动状态**」和「**动状态那一下**」这两件事——而
``governance/lifecycle.py`` 管的是「**哪些状态变化被允许**」。三者刻意分开：状态
机只认转换表，策略只认统计，执行通道只认「决策已授权 + 这一步合法」。

## 本包做什么

1. :mod:`~anima.capability.ops.policy` —— :class:`HealthPolicy` 与 ``POLICIES``
   注册表（F8.12，P1）。**只有 schema，出厂无取值**：F8 阶段没有生产数据能支撑
   阈值该定多少，猜错会误隔离真实可用的 capability。
2. :mod:`~anima.capability.ops.decision` —— :class:`RecoveryDecision`：策略层的
   一等解释对象。不落自己的库，靠结构化 ``reason`` 随 transition 进
   ``TransitionStore``（P3）。
3. :class:`~anima.capability.ops.health_policy_evaluator.HealthPolicyEvaluator`
   —— 决策者（F8.12）：读 evidence 时间窗 + policy，产出 ``RecoveryDecision``。
   **只出决策，不改状态。**
4. :class:`~anima.capability.ops.observer.HealthObserver` —— 执行通道：
   ``report_unhealthy`` 落 QUARANTINED（F8.8），``recover`` 落 ACTIVE（F8.12，
   只从隔离态出发）。
5. :class:`~anima.capability.ops.transition_store.TransitionStore` ——
   ``LifecycleTransition`` 的 JSONL append-only 持久化（复用 F8.7 的 envelope /
   retention 模式，独立实现，零触碰 ``capability/audit``）。

## 四层单向链路（F8.12 验收面）

.. code-block:: text

    HealthEvidence  →  NormalizedExecutionOutcome  →  RecoveryDecision  →  transition_to()
       事实                  解释                        策略                 状态变更

逐层只消费自己的上游，不许跳级；``transition_to()`` 是**唯一**状态写入口，本包内
只有 ``observer.py`` 会调它（有源码扫描测试守门）。

## 红线（R1/R2/R3，测试钉死）

- **R1**：不允许 selection → health feedback loop。本包**只写 lifecycle**，绝不
  读写 resolver selection；parity 测试证明 QUARANTINED 后 ``resolve()`` 输出逐字节
  不变（resolver 本就不读 lifecycle——F5.4b 冻结语义保持）。
- **R2**：evidence 只读不改（策略是对事实的解释，不是对事实的编辑）；transition
  幂等——重复 ``report_unhealthy`` / ``recover`` 不产生第二条 transition。
- **R3**：无 timer / daemon / polling。评估与执行都由外部事件显式驱动（evidence
  写入后 push），本包不持有任何后台执行体。

## 依赖方向纪律

本包**不 import** ``anima.runtime`` / ``anima.tools``：lifecycle 引用经
``lifecycle_accessor`` 注入（生产接线传 ``registry.lifecycle_for``），store 独立。
governance 包 / 冻结文件零改动。
"""
from __future__ import annotations

from .decision import RecoveryAction, RecoveryDecision
from .health_policy_evaluator import (
    OUTCOME_METADATA_KEY,
    HealthPolicyEvaluator,
    WindowStats,
)
from .observer import HealthObserver
from .policy import (
    CONDITION_FAILURE_ABSENT_AND_SUCCESS_MAJORITY,
    POLICIES,
    HealthPolicy,
    RecoveryCondition,
    UnknownHandling,
    get_policy,
    register_policy,
)
from .transition_store import CURRENT_SCHEMA_VERSION, TransitionStore

__all__ = [
    "CONDITION_FAILURE_ABSENT_AND_SUCCESS_MAJORITY",
    "CURRENT_SCHEMA_VERSION",
    "OUTCOME_METADATA_KEY",
    "POLICIES",
    "HealthObserver",
    "HealthPolicy",
    "HealthPolicyEvaluator",
    "RecoveryAction",
    "RecoveryCondition",
    "RecoveryDecision",
    "TransitionStore",
    "UnknownHandling",
    "WindowStats",
    "get_policy",
    "register_policy",
]
