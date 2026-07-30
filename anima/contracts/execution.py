"""M0.1 —— ExecutionContext 最小冻结面（生命周期 ABI，Mutable）。

与 PipelineContextProtocol 正交（见 ADR-0002）：数据不可变、生命周期可变。
本版极度克制：只声明 interface，不为缺失的运行时对象（CancellationToken / Deadline /
Checkpoint）创造复杂实现——它们的契约见 lifecycle.py。

ResourceBudget 已在 anima.agent.guardrails 存在，本层只引用其解耦契约
ResourceBudgetProtocol（见 lifecycle.py），保持 contracts 层零运行时依赖。
"""
from __future__ import annotations

from typing import Protocol

from .lifecycle import (
    CancellationToken,
    Checkpoint,
    Deadline,
    ResourceBudgetProtocol,
)


class ExecutionContextProtocol(Protocol):
    """单次执行（消息流水线回合 / Agent 任务）的生命周期 ABI。

    与 PipelineContextProtocol 正交（见 ADR-0002）：
    - PipelineContextProtocol 携带 Immutable data（数据不可变）；
    - 本 Protocol 携带 Mutable lifecycle state（生命周期可变）。
    所有字段均为「执行行为」状态，而非「任务数据」。
    """

    # ── Mutable lifecycle state ──
    # 统一追踪号：旧世界有两套（handler secrets.token_hex(4) / llm uuid hex），
    # 此处冻结为单一 trace_id，stage 内部统一可读，消除「状态散落」。
    trace_id: str

    # 资源预算；旧世界 ResourceBudget 在 anima.agent.guardrails，此处允许 None（渐进接入）。
    budget: ResourceBudgetProtocol | None

    # 取消令牌；旧来源 executor._cancel（executor.py）。
    cancellation: CancellationToken

    # 截止时间；旧来源 STEP_TIMEOUT（executor.py）。可为 None（消息流水线层当前无 deadline）。
    deadline: Deadline | None

    # 当前尝试序号（1-indexed）。语义 = 第 N 次 attempt，与「已重试次数」相差 1。
    # 旧来源：executor._run_step 的局部变量 attempt（executor.py:177）。
    # 注意：不是 task.steps[task.current_step:] —— 那是断点续跑（= checkpoint），非重试。
    attempt: int

    # 续跑检查点；旧世界消息流水线层缺失（inventory: checkpoint missing），允许 None。
    checkpoint: Checkpoint | None
