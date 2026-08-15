"""R6.4 —— Execution Isolation：工业级执行隔离策略层验证。

覆盖 R6.4 完成标准（可限制 / 可终止 / 可追踪）：
- CPU limit：CPU 时间配额，超限即终止
- Memory limit：内存上限
- Timeout：墙钟超时
- Process isolation：最大子进程数
- Resource accounting：资源记账（CPU 秒 / 字节 / 墙钟）可对账
- syscall restriction：seccomp 策略档（无网络 scope 强制收窄）
- fail-closed：请求超出系统/分层上限 → 校验失败，调用方必须 DENY，绝不降级
- 反提权：显式传入的请求参数只会收窄、不会放宽分层上界
- 审计：隔离事实投影到 R6.2 事件链 sandbox 字段

纪律（R6.4 anchored）：只写新增模块 + 测试，不修改 sandbox.py 核心。
"""
from __future__ import annotations

import pytest

from anima.capability.isolation import (
    ISO_CPU_OVER,
    ISO_MEMORY_OVER,
    ISO_OK,
    ISO_PROCS_OVER,
    ISO_TIMEOUT_OVER,
    IsolatorLimits,
    ResourceAccount,
    isolation_limits,
    isolation_to_event,
    terminate_signal,
    validate_limits,
)


# --------------------------------------------------------------------------
# 一、策略派生：权限分级上限（可限制）
# --------------------------------------------------------------------------

def test_user_default_limits():
    """user(2) 默认隔离档：无网络、严格 seccomp、限内存/超时/进程。"""
    lim = isolation_limits(2)
    assert lim.permission_level == 2
    assert lim.seccomp == "user"
    assert lim.timeout == 10
    assert lim.memory_mb == 128
    assert lim.max_procs == 4
    assert lim.isolator_name == "iso-user"


def test_owner_default_limits():
    """owner(4) 默认隔离档：宽松 seccomp、放宽内存/超时/进程（有网络 scope 时）。"""
    lim = isolation_limits(4, scopes=frozenset({"network"}))
    assert lim.seccomp == "owner"
    assert lim.timeout == 300
    assert lim.memory_mb == 512
    assert lim.max_procs == 64


def test_owner_without_network_scope_restricted():
    """owner 调用无网络 scope 的能力 → seccomp 强制收窄到 user（防网络侧信道）。"""
    lim = isolation_limits(4)  # 默认无 network scope
    assert lim.seccomp == "user"
    # 但其余资源上限仍是 owner 级（可限制性不降，仅 syscall 收窄）
    assert lim.timeout == 300
    assert lim.memory_mb == 512


def test_requested_params_never_raise_bound():
    """显式请求参数只会收窄、不会放宽分层上界（反提权）。"""
    lim = isolation_limits(2, timeout=9999, memory_mb=99999, cpu_seconds=9999)
    assert lim.timeout == 10        # 夹回 user 上限
    assert lim.memory_mb == 128     # 夹回 user 上限
    assert lim.cpu_seconds == 5.0   # 夹回 user 上限


def test_no_network_scope_forces_user_seccomp():
    """admin 请求无 network scope 的能力 → seccomp 强制收窄到 user（防网络侧信道）。"""
    lim = isolation_limits(3, scopes=frozenset())
    assert lim.seccomp == "user"
    lim2 = isolation_limits(3, scopes=frozenset({"network"}))
    assert lim2.seccomp == "admin"  # 有 network 才保留 admin 档


# --------------------------------------------------------------------------
# 二、fail-closed 校验：可满足性（不满足必须 DENY）
# --------------------------------------------------------------------------

def test_validate_ok():
    status, _ = validate_limits(2, timeout=5, memory_mb=64, cpu_seconds=2, max_procs=2)
    assert status == ISO_OK


def test_validate_timeout_over():
    status, detail = validate_limits(2, timeout=60)  # 请求超 user 上限 10
    assert status == ISO_TIMEOUT_OVER
    assert "timeout" in detail


def test_validate_memory_over():
    status, _ = validate_limits(2, memory_mb=512)
    assert status == ISO_MEMORY_OVER


def test_validate_cpu_over():
    status, _ = validate_limits(2, cpu_seconds=100)
    assert status == ISO_CPU_OVER


def test_validate_procs_over():
    status, _ = validate_limits(2, max_procs=50)
    assert status == ISO_PROCS_OVER


# --------------------------------------------------------------------------
# 三、resource accounting：可追踪
# --------------------------------------------------------------------------

def test_resource_account_accumulates():
    acct = ResourceAccount()
    acct.record(cpu=2.0, wall=3.0, nbytes=1024)
    acct.record(cpu=1.5, wall=2.0, nbytes=2048)
    assert acct.cpu_seconds == 3.5
    assert acct.wall_seconds == 5.0
    assert acct.bytes_written == 3072
    assert acct.executions == 2
    assert acct.degraded is False


def test_account_within_budget():
    acct = ResourceAccount(cpu_seconds=2.0, wall_seconds=3.0)
    lim = isolation_limits(2)  # cpu budget 5.0, timeout 10
    assert acct.within_budget(lim) is True
    acct.cpu_seconds = 6.0  # 超 cpu 预算
    assert acct.within_budget(lim) is False


# --------------------------------------------------------------------------
# 四、terminate 时序：可终止
# --------------------------------------------------------------------------

def test_terminate_none_while_within_budget():
    acct = ResourceAccount(cpu_seconds=1.0, wall_seconds=1.0)
    lim = isolation_limits(2)
    assert terminate_signal(lim, acct) is None


def test_terminate_term_on_mild_overrun():
    acct = ResourceAccount(cpu_seconds=0.0, wall_seconds=12.0)  # 略超 timeout 10
    lim = isolation_limits(2)
    assert terminate_signal(lim, acct) == "TERM"


def test_terminate_kill_on_severe_overrun():
    acct = ResourceAccount(cpu_seconds=0.0, wall_seconds=30.0)  # > 2*timeout(10)
    lim = isolation_limits(2)
    assert terminate_signal(lim, acct) == "KILL"


# --------------------------------------------------------------------------
# 五、审计投影：可追踪落 R6.2 事件链
# --------------------------------------------------------------------------

def test_isolation_to_event_fields():
    lim = isolation_limits(2)
    acct = ResourceAccount(cpu_seconds=1.0, wall_seconds=2.0, bytes_written=512)
    ev = isolation_to_event(lim, acct, "success")
    assert ev["sandbox"] == "iso-user"
    assert ev["sandbox_limits"]["seccomp"] == "user"
    assert ev["sandbox_limits"]["memory_mb"] == 128
    assert ev["resource_account"]["cpu_seconds"] == 1.0
    assert ev["result"] == "success"


def test_isolation_to_event_composes_with_chain_sink(tmp_path):
    """隔离事实可经 R6.2 event_chain 落盘（全链路可追踪闭环）。"""
    from anima.capability.audit.event_chain import (
        build_chain_event, ChainAuditSink, ChainEventStore,
    )

    lim = isolation_limits(2)
    acct = ResourceAccount(cpu_seconds=1.0)
    iso = isolation_to_event(lim, acct, "success")
    sink = ChainAuditSink(ChainEventStore(str(tmp_path / "chain.jsonl")))
    ev = build_chain_event(
        caller="alice",
        capability="search.web",
        permission="USER",
        provider="web_provider",
        sandbox=iso["sandbox"],
        result=iso["result"],
        role="user",
        decision="allowed",
    )
    sink(ev)
    read_back = sink.store.read_events()
    assert len(read_back) == 1
    assert read_back[0].sandbox == "iso-user"
    assert read_back[0].result == "success"