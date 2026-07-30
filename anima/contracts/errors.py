"""M0.1 —— 契约层错误类型。

契约违规的统一异常基线。Commit 2 的兼容适配器（OldPipelineContext / OldExecutorState）
将使用 ContextAbiError 表达「旧实现未满足 Protocol 表面」。
"""
from __future__ import annotations


class ContractError(Exception):
    """契约层异常基类。"""


class CapabilityNotFoundError(ContractError):
    """请求的 Capability 不存在（M0.3 Resolver 接入后使用）。"""


class ContextAbiError(ContractError):
    """上下文未满足 Protocol ABI（如旧 PipelineContext 缺字段）。"""
