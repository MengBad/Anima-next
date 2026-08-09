"""F8.10 —— evidence 投影 adapter（runtime → governance 单向）。

把 runtime 侧的执行事实投影为 governance 内部的 :class:`HealthEvidence`，
经 :class:`HealthEvidenceSink` 推送（**push 模式**，Q2 裁定）：

    runtime emits evidence
            ↓
    EvidenceAdapter.project / record
            ↓
    HealthEvidenceSink (HealthEvidenceStore)
            ↓
    (future) HealthObserver → Lifecycle

边界纪律（与 F8.10 红线一致）：
- 本 adapter 位于 governance 侧，**不读 runtime 内部**（不 inspect providers /
  executor / resolver）；observation 以**鸭子类型**传入（结构上有 ``success`` /
  ``error`` / ``metadata`` 即可），**不 import anima.runtime**；
- **Q4**：``success`` 只从 observation 读取，**绝不**从 resolution 的 SELECTED 推导；
- **fail-open**（F8.8 红线延续）：sink.append 抛错 → ``failures += 1``，证据仍产生
  并返回（持久化失败 ≠ 事实不存在）；
- **零 policy**：adapter 只投影事实，不做健康评分 / 隔离 / 恢复决策（Q3）。
"""
from __future__ import annotations

import time
import uuid
from typing import Any, Protocol

from anima.capability.health.model import HealthEvidence


class HealthEvidenceSink(Protocol):
    """evidence 出口。任何 ``append(evidence) -> None`` 的可调用对象都算。"""

    def append(self, evidence: HealthEvidence) -> None:
        ...


def _classify_error(error: str) -> str:
    """best-effort 从错误文本提取类型名（"" = 无错误）。

    示例：``"搜索时出错了：TimeoutError"`` → ``"TimeoutError"``。
    取最后一个分隔符（``：`` 或 ``: ``）之后的部分；无分隔符则截断原文。
    """
    if not error:
        return ""
    text = error.strip()
    for sep in ("：", ": "):
        if sep in text:
            text = text.rsplit(sep, 1)[1].strip()
            break
    return text[:80]


class EvidenceAdapter:
    """把执行事实投影为 HealthEvidence 并推送给 sink（fail-open）。"""

    def __init__(self, sink: HealthEvidenceSink) -> None:
        self._sink = sink
        #: sink 写失败次数（fail-open 可观测；绝不阻断事实产生）。
        self.failures = 0

    def project(
        self,
        observation: Any,
        *,
        capability_id: str,
        provider_id: str | None,
        execution_key: tuple,
        selected: bool,
        duration_ms: float | None = None,
    ) -> HealthEvidence:
        """投影一次执行事实（不写 sink）。

        :param observation: 执行结果（鸭子类型：``success`` / ``error`` /
            ``metadata`` / ``task_id``）；``None`` = 执行未尝试（selected=False 形态）。
        """
        executed = observation is not None
        success = bool(getattr(observation, "success", False)) if executed else False
        error = str(getattr(observation, "error", "") or "") if executed else ""
        meta: dict[str, Any] = {}
        if executed:
            obs_meta = getattr(observation, "metadata", None) or {}
            if isinstance(obs_meta, dict):
                meta = dict(obs_meta)
            task_id = getattr(observation, "task_id", "")
            if task_id:
                meta.setdefault("task_id", str(task_id))
        if duration_ms is None:
            duration_ms = float(meta.get("duration_ms", 0.0) or 0.0)
        return HealthEvidence(
            evidence_id=uuid.uuid4().hex[:12],
            capability_id=capability_id,
            provider_id=provider_id,
            execution_id=str(execution_key),
            observed_at=time.time(),
            selected=bool(selected),
            executed=executed,
            success=success,
            error_type=_classify_error(error),
            duration_ms=float(duration_ms),
            metadata=meta,
        )

    def record(
        self,
        observation: Any,
        *,
        capability_id: str,
        provider_id: str | None,
        execution_key: tuple,
        selected: bool,
        duration_ms: float | None = None,
    ) -> HealthEvidence:
        """project + push（fail-open）。返回证据；sink 失败不抛，仅计数。"""
        evidence = self.project(
            observation,
            capability_id=capability_id,
            provider_id=provider_id,
            execution_key=execution_key,
            selected=selected,
            duration_ms=duration_ms,
        )
        try:
            self._sink.append(evidence)
        except Exception:  # noqa: BLE001 - 持久化失败 ≠ 事实不存在（fail-open）
            self.failures += 1
        return evidence
