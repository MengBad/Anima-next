# -*- coding: utf-8 -*-
"""M1 安全边界回归（2026-08-25 全量扫描修复）。

- /vnc 不再免认证：_open_auth_routes 是真实现（_auth 调用同一函数），测它即测真实闸门。
- 群维度「只收紧」对 owner/admin 生效（Q3 拍板）。
- M1.3 决策：token 登录即 owner 凭证，audit reviewer=owner 非伪造，故不更改（记于 plan）。
"""
from types import SimpleNamespace

from anima.webui.server import _open_auth_routes
from anima.pipeline.stages.permission import _check_permission


def _cfg(owners=(), admins=(), banned=(), readonly=(), banned_g=None, readonly_g=None):
    return SimpleNamespace(permissions=SimpleNamespace(
        banned=set(banned), readonly=set(readonly), owners=set(owners), admins=set(admins),
        banned_groups=set(banned_g or ()), readonly_groups=set(readonly_g or ()),
        group_policies={},
    ))


# ---- M1.1 /vnc 免认证 ----
def test_open_auth_routes_keeps_legit_public():
    for p in ("/login", "/api/auth/login", "/source", "/source/archive", "/assets/app.js"):
        assert _open_auth_routes(p) is True, p

def test_open_auth_routes_blocks_vnc():
    for p in ("/vnc", "/vnc/", "/vnc/ws", "/vnc/static/x.js"):
        assert _open_auth_routes(p) is False, f"/vnc 曾整段免认证，现必须走 _auth: {p}"

# ---- M1.2 群维度只收紧（owner/admin 同样受限）----
def test_owner_in_banned_group_is_tightened():
    assert _check_permission(10001, 20001, _cfg(owners=(10001,), banned_g=(20001,)))[0] == 0

def test_owner_in_readonly_group_is_tightened():
    assert _check_permission(10001, 20002, _cfg(owners=(10001,), readonly_g=(20002,)))[0] == 1

def test_admin_in_banned_group_is_tightened():
    assert _check_permission(10002, 20001, _cfg(admins=(10002,), banned_g=(20001,)))[0] == 0

def test_admin_in_readonly_group_is_tightened():
    assert _check_permission(10002, 20002, _cfg(admins=(10002,), readonly_g=(20002,)))[0] == 1

def test_owner_normal_group_keeps_owner():
    assert _check_permission(10001, 99999, _cfg(owners=(10001,), readonly_g=(20002,))) == (4, "owner")

def test_normal_user_unaffected():
    assert _check_permission(30003, 20010, _cfg(banned_g=(20010,)))[0] == 0
    assert _check_permission(30003, 20002, _cfg(readonly_g=(20002,)))[0] == 1
    assert _check_permission(30003, 99999, _cfg())[0] == 2

def test_hard_banned_user_always_denied():
    assert _check_permission(40004, 20002, _cfg(banned=(40004,), readonly_g=(20002,)))[0] == 0