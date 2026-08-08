"""F8.1 —— A 面（legacy declaration）↔ canonical 的纯函数投影。

A 面 = ``anima/capability/contract.py:24`` 的 ``CapabilityContract``：
``id / version / domain / description / stability / permission: PermissionScopes /
timeout: float | None / retry``。

投影规则：

- **A → canonical 无损**：A 的每个字段都有对应落点，往返 ``to_legacy_contract(
  from_legacy_contract(x)) == x``；
- **canonical → A 丢弃 A 无处安放的字段**（permission_token / side_effect /
  idempotent / deterministic / cacheable / streaming）——丢弃是显式的，不做任何
  等价改写，因为 A 面本来就不表达执行画像；
- A 面无法声明的字段在 canonical 上标记为
  ``source=LEGACY_DECLARATION``，由 ``unverified_metadata()`` 暴露，绝不冒充
  已声明策略（F7.1：Provider 才是 runtime truth）。
"""
from __future__ import annotations

from ..canonical import CanonicalCapabilityContract, ContractSource
from ..contract import CapabilityContract
from ..scopes import PermissionScopes


def from_legacy_contract(contract: CapabilityContract) -> CanonicalCapabilityContract:
    """A 面 Contract → canonical（不修改入参）。"""
    timeout = contract.timeout
    return CanonicalCapabilityContract(
        id=str(contract.id),
        version=str(contract.version),
        domain=str(contract.domain),
        description=str(contract.description),
        stability=contract.stability,
        permission_scopes=PermissionScopes.from_mapping(contract.permission),
        timeout=None if timeout is None else float(timeout),
        retry=int(contract.retry),
        source=ContractSource.LEGACY_DECLARATION,
    )


def to_legacy_contract(canonical: CanonicalCapabilityContract) -> CapabilityContract:
    """canonical → A 面 Contract（运行面独有字段被显式丢弃）。"""
    return CapabilityContract(
        id=canonical.id,
        version=canonical.version,
        domain=canonical.domain,
        description=canonical.description,
        stability=canonical.stability,
        permission=canonical.permission_scopes,
        timeout=canonical.timeout,
        retry=canonical.retry,
    )
