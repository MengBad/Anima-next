"""Phase 1 沙箱分层测试：权限等级 → 沙箱能力映射与 import 守卫。

覆盖：
- SANDBOX_TIERS 映射正确性（网络/超时/内存/import 黑名单）
- run_python 按 permission_level 分层：普通用户阻止 subprocess/socket，主人放行
- 默认（不传 permission_level）行为等同于普通用户（向后兼容零回归）
- 越界等级（>4）按主人处理
"""

import asyncio

import pytest

from anima.tools.sandbox import (
    SANDBOX_TIERS,
    _LEVEL_OWNER,
    _LEVEL_USER,
    run_python,
    sandbox_tier_for,
)


def test_tier_mapping_basic():
    user = sandbox_tier_for(_LEVEL_USER)
    admin = sandbox_tier_for(3)
    owner = sandbox_tier_for(_LEVEL_OWNER)

    # 普通用户
    assert user.network is False
    assert user.timeout == 10
    assert user.memory_mb == 128
    assert "subprocess" in user.blocked_imports

    # 管理员：延长超时与内存，仍无网络
    assert admin.network is False
    assert admin.timeout == 30
    assert admin.memory_mb == 256

    # 主人：全能力
    assert owner.network is True
    assert owner.timeout == 120
    assert owner.memory_mb == 512
    assert owner.blocked_imports == {"ctypes", "multiprocessing"}


def test_tier_out_of_range_caps_to_owner():
    # 越界（>4）按主人处理；过低（0/负数）按普通用户处理
    assert sandbox_tier_for(99).network is True
    assert sandbox_tier_for(0).network is False
    assert sandbox_tier_for(-5).network is False


def test_tiers_registry_shape():
    assert set(SANDBOX_TIERS.keys()) == {_LEVEL_USER, 3, _LEVEL_OWNER}


async def test_run_python_blocks_subprocess_for_user():
    out = await run_python("import subprocess\nprint('LEAKED')", permission_level=_LEVEL_USER)
    assert "LEAKED" not in out
    assert "blocked" in out or "ImportError" in out


async def test_run_python_allows_subprocess_for_owner():
    out = await run_python("import subprocess\nprint('OWNER_OK')", permission_level=_LEVEL_OWNER)
    assert "OWNER_OK" in out


async def test_run_python_blocks_socket_for_user_allows_for_owner():
    user_out = await run_python(
        "import socket\nprint('SOCK_LEAK')", permission_level=_LEVEL_USER
    )
    assert "SOCK_LEAK" not in user_out

    owner_out = await run_python(
        "import socket\nprint('SOCK_OK')", permission_level=_LEVEL_OWNER
    )
    assert "SOCK_OK" in owner_out


async def test_run_python_default_is_user_tier_backward_compat():
    # 不传 permission_level 时，行为必须与普通用户完全一致（零回归）
    out = await run_python("import subprocess\nprint('DEF_LEAK')")
    assert "DEF_LEAK" not in out
    assert "blocked" in out or "ImportError" in out


async def test_run_python_writes_utf8_temporary_source():
    """Temporary Python source must remain valid on non-UTF-8 host locales."""
    out = await run_python("print('UTF8_OK', '你好，sandbox')", permission_level=_LEVEL_USER)
    assert "UTF8_OK" in out
