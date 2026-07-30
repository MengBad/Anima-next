"""M0.1 —— Capability 最小冻结面（能力 ABI）。

只声明契约，**不引入 Resolver、不移动业务逻辑**。Stability 枚举来自 RFC-0001 §③
（用户坚持的 stability 字段：experimental / beta / stable / deprecated）。
完整的 Resolver / Provider 接入在 M0.3 才发生。

invoke 签名引用 ExecutionContextProtocol 仅作类型提示（Protocol 不引入运行时依赖）；
第一版不要求任何实现落地。
"""
from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .execution import ExecutionContextProtocol


class Stability(str, Enum):
    EXPERIMENTAL = "experimental"
    BETA = "beta"
    STABLE = "stable"
    DEPRECATED = "deprecated"


class CapabilityProtocol(Protocol):
    """一个能力对外暴露的最小契约表面。

    字段分两类：
    - identity metadata：name / stability（能力自身的标识与成熟度，Immutable）；
    - action：invoke（能力动作入口，Mutable 行为）。
    """

    # 能力标识（identity metadata，不可变）
    @property
    def name(self) -> str: ...

    # 能力自身成熟度（lifecycle stage of the capability）：experimental/beta/stable/deprecated
    @property
    def stability(self) -> Stability: ...

    # 能力动作入口（action）；execution 为可选生命周期上下文
    def invoke(
        self, args: dict, execution: "ExecutionContextProtocol | None" = None
    ) -> object: ...
