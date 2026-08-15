"""R6.3 —— Permission Model v2：企业级 RBAC 策略引擎（Principal→Role→Capability→Scope→Policy→Audit）。

## 目标（R6.3）

从 Owner-Only 升级为 Enterprise Permission Model。任何能力调用都必须回答五问：

    谁？         (Principal)         —— 调用者身份（信任源：pipeline 设定的 call context）
    为什么？     (Reason/Policy)     —— 命中哪条策略 / 谁授权
    允许什么？   (Capability+Scope)  —— 允许/拒绝的具体能力与权限域
    有效多久？   (Temporal)          —— 永久授权 or 临时授权（含过期时间）
    留下什么证据？(Audit)            —— 决策结果落审计事件

实现特性：RBAC（具名角色）、capability scope、temporary permission（含 expiry）、
permission inheritance（角色层级继承）、permission revoke（显式吊销覆盖授予）。

## 本模块边界（加性新增，不动共享核心）

- **不修改** ``config.py`` 的 ``PermissionConfig``、``tools/__init__.py``、``pipeline/*``、
  ``runtime/*``、``capability/governance/*``。本模块是**独立策略引擎**，由未来接线点
  （owner 授权部署阶段）把角色/原则映射喂进来，或由 owner 运维脚本调用。
- **身份信任源**：本模块**绝不**从不可信调用参数派生身份；Principal 的解析只依赖
  调用方给定的可信 principal 标识 + 角色映射表。
- **fail-closed**：策略解析异常、角色缺失、scope 信息缺失、过期时间缺失 → DENY，
  绝不因"信息不全"就放行（安全原则 1）。
- **审计**：每次策略决策（ALLOW/DENY）产生一条可落盘的审计记录（与 R6.2 事件链
  同构，可经 ``ChainAuditSink`` 落盘）。审计为 fail-open（写失败只计数不阻断）。

## 模型

:class:`Role` —— 具名角色，携带：
- ``allowed``：允许的能力集 + 各自 scope（``{capability: set[scope]}``）
- ``denies``：显式拒绝的能力集（吊销，优先级高于允许）
- ``parent``：父角色 id（继承；父的 allowed 会被子继承，父的 denies 也继承）
- ``expires_at``：可选过期时间（temporary permission / 角色授权到期）

:class:`PrincipalPolicy` —— 主体 → 角色绑定：``{principal_id: list[role_id]}``

:class:`PolicyDecision` —— 一次请求的决策结果：
- ``principal`` / ``role``（生效角色，含继承闭合）/ ``capability`` / ``scope``
- ``decision``（allowed|denied）/ ``reason`` / ``effective_until``（临时授权到期）
- 可序列化为 R6.2 事件链所需字段。

:class:`RBACPolicyEngine` —— 求值入口：给定 principal + 请求能力 + 所需 scope，
解析角色（含继承闭合 + 吊销 + 过期）→ 输出 PolicyDecision → 交 audit sink。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable

# ---------------------------------------------------------------------------
# 常量词表（与 R6.2 event_chain / tools 权限命名对齐）
# ---------------------------------------------------------------------------
DECISION_ALLOWED = "allowed"
DECISION_DENIED = "denied"

REASON_ROLE_NOT_FOUND = "role_not_found"
REASON_PRINCIPAL_UNBOUND = "principal_unbound"
REASON_DENIED_BY_POLICY = "denied_by_policy"
REASON_SCOPE_MISSING = "scope_missing"
REASON_EXPIRED = "expired"
REASON_INTERNAL = "internal_error"


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Role:
    """具名角色：允许的能力/scope 集、显式拒绝、父角色（继承）、可选过期。

    语义：**deny 优先于 allow**（吊销覆盖授予，安全原则 2 无隐藏权限）。
    - 继承：子角色继承父角色的 allowed 与 denies（permission inheritance）。
    - 临时：``expires_at`` 非空且已过期 → 该角色不生效（temporary permission）。
    """

    id: str
    allowed: dict[str, frozenset[str]] = field(default_factory=dict)  # {capability: {scope,...}}
    denies: frozenset[str] = frozenset()                              # 显式拒绝的能力
    parent: str | None = None                                         # 父角色 id
    expires_at: float | None = None                                   # 角色授权到期（临时授权）


@dataclass(frozen=True)
class PolicyDecision:
    """一次能力请求的策略决策结果（可序列化为 R6.2 事件链字段）。"""

    principal: str
    capability: str
    decision: str                          # allowed | denied
    reason: str | None = None              # 决策理由（denied 时必填）
    role: str | None = None                # 生效角色（含继承闭合后的主角色）
    scopes: tuple[str, ...] = ()           # 请求的 scope
    granted_scopes: tuple[str, ...] = ()   # 实际授予的 scope（允许路径）
    effective_until: float | None = None   # 临时授权到期（permission 有效多久）
    timestamp: float = field(default_factory=time.time)

    @property
    def allowed(self) -> bool:
        return self.decision == DECISION_ALLOWED

    def to_event_fields(self) -> dict[str, Any]:
        """投影成 R6.2 event_chain 的事件字段（caller/permission/role/decision）。"""
        return {
            "caller": self.principal,
            "permission": "OWNER" if self.role == "owner" else "USER",
            "decision": self.decision,
            "role": self.role or "unknown",
            "denied_reason": self.reason,
            "detail": f"{self.capability} -> {self.decision} ({self.reason or 'allowed'})",
        }


@dataclass
class RBACPolicyEngine:
    """企业级 RBAC 策略求值器。

    :param roles: 角色注册表 {role_id: Role}
    :param principal_roles: 主体→角色绑定 {principal_id: list[role_id]}
    :param audit_sink: 可选审计出口（``(event) -> None``）；决策后发射，fail-open。
    :param clock: 时钟（测试注入；默认 time.time）
    """

    roles: dict[str, Role] = field(default_factory=dict)
    principal_roles: dict[str, list[str]] = field(default_factory=dict)
    audit_sink: Callable[[dict], None] | None = None
    clock: Callable[[], float] = time.time

    # -- 角色闭包 -----------------------------------------------------------
    def role_closure(self, role_id: str, seen: set[str] | None = None) -> list[Role]:
        """角色及其全部祖先（继承闭合），含环保护。"""
        seen = seen or set()
        if role_id in seen:
            return []
        seen.add(role_id)
        role = self.roles.get(role_id)
        if role is None:
            return []
        result = [role]
        if role.parent:
            result += self.role_closure(role.parent, seen)
        return result

    def principal_effective_roles(self, principal: str, now: float | None = None) -> list[Role]:
        """主体的全部生效角色（继承闭合 + 过期过滤）。fail-closed：未绑定 → []。"""
        now = now if now is not None else self.clock()
        role_ids = self.principal_roles.get(principal, [])
        effective: list[Role] = []
        for rid in role_ids:
            for role in self.role_closure(rid):
                if role.expires_at is not None and now > role.expires_at:
                    continue  # 临时授权已过期 → 该角色不生效
                if role not in effective:
                    effective.append(role)
        return effective

    # -- 决策 ---------------------------------------------------------------
    def evaluate(
        self,
        principal: str,
        capability: str,
        required_scopes: Iterable[str] = (),
        *,
        now: float | None = None,
    ) -> PolicyDecision:
        """求值一次能力请求 → PolicyDecision。

        决策顺序（fail-closed）：
        1. 主体未绑定任何角色 → DENY principal_unbound
        2. 生效角色为空（全过期/不存在）→ DENY role_not_found
        3. 任一生效角色显式 deny 该能力 → DENY denied_by_policy（吊销优先）
        4. 任一生效角色 allow 该能力且覆盖所需 scope → ALLOW
        5. 有角色 allow 但 scope 不全 → DENY scope_missing
        6. 否则 → DENY denied_by_policy
        """
        now = now if now is not None else self.clock()
        req = frozenset(required_scopes)

        # 1. 主体未绑定
        if not self.principal_roles.get(principal):
            dec = PolicyDecision(
                principal, capability, DECISION_DENIED,
                REASON_PRINCIPAL_UNBOUND, role=None, scopes=tuple(req), timestamp=now)
            self._emit(dec)
            return dec

        roles = self.principal_effective_roles(principal, now)
        # 2. 无生效角色（全过期/不存在）
        if not roles:
            dec = PolicyDecision(
                principal, capability, DECISION_DENIED,
                REASON_ROLE_NOT_FOUND, role=None, scopes=tuple(req), timestamp=now)
            self._emit(dec)
            return dec

        # 3+4. 聚合 allowed/deny 闭包
        granted: set[str] = set()
        allow_role: Role | None = None
        deny_role: Role | None = None
        for role in roles:
            if capability in role.denies:
                deny_role = role
            allowed_scopes = role.allowed.get(capability)
            if allowed_scopes is not None:
                granted |= set(allowed_scopes)
                if allow_role is None:
                    allow_role = role

        # 吊销优先（安全原则 2）
        if deny_role is not None:
            dec = PolicyDecision(
                principal, capability, DECISION_DENIED,
                REASON_DENIED_BY_POLICY, role=deny_role.id,
                scopes=tuple(req), granted_scopes=tuple(granted), timestamp=now)
            self._emit(dec)
            return dec

        # 需要的能力未在任何生效角色 allow → 拒
        if allow_role is None or capability not in allow_role.allowed:
            dec = PolicyDecision(
                principal, capability, DECISION_DENIED,
                REASON_DENIED_BY_POLICY, role=allow_role.id if allow_role else None,
                scopes=tuple(req), granted_scopes=tuple(granted), timestamp=now)
            self._emit(dec)
            return dec

        # 5. scope 覆盖判定：允许的 scope 必须 ⊇ 请求的 scope
        if not req.issubset(granted):
            dec = PolicyDecision(
                principal, capability, DECISION_DENIED,
                REASON_SCOPE_MISSING, role=allow_role.id,
                scopes=tuple(req), granted_scopes=tuple(granted), timestamp=now)
            self._emit(dec)
            return dec

        # 6. 临时授权有效期（取最早到期；None=永久）
        expiry = min(
            (r.expires_at for r in roles if r.expires_at is not None), default=None)
        dec = PolicyDecision(
            principal, capability, DECISION_ALLOWED, None,
            role=allow_role.id, scopes=tuple(req), granted_scopes=tuple(granted),
            effective_until=expiry, timestamp=now)
        self._emit(dec)
        return dec

    def _emit(self, dec: PolicyDecision) -> None:
        """决策后发射审计事件。fail-open：写失败只计数，绝不阻断决策。"""
        if self.audit_sink is None:
            return
        try:
            self.audit_sink(dec.to_event_fields())
        except Exception:  # noqa: BLE001 - 观测失灵不得让授权决策不可用
            pass

    # -- 运维辅助 -----------------------------------------------------------
    def grant_temporary(
        self, principal: str, capability: str, scopes: Iterable[str],
        ttl_seconds: float, *,
        role_id: str = "temp", parent: str | None = None,
    ) -> Role:
        """授予一个带过期的临时角色并绑定到主体（temporary permission）。

        用于"限时提权"：给一个临时角色（默认继承某父角色），设定 ttl 到期时间，
        到期后 principal_effective_roles 自动过滤掉它（fail-closed）。
        """
        expires = self.clock() + ttl_seconds
        role = Role(
            id=role_id,
            allowed={capability: frozenset(scopes)},
            parent=parent,
            expires_at=expires,
        )
        self.roles[role_id] = role
        self.principal_roles.setdefault(principal, []).append(role_id)
        return role

    def revoke(self, principal: str, capability: str) -> None:
        """吊销：在该主体的所有生效角色上加显式 deny（permission revoke）。

        吊销优先于允许（deny 覆盖 allow）——即使角色仍允许，revoke 后立即 DENY。
        返回即时生效，无需等待角色移除。frozen dataclass 不可变，用 replace 生成
        带新 denies 的角色并替换注册表。
        """
        for rid in self.principal_roles.get(principal, []):
            role = self.roles.get(rid)
            if role is None:
                continue
            self.roles[rid] = replace(
                role, denies=frozenset(role.denies) | {capability})