"""F8.10 —— health evidence 事实层（governance 内部）。

单向投影链（Q1-C 裁定 + Implementation Authorization）：

    runtime evidence (ExecutionObservation / ProviderResult / resolution metadata)
            ↓
    EvidenceAdapter（governance 侧投影，Q4：selected ≠ success）
            ↓
    HealthEvidenceSink → HealthEvidenceStore（JSONL）
            ↓
    (future) HealthObserver → Lifecycle

本包刻意只建**事实层**：model + adapter + store + sink seam。**不实现 policy**
（health_score / quarantine / recovery 不入模型，Q3）、**不接线生产发射点**、
**不改 runtime**（ExecutionObservation / execution_ledger / resolver / provider
contract / audit 全部冻结）。

R1 红线延续：本包不触碰 runtime selection；observer（F8.8，在 ``anima.capability.ops``）
只消费 evidence、只写 lifecycle，绝无 selection → health feedback loop。
"""
from __future__ import annotations

from anima.capability.health.adapter import (
    EvidenceAdapter,
    HealthEvidenceSink,
)
from anima.capability.health.model import HealthEvidence
from anima.capability.health.store import HealthEvidenceStore

__all__ = [
    "HealthEvidence",
    "EvidenceAdapter",
    "HealthEvidenceSink",
    "HealthEvidenceStore",
]
