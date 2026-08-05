"""权限模型测试。"""

import pytest

from anima.core.config import AnimaConfig, PermissionConfig
from anima.pipeline.stages import _check_permission


def test_global_owner():
    cfg = AnimaConfig()
    cfg.permissions.owners = [123456]
    level, role = _check_permission(123456, None, cfg)
    assert level == 4
    assert role == "owner"


def test_global_admin():
    cfg = AnimaConfig()
    cfg.permissions.admins = [111]
    level, role = _check_permission(111, None, cfg)
    assert level == 3
    assert role == "admin"


def test_banned_group():
    cfg = AnimaConfig()
    cfg.permissions.banned_groups = [100]
    level, role = _check_permission(10001, 100, cfg)
    assert level == 0
    assert role == "banned"


def test_readonly_group():
    cfg = AnimaConfig()
    cfg.permissions.readonly_groups = [100]
    level, role = _check_permission(10001, 100, cfg)
    assert level == 1
    assert role == "readonly"


def test_banned_user_overrides_group():
    cfg = AnimaConfig()
    cfg.permissions.banned = [10001]
    cfg.permissions.readonly_groups = [100]
    level, role = _check_permission(10001, 100, cfg)
    assert level == 0
    assert role == "banned"


# --- M0.4.3 S1: UID-only 授权边界 ---------------------------------------
# 授权主体唯一为 QQ UID，且必须显式授予。QQ 群角色不参与授权。


def test_check_permission_signature_has_no_sender_role():
    """签名级守门：_check_permission 不得再接受群角色参数。"""
    import inspect

    params = list(inspect.signature(_check_permission).parameters)
    assert params == ["user_id", "group_id", "config"]
    assert "sender_role" not in params


def test_unauthorized_uid_defaults_to_user():
    """未显式授权的 UID 一律为 level 2，不可执行 admin 级操作。"""
    cfg = AnimaConfig()
    level, role = _check_permission(10001, 100, cfg)
    assert level == 2
    assert role == "user"


def test_authorized_uid_allowed_in_group():
    """显式授权的 UID 在群内同样生效（授权来自 UID，不来自群）。"""
    cfg = AnimaConfig()
    cfg.permissions.owners = [10001]
    level, role = _check_permission(10001, 100, cfg)
    assert level == 4
    assert role == "owner"


def test_group_policy_cannot_elevate_unauthorized_uid():
    """群策略只能收紧：未授权 UID 在任何群配置下都拿不到 admin。"""
    cfg = AnimaConfig()
    cfg.permissions.group_policies = {"100": {"allow_tools": True}}
    level, role = _check_permission(10001, 100, cfg)
    assert level == 2
    assert role == "user"
