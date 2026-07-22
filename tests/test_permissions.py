"""权限模型测试。"""

import pytest

from anima.core.config import AnimaConfig, PermissionConfig
from anima.pipeline.stages import _check_permission


def test_global_owner():
    cfg = AnimaConfig()
    cfg.permissions.owners = [123456]
    level, role = _check_permission(123456, None, cfg, "member")
    assert level == 4
    assert role == "owner"


def test_global_admin():
    cfg = AnimaConfig()
    cfg.permissions.admins = [111]
    level, role = _check_permission(111, None, cfg, "member")
    assert level == 3
    assert role == "admin"


def test_group_owner_maps_to_owner():
    """群 owner 应被识别为 owner 级别，而不是 admin。"""
    cfg = AnimaConfig()
    level, role = _check_permission(10001, 100, cfg, "owner")
    assert level == 4
    assert role == "owner"


def test_group_admin_maps_to_admin():
    cfg = AnimaConfig()
    level, role = _check_permission(10001, 100, cfg, "admin")
    assert level == 3
    assert role == "admin"


def test_banned_group():
    cfg = AnimaConfig()
    cfg.permissions.banned_groups = [100]
    level, role = _check_permission(10001, 100, cfg, "member")
    assert level == 0
    assert role == "banned"


def test_readonly_group():
    cfg = AnimaConfig()
    cfg.permissions.readonly_groups = [100]
    level, role = _check_permission(10001, 100, cfg, "member")
    assert level == 1
    assert role == "readonly"


def test_banned_user_overrides_group():
    cfg = AnimaConfig()
    cfg.permissions.banned = [10001]
    cfg.permissions.readonly_groups = [100]
    level, role = _check_permission(10001, 100, cfg, "member")
    assert level == 0
    assert role == "banned"
