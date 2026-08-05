"""M0.1 —— 契约层错误类型。

契约违规的统一异常基线。Commit 2 的兼容适配器（OldPipelineContext / OldExecutorState）
将使用 ContextAbiError 表达「旧实现未满足 Protocol 表面」。
"""
from __future__ import annotations


class ContractError(Exception):
    """契约层异常基类。"""


class CapabilityNotFoundError(ContractError):
    """请求的 Capability 不存在（RFC-0001 §8 CapabilityNotFound）。"""


class NoProviderError(ContractError):
    """该 Capability 无任何可用 Provider（RFC-0001 §8 NoProvider）。"""


class PermissionDeniedError(ContractError):
    """所有 Provider 权限均不满足该 Capability（RFC-0001 §8 PermissionDenied）。"""


class ContextAbiError(ContractError):
    """上下文未满足 Protocol ABI（如旧 PipelineContext 缺字段）。"""


class RawAccessForbidden(ContractError):
    """经 raw 访问已有 facade 的能力（应改用 ctx.core.<facade>()）。"""


class RawAccessNotAllowed(ContractError):
    """经 raw 访问不在允许列表的能力（非既定迁移桥）。"""
