"""DeprecatedCoreAccess — M0.1 Commit 3b。

旧世界 → 新边界的「受治理入口」。这是第一次真正改变依赖方向：
Stage 不再直接抓整个 core，而是经由 ctx.core 的窄 facade 表面。

本对象只做边界组装（composition），不持有业务逻辑：
- 七个 facade 方法（memory/config/outbound/telemetry/permission/agenda/tools）
  各自返回对应 Facade Protocol 的薄封装，委托给注入进来的真实 core（鸭子类型，
  contracts 层零反向依赖 —— core 由 handler 注入，本模块不 import 任何业务模块）。
- raw.require(name, caller) 是显式债务入口：仅允许尚未建 facade 的后门
  （llm/danger/exploration/tool_learning），且必须记录 caller，否则禁止。

设计约束（用户拍板）：
- Facade 是默认路径；raw 是显式动作（grep `raw.require(` 即可定位全部债务）。
- raw 不是新版 Core API，而是「暂未迁移能力的临时桥」，无限扩张是被禁止的。
"""
from __future__ import annotations

import logging
import os
from typing import Any, Callable

from .errors import RawAccessForbidden, RawAccessNotAllowed
from .facades import (
    AgendaProtocol,
    ConfigProtocol,
    LegacyToolAccessProtocol,
    MemoryProtocol,
    OutboundMsg,
    OutboundProtocol,
    PermissionProtocol,
    RAW_ACCESS_ALLOWLIST,
    RAW_ACCESS_FORBIDDEN,
    TelemetryProtocol,
)

logger = logging.getLogger("anima.contracts.access")


def _default_audit(event: dict) -> None:
    """默认审计：每个 raw 访问都打 warning 并带 caller，防止 raw 沉默扩散。"""
    logger.warning(
        "Deprecated raw core access: capability=%s caller=%s",
        event.get("capability"), event.get("caller"),
    )


class _RawAccess:
    """raw 显式债务入口。只能通过 require(name, caller) 访问，禁止属性直取。"""

    def __init__(self, core: Any, audit: Callable[[dict], None]) -> None:
        self._core = core
        self._audit = audit

    def require(self, name: str, *, caller: str) -> Any:
        if name in RAW_ACCESS_FORBIDDEN:
            raise RawAccessForbidden(
                f"raw access to '{name}' is forbidden: use ctx.core.{name}() facade instead"
            )
        if name not in RAW_ACCESS_ALLOWLIST:
            raise RawAccessNotAllowed(
                f"raw access to '{name}' not in allowlist {sorted(RAW_ACCESS_ALLOWLIST)}"
            )
        self._audit({"capability": name, "caller": caller})
        return getattr(self._core, name)


# ---- Facade 薄封装：真实路径均经读代码核实（非记忆） ----
# Memory 真实路径 = core.vector_memory（非 core.memory.vector_memory）
# Config 真实路径 = core.config.*（含 permissions.group_policies）
# Outbound 真实路径 = core.gateway.send(OutboundMsg)
# Telemetry 真实路径 = core.reply_quality + core.audit
# Agenda 真实路径 = core.propose_agenda(group_id, text, *, priority)
# Tools 真实路径 = core.tools（ToolRegistry；Commit 3 仅窄视图，Resolver 留 M0.3）
# Permission 真实路径 = core.config.permissions.*（V1 无独立子系统；未来 Policy Engine）


class _MemoryFacade:
    def __init__(self, vector_memory: Any) -> None:
        self._vm = vector_memory

    def search(self, query: str, **kwargs) -> Any:
        return self._vm.search(query, **kwargs)

    def add(self, text: str, **kwargs) -> Any:
        return self._vm.add(text, **kwargs)


class _AgendaFacade:
    def __init__(self, core: Any) -> None:
        self._core = core

    def propose_agenda(self, group_id, text, *, priority=0.5, **kwargs) -> list:
        return self._core.propose_agenda(group_id, text, priority=priority, **kwargs)


class _TelemetryFacade:
    def __init__(self, core: Any) -> None:
        self._core = core

    def record_guard_fixation(self) -> None:
        rq = getattr(self._core, "reply_quality", None)
        if rq is not None:
            rq.record_guard_fixation()

    def emit(self, event: str, **kwargs) -> None:
        audit = getattr(self._core, "audit", None)
        if audit is not None:
            audit.emit(event, **kwargs)


class _ConfigFacade:
    def __init__(self, config: Any) -> None:
        self._cfg = config

    @property
    def persistence(self) -> Any:
        return getattr(self._cfg, "persistence", None)

    @property
    def sticker(self) -> Any:
        return getattr(self._cfg, "sticker", None)

    @property
    def security(self) -> Any:
        return getattr(self._cfg, "security", None)

    @property
    def permissions(self) -> Any:
        return getattr(self._cfg, "permissions", None)

    @property
    def qzone(self) -> Any:
        return getattr(self._cfg, "qzone", None)

    @property
    def voice(self) -> Any:
        return getattr(self._cfg, "voice", None)

    @property
    def meme(self) -> Any:
        return getattr(self._cfg, "meme", None)


class _PermissionFacade:
    def __init__(self, config: Any) -> None:
        self._cfg = config

    def group_policy_for(self, group_id, **kwargs) -> dict:
        perms = getattr(self._cfg, "permissions", None)
        if perms is None:
            return {}
        gp = getattr(perms, "group_policies", None)
        if not gp:
            return {}
        return gp.get(str(group_id), {})

    def role_for(self, user_id, group_id, **kwargs) -> str:
        # V1 无独立 role_for；角色在 _check_permission 内联计算。
        # 未来 Policy Engine 形成后在此接入；返回 "" 表示未判定（非假架构）。
        perms = getattr(self._cfg, "permissions", None)
        rf = getattr(perms, "role_for", None)
        if callable(rf):
            return rf(user_id, group_id, **kwargs)
        return ""


class _OutboundFacade:
    def __init__(self, gateway: Any) -> None:
        self._gw = gateway

    def send(self, message: OutboundMsg) -> Any:
        return self._gw.send(message)


class _LegacyToolAccessFacade:
    def __init__(self, tools: Any) -> None:
        self._tools = tools

    @property
    def tools(self) -> Any:
        return self._tools

    def get(self, name: str) -> Any:
        if self._tools is None:
            return None
        g = getattr(self._tools, "get", None)
        if callable(g):
            return g(name)
        return getattr(self._tools, "tools", {}).get(name)


class DeprecatedCoreAccess:
    """旧 core → 新边界的受治理入口（M0.1 Commit 3b）。

    只做组合，不持有业务。core 以 Any 注入，contracts 层零反向依赖。
    Facade 是默认路径；raw 是显式债务入口。

    使用：
        access = DeprecatedCoreAccess(real_core)
        access.memory().search(...)            # 窄 facade
        access.outbound().send(msg)            # 窄 facade
        access.raw.require("llm", caller=...)  # 仅 allowlist 后门，须带 caller
    """

    def __init__(self, core: Any, *, audit: Callable[[dict], None] | None = None) -> None:
        self._core = core
        self._audit = audit or _default_audit

    # ---- Facade 默认路径 ----
    def memory(self) -> MemoryProtocol:
        return _MemoryFacade(getattr(self._core, "vector_memory", None))

    def config(self) -> ConfigProtocol:
        return _ConfigFacade(getattr(self._core, "config", None))

    def outbound(self) -> OutboundProtocol:
        return _OutboundFacade(getattr(self._core, "gateway", None))

    def telemetry(self) -> TelemetryProtocol:
        return _TelemetryFacade(self._core)

    def permission(self) -> PermissionProtocol:
        return _PermissionFacade(getattr(self._core, "config", None))

    def agenda(self) -> AgendaProtocol:
        return _AgendaFacade(self._core)

    def tools(self) -> LegacyToolAccessProtocol:
        return _LegacyToolAccessFacade(getattr(self._core, "tools", None))

    # ---- raw 显式债务入口（grep `raw.require(` 即可定位全部债务） ----
    @property
    def raw(self) -> _RawAccess:
        return _RawAccess(self._core, self._audit)


def facade_enabled() -> bool:
    """M0.1 Commit 3b 的 flag 决策（handler 构造层使用）。

    默认开启（Facade 生效）；设为 "false" 则仅保留 legacy meta["core"]，可回滚。
    抽成纯函数以便单测，handler 保持轻量。
    """
    return os.environ.get("ANIMA_CORE_FACADE_ENABLED", "true").lower() != "false"
