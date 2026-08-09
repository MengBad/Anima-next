"""F8.10.1 —— Evidence Emission Wiring：真实 governed execution → HealthEvidence。

把 F8.10 的事实层接到生产 governed 路径（用户裁定）：

    call_capability()
        ↓
    _governed_capability_route()        ← 唯一 emission owner（tools/__init__.py）
        ↓
    gateway.handle() → ProviderResult
        ↓
    EvidenceAdapter.record() → HealthEvidenceStore（sink 注入）

Q1-Q5 裁定钉死：
- Q1 owner = `_governed_capability_route`（唯一漏斗，同时持有 cap_id/execution_key/
  caller/presult）；provider_id v1 = None（route 不可见，Lifecycle 决策 capability 级）；
- Q2 不接 ExecutionObservation（tools 路径不产它；两个执行事实域不强行统一）；
- Q3 一次 governed execution = 0 或 1 evidence（handle 返回后成功/失败分支各恰好 1 条；
  基础设施异常分支跳过——evidence 永不作 execution blocker）；
- Q4 ResolutionEvent（为什么选）与 HealthEvidence（执行后发生了什么）语义分离，
  各自恰好 1 条，互不干扰；
- Q5 边界诚实：只记 selected/executed/success=presult.success；**不做 metadata
  preview**（避免隐式 schema）；handler 吞失败为成功字符串时，治理层看到的就是
  success=True（当前系统事实；未来另开 Execution Outcome Normalization RFC）。

C1 默认关：`health_evidence_sink=None` → 不建 evidence、不影响 latency、返回值逐字节不变。
C2 精确豁免：tools/__init__.py 仅两处（sink 注入 + route 调用 adapter）。

RED 首测（用户谓词）：注入 fake sink → 真实 call_capability("filesystem.read") →
`expected 1 HealthEvidence, got 0`（当前未接线）。
"""
from __future__ import annotations

import pytest

from anima.capability.governance.audit import InMemoryAuditSink
from anima.tools import (
    LEVEL_USER,
    create_default_registry,
)

FS_READ = "filesystem.read"
FS_WRITE = "filesystem.write"
ALICE = "user:alice"


class _CaptureSink:
    """收集 HealthEvidence 的 fake sink（实现 HealthEvidenceSink.append 协议）。"""

    def __init__(self) -> None:
        self.evidences = []

    def append(self, evidence) -> None:
        self.evidences.append(evidence)


class _BrokenSink:
    """append 必抛（fail-open 测试）。"""

    def append(self, evidence) -> None:
        raise OSError("disk full")


def _mk_file(tmp_path, name: str = "hello.txt", content: str = "hello world") -> None:
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / name).write_text(content, encoding="utf-8")


def _governed_registry(monkeypatch, tmp_path, audit=None, health_sink=None):
    """governed 路径：gateway_mode=ON，data_dir 经 chdir 指向 tmp_path/data（真实执行）。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    return create_default_registry(
        None,
        capability_mode=True,
        pilot_capability_allowlist=[ALICE],
        capability_gateway_mode=True,
        governance_audit_sink=audit,
        health_evidence_sink=health_sink,
    )


# ---------------------------------------------------------------------------
# RED 首测：真实 governed execution → 恰好 1 条 HealthEvidence
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_governed_execution_emits_one_health_evidence(monkeypatch, tmp_path):
    """注入 fake sink → 真实 call_capability("filesystem.read")（真实文件）→ 恰好 1 条
    HealthEvidence（capability_id / execution_key / selected / executed / success）。
    RED：未接线 → expected 1 got 0。GREEN：1 条，且 Q4 分离（ResolutionEvent 也 1 条）。"""
    _mk_file(tmp_path)
    capture = _CaptureSink()
    audit = InMemoryAuditSink()
    reg = _governed_registry(monkeypatch, tmp_path, audit=audit, health_sink=capture)

    result = await reg.call_capability(
        FS_READ, {"filename": "hello.txt"}, permission_level=LEVEL_USER, caller=ALICE
    )
    assert "hello world" in result

    # RED 谓词：expected 1 evidence, got 0（未接线前）
    assert len(capture.evidences) == 1, (
        f"expected 1 HealthEvidence, got {len(capture.evidences)}"
    )
    ev = capture.evidences[0]
    assert ev.capability_id == FS_READ
    assert "'filesystem.read', 0)" in ev.execution_id  # execution_key = (request_id, cap_id, 0)
    assert ev.selected is True
    assert ev.executed is True
    assert ev.success is True
    assert ev.provider_id is None  # Q1：v1 route 不可见 provider_id

    # Q4 分离：ResolutionEvent 恰好 1 条（与 HealthEvidence 各自独立、互不干扰）
    assert len([e for e in audit.events if e.capability_id == FS_READ]) == 1


# ---------------------------------------------------------------------------
# Q5：边界诚实 —— 失败被吞成成功字符串时，success 随 presult.success
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_q5_boundary_honesty_failure_records_success(monkeypatch, tmp_path):
    """读不存在的文件：_invoke 吞 FileNotFoundError → self.call 返回 _SAFE_TOOL_FAILURE
    字符串 → ProviderResult.success=True → evidence.success=True（边界实际发生的事实；
    Q5：治理层看到的就是成功——不偷修 handler 语义、不做 metadata preview）。"""
    capture = _CaptureSink()
    reg = _governed_registry(monkeypatch, tmp_path, health_sink=capture)
    result = await reg.call_capability(
        FS_READ, {"filename": "missing.txt"}, permission_level=LEVEL_USER, caller=ALICE
    )
    assert isinstance(result, str)  # 安全失败字符串，行为不变
    assert len(capture.evidences) == 1
    ev = capture.evidences[0]
    assert ev.selected is True
    assert ev.executed is True
    assert ev.success is True  # 边界事实：调用完成并返回了值
    assert ev.error_type == ""  # Q5：不做字符串匹配分类


# ---------------------------------------------------------------------------
# C1：默认关闭 —— 无 sink 时行为零变化、返回值逐字节一致
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_c1_default_off_no_sink_parity(monkeypatch, tmp_path):
    """无 health_evidence_sink（默认）→ 不建 evidence、不改变返回值（与有 sink 逐字节
    一致）。"""
    _mk_file(tmp_path)
    reg_no = _governed_registry(monkeypatch, tmp_path)
    r_no = await reg_no.call_capability(
        FS_READ, {"filename": "hello.txt"}, permission_level=LEVEL_USER, caller=ALICE
    )

    capture = _CaptureSink()
    reg_yes = _governed_registry(monkeypatch, tmp_path, health_sink=capture)
    r_yes = await reg_yes.call_capability(
        FS_READ, {"filename": "hello.txt"}, permission_level=LEVEL_USER, caller=ALICE
    )
    assert r_no == r_yes
    assert len(capture.evidences) == 1


# ---------------------------------------------------------------------------
# fail-open：evidence sink 抛错绝不阻断执行
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_fail_open_sink_error_does_not_block(monkeypatch, tmp_path):
    """sink.append 抛 OSError → 执行照常成功（evidence 永不作 execution blocker，
    F8 系列 fail-open 原则）；resolution 审计流不受影响。"""
    _mk_file(tmp_path)
    audit = InMemoryAuditSink()
    reg = _governed_registry(monkeypatch, tmp_path, audit=audit, health_sink=_BrokenSink())
    result = await reg.call_capability(
        FS_READ, {"filename": "hello.txt"}, permission_level=LEVEL_USER, caller=ALICE
    )
    assert "hello world" in result
    assert len([e for e in audit.events if e.capability_id == FS_READ]) == 1


# ---------------------------------------------------------------------------
# 通用性：同一漏斗覆盖所有 pilot capability（filesystem.write 也恰好 1 条）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_emission_covers_other_pilot_capability(monkeypatch, tmp_path):
    """filesystem.write 走同一 _governed_capability_route 漏斗 → 也恰好 1 条 evidence
    （emission 是 capability 无关的，接在唯一漏斗上）。"""
    capture = _CaptureSink()
    reg = _governed_registry(monkeypatch, tmp_path, health_sink=capture)
    result = await reg.call_capability(
        FS_WRITE, {"filename": "out.txt", "content": "x"},
        permission_level=LEVEL_USER, caller=ALICE,
    )
    assert result.startswith("已生成文件:")
    assert len(capture.evidences) == 1
    assert capture.evidences[0].capability_id == FS_WRITE
    assert capture.evidences[0].success is True
