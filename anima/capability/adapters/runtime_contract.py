"""F8.1 —— B 面（runtime registration）↔ canonical 的纯函数投影。

B 面 = ``anima/runtime/capability_resolver.py:129`` 的 ``CapabilityContract``：
``capability_id / permission: str / side_effect / idempotent / timeout: float /
retry``。它是 F5.5 实跑路径上被 ``CapabilityRegistry.register`` 与
``CapabilityResolver.resolve`` 直接消费的那个类。

投影规则：

- **B → canonical 无损**，往返 ``to_runtime_contract(from_runtime_contract(x)) == x``；
- **canonical → B**：``timeout=None`` 用 ``DEFAULT_RUNTIME_TIMEOUT`` 补齐——这是
  唯一一处有记录的有损方向（A 面允许 None，B 面要求 float）；
- ``side_effect`` / ``idempotent`` 原样带过，保证投影出来的 Contract 仍会正常
  触发 F7.1 注册一致性门（``capability_resolver.py:169-183``），不会因为绕道
  canonical 就绕过安全门；
- B 面无法声明的字段标记 ``source=RUNTIME_REGISTRATION``。

``anima.runtime.capability_resolver`` 采用**惰性 import**：它会连带拉入
``anima.constitution.execution`` 与 ``anima.runtime.providers``，不应该让
``import anima.capability`` 平白背上这份依赖（沿用 ``scopes.py:95``
``map_scopes_to_sandbox`` 的既有做法）。``from_runtime_contract`` 只读属性，
因此按鸭子类型接收，完全不需要 import。
"""
from __future__ import annotations

from typing import Any

from ..canonical import (
    DEFAULT_RUNTIME_TIMEOUT,
    CanonicalCapabilityContract,
    ContractSource,
)


def from_runtime_contract(contract: Any) -> CanonicalCapabilityContract:
    """B 面 Contract → canonical（只读属性，不修改入参）。"""
    timeout = contract.timeout
    return CanonicalCapabilityContract(
        id=str(contract.capability_id),
        permission_token=str(contract.permission),
        side_effect=bool(contract.side_effect),
        idempotent=bool(contract.idempotent),
        timeout=None if timeout is None else float(timeout),
        retry=int(contract.retry),
        source=ContractSource.RUNTIME_REGISTRATION,
    )


def to_runtime_contract(canonical: CanonicalCapabilityContract) -> Any:
    """canonical → B 面 Contract（``timeout=None`` 时补运行时默认值）。"""
    from anima.runtime.capability_resolver import CapabilityContract  # lazy

    return CapabilityContract(
        capability_id=canonical.id,
        permission=canonical.permission_token,
        side_effect=canonical.side_effect,
        idempotent=canonical.idempotent,
        timeout=DEFAULT_RUNTIME_TIMEOUT if canonical.timeout is None else float(canonical.timeout),
        retry=canonical.retry,
    )
