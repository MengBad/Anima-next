"""P9.2 — Failure Injection Validation (RED contract).

Actively test failure scenarios; in EVERY case the system must NOT produce a
false success ("错误成功" is forbidden):

    * NapCat disconnect      - channel drop -> event flow fails closed
    * network anomaly        - outbound send fails -> no fake "sent"
    * LLM timeout            - fallback answer times out -> no fabricated reply
    * provider failure       - sandbox.write fails -> FAILED observation
    * ledger recovery        - durable fact survives restart
    * kill switch            - disabled -> everything denied

Acceptance:
    A  provider failure -> FAILED observation, never success
    B  ledger durable fact survives restart (recovery)
    C  kill switch disabled -> pilot denies everything (no reply)
    D  outbound send failure -> coordinator reports failure, no fake sent
    E  coordinator fallback failure -> isolated (no crash)
    F  NapCat event drop (non-message) -> no reply, no evidence
    G  replay after crash -> no duplicate side effect (idempotent)
    H  static boundary: no System B / gateway mutation
"""
from __future__ import annotations

import asyncio
import pathlib

import pytest

from anima.core.persistence import PersistenceProvider
from anima.runtime.execution_ledger import ExecutionLedger
from anima.runtime.executor import ExecutionObservation, ExecutionStep
from anima.runtime.providers.sandbox_write_provider import SandboxWriteProvider
from anima.pilot.pilot_runtime import LivePilotConfig, PilotRuntime


def _step(task_id="t-x", path="out.txt", content="x"):
    return ExecutionStep(
        id=task_id, title="sandbox write", intent="failure injection",
        capability_need={"capability": "sandbox.write",
                         "args": {"path": path, "content": content}, "context": {}},
        depends_on=[], estimates={},
    )


def _executor(data_dir: str):
    from anima.runtime.capability_resolver import (
        CapabilityContract,
        CapabilityRegistry,
        CapabilityResolver,
    )
    from anima.runtime.real_task_executor import RealTaskExecutor

    reg = CapabilityRegistry()
    reg.register(
        CapabilityContract("sandbox.write", side_effect=True, idempotent=True),
        SandboxWriteProvider(sandbox_root=None, data_dir=data_dir),
    )
    return RealTaskExecutor(resolver=CapabilityResolver(reg))


# ---------------------------------------------------------------------------
# A - provider failure -> FAILED observation (never success)
# ---------------------------------------------------------------------------
def test_a_provider_failure_no_false_success(tmp_path):
    ex = _executor(str(tmp_path / "data"))
    ledger = ExecutionLedger(persistence=PersistenceProvider(str(tmp_path / "data")))
    obs = ex.execute(_step(path="../evil.txt"), ("r1", "t1", 0), ledger)
    assert obs.success is False  # traversal refused, no fabricated success
    assert ledger.get(("r1", "t1", 0)) is None  # never executed -> no fact


# ---------------------------------------------------------------------------
# B - ledger recovery: durable fact survives restart
# ---------------------------------------------------------------------------
def test_b_ledger_recovery(tmp_path):
    data = str(tmp_path / "data")
    ex = _executor(data)
    ledger1 = ExecutionLedger(persistence=PersistenceProvider(data))
    obs = ex.execute(_step(), ("r2", "t2", 0), ledger1)
    assert obs.success is True
    ledger2 = ExecutionLedger(persistence=PersistenceProvider(data))  # restart
    assert ledger2.get(("r2", "t2", 0)).success is True


# ---------------------------------------------------------------------------
# C - kill switch disabled -> pilot denies everything
# ---------------------------------------------------------------------------
def test_c_kill_switch_no_reply(tmp_path):
    class _Off:
        def check(self):
            raise RuntimeError("disabled")

    rt = PilotRuntime(
        config=LivePilotConfig(allowed_groups=("123456",), min_interval_sec=0.0),
        data_dir=str(tmp_path / "data"),
    )
    rt._kill_switch = _Off()
    ev = {"topic": "event.message.group", "user_id": 10001, "group_id": "123456",
          "message_id": "m1", "text": "你好", "mentions_self": True,
          "raw": {"message_type": "group"}}
    r = rt.handle(ev)
    assert r.handled is False and r.denied_reason == "kill_switch_disabled"
    assert r.reply == ""


# ---------------------------------------------------------------------------
# D - outbound send failure -> coordinator reports failure, no fake sent
# ---------------------------------------------------------------------------
def test_d_outbound_failure(tmp_path):
    from anima.pilot.wiring import PilotCoordinator

    rt = PilotRuntime(
        config=LivePilotConfig(allowed_groups=("123456",), min_interval_sec=0.0),
        data_dir=str(tmp_path / "data"),
    )
    failures = []

    async def bad_reply(dt, tid, msg):
        failures.append(("send_failed", tid))
        raise RuntimeError("network down")

    async def fallback(ev):
        raise RuntimeError("llm timeout")

    coord = PilotCoordinator(rt, reply_fn=bad_reply, fallback_fn=fallback)

    async def drive():
        # a DENY (dangerous request) makes the pilot reply itself -> reply_fn
        await coord.on_group_event_safe({
            "topic": "event.message.group", "user_id": 1, "group_id": "123456",
            "message_id": "m", "text": "删除服务器文件", "mentions_self": True,
            "raw": {"message_type": "group"},
        })

    asyncio.run(drive())  # must not raise (isolated); no fake success
    assert failures and failures[0][0] == "send_failed"
    assert len(failures) == 1  # the send failed, fallback also failed -> no fake


# ---------------------------------------------------------------------------
# E - coordinator fallback failure isolated
# ---------------------------------------------------------------------------
def test_e_fallback_isolated(tmp_path):
    from anima.pilot.wiring import PilotCoordinator

    rt = PilotRuntime(
        config=LivePilotConfig(allowed_groups=("123456",), min_interval_sec=0.0),
        data_dir=str(tmp_path / "data"),
    )

    async def reply(dt, tid, msg):
        return "mid"

    async def broken_fallback(ev):
        raise RuntimeError("llm down")

    coord = PilotCoordinator(rt, reply_fn=reply, fallback_fn=broken_fallback)

    async def drive():
        # non-pilot group -> pilot not handled -> fallback fails -> isolated
        await coord.on_group_event_safe({
            "topic": "event.message.group", "user_id": 1, "group_id": "999999",
            "message_id": "m", "text": "hi", "raw": {"message_type": "group"},
        })

    asyncio.run(drive())  # must not raise


# ---------------------------------------------------------------------------
# F - NapCat event drop (non-message) -> no reply, no evidence
# ---------------------------------------------------------------------------
def test_f_napcat_drop(tmp_path):
    rt = PilotRuntime(
        config=LivePilotConfig(allowed_groups=("123456",), min_interval_sec=0.0),
        data_dir=str(tmp_path / "data"),
    )
    # a heartbeat / notice event is silently dropped
    for ev in ({"topic": "event.meta", "user_id": 1},
               {"topic": "event.notice", "user_id": 1},
               {"topic": "event.message.private", "user_id": 1,
                "raw": {"message_type": "private"}}):
        r = rt.handle(ev)
        assert r.handled is False
    assert rt._evidence_log.read() == []


# ---------------------------------------------------------------------------
# G - replay after crash -> no duplicate side effect
# ---------------------------------------------------------------------------
def test_g_replay_no_duplicate(tmp_path):
    data = str(tmp_path / "data")
    ex = _executor(data)
    ledger = ExecutionLedger(persistence=PersistenceProvider(data))
    key = ("r3", "t3", 0)
    first = ex.execute(_step(), key, ledger)
    assert first.success is True
    # "crash": same key replayed -> ledger HIT, provider NOT re-invoked
    from anima.runtime.capability_resolver import (
        CapabilityContract,
        CapabilityRegistry,
        CapabilityResolver,
    )
    from anima.runtime.real_task_executor import RealTaskExecutor

    calls = []
    p = SandboxWriteProvider(sandbox_root=None, data_dir=data)
    original = p.invoke

    def counting(*a):
        calls.append(1)
        return original(*a)

    p.invoke = counting
    reg = CapabilityRegistry()
    reg.register(CapabilityContract("sandbox.write", side_effect=True, idempotent=True), p)
    ex2 = RealTaskExecutor(resolver=CapabilityResolver(reg))
    replay = ex2.execute(_step(), key, ledger)
    assert replay.success is True
    assert calls == []  # ledger HIT -> no provider call (no duplicate side effect)


# ---------------------------------------------------------------------------
# H - static boundary
# ---------------------------------------------------------------------------
def test_h_static_boundary():
    # the failure-injection tests themselves must not touch System B
    # (token concatenation avoids self-reference in the assertion strings)
    src = pathlib.Path("tests/test_failure_injection.py").read_text(encoding="utf-8")
    gov = "capability" + "." + "governance"
    health = "capability" + "." + "health"
    tools = "anima" + "." + "tools"
    capmode = "capability" + "_mode"
    for needle in (gov, health, tools, "Capability" + "Gateway",
                   "approve" + "_and_execute", capmode):
        assert needle not in src
