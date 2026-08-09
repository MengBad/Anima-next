"""F8.7 —— Audit Persistence（ResolutionEvent → Persistent Store）。

验收门（G1–G5）+ 用户红线（持久化失败 ≠ capability execution failure）+ governance 纪律。

最小修改边界（用户 F8.7 Implementation Authorization 已裁定）：
- 允许：新增 ``anima/capability/audit/``（persistent_sink.py / storage.py / query.py）+ 本测试文件
- 禁止：修改 ``capability/governance/audit.py`` / ``runtime/capability_resolver.py`` /
  execution ledger / Gateway routing / ``tools/__init__.py`` / 既有 audit 测试

RED 首测（用户裁定）：失败条件 = 真实入口产生的 ResolutionEvent **无法跨进程持久化读取**
（process A 写入 → process B 新开 store 实例 → ``query(capability_id)`` 应命中）。证明的是
persistence contract（事件行为），不是文件系统 magic（文件存在）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from anima.capability.governance.audit import (
    CandidateRecord,
    ResolutionEvent,
    ResolutionOutcome,
)
from anima.tools import create_default_registry

PILOT = "code.execute"
ALICE = "user:alice"


def make_event(
    *,
    capability_id: str = PILOT,
    execution_key: tuple = ("req-1", PILOT, 0),
    outcome: ResolutionOutcome = ResolutionOutcome.SELECTED,
    selected_provider: str = "code_exec",
    timestamp: float = 1754700000.0,
) -> ResolutionEvent:
    """构造一条可预测的 ResolutionEvent（与真实 Resolver 产出的字段形状一致）。"""
    return ResolutionEvent(
        timestamp=timestamp,
        capability_id=capability_id,
        execution_key=execution_key,
        requested_version=None,
        candidates=(
            CandidateRecord(selected_provider, 1.0, None),
            CandidateRecord("other_provider", 0.5, "not_attempted"),
        ),
        selected_provider=selected_provider,
        metadata_snapshot={"permission": "exec", "timeout": 30, "side_effect": True},
        outcome=outcome,
        error=None,
    )


def _stub_run_python(registry, recorder: list):
    """F8.6 同款 hermetic stub：只替换最底层沙箱 handler，call → _invoke 全链路真实。"""
    async def handler(code: str, **kw):
        recorder.append({"code": code, **kw})
        return f"STUB:{code}"

    registry.tools["run_python"].handler = handler


def _registry_with(sink, *, gateway_mode: bool = True):
    return create_default_registry(
        None,
        capability_mode=True,
        pilot_capability_allowlist=[ALICE],
        capability_gateway_mode=gateway_mode,
        governance_audit_sink=sink,
    )


# ---------------------------------------------------------------------------
# G1 Persistence —— RED 首测（persistence contract，非文件 magic）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_persistence_contract_survives_reopen(tmp_path):
    """RED 首测：process A 真实入口 produce 事件并落盘；process B（新开 store 实例 =
    跨进程重启的最小模拟）读回 query 命中。证明 persistence contract 而非文件存在。
    """
    from anima.capability.audit.persistent_sink import PersistentAuditSink
    from anima.capability.audit.query import AuditQuery

    path = str(tmp_path / "governance_audit.jsonl")

    # process A：真实入口（gateway ON + 持久化 sink）产生 ResolutionEvent
    sink_a = PersistentAuditSink(path)
    registry = _registry_with(sink_a)
    rec: list = []
    _stub_run_python(registry, rec)
    result = await registry.call_capability(PILOT, {"code": "1+1"}, caller=ALICE)
    assert result == "STUB:1+1"  # 执行语义保持（前置不变量，不参与 RED 失败谓词）

    # process B：重新打开 store（模拟进程重启）读回
    sink_b = PersistentAuditSink(path)
    events = AuditQuery(sink_b.store).query(capability_id=PILOT)
    assert len(events) == 1, (
        f"RED: 真实入口产生的 ResolutionEvent 无法跨进程持久化读回（got {len(events)})"
    )
    ev = events[0]
    assert ev.outcome == ResolutionOutcome.SELECTED
    assert ev.execution_key[1] == PILOT
    assert ev.selected_provider is not None


# ---------------------------------------------------------------------------
# G2 Schema —— envelope 带 schema_version；旧行可解析且标注，不抛不丢
# ---------------------------------------------------------------------------
def test_envelope_has_schema_version_on_written_rows(tmp_path):
    from anima.capability.audit.storage import CURRENT_SCHEMA_VERSION, JsonlAuditStore

    store = JsonlAuditStore(str(tmp_path / "a.jsonl"))
    store.append(make_event())
    rows = store.read_rows()
    assert len(rows) == 1
    assert rows[0]["schema_version"] == CURRENT_SCHEMA_VERSION
    assert "event" in rows[0]
    assert "recorded_at" in rows[0]


def test_old_row_without_version_tolerated_and_annotated(tmp_path):
    """G2：无 schema_version 的旧行（字段结构相同）→ 尽力解析 + 计数标注，不抛不丢。"""
    from anima.capability.audit.storage import JsonlAuditStore

    store = JsonlAuditStore(str(tmp_path / "a.jsonl"))
    store.append(make_event())  # 新行（带版本）

    ev = make_event(execution_key=("legacy", PILOT, 0), timestamp=1.0)
    legacy_row = {"recorded_at": 2.0, "event": {
        "timestamp": ev.timestamp,
        "capability_id": ev.capability_id,
        "execution_key": list(ev.execution_key),
        "requested_version": ev.requested_version,
        "candidates": [
            {"provider": c.provider, "score": c.score, "rejected_reason": c.rejected_reason}
            for c in ev.candidates
        ],
        "selected_provider": ev.selected_provider,
        "metadata_snapshot": ev.metadata_snapshot,
        "outcome": ev.outcome.value,
        "error": ev.error,
    }}
    with open(store.primary_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(legacy_row, ensure_ascii=False) + "\n")

    events = store.read_events()          # 不抛、不丢
    assert len(events) == 2
    assert store.unknown_versions == 1    # 标注：1 条未知版本行
    keys = {e.execution_key[0] for e in events}
    assert "legacy" in keys               # 旧行被尽力解析
    assert "req-1" in keys


# ---------------------------------------------------------------------------
# G3 Retention —— 大小轮转 + 天数清理 + 可关
# ---------------------------------------------------------------------------
def test_retention_rotation_on_size(tmp_path):
    from anima.capability.audit.storage import JsonlAuditStore

    store = JsonlAuditStore(str(tmp_path / "a.jsonl"), max_bytes_per_file=300)
    for _ in range(5):
        store.append(make_event())
    # 主文件 + 至少一个轮转文件存在
    assert store.primary_path.exists()
    rotated = sorted(Path(tmp_path).glob("a.jsonl.*"))
    assert len(rotated) >= 1, "超过 max_bytes 后应轮转出分片文件"
    # 全部事件仍可读回（轮转不丢数据）
    events = store.read_events()
    assert len(events) == 5


def test_retention_cleanup_days_and_disabled(tmp_path):
    from anima.capability.audit.storage import JsonlAuditStore

    store = JsonlAuditStore(str(tmp_path / "a.jsonl"), retention_days=30)
    store.append(make_event())

    # 伪造一个超期轮转文件 + 一个未超期轮转文件
    rotated_old = Path(tmp_path) / "a.jsonl.1"
    rotated_old.write_text("{}", encoding="utf-8")
    old = time.time() - 40 * 86400
    os.utime(rotated_old, (old, old))
    rotated_fresh = Path(tmp_path) / "a.jsonl.2"
    rotated_fresh.write_text("{}", encoding="utf-8")

    removed = store.cleanup()
    assert removed == 1                    # 只删超期的
    assert not rotated_old.exists()
    assert rotated_fresh.exists()          # 未超期保留
    assert store.primary_path.exists()     # 活跃主文件永不删

    # retention_days=0 → cleanup 无操作
    store0 = JsonlAuditStore(str(tmp_path / "b.jsonl"), retention_days=0)
    store0.append(make_event())
    stale = Path(tmp_path) / "b.jsonl.1"
    stale.write_text("{}", encoding="utf-8")
    os.utime(stale, (old, old))
    assert store0.cleanup() == 0
    assert stale.exists()


def test_cleanup_only_own_files(tmp_path):
    """安全红线：cleanup 只删本 sink 命名前缀（a.jsonl.*）的文件，不碰目录里别的文件。"""
    from anima.capability.audit.storage import JsonlAuditStore

    store = JsonlAuditStore(str(tmp_path / "a.jsonl"), retention_days=30)
    other = tmp_path / "b.jsonl"
    other.write_text("keep me", encoding="utf-8")
    stale = tmp_path / "a.jsonl.1"
    stale.write_text("{}", encoding="utf-8")
    old = time.time() - 40 * 86400
    os.utime(stale, (old, old))

    store.cleanup()
    assert not stale.exists()
    assert other.exists()                  # 别家文件原样


# ---------------------------------------------------------------------------
# G4 Query / Replay —— 过滤键 + 逐字段重建
# ---------------------------------------------------------------------------
def test_query_filters_by_fields(tmp_path):
    from anima.capability.audit.persistent_sink import PersistentAuditSink
    from anima.capability.audit.query import AuditQuery

    sink = PersistentAuditSink(str(tmp_path / "a.jsonl"))
    sink(make_event(execution_key=("r1", PILOT, 0), outcome=ResolutionOutcome.SELECTED))
    sink(make_event(
        execution_key=("r2", PILOT, 0),
        outcome=ResolutionOutcome.UNAVAILABLE,
        selected_provider=None,
        timestamp=2.0,
    ))
    sink(make_event(
        capability_id="search.web",
        execution_key=("r3", "search.web", 0),
        outcome=ResolutionOutcome.SELECTED,
        selected_provider="web_provider",
        timestamp=3.0,
    ))

    q = AuditQuery(sink.store)
    assert len(q.query(capability_id=PILOT)) == 2
    assert len(q.query(capability_id="search.web")) == 1
    assert len(q.query(outcome=ResolutionOutcome.SELECTED)) == 2
    assert len(q.query(outcome=ResolutionOutcome.UNAVAILABLE)) == 1
    assert len(q.query(selected_provider="web_provider")) == 1
    assert len(q.query(execution_key=("r2", PILOT, 0))) == 1
    assert len(q.query(start=2.5, end=3.5)) == 1     # 时间范围 [start, end]
    assert len(q.query(capability_id=PILOT, outcome=ResolutionOutcome.SELECTED)) == 1


def test_replay_byte_fidelity(tmp_path):
    """G4：replay(execution_key) 重建的事件与写入时逐字段相等（frozen dataclass ==）。"""
    from anima.capability.audit.persistent_sink import PersistentAuditSink
    from anima.capability.audit.query import AuditQuery

    sink = PersistentAuditSink(str(tmp_path / "a.jsonl"))
    original = make_event(
        execution_key=("req-replay", PILOT, 0),
        outcome=ResolutionOutcome.SELECTED,
        selected_provider="code_exec",
        timestamp=1754700123.456,
    )
    sink(original)

    q = AuditQuery(sink.store)
    rebuilt = q.replay(("req-replay", PILOT, 0))
    assert rebuilt is not None
    assert rebuilt == original            # frozen dataclass 逐字段可比较
    assert rebuilt.execution_key == ("req-replay", PILOT, 0)  # tuple 身份还原


# ---------------------------------------------------------------------------
# G5 / 红线 —— fail-open 双通道：持久化失败 ≠ capability execution failure
# ---------------------------------------------------------------------------
def test_fail_open_unwritable_store_counts_failures(tmp_path):
    """G5：不可写路径 → sink 绝不 raise，write_failures 递增（观测不丢）。"""
    from anima.capability.audit.persistent_sink import PersistentAuditSink

    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")  # 用文件占住目录位 → append 必失败
    sink = PersistentAuditSink(str(blocker / "audit.jsonl"))
    sink(make_event())
    assert sink.write_failures == 1
    # 连续失败也绝不 raise、计数持续
    sink(make_event())
    sink(make_event())
    assert sink.write_failures == 3


@pytest.mark.asyncio
async def test_persistence_failure_not_execution_failure(tmp_path):
    """用户红线：resolve() → emit(event) → sink best effort。sink 写失败 ≠ capability 执行失败。"""
    from anima.capability.audit.persistent_sink import PersistentAuditSink

    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    sink = PersistentAuditSink(str(blocker / "audit.jsonl"))

    registry = _registry_with(sink)
    registry._make_code_executor = lambda: (  # type: ignore[assignment]
        (lambda code, execution_key: "OK")  # 确定性执行基底，聚焦 sink 行为
    )
    result = await registry.call_capability(PILOT, {"code": "1+1"}, caller=ALICE)
    assert result == "OK"                  # 业务照常成功
    assert sink.write_failures == 1        # 写失败被吞掉但可观测（双通道）


@pytest.mark.asyncio
async def test_rotation_does_not_block_resolve_hot_path(tmp_path):
    """红线：rotation 不得阻塞 resolve 热路径——max_bytes 极小导致每次 emit 都轮转，
    真实入口 resolve 仍照常成功，且全部事件可读回。"""
    from anima.capability.audit.persistent_sink import PersistentAuditSink
    from anima.capability.audit.query import AuditQuery

    sink = PersistentAuditSink(str(tmp_path / "a.jsonl"), max_bytes_per_file=64)
    registry = _registry_with(sink)
    registry._make_code_executor = lambda: (  # type: ignore[assignment]
        (lambda code, execution_key: "OK")
    )
    for _ in range(4):
        result = await registry.call_capability(PILOT, {"code": "1+1"}, caller=ALICE)
        assert result == "OK"
    assert sink.write_failures == 0
    assert len(AuditQuery(sink.store).query(capability_id=PILOT)) == 4


# ---------------------------------------------------------------------------
# Governance 纪律 —— 新包单向依赖，不拖入 runtime/tools
# ---------------------------------------------------------------------------
def test_no_import_leak_runtime_tools():
    """import anima.capability.audit 后，anima.runtime / anima.tools 不得进入 sys.modules。"""
    probe = (
        "import sys\n"
        "import anima.capability.audit\n"
        "leaked = ('anima.runtime' in sys.modules) or ('anima.tools' in sys.modules)\n"
        "print('LEAKED' if leaked else 'CLEAN')\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd="G:/Anima-next",
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "CLEAN", (
        f"governance audit 包拖入了 runtime/tools：{out.stdout.strip()}"
    )
