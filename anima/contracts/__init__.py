"""anima.contracts —— M0.1 内核协议层（最小冻结面 / Minimum Frozen Surface）。

本包**只定义契约**（Protocol / Enum / 异常），零运行时依赖、零业务行为。
是 Dependency Rule 的最底层（Presentation → Workflow → Capability → Provider 之下），
任何上层模块都*依赖*本包，本包不*依赖*任何上层。

设计纪律（来自用户拍板的 M0.1）：
- 第一版只建立契约，**不改变运行行��**。
- 不修改任何 Stage；不修改 Pipeline.run；不移除 meta；不引入 Resolver；不移动业务逻辑。
- 字段集合来自 Protocol_Inventory.md，剔除死字段与「待观察」字段，避免 Protocol 变成新万能袋。
- PipelineContext（数据 ABI, Immutable）与 ExecutionContext（生命周期 ABI, Mutable）正交（见 ADR-0002）。
"""
from __future__ import annotations

from .capability import CapabilityProtocol, Stability
from .errors import CapabilityNotFoundError, ContextAbiError, ContractError
from .execution import ExecutionContextProtocol
from .lifecycle import CancellationToken, Checkpoint, Deadline, ResourceBudgetProtocol
from .pipeline import PipelineContextProtocol, PipelineMetaProtocol

__all__ = [
    "PipelineContextProtocol",
    "PipelineMetaProtocol",
    "ExecutionContextProtocol",
    "CancellationToken",
    "Deadline",
    "Checkpoint",
    "ResourceBudgetProtocol",
    "CapabilityProtocol",
    "Stability",
    "ContractError",
    "CapabilityNotFoundError",
    "ContextAbiError",
]
