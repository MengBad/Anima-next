"""F8.1 —— Capability Contract adapter layer。

代码库里两个同名不同义的 ``CapabilityContract`` 都**保持存在、各自继续服务既有
调用方**；本包只提供它们与 :class:`~anima.capability.canonical.
CanonicalCapabilityContract` 之间的纯函数投影，让治理层可以把两个面当作同一个
受治理对象来推理，而不必先做一次大范围重构（§58 冻结裁定）。

四个函数全部是无副作用的纯投影：不注册、不解析、不修改入参。
"""
from __future__ import annotations

from .legacy_contract import from_legacy_contract, to_legacy_contract
from .runtime_contract import from_runtime_contract, to_runtime_contract

__all__ = [
    "from_legacy_contract",
    "to_legacy_contract",
    "from_runtime_contract",
    "to_runtime_contract",
]
