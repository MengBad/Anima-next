"""F8.12 —— ``RecoveryDecision``：策略层的一等解释对象。

Evaluator **不直接改状态**，而是产出一条可被读懂的决策，交给执行通道去落。
这一层存在的全部理由是：让每一次状态变更都能被回答「凭什么」。

## 为什么不建第四个 store（P3）

系统里已经有三个 append-only store，各记一种事实：

| Store | 记录 |
|-|-|
| ``AuditStore``（F8.7） | capability resolution |
| ``HealthEvidenceStore``（F8.10） | execution fact |
| ``TransitionStore``（F8.8） | lifecycle decision |

``RecoveryDecision`` 属于第三类——它**就是**那条 lifecycle decision 的理由。所以
它不落自己的库，而是经 :meth:`RecoveryDecision.as_transition_reason` 序列化成
``transition_to(reason=...)`` 的结构化字符串，随 transition 一起进
``TransitionStore``。回溯时读一条 transition 就能同时看到「变成了什么」和「基于
哪段窗口、什么统计做的决定」，不需要跨两个库对时间戳。

真要单开 store，得先有「按 decision 查询 / 聚合」的需求——目前没有。

## 生命周期

进程内内存对象，evaluator 产出、执行通道消费，不持久化、不缓存、不复用。
"""
from __future__ import annotations

import enum
from dataclasses import dataclass


class RecoveryAction(str, enum.Enum):
    """策略层对一个 capability 当前处境的裁定（RFC §5.2）。"""

    #: 隔离候选成立 → 执行通道映射 ``transition_to(QUARANTINED, reason)``。
    QUARANTINE = "QUARANTINE"
    #: 恢复条件满足 → 执行通道映射 ``transition_to(ACTIVE, reason)``。
    RECOVER = "RECOVER"
    #: 证据不足 / 阈值未达 → 无状态变更。
    NO_ACTION = "NO_ACTION"


@dataclass(frozen=True)
class RecoveryDecision:
    """一次策略评估的结论（不可变；执行与否由执行通道另行裁定）。"""

    #: 被评估的 capability。
    capability_id: str
    #: 裁定动作。
    action: RecoveryAction
    #: 人类可读的判据（含关键统计）。**必填非空**——与 ``transition_to`` 的
    #: reason 纪律一致：读不出「为什么」的记录，事后价值是零。
    reason: str
    #: 评估所依据的 ``observed_at`` 窗口 ``(start, end)``，使决策可回溯到
    #: 具体那段 evidence。
    evidence_range: tuple[float, float]

    def __post_init__(self) -> None:
        if not str(self.capability_id).strip():
            raise ValueError("RecoveryDecision.capability_id 不得为空")
        if not str(self.reason).strip():
            raise ValueError(
                "RecoveryDecision.reason 不得为空——无理由的决策不是审计记录"
            )
        if not isinstance(self.action, RecoveryAction):
            raise ValueError(
                f"RecoveryDecision.action 必须是 RecoveryAction，"
                f"收到 {type(self.action).__name__}"
            )
        start, end = self.evidence_range
        if float(end) < float(start):
            raise ValueError(
                f"RecoveryDecision.evidence_range 起止颠倒：{self.evidence_range!r}"
            )

    def as_transition_reason(self) -> str:
        """序列化为 lifecycle transition 的结构化 ``reason``（P3 审计出口）。

        形如::

            policy:RECOVER — failure_rate=0.000 over healthy window
              — evidence_range=[999940.000,1000000.000]
        """
        start, end = self.evidence_range
        return (
            f"policy:{self.action.value} — {self.reason} "
            f"— evidence_range=[{float(start):.3f},{float(end):.3f}]"
        )
