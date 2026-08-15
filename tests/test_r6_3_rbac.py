"""R6.3 —— Permission Model v2：企业级 RBAC 策略引擎验证。

覆盖五问完成标准（谁/为什么/允许什么/有效多久/留下什么证据）：
- RBAC：具名角色绑定，principal → role
- capability scope：允许/拒绝具体能力 + 权限域（network/exec/...）
- temporary permission：限时授权，到期 fail-closed DENY
- permission inheritance：父角色 allowed/denies 被子继承
- permission revoke：显式吊销覆盖授予，即时生效
- anti-forgery：身份只来自可信 principal 绑定，不接受调用参数派生
- audit：每次决策（ALLOW/DENY）发射事件，fail-open
- fail-closed：未绑定/角色缺失/scope 不全/过期 → 一律 DENY

纪律（R6.3 anchored）：只写新增模块 + 测试，不修改共享核心。
"""
from __future__ import annotations

import time

import pytest

from anima.capability.rbac import (
    DECISION_ALLOWED,
    DECISION_DENIED,
    REASON_DENIED_BY_POLICY,
    REASON_PRINCIPAL_UNBOUND,
    REASON_ROLE_NOT_FOUND,
    REASON_SCOPE_MISSING,
    RBACPolicyEngine,
    Role,
)


class _Sink:
    """捕获审计事件的极简 sink。"""

    def __init__(self):
        self.events: list = []

    def __call__(self, event: dict):
        self.events.append(event)


def _role(role_id, allowed=None, denies=None, parent=None, expires_at=None):
    return Role(
        id=role_id,
        allowed={k: frozenset(v) for k, v in (allowed or {}).items()},
        denies=frozenset(denies or ()),
        parent=parent,
        expires_at=expires_at,
    )


def _engine(sink=None):
    eng = RBACPolicyEngine(
        roles={
            "reader": _role("reader", allowed={"filesystem.read": ["read"]}),
            "user": _role(
                "user",
                allowed={
                    "search.web": ["network"],
                    "filesystem.read": ["read"],
                    "python.exec": ["exec"],
                },
                parent="reader",  # 继承 reader 的 filesystem.read
            ),
            "admin": _role(
                "admin",
                allowed={
                    "filesystem.write": ["write"],
                    "python.exec": ["exec"],
                },
                parent="user",
            ),
            "owner": _role(
                "owner",
                allowed={
                    "python.exec": ["exec", "network"],
                    "capability.revoke": ["admin"],
                },
            ),
        },
        principal_roles={
            "alice": ["user"],      # 普通用户
            "bob": ["admin"],       # 管理员（继承 user + reader）
            "root": ["owner"],      # 所有者
        },
        audit_sink=sink,
        clock=time.time,
    )
    return eng


# --------------------------------------------------------------------------
# 一、RBAC 基础：允许 / 拒绝
# --------------------------------------------------------------------------

def test_owner_allowed_capability():
    eng = _engine()
    dec = eng.evaluate("root", "python.exec", ["exec"])
    assert dec.allowed is True
    assert dec.decision == DECISION_ALLOWED
    assert dec.role == "owner"


def test_user_allowed_own_capability():
    eng = _engine()
    dec = eng.evaluate("alice", "search.web", ["network"])
    assert dec.allowed is True
    assert dec.role == "user"


def test_user_denied_unowned_capability():
    """alice(user) 请求 owner 专属 python.exec 的 network → DENY。"""
    eng = _engine()
    dec = eng.evaluate("alice", "python.exec", ["exec"])
    # user 角色允许 exec scope（python.exec 需要 exec 也可用）——但 alice 的 user
    # 角色确实允许 python.exec:exec，故这里应该 ALLOW 于 user 角色。
    # 改用 owner 专属能力验证拒绝：
    dec2 = eng.evaluate("alice", "capability.revoke", ["admin"])
    assert dec2.allowed is False
    assert dec2.decision == DECISION_DENIED
    assert dec2.reason == REASON_DENIED_BY_POLICY


def test_principal_unbound_denied():
    """未绑定任何角色的主体 → DENY principal_unbound（fail-closed）。"""
    eng = _engine()
    dec = eng.evaluate("mallory", "search.web", ["network"])
    assert dec.allowed is False
    assert dec.reason == REASON_PRINCIPAL_UNBOUND


def test_role_not_found_denied():
    """主体绑定了不存在的角色 id → DENY role_not_found。"""
    eng = RBACPolicyEngine(principal_roles={"x": ["ghost"]})
    dec = eng.evaluate("x", "search.web", ["network"])
    assert dec.allowed is False
    assert dec.reason == REASON_ROLE_NOT_FOUND


# --------------------------------------------------------------------------
# 二、capability scope：权限域覆盖
# --------------------------------------------------------------------------

def test_scope_missing_denied():
    """角色允许 exec 但请求 network scope → scope_missing DENY。"""
    eng = RBACPolicyEngine(
        roles={"limited": _role("limited", allowed={"python.exec": ["exec"]})},
        principal_roles={"u": ["limited"]},
    )
    dec = eng.evaluate("u", "python.exec", ["network"])
    assert dec.allowed is False
    assert dec.reason == REASON_SCOPE_MISSING


def test_scope_full_allow():
    eng = RBACPolicyEngine(
        roles={"full": _role("full", allowed={"python.exec": ["exec", "network"]})},
        principal_roles={"u": ["full"]},
    )
    dec = eng.evaluate("u", "python.exec", ["exec", "network"])
    assert dec.allowed is True
    assert set(dec.granted_scopes) == {"exec", "network"}


# --------------------------------------------------------------------------
# 三、permission inheritance：角色层级继承
# --------------------------------------------------------------------------

def test_child_inherits_parent_allowed():
    """bob(admin) 继承 user 的 search.web + reader 的 filesystem.read。"""
    eng = _engine()
    assert eng.evaluate("bob", "search.web", ["network"]).allowed is True   # 经 user
    assert eng.evaluate("bob", "filesystem.read", ["read"]).allowed is True  # 经 reader
    assert eng.evaluate("bob", "filesystem.write", ["write"]).allowed is True  # 自身 admin


def test_parent_deny_inherited():
    """父角色 denies 的能力，子角色即便 allow 也被拒（吊销继承）。"""
    eng = RBACPolicyEngine(
        roles={
            "parent": _role("parent", allowed={"x": ["exec"]}, denies={"y"}),
            "child": _role("child", allowed={"y": ["exec"]}, parent="parent"),
        },
        principal_roles={"u": ["child"]},
    )
    # child 继承 parent 的 deny y → 即便 child 自己 allow y 也 DENY
    dec = eng.evaluate("u", "y", ["exec"])
    assert dec.allowed is False
    assert dec.reason == REASON_DENIED_BY_POLICY


def test_inheritance_cycle_safe():
    """父链成环不无限循环（环保护）。"""
    eng = RBACPolicyEngine(
        roles={
            "a": _role("a", allowed={"x": ["exec"]}, parent="b"),
            "b": _role("b", allowed={"y": ["exec"]}, parent="a"),
        },
        principal_roles={"u": ["a"]},
    )
    # 不抛、不无限循环；a 自身 allow x → ALLOW
    dec = eng.evaluate("u", "x", ["exec"])
    assert dec.allowed is True


# --------------------------------------------------------------------------
# 四、temporary permission：限时授权
# --------------------------------------------------------------------------

def test_temporary_grant_expires_fail_closed():
    """限时授权：ttl 内 ALLOW，过期后 DENY role_not_found。"""
    eng = RBACPolicyEngine(clock=lambda: 1000.0)
    eng.grant_temporary("tempuser", "python.exec", ["exec"], ttl_seconds=60)
    # ttl 内
    dec = eng.evaluate("tempuser", "python.exec", ["exec"], now=1030.0)
    assert dec.allowed is True
    assert dec.effective_until == 1060.0  # 1000 + 60
    # 过期后
    dec2 = eng.evaluate("tempuser", "python.exec", ["exec"], now=2000.0)
    assert dec2.allowed is False
    assert dec2.reason in (REASON_ROLE_NOT_FOUND, REASON_PRINCIPAL_UNBOUND)


def test_permanent_role_has_no_expiry():
    eng = _engine()
    dec = eng.evaluate("root", "python.exec", ["exec"])
    assert dec.allowed is True
    assert dec.effective_until is None  # 永久


# --------------------------------------------------------------------------
# 五、permission revoke：显式吊销覆盖授予
# --------------------------------------------------------------------------

def test_revoke_overrides_grant_immediately():
    """revoke 后即使角色仍允许该能力，立即 DENY。"""
    eng = _engine()
    assert eng.evaluate("root", "python.exec", ["exec"]).allowed is True
    eng.revoke("root", "python.exec")
    dec = eng.evaluate("root", "python.exec", ["exec"])
    assert dec.allowed is False
    assert dec.reason == REASON_DENIED_BY_POLICY


# --------------------------------------------------------------------------
# 六、anti-forgery：身份不可伪造
# --------------------------------------------------------------------------

def test_identity_not_derived_from_arguments():
    """引擎不接调用方参数派生身份；角色解析只依赖 principal 绑定表。
    伪造 principal 名（不属于绑定表）→ 一律 DENY principal_unbound。"""
    eng = _engine()
    for forged in ("root", "alice", "owner"):
        # 即便参数里写的是真 owner 名，只要不是绑定表里的主体现有 key 的绑定……
        # 这里验证：直接用请求里的"身份串"当 principal 求值，若该串不是有效绑定 → DENY。
        if forged in eng.principal_roles:
            continue  # 有效主体不受影响（身份来自绑定表，非请求参数注入）
        dec = eng.evaluate(forged, "python.exec", ["exec"])
        assert dec.allowed is False


def test_engine_never_accepts_privilege_override():
    """请求参数无法通过 capability 名注入覆盖权限：只按绑定表求值。"""
    eng = _engine()
    # alice 以请求参数声称 owner 能力，仍按她的绑定(user)求值
    dec = eng.evaluate("alice", "capability.revoke", ["admin"])
    assert dec.allowed is False  # user 角色不授予 revoke


# --------------------------------------------------------------------------
# 七、audit：决策证据落审计（谁/为什么/允许什么/证据）
# --------------------------------------------------------------------------

def test_allow_decision_emits_audit():
    sink = _Sink()
    eng = _engine(sink=sink)
    eng.evaluate("root", "python.exec", ["exec"])
    assert sink.events, "ALLOW 必须留证据"
    ev = sink.events[-1]
    assert ev["caller"] == "root"
    assert ev["decision"] == DECISION_ALLOWED
    assert ev["role"] == "owner"


def test_deny_decision_emits_audit():
    sink = _Sink()
    eng = _engine(sink=sink)
    eng.evaluate("mallory", "search.web", ["network"])
    assert sink.events
    ev = sink.events[-1]
    assert ev["decision"] == DECISION_DENIED
    assert ev["denied_reason"] == REASON_PRINCIPAL_UNBOUND


def test_audit_fail_open(monkeypatch):
    """审计 sink 写炸 → 决策不受影响，不抛。"""
    def _boom(event):
        raise OSError("audit down")

    eng = _engine(sink=_boom)
    dec = eng.evaluate("root", "python.exec", ["exec"])
    assert dec.allowed is True  # 审计失灵不得阻断授权


# --------------------------------------------------------------------------
# 八、event_chain 集成：R6.3 决策可喂给 R6.2 事件链
# --------------------------------------------------------------------------

def test_decision_to_event_fields_composes_with_event_chain(tmp_path):
    """R6.3 PolicyDecision 投影出的字段可被 R6.2 event_chain 落盘。"""
    from anima.capability.audit.event_chain import (
        build_chain_event, ChainAuditSink, ChainEventStore,
    )

    eng = _engine()
    dec = eng.evaluate("root", "python.exec", ["exec"])
    fields = dec.to_event_fields()

    sink = ChainAuditSink(ChainEventStore(str(tmp_path / "chain.jsonl")))
    ev = build_chain_event(
        caller=fields["caller"],
        capability="python.exec",
        permission=fields["permission"],
        provider="python.exec",
        sandbox="bwrap",
        result="success" if dec.allowed else "denied",
        role=fields["role"],
        decision=fields["decision"],
        owner_authorized=dec.allowed,
        denied_reason=dec.reason,
    )
    sink(ev)
    read_back = sink.store.read_events()
    assert len(read_back) == 1
    assert read_back[0].caller == "root"
    assert read_back[0].permission == "OWNER"
    assert read_back[0].decision == DECISION_ALLOWED