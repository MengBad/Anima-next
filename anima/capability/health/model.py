"""F8.10 —— health evidence 事实模型（governance 内部）。

一条 ``HealthEvidence`` 记录**一次治理调用的完整执行事实**，是 future
``HealthObserver``（→ Lifecycle）与 Recovery policy 的**输入层**。它刻意只装
「发生了什么」，不装「该怎么判断」：

- **Q3 裁定**：不包含任何 policy 字段（``health_score`` /
  ``quarantine_decision`` / ``recovery_decision`` 属于 policy 层，不入模型）；
- **Q4 语义**：``selected``（resolution 选了 provider）与 ``success``（执行成败，
  来自 execution observation）**分离**——``SELECTED ≠ SUCCESS``。

本模块依赖为零（仅 stdlib），不 import runtime / tools / audit / ops——
证据投影发生在 governance 侧 adapter，不触碰 runtime 事实载体。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class HealthEvidence:
    """一次治理调用的一次执行事实（append-only，不可变）。"""

    #: 唯一证据 id（uuid hex）。
    evidence_id: str
    #: 被调用的 capability id（如 "search.web"）。
    capability_id: str
    #: 被选中的 provider 身份（resolution 层）；未选中时为 ``None``。
    provider_id: str | None
    #: execution key 的字符串形态（``str((run_id, task_id, attempt))``）。
    execution_id: str
    #: 观测时间戳（投影时刻）。
    observed_at: float

    # -- Q4：resolution 与 execution 分离 ---------------------------------
    #: resolution 是否选中了一个 provider（来自 resolution metadata）。
    selected: bool
    #: 执行是否被尝试（产生了 observation）。
    executed: bool
    #: 执行是否成功（**来自 observation.success**，绝不从 SELECTED 推导）。
    success: bool

    #: 失败分类（"" 表示成功；best-effort 从 error 文本提取类型名）。
    error_type: str
    #: 执行耗时（毫秒；0.0 = 未知/未执行）。
    duration_ms: float
    #: 附加事实（如 provider 上报的 timings / task_id 等），保持 JSON 可序列化。
    metadata: dict[str, Any] = field(default_factory=dict)
