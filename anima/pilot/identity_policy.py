"""P8.10 — QQ Identity Policy (qq/group/user scopes + allow/deny + admin).

A single policy object decides whether a (qq_user, group) pair may act under
the pilot, with explicit scopes:

    * group scope  - per-group allowlist / denylist
    * user scope   - per-user allowlist / denylist (users scoped to a group)
    * admin role   - an explicit admin allowlist (admins bypass user denylist
                     but NEVER bypass the group denylist or kill switch)
    * fail closed  - absent config (empty allowlists) denies everything

Decision order (fail closed):
    1. kill switch / policy disabled        -> DENY
    2. group in denylist                    -> DENY (hard, even admins)
    3. group not in allowlist               -> DENY (empty allowlist = deny all)
    4. user in user-denylist AND not admin  -> DENY
    5. user allowlist non-empty AND user not in it AND not admin -> DENY
    6. otherwise                            -> ALLOW

Scope limits (P8.10): no System B, no gateway, no capability activation, no
env bypass; the policy only decides, it never executes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

__all__ = [
    "QQIdentityPolicy",
    "IdentityPolicyConfig",
    "PolicyDecision",
    "grant_subject",
]

ALLOW = "ALLOW"
DENY = "DENY"


def grant_subject(user_id: str) -> str:
    """P10.2-C: DETERMINISTIC one-way digest of a qq id for grant binding.

    Unlike ``hash_user_id`` (evidence, per-record random salt for privacy),
    grants must bind to a STABLE subject — the same user always resolves to
    the same digest, so a permanent grant stays matchable.  The plaintext
    qq id is never stored or compared directly.
    """
    import hashlib

    return hashlib.sha256(f"anima-grant:{user_id}".encode("utf-8")).hexdigest()[:24]


@dataclass(frozen=True, slots=True)
class IdentityPolicyConfig:
    """P8.10 policy configuration (all fail-closed defaults)."""

    enabled: bool = False                    # default OFF
    group_allowlist: tuple = ()             # empty -> deny all groups
    group_denylist: tuple = ()              # hard deny, even admins
    user_allowlist: tuple = ()              # empty -> any allowed-group member
    user_denylist: tuple = ()               # hard deny unless admin
    admin_users: tuple = ()                 # admin role (bypass user denylist)
    # P8.10 permission levels: a user may be granted a bounded numeric level
    # (admin=100 default).  Unknown users get level 0 (fail closed).  Callers
    # compare the returned level against the level a capability requires.
    user_levels: dict = None                # {user_id: int}
    admin_level: int = 100
    unknown_level: int = 0
    # P9.2 capability scope: {capability: required_level}.  A capability not
    # listed here is NOT authorized (fail closed); a user may act on a listed
    # capability only when their level >= its required level.
    capability_levels: dict = None          # {capability: int}

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "IdentityPolicyConfig":
        d = d or {}
        return cls(
            enabled=bool(d.get("enabled", False)),
            group_allowlist=tuple(d.get("group_allowlist") or ()),
            group_denylist=tuple(d.get("group_denylist") or ()),
            user_allowlist=tuple(d.get("user_allowlist") or ()),
            user_denylist=tuple(d.get("user_denylist") or ()),
            admin_users=tuple(d.get("admin_users") or ()),
            user_levels=dict(d.get("user_levels") or {}),
            admin_level=int(d.get("admin_level", 100)),
            unknown_level=int(d.get("unknown_level", 0)),
            capability_levels=dict(d.get("capability_levels") or {}),
        )


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """One policy decision (read-only)."""

    allowed: bool
    reason: str
    role: str = "user"                      # user / admin
    level: int = 0                          # P8.10 permission level


class QQIdentityPolicy:
    """Fail-closed QQ identity policy (group/user scopes + admin + deny).

    P8.10: a decision carries a numeric ``level`` — unknown users default to
    ``unknown_level`` (0, fail closed); admins get ``admin_level``; explicit
    ``user_levels`` grant bounded levels.  Callers must require a level >= the
    capability's required level; a DENY decision always carries level 0.
    """

    def __init__(self, config: Optional[IdentityPolicyConfig] = None) -> None:
        self._config = config or IdentityPolicyConfig()
        if self._config.user_levels is None:
            object.__setattr__(self._config, "user_levels", {})
        if self._config.capability_levels is None:
            object.__setattr__(self._config, "capability_levels", {})

    @property
    def config(self) -> IdentityPolicyConfig:
        return self._config

    def _is_admin(self, user_id: str) -> bool:
        return user_id in self._config.admin_users

    def level_for(self, user_id: str) -> int:
        """The permission level of a user (fail closed: unknown -> 0)."""
        if self._is_admin(user_id):
            return self._config.admin_level
        levels = self._config.user_levels or {}
        return int(levels.get(user_id, self._config.unknown_level))

    def can(
        self, user_id: str, capability: str, *, group_id: str
    ) -> bool:
        """P9.2: may this qq user act on ``capability`` in ``group_id``?

        Fail closed — ALL of the following must hold:
          * policy enabled
          * capability declared in ``capability_levels`` (undeclared is not
            authorized, even for admins)
          * (user, group) passes the identity/group policy
          * user level >= the capability's required level
        """
        if not self._config.enabled:
            return False
        levels = self._config.capability_levels or {}
        if capability not in levels:
            return False  # not configured = not authorized
        decision = self.decide(group_id=group_id, user_id=user_id)
        if not decision.allowed:
            return False
        return self.level_for(user_id) >= int(levels[capability])

    def decide(self, *, group_id: str, user_id: str) -> PolicyDecision:
        """Decide whether (user, group) may act (fail closed)."""
        if not self._config.enabled:
            return PolicyDecision(False, "policy_disabled")
        # 1. group denylist: hard deny even for admins
        if group_id in self._config.group_denylist:
            return PolicyDecision(False, "group_denylisted", role="user")
        # 2. group allowlist: empty -> deny all; not listed -> deny
        if not self._config.group_allowlist:
            return PolicyDecision(False, "group_allowlist_empty")
        if group_id not in self._config.group_allowlist:
            return PolicyDecision(False, "group_not_allowed")
        # 3. admin role (bypasses user denylist and user allowlist)
        admin = self._is_admin(user_id)
        role = "admin" if admin else "user"
        level = self.level_for(user_id)  # P8.10: fail-closed numeric level
        # 4. user denylist: deny unless admin
        if user_id in self._config.user_denylist and not admin:
            return PolicyDecision(False, "user_denylisted", role=role, level=0)
        # 5. user allowlist: non-empty and not listed -> deny unless admin
        if self._config.user_allowlist and user_id not in self._config.user_allowlist and not admin:
            return PolicyDecision(False, "user_not_allowed", role=role, level=0)
        return PolicyDecision(True, "allowed", role=role, level=level)
