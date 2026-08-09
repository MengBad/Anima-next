"""F8.9.1 —— filesystem.read Pilot Expansion（第二条治理路径：sync provider）。

闭环证明 + 验收门（M1 identity / M2 mixed-mode preservation / M3 no capability_only
regression / Q4 failure parity / RED 首测）。

最小修改边界（已裁定）：
- 仅改 ``anima/tools/__init__.py`` 的 ``call_capability`` pilot routing 分支（Q2 精确豁免）
  + 新增 ``_make_filesystem_executor`` + 在 ``_build_capability_gateway`` 挂
  ``bootstrap_filesystem_read``；
- 新文件 ``anima/tools/capability_fs.py``（tools 层 adapter，不碰 ``governance/gateway.py``，
  Q3）；
- runtime/capability_resolver、providers/base、governance/*、audit/、ops/、execution_ledger、
  workflow/orchestrator/recovery、constitution、ARCHITECTURE、builtin/file.py 一律不碰。

RED 阶段：本文件首测必须失败于——

    真实入口 ``call_capability("filesystem.read")`` 在
    ``capability_mode=ON / gateway_mode=ON / allowlist 放行`` 下，
    **没有产生任何 filesystem.read 的 ResolutionEvent**（入口尚未把 filesystem.read
    路由进 Governance Event 流）。

GREEN 后：同调用恰好产生 1 条 ``ResolutionEvent(SELECTED)``，且结果与 legacy 逐字节一致。
"""
from __future__ import annotations

import pytest

from anima.capability.governance.audit import (
    InMemoryAuditSink,
    ResolutionOutcome,
)
from anima.tools import (
    LEVEL_USER,
    create_default_registry,
)

FS = "filesystem.read"
ALICE = "user:alice"


def _governed_registry(monkeypatch, tmp_path, sink):
    """governed 路径：gateway_mode=ON，data_dir 经 chdir 指向 tmp_path/data（真实执行）。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    return create_default_registry(
        None,
        capability_mode=True,
        pilot_capability_allowlist=[ALICE],
        capability_gateway_mode=True,
        governance_audit_sink=sink,
    )


def _legacy_registry(monkeypatch, tmp_path):
    """legacy 路径：gateway_mode=OFF，仍走 Line A（read_file 真实执行）。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    return create_default_registry(
        None,
        capability_mode=True,
        pilot_capability_allowlist=[ALICE],
        capability_gateway_mode=False,
    )


# ---------------------------------------------------------------------------
# 闭环证明（RED 首测）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_filesystem_read_governed_emits_resolution_event_and_parity(monkeypatch, tmp_path):
    """真实入口 filesystem.read 必须进入 Governance Event 流，产出恰好 1 条 SELECTED
    事件，且结果与 legacy 逐字节一致（经过 Gateway 后仍是同一个工具）。"""
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "hello.txt").write_text("hello world", encoding="utf-8")
    sink = InMemoryAuditSink()
    reg_g = _governed_registry(monkeypatch, tmp_path, sink)
    reg_l = _legacy_registry(monkeypatch, tmp_path)

    legacy = await reg_l.call_capability(
        FS, {"filename": "hello.txt"}, permission_level=LEVEL_USER, caller=ALICE
    )
    governed = await reg_g.call_capability(
        FS, {"filename": "hello.txt"}, permission_level=LEVEL_USER, caller=ALICE
    )

    # RED: 入口未接 filesystem.read 治理 -> 0 事件 -> 失败。GREEN: 恰好 1 条。
    fs_events = [e for e in sink.events if e.capability_id == FS]
    assert len(fs_events) == 1, f"expected 1 filesystem.read ResolutionEvent, got {len(fs_events)}"
    ev = fs_events[0]
    assert ev.outcome == ResolutionOutcome.SELECTED
    assert ev.selected_provider is not None
    assert ev.execution_key[1] == FS
    # 行为级相等（验收门核心）：经过 Gateway 后仍是同一个工具（真实 read_file 执行）
    assert governed == legacy == "hello world", f"legacy {legacy!r} != governed {governed!r}"


# ---------------------------------------------------------------------------
# M1：capability identity —— 复用既有 id，不重新声明
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_m1_capability_identity_reused_not_redeclared(monkeypatch, tmp_path):
    """M1：filesystem.read 复用既有 capability id（Line A 早已 declare + read_file 挂
    capability_ids），F8.9.1 不应重新声明一个新 capability。"""
    reg = _legacy_registry(monkeypatch, tmp_path)
    # 既有 read_file Tool 早已挂 capability_ids=["filesystem.read"]
    assert FS in reg.capability_index.capabilities()
    tool = reg.tools.get("read_file")
    assert tool is not None
    assert FS in tool.capability_ids
    # 既有的 filesystem.read contract 由 declare_filesystem_read_contract 注册（Line A）
    assert reg.capability_index.capabilities().count(FS) >= 1


# ---------------------------------------------------------------------------
# M2：mixed-mode preservation —— OFF legacy 不变；ON governed 一致
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_m2_mixed_mode_preservation(monkeypatch, tmp_path):
    """M2：gateway OFF 时 legacy read_file 完全不变；gateway ON 时 governed 结果一致。"""
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "note.txt").write_text("content-123", encoding="utf-8")

    # OFF：legacy 直接走 Line A，结果与真实文件内容一致
    reg_off = _legacy_registry(monkeypatch, tmp_path)
    off = await reg_off.call_capability(
        FS, {"filename": "note.txt"}, permission_level=LEVEL_USER, caller=ALICE
    )
    assert off == "content-123"

    # ON：governed 走 Gateway，结果与 legacy 一致，且产生 ResolutionEvent
    sink = InMemoryAuditSink()
    reg_on = _governed_registry(monkeypatch, tmp_path, sink)
    on = await reg_on.call_capability(
        FS, {"filename": "note.txt"}, permission_level=LEVEL_USER, caller=ALICE
    )
    assert on == off == "content-123"
    assert any(e.capability_id == FS for e in sink.events)


# ---------------------------------------------------------------------------
# M3：no capability_only regression —— read_file 保持 mixed-mode
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_m3_read_file_stays_mixed_mode(monkeypatch, tmp_path):
    """M3：read_file 不得被隐式改为 capability_only=True（保持 mixed-mode，与 code.execute
    的 capability_only=True 语义区分；禁止让 capability_only=True 成为隐式要求）。"""
    reg = _legacy_registry(monkeypatch, tmp_path)
    tool = reg.tools.get("read_file")
    assert tool is not None
    assert tool.capability_only is False


# ---------------------------------------------------------------------------
# Q4：failure parity —— legacy 与 governed 用户可见行为一致（均安全失败）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_q4_failure_parity_safe_failure(monkeypatch, tmp_path):
    """Q4：filesystem.read 失败（文件不存在）时，legacy 与 governed 用户可见行为一致
    （均返回安全失败字符串，不泄露内部错误）；governed 失败路径仍产生 filesystem.read
    ResolutionEvent（内部观测更丰富，外部契约稳定）。

    注：真实 ``self.call`` 会在 ``_invoke`` 捕获 handler 异常后返回安全失败字符串（不向上
    抛出），因此治理能力视作 SELECTED（provider 成功返回了结果），与真实 code.execute 在
    ``self.call`` 捕获异常时的行为一致——这正是 Q4「审计中保留 ResolutionEvent」的体现。
    """
    # legacy failure（gateway OFF）
    reg_l = _legacy_registry(monkeypatch, tmp_path)
    legacy = await reg_l.call_capability(
        FS, {"filename": "missing.txt"}, permission_level=LEVEL_USER, caller=ALICE
    )
    # governed failure（gateway ON）
    sink = InMemoryAuditSink()
    reg_g = _governed_registry(monkeypatch, tmp_path, sink)
    governed = await reg_g.call_capability(
        FS, {"filename": "missing.txt"}, permission_level=LEVEL_USER, caller=ALICE
    )
    # 用户可见行为一致（均为安全失败，不泄露内部错误）
    assert legacy == governed
    assert isinstance(legacy, str)
    # governed 失败路径仍产生 filesystem.read ResolutionEvent（内部观测更丰富）
    fs_events = [e for e in sink.events if e.capability_id == FS]
    assert len(fs_events) == 1, f"expected 1 filesystem.read event on failure, got {len(fs_events)}"
    assert fs_events[0].outcome == ResolutionOutcome.SELECTED

    # 对照：成功路径也产生事件（失败/成功均进入治理观测流）
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "ok.txt").write_text("ok", encoding="utf-8")
    await reg_g.call_capability(
        FS, {"filename": "ok.txt"}, permission_level=LEVEL_USER, caller=ALICE
    )
    assert len([e for e in sink.events if e.capability_id == FS]) == 2
