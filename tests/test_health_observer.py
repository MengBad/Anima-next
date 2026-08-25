"""F8.8 —— Provider Health Observation → Governance Lifecycle（最小观察闭环）。

授权边界（用户 F8.8 Implementation Authorization）：**Health Observation →
Lifecycle Recording**，不实现健康控制系统：
- ✅ 最小 health signal adapter（``observer.report_unhealthy(capability_id, reason)``）；
- ✅ Lifecycle transition persistence（``LifecycleTransition → JSONL store``）；
- ✅ parity proof（QUARANTINED **不改变** ``resolve()`` / provider selection / execution result）。
- ❌ resolver 读取 lifecycle（=改 F5.4b）；❌ 自动 recovery；❌ override。

强制裁定：
- **R1**：不允许 selection → health feedback loop（observer 只写 lifecycle，绝不读写
  resolver selection；parity 测试钉死 resolve 输出逐字节不变）；
- **R2**：transition 幂等（重复 report 只产生 1 条 transition，第二次起返回 False）。

RED 首测：observer 触发 QUARANTINED 后——① lifecycle state 可见（QUARANTINED）；
② TransitionStore 可 query 到 1 条 {source,target,reason,at}；③ 相同 CapabilityNeed
走 resolve 输出逐字节不变（selection 未被污染）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from anima.capability.governance.lifecycle import LifecycleState
from anima.tools import create_default_registry

PILOT = "code.execute"
ALICE = "user:alice"
REASON = "health observation: provider failing 5/5"


def _stub_run_python(registry, recorder: list):
    """F8.6 同款 hermetic stub：只替换最底层沙箱 handler，call → _invoke 全链路真实。"""
    async def handler(code: str, **kw):
        recorder.append({"code": code, **kw})
        return f"STUB:{code}"

    registry.tools["run_python"].handler = handler


def _make_registry():
    from anima.capability.governance.audit import InMemoryAuditSink

    sink = InMemoryAuditSink()
    registry = create_default_registry(
        None,
        capability_mode=True,
        pilot_capability_allowlist=[ALICE],
        capability_gateway_mode=True,
        governance_audit_sink=sink,
    )
    return registry, sink


def _registry_for(registry):
    """经公开 seam 拿 CapabilityRegistry 只读面（lifecycle_for）。"""
    return registry.capability_gateway.resolver._registry  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# RED 首测：Health Observation → Lifecycle Recording 闭环 + parity
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_health_observation_records_quarantine_and_keeps_selection(tmp_path):
    """闭环证明：report_unhealthy → QUARANTINED 可见 + store 可查 + resolve 输出不变。"""
    from anima.capability.audit.persistent_sink import PersistentAuditSink  # noqa: F401 - 纪律对照
    from anima.capability.ops.observer import HealthObserver
    from anima.capability.ops.transition_store import TransitionStore
    from anima.capability.governance.audit import ResolutionOutcome

    registry, sink = _make_registry()
    reg = _registry_for(registry)
    rec: list = []
    _stub_run_python(registry, rec)

    # 基线：真实入口 resolve 一次，确立 selection 输出
    base_result = await registry.call_capability(PILOT, {"code": "1+1"}, caller=ALICE)
    assert base_result == "STUB:1+1"
    assert len(sink) == 1
    base_event = sink.events[0]
    assert base_event.outcome == ResolutionOutcome.SELECTED
    assert base_event.selected_provider is not None

    # 观察者：健康事实 → lifecycle 记录
    store = TransitionStore(str(tmp_path / "transitions.jsonl"))
    observer = HealthObserver(reg.lifecycle_for, store)
    applied = observer.report_unhealthy(PILOT, reason=REASON)
    assert applied is True

    # ① lifecycle 状态可见（QUARANTINED）
    lifecycle = reg.lifecycle_for(PILOT)
    assert lifecycle is not None
    assert lifecycle.state is LifecycleState.QUARANTINED

    # ② store 可 query 到恰好 1 条 transition
    records = store.query(capability_id=PILOT)
    assert len(records) == 1, f"RED: 期望 1 条 transition 记录，got {len(records)}"
    tr = records[0]
    assert tr.source is LifecycleState.ACTIVE
    assert tr.target is LifecycleState.QUARANTINED
    assert tr.reason == REASON

    # ③ parity：QUARANTINED 不改变 resolve 输出（R1：无 selection feedback loop）
    after_result = await registry.call_capability(PILOT, {"code": "1+1"}, caller=ALICE)
    assert after_result == base_result == "STUB:1+1"
    assert len(sink) == 2
    assert sink.events[1].outcome == ResolutionOutcome.SELECTED      # 未被 QUARANTINED 拦截
    assert sink.events[1].selected_provider == base_event.selected_provider  # 选择未变


# ---------------------------------------------------------------------------
# R2：transition 幂等
# ---------------------------------------------------------------------------
def test_repeat_report_is_idempotent(tmp_path):
    from anima.capability.ops.observer import HealthObserver
    from anima.capability.ops.transition_store import TransitionStore
    from anima.capability.governance.audit import InMemoryAuditSink

    registry = create_default_registry(
        None, capability_mode=True, pilot_capability_allowlist=[ALICE],
        capability_gateway_mode=True, governance_audit_sink=InMemoryAuditSink(),
    )
    reg = _registry_for(registry)
    store = TransitionStore(str(tmp_path / "t.jsonl"))
    observer = HealthObserver(reg.lifecycle_for, store)

    assert observer.report_unhealthy(PILOT, reason=REASON) is True
    assert observer.report_unhealthy(PILOT, reason=REASON) is False   # 幂等
    assert observer.report_unhealthy(PILOT, reason=REASON) is False

    assert len(store.query(capability_id=PILOT)) == 1                  # 只 1 条
    lifecycle = reg.lifecycle_for(PILOT)
    assert lifecycle is not None
    assert lifecycle.state is LifecycleState.QUARANTINED
    # history 只多 1 条 QUARANTINED transition
    quarantines = [t for t in lifecycle.history if t.target is LifecycleState.QUARANTINED]
    assert len(quarantines) == 1


# ---------------------------------------------------------------------------
# 边界保护：未治理 / 终态不误伤
# ---------------------------------------------------------------------------
def test_unregistered_capability_ignored(tmp_path):
    from anima.capability.ops.observer import HealthObserver
    from anima.capability.ops.transition_store import TransitionStore
    from anima.capability.governance.audit import InMemoryAuditSink

    registry = create_default_registry(
        None, capability_mode=True, pilot_capability_allowlist=[ALICE],
        capability_gateway_mode=True, governance_audit_sink=InMemoryAuditSink(),
    )
    reg = _registry_for(registry)
    store = TransitionStore(str(tmp_path / "t.jsonl"))
    observer = HealthObserver(reg.lifecycle_for, store)

    assert observer.report_unhealthy("no.such.capability", reason="x") is False
    assert len(store.query()) == 0
    assert observer.store_failures == 0


def test_terminal_state_not_forced(tmp_path):
    """终态（REMOVED）不因 health 观察被唤醒：返回 False，不 raise、不写 store。"""
    from anima.capability.ops.observer import HealthObserver
    from anima.capability.ops.transition_store import TransitionStore
    from anima.capability.governance.audit import InMemoryAuditSink
    from anima.capability.governance.lifecycle import LifecycleState as LS

    registry = create_default_registry(
        None, capability_mode=True, pilot_capability_allowlist=[ALICE],
        capability_gateway_mode=True, governance_audit_sink=InMemoryAuditSink(),
    )
    reg = _registry_for(registry)
    lifecycle = reg.lifecycle_for(PILOT)
    assert lifecycle is not None
    lifecycle.transition_to(LS.REMOVED, reason="test: deliberate removal")

    store = TransitionStore(str(tmp_path / "t.jsonl"))
    observer = HealthObserver(reg.lifecycle_for, store)
    assert observer.report_unhealthy(PILOT, reason=REASON) is False     # 不强制
    assert len(store.query(capability_id=PILOT)) == 0


# ---------------------------------------------------------------------------
# TransitionStore：持久化契约（process A 写 → process B 读）
# ---------------------------------------------------------------------------
def test_transition_store_persistence_contract(tmp_path):
    from anima.capability.ops.transition_store import CURRENT_SCHEMA_VERSION, TransitionStore
    from anima.capability.governance.lifecycle import LifecycleTransition, LifecycleState as LS

    path = str(tmp_path / "t.jsonl")
    store_a = TransitionStore(path)
    store_a.append(PILOT, LifecycleTransition(LS.ACTIVE, LS.QUARANTINED, REASON, 1.0))

    # 读原始行：envelope 带 schema_version
    with open(path, encoding="utf-8") as f:
        row = json.loads(f.readline().strip())
    assert row["schema_version"] == CURRENT_SCHEMA_VERSION
    assert row["capability_id"] == PILOT
    assert row["transition"]["target"] == LS.QUARANTINED.value

    # process B：新实例读回
    store_b = TransitionStore(path)
    records = store_b.query(capability_id=PILOT)
    assert len(records) == 1
    assert records[0] == LifecycleTransition(LS.ACTIVE, LS.QUARANTINED, REASON, 1.0)


def test_transition_store_retention(tmp_path):
    from anima.capability.ops.transition_store import TransitionStore
    from anima.capability.governance.lifecycle import LifecycleTransition, LifecycleState as LS

    store = TransitionStore(str(tmp_path / "t.jsonl"), max_bytes_per_file=300, retention_days=30)
    for _ in range(5):
        store.append(PILOT, LifecycleTransition(LS.ACTIVE, LS.QUARANTINED, REASON, time.time()))
    # 轮转出分片，且全部可读回
    assert len(list(Path(tmp_path).glob("t.jsonl.*"))) >= 1
    assert len(store.query(capability_id=PILOT)) == 5

    # cleanup：只删超期分片；retention_days=0 关闭
    stale = Path(tmp_path) / "t.jsonl.1"
    stale.write_text("{}", encoding="utf-8")
    old = time.time() - 40 * 86400
    os.utime(stale, (old, old))
    other = Path(tmp_path) / "keep.jsonl"
    other.write_text("keep", encoding="utf-8")
    assert store.cleanup() == 1
    assert not stale.exists()
    assert other.exists()

    store0 = TransitionStore(str(tmp_path / "t0.jsonl"), retention_days=0)
    assert store0.cleanup() == 0


def test_transition_store_query_filters(tmp_path):
    from anima.capability.ops.transition_store import TransitionStore
    from anima.capability.governance.lifecycle import LifecycleTransition, LifecycleState as LS

    store = TransitionStore(str(tmp_path / "t.jsonl"))
    store.append(PILOT, LifecycleTransition(LS.ACTIVE, LS.QUARANTINED, "failing", 1.0))
    store.append(PILOT, LifecycleTransition(LS.QUARANTINED, LS.ACTIVE, "recovered", 2.0))
    store.append("search.web", LifecycleTransition(LS.VALIDATED, LS.ACTIVE, "serving", 3.0))

    assert len(store.query(capability_id=PILOT)) == 2
    assert len(store.query(target=LS.QUARANTINED)) == 1
    assert len(store.query(source=LS.QUARANTINED)) == 1
    assert len(store.query(start=1.5, end=2.5)) == 1
    assert len(store.query(capability_id=PILOT, target=LS.ACTIVE)) == 1


# ---------------------------------------------------------------------------
# 红线延续：持久化失败 ≠ 治理失败（store best-effort）
# ---------------------------------------------------------------------------
def test_store_failure_does_not_block_observation(tmp_path):
    from anima.capability.ops.observer import HealthObserver
    from anima.capability.ops.transition_store import TransitionStore
    from anima.capability.governance.audit import InMemoryAuditSink

    registry = create_default_registry(
        None, capability_mode=True, pilot_capability_allowlist=[ALICE],
        capability_gateway_mode=True, governance_audit_sink=InMemoryAuditSink(),
    )
    reg = _registry_for(registry)

    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")  # 用文件占住目录位 → append 必失败
    store = TransitionStore(str(blocker / "t.jsonl"))
    observer = HealthObserver(reg.lifecycle_for, store)

    assert observer.report_unhealthy(PILOT, reason=REASON) is True   # 治理事实仍生效
    assert reg.lifecycle_for(PILOT).state is LifecycleState.QUARANTINED
    assert observer.store_failures == 1                              # 写失败可观测


# ---------------------------------------------------------------------------
# Governance 纪律：新包单向依赖，不拖入 runtime/tools
# ---------------------------------------------------------------------------
def test_no_import_leak_runtime_tools():
    probe = (
        "import sys\n"
        "import anima.capability.ops\n"
        "leaked = ('anima.runtime' in sys.modules) or ('anima.tools' in sys.modules)\n"
        "print('LEAKED' if leaked else 'CLEAN')\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "CLEAN", f"ops 包拖入了 runtime/tools：{out.stdout.strip()}"
