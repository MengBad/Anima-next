"""Facade Protocols — M0.1 Commit 3a。

这些 Protocol 是 Stage → 真实子系统 之间的「边界投影」。
只覆盖真实调用（最小冻结面），不预留未来能力。最终分别由
Memory / Agenda / Telemetry / Config / Permission / Outbound 子系统
+ RFC-0001 Capability Layer 实现。

本模块不含任何接线逻辑（接线在 Commit 3b 的 DeprecatedCoreAccess）。

ctx.core.raw 治理（Commit 3b 的 DeprecatedCoreAccess.raw 必须遵循，写在此处作为契约真相源）：
1. raw 语义 = Migration escape hatch only，NOT a core facade。
2. raw 必须记录访问来源 caller，否则半年后 raw 变成新的 meta["core"]。
3. raw 必须有允许列表：仅允许尚未有 facade 的后门；已建 facade 的能力禁止走 raw。
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


# ---- ctx.core.raw 允许列表（Commit 3b 强制） ----
# 允许：尚未建立 facade 的整体透传点（llm/danger/exploration/tool_learning）。
RAW_ACCESS_ALLOWLIST = frozenset({"llm", "danger", "exploration", "tool_learning"})
# 禁止：已有对应 facade，再走 raw 等于迁移无意义。
RAW_ACCESS_FORBIDDEN = frozenset({"memory", "tools", "gateway", "permission", "config"})


@runtime_checkable
class MemoryProtocol(Protocol):
    """vector_memory 边界（最小）。未来拆 Episodic/Semantic/Identity/Reflection，第一版不承载。"""

    def search(self, query: str, **kwargs) -> Any: ...

    def add(self, text: str, **kwargs) -> Any: ...


@runtime_checkable
class AgendaProtocol(Protocol):
    """主动性系统边界（不进 Memory）：Goal → Intention → Agenda → Execution。"""

    def propose_agenda(
        self, group_id: int | None, text: str, *, priority: float = 0.5, **kwargs
    ) -> list: ...


@runtime_checkable
class TelemetryProtocol(Protocol):
    """事件记录（非日志工具）。guard fixation / 兜底出站 / emit 事件。"""

    def record_guard_fixation(self) -> None: ...

    def record_fallback(self, kind: str) -> None: ...

    def emit(self, event: str, **kwargs) -> None: ...


@runtime_checkable
class ConfigProtocol(Protocol):
    """配置治理（非能力）。Commit 3 先保持真实入口，M1 整理为通用 get。

    注意：当前 permission 读取实际经 core.config.permissions.*，本 Protocol 的
    permissions 属性即覆盖该路径；PermissionProtocol 是未来的治理拆分目标。
    """

    @property
    def persistence(self) -> Any: ...

    @property
    def sticker(self) -> Any: ...

    @property
    def security(self) -> Any: ...

    @property
    def permissions(self) -> Any: ...

    @property
    def qzone(self) -> Any: ...

    @property
    def voice(self) -> Any: ...

    @property
    def meme(self) -> Any: ...


@runtime_checkable
class PermissionProtocol(Protocol):
    """治理层（不进 Capability）。未来 Capability Resolver 依赖之。

    现实提示：当前 permission 读取经 core.config.permissions.*，Commit 3 由
    ConfigProtocol 满足；本 Protocol 是用户定的未来治理拆分目标（group_policy_for /
    role_for），不强制 1:1 对应现有访问路径。
    """

    def group_policy_for(self, group_id: int | None, **kwargs) -> dict: ...

    def role_for(self, user_id: int, group_id: int | None, **kwargs) -> str: ...


@runtime_checkable
class OutboundMsg(Protocol):
    """出站消息信封（Provider 无关）。字段对齐 core.gateway.OutboundMsg。"""

    detail_type: str
    target_id: Any
    message: Any


@runtime_checkable
class OutboundProtocol(Protocol):
    """出站边界（Provider 不可泄漏）：QQ/NapCat 只是未来 Provider 之一。"""

    def send(self, message: OutboundMsg) -> Any: ...


@runtime_checkable
class LegacyToolAccessProtocol(Protocol):
    """旧 ReActLoop 工具列表迁移接口（非 RFC-0001 Capability Layer）。

    Commit 3 仅投影 core.tools 窄视图；Resolver/Provider 留 M0.3。
    命名 Legacy* 明确区别于 RFC-0001 的 CapabilityProtocol（Provider 侧）。
    """

    @property
    def tools(self) -> Any: ...

    def get(self, name: str) -> Any: ...
