"""M0.3 —— Capability Contract（RFC-0001 §5.1 的运行时最小投影）。

只落地 Resolver 当前真正消费的字段：id / version / domain / description /
stability / permission / timeout / retry。

明确不做（M0.3 纪律）：
- 不实现 YAML 加载（RFC-0001 §5.1 的 ``capabilities/*.yaml`` 属后续阶段）；
- 不实现 input / output schema 校验（``ContractViolation`` 属 RFC-0001 §8 后续阶段）；
- ``stability`` 直接复用 M0.1 ABI ``anima.contracts.capability.Stability``，不另造枚举。

Contract 来源有两条，优先级：显式 ``declare_contract()`` > 由 Tool 声明派生
（:meth:`CapabilityContract.derive`）。派生契约不要求任何 scope，保证既有 Tool
标注 capability 后即可直接工作（additive，不破坏 legacy path）。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

from anima.contracts.capability import Stability

from .scopes import NO_SCOPES, PermissionScopes


@dataclass(frozen=True)
class CapabilityContract:
    """一个能力的稳定契约（immutable）。"""

    id: str
    version: str = "1.0.0"
    domain: str = ""
    description: str = ""
    stability: Stability = Stability.EXPERIMENTAL
    permission: PermissionScopes = field(default_factory=lambda: NO_SCOPES)
    timeout: float | None = None
    retry: int = 0

    @property
    def major(self) -> int:
        """semver major；Resolver 只匹配同 major（RFC-0001 §9）。"""
        head = str(self.version).split(".", 1)[0]
        try:
            return int(head)
        except ValueError:
            return 0

    def with_description(self, description: str) -> "CapabilityContract":
        if self.description or not description:
            return self
        return replace(self, description=description)

    @classmethod
    def derive(cls, capability_id: str, description: str = "") -> "CapabilityContract":
        """由 Tool 声明派生的最小契约（未显式声明 Contract 时的兜底）。

        - domain = capability id 的首段（``search.web`` → ``search``）；
        - permission = 空集（不要求任何 scope）；
        - stability = experimental（未经声明的能力不进入稳定候选）。
        """
        cap_id = str(capability_id)
        domain = cap_id.split(".", 1)[0] if "." in cap_id else cap_id
        return cls(id=cap_id, domain=domain, description=description or "")


def normalize_contract(contract: CapabilityContract) -> CapabilityContract:
    """补齐 domain 等可推导字段。"""
    if contract.domain:
        return contract
    cap_id = str(contract.id)
    domain = cap_id.split(".", 1)[0] if "." in cap_id else cap_id
    return replace(contract, domain=domain)
