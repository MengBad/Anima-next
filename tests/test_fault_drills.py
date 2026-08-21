"""P9.5 Phase 4 — fault drills (RED -> GREEN).

Drills (must never produce a false success / side effect):
  1. kill switch disable -> new traffic rejected, old evidence preserved
  2. provider failure injection -> FAILED, never a fake success
  3. permission denial -> DENIED, never executed
  4. replay duplicate -> no duplicate side effect (idempotency conserved)
"""

import asyncio
import pathlib

import pytest

from anima.pilot.pilot_runtime import LivePilotConfig, PilotRuntime

# ---------------------------------------------------------------------------
# 1 - kill switch disable: new traffic rejected, old evidence preserved
# ---------------------------------------------------------------------------


def test_drill1_kill_switch(tmp_path):
    rt = PilotRuntime(
        config=LivePilotConfig(allowed_groups=("123456",), min_interval_sec=0.0),
        data_dir=str(tmp_path / "data"),
    )
    # admit one message while the switch is up -> evidence recorded
    r1 = rt.handle(_ev())
    assert r1.denied_reason == "llm_answer"  # gate passed, LLM answers
    assert len(rt._evidence_log.read()) == 1

    # flip the kill switch off -> everything denied, no new evidence
    class _Off:
        def check(self):
            raise RuntimeError("disabled")

    rt._kill_switch = _Off()
    r2 = rt.handle(_ev(message_id="m-2"))
    assert r2.handled is False
    assert r2.denied_reason == "kill_switch_disabled"
    # old evidence preserved, nothing new appended
    assert len(rt._evidence_log.read()) == 1


# ---------------------------------------------------------------------------
# 2 - provider failure injection: FAILED, never a fake success
# ---------------------------------------------------------------------------


def test_drill2_provider_failure(tmp_path):
    # the coordinator's fallback raises -> safe wrapper must not fake success
    from anima.pilot.wiring import PilotCoordinator

    class _BrokenRuntime:
        def handle(self, event):
            raise RuntimeError("provider down")

    calls = {"fallback": 0, "reply": 0}

    async def fallback(ev):
        calls["fallback"] += 1
        raise RuntimeError("LLM also down")

    async def reply(dt, tid, msg):
        calls["reply"] += 1

    coord = PilotCoordinator(_BrokenRuntime(), reply_fn=reply, fallback_fn=fallback)
    # the safe wrapper must swallow both failures WITHOUT sending a fake reply
    asyncio.run(coord.on_group_event_safe(_ev()))
    assert calls["reply"] == 0  # no fake success reply
    assert calls["fallback"] == 1  # fallback was attempted once


# ---------------------------------------------------------------------------
# 3 - permission denial: DENIED, never executed
# ---------------------------------------------------------------------------


def test_drill3_permission_denial(tmp_path):
    rt = PilotRuntime(
        config=LivePilotConfig(allowed_groups=("123456",), min_interval_sec=0.0),
        data_dir=str(tmp_path / "data"),
    )
    r = rt.handle(_ev(text="删除服务器文件"))
    assert r.handled is True
    assert "不在允许范围" in r.reply
    assert rt._evidence_log.read()[0]["result"] == "DENIED"
    # no execution happened: no ledger facts / no sandbox files
    assert not list((pathlib.Path(tmp_path) / "data").rglob("*.json")) or \
        all("execution" not in p.name for p in
            (pathlib.Path(tmp_path) / "data").rglob("*.json"))


# ---------------------------------------------------------------------------
# 4 - replay duplicate: no duplicate side effect
# ---------------------------------------------------------------------------


def test_drill4_replay_duplicate(tmp_path):
    from anima.core.persistence import PersistenceProvider
    from anima.runtime.execution_ledger import ExecutionLedger
    from anima.runtime.executor import ExecutionStep
    from anima.runtime.providers.sandbox_write_provider import SandboxWriteProvider
    from anima.runtime.capability_resolver import (
        CapabilityContract,
        CapabilityRegistry,
        CapabilityResolver,
    )
    from anima.runtime.real_task_executor import RealTaskExecutor

    data_dir = str(tmp_path / "data")
    reg = CapabilityRegistry()
    invokes = {"n": 0}

    class _CountingProvider(SandboxWriteProvider):
        def invoke(self, *args, **kwargs):
            invokes["n"] += 1
            return super().invoke(*args, **kwargs)

    reg.register(
        CapabilityContract("sandbox.write", side_effect=True, idempotent=True),
        _CountingProvider(sandbox_root=None, data_dir=data_dir),
    )
    ex = RealTaskExecutor(resolver=CapabilityResolver(reg))
    ledger = ExecutionLedger(persistence=PersistenceProvider(data_dir))

    step = ExecutionStep(
        id="t-1", title="x", intent="x",
        capability_need={"capability": "sandbox.write",
                         "args": {"path": "f1.txt", "content": "c1"},
                         "context": {}},
        depends_on=[], estimates={},
    )
    key = ("run-1", "t-1", 0)
    obs1 = ex.execute(step, key, ledger)
    assert obs1.success is True
    target = pathlib.Path(data_dir) / "sandbox_write" / "f1.txt"
    assert target.exists()
    assert invokes["n"] == 1  # provider invoked exactly once so far

    # replay the SAME key 50 times -> provider is NEVER re-invoked (ledger
    # idempotency hit), file unchanged, no duplicate side effect
    content = target.read_text(encoding="utf-8")
    for _ in range(50):
        obs = ex.execute(step, key, ledger)
        assert obs.success is True
    assert invokes["n"] == 1  # idempotency conserved: one provider write total
    assert target.read_text(encoding="utf-8") == content  # unchanged
    assert target.exists()


# ---------------------------------------------------------------------------
# 5 - identity isolation under replay (no identity confusion)
# ---------------------------------------------------------------------------


def test_drill5_identity_isolation(tmp_path):
    from anima.pilot.identity_correlator import IdentityCorrelator

    corr = IdentityCorrelator()
    c1 = corr.bind(group_id="123456", user_id="10001", request_id="r1")
    c2 = corr.bind(group_id="123456", user_id="20002", request_id="r2")
    # different users -> different EXECUTION identities, never merged; the
    # authz_id is scope+group derived (P8.3) and may coincide for the same
    # group — execution identity is what isolates the side effects.
    assert c1.execution_key != c2.execution_key
    assert c1.intent_id != c2.intent_id
    assert c1.evidence_id != c2.evidence_id
    assert "10001" not in [str(x) for x in c2.execution_key]
    assert "20002" not in [str(x) for x in c1.execution_key]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _ev(**overrides):
    ev = {
        "topic": "event.message.group",
        "user_id": 10001,
        "group_id": "123456",
        "message_id": "m-1",
        "text": "什么是量子计算？",
        "time": 1786450000,
        "mentions_self": True,
        "raw": {"message_type": "group", "sender": {"nickname": "u1"}},
    }
    ev.update(overrides)
    return ev
