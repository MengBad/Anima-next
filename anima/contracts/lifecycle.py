"""M0.1 —— 执行生命周期原语契约（cancel / timeout / checkpoint）。

这些对象今天部分不存在（CancellationToken / Deadline / Checkpoint），部分已存在
（ResourceBudget 在 anima.agent.guardrails）。本文件**只定义 interface，不创建复杂实现**。

约定：
- ResourceBudget 已存在，这里定义 ResourceBudgetProtocol 作为解耦契约；
  真实 guardrails.ResourceBudget 结构性满足之，即其 Reference Implementation（RFC-0000 §9.6）。
- pause / resume 属于 ExecutionContext 级的编排能力，将在后续版本加到 ExecutionContextProtocol，
  不塞进本文件的原子原语。
"""
from __future__ import annotations

from typing import Protocol


class CancellationToken(Protocol):
    """取消令牌：可查询是否已取消，可主动取消。"""

    def cancelled(self) -> bool: ...

    def cancel(self) -> None: ...


class Deadline(Protocol):
    """截止期限：对应超时（timeout）。"""

    def expired(self) -> bool: ...

    def remaining_seconds(self) -> float | None: ...


class Checkpoint(Protocol):
    """检查点：对应断点续跑（resume）。save 落盘，restore 恢复。"""

    def save(self) -> None: ...

    def restore(self) -> bool: ...


class ResourceBudgetProtocol(Protocol):
    """解耦契约：对应已存在的 anima.agent.guardrails.ResourceBudget。

    方法签名依据 executor.py 真实调用（check_duration / consume_network_request /
    consume_output）对齐，确保真实类结构性满足本协议。
    """

    def check_duration(self) -> None: ...

    def consume_network_request(self) -> None: ...

    def consume_output(self, kb: float) -> None: ...
