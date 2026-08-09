"""F8.9.2 —— filesystem.write Pilot Expansion（第三条治理路径：side-effect capability）。

闭环证明 + 验收门（M1 identity / M2 mixed-mode preservation / M3 no capability_only
regression / Q4 failure parity / C1 side-effect metadata pinning / RED 首测）。

最小修改边界（已裁定）：
- 仅改 ``anima/tools/__init__.py`` 的 ``_GATEWAY_PILOT_CAPABILITIES`` 加 ``"filesystem.write"``
  （Q5 精确豁免，F8.9.1 模式）+ 新增 ``_make_filesystem_write_executor`` + 在
  ``_build_capability_gateway`` 挂 ``bootstrap_filesystem_write``；
- ``anima/tools/capability_fs.py`` 新增 ``FileSystemWriteProvider`` + ``bootstrap_filesystem_write``
  （C1 显式 pin metadata：side_effect=True / idempotent=False / permission=filesystem；Q3 不改
  governance/gateway.py）；
- runtime/capability_resolver、providers/base、governance/*、audit/、ops/、execution_ledger、
  workflow/orchestrator/recovery、constitution、ARCHITECTURE、builtin/file.py 一律不碰。

RED 阶段：本文件首测必须失败于——

    真实入口 ``call_capability("filesystem.write")`` 在
    ``capability_mode=ON / gateway_mode=ON / allowlist 放行`` 下，
    **没有产生任何 filesystem.write 的 ResolutionEvent**（入口尚未把 filesystem.write
    路由进 Governance Event 流）。

GREEN 后：同调用恰好产生 1 条 ``ResolutionEvent(SELECTED)``，且结果与 legacy 逐字节一致
（文件真实写入 <data_dir>/output/），且与 read 的 ``side_effect=False`` 形成诚实对照。
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

FS = "filesystem.write"
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
    """legacy 路径：gateway_mode=OFF，仍走 Line A（write_file 真实执行）。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    return create_default_registry(
        None,
        capability_mode=True,
        pilot_capability_allowlist=[ALICE],
        capability_gateway_mode=False,
    )


# ---------------------------------------------------------------------------
# 闭环证明（RED 首测，C3 谓词）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_filesystem_write_governed_emits_resolution_event_and_parity(monkeypatch, tmp_path):
    """真实入口 filesystem.write 必须进入 Governance Event 流，产出恰好 1 条 SELECTED
    事件，且结果与 legacy 逐字节一致（经过 Gateway 后仍是同一个工具，文件真实写入 output/）。"""
    sink = InMemoryAuditSink()
    reg_g = _governed_registry(monkeypatch, tmp_path, sink)
    reg_l = _legacy_registry(monkeypatch, tmp_path)

    legacy = await reg_l.call_capability(
        FS, {"filename": "out.txt", "content": "hello write"},
        permission_level=LEVEL_USER, caller=ALICE,
    )
    governed = await reg_g.call_capability(
        FS, {"filename": "out.txt", "content": "hello write"},
        permission_level=LEVEL_USER, caller=ALICE,
    )

    # RED: 入口未接 filesystem.write 治理 -> 0 事件 -> 失败。GREEN: 恰好 1 条。
    fs_events = [e for e in sink.events if e.capability_id == FS]
    assert len(fs_events) == 1, f"expected 1 filesystem.write ResolutionEvent, got {len(fs_events)}"
    ev = fs_events[0]
    assert ev.outcome == ResolutionOutcome.SELECTED
    assert ev.selected_provider is not None
    assert ev.execution_key[1] == FS
    # 行为级相等（验收门核心）：经过 Gateway 后仍是同一个工具（真实 write_file 执行）
    assert governed == legacy
    assert governed.startswith("已生成文件:")
    # 文件真实落盘于 <data_dir>/output/（沙箱保真）
    written = tmp_path / "data" / "output" / "out.txt"
    assert written.is_file()
    assert written.read_text(encoding="utf-8") == "hello write"


# ---------------------------------------------------------------------------
# M1：capability identity —— 复用既有 id，不重新声明
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_m1_capability_identity_reused_not_redeclared(monkeypatch, tmp_path):
    """M1：filesystem.write 复用既有 capability id（Line A 早已 declare + write_file 挂
    capability_ids），F8.9.2 不应重新声明一个新 capability。"""
    reg = _legacy_registry(monkeypatch, tmp_path)
    assert FS in reg.capability_index.capabilities()
    tool = reg.tools.get("write_file")
    assert tool is not None
    assert FS in tool.capability_ids
    assert reg.capability_index.capabilities().count(FS) >= 1


# ---------------------------------------------------------------------------
# M2：mixed-mode preservation —— OFF legacy 不变；ON governed 一致
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_m2_mixed_mode_preservation(monkeypatch, tmp_path):
    """M2：gateway OFF 时 legacy write_file 完全不变；gateway ON 时 governed 结果一致。"""
    reg_off = _legacy_registry(monkeypatch, tmp_path)
    off = await reg_off.call_capability(
        FS, {"filename": "a.txt", "content": "x"}, permission_level=LEVEL_USER, caller=ALICE
    )
    assert off.startswith("已生成文件:")

    sink = InMemoryAuditSink()
    reg_on = _governed_registry(monkeypatch, tmp_path, sink)
    on = await reg_on.call_capability(
        FS, {"filename": "a.txt", "content": "x"}, permission_level=LEVEL_USER, caller=ALICE
    )
    assert on == off
    assert any(e.capability_id == FS for e in sink.events)


# ---------------------------------------------------------------------------
# M3：no capability_only regression —— write_file 保持 mixed-mode
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_m3_write_file_stays_mixed_mode(monkeypatch, tmp_path):
    """M3：write_file 不得被隐式改为 capability_only=True（保持 mixed-mode，与 code.execute
    的 capability_only=True 语义区分；禁止让 capability_only=True 成为隐式要求）。"""
    reg = _legacy_registry(monkeypatch, tmp_path)
    tool = reg.tools.get("write_file")
    assert tool is not None
    assert tool.capability_only is False


# ---------------------------------------------------------------------------
# Q4：failure parity —— legacy 与 governed 用户可见行为一致（均安全失败）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_q4_failure_parity_safe_failure(monkeypatch, tmp_path):
    """Q4：filesystem.write 失败（目录穿越 ../x）时，legacy 与 governed 用户可见行为一致
    （均返回安全失败字符串）；governed 失败路径仍产生 filesystem.write ResolutionEvent
    （内部观测更丰富，外部契约稳定）。"""
    reg_l = _legacy_registry(monkeypatch, tmp_path)
    legacy = await reg_l.call_capability(
        FS, {"filename": "../escape.txt", "content": "x"},
        permission_level=LEVEL_USER, caller=ALICE,
    )
    sink = InMemoryAuditSink()
    reg_g = _governed_registry(monkeypatch, tmp_path, sink)
    governed = await reg_g.call_capability(
        FS, {"filename": "../escape.txt", "content": "x"},
        permission_level=LEVEL_USER, caller=ALICE,
    )
    assert legacy == governed
    assert isinstance(legacy, str)
    fs_events = [e for e in sink.events if e.capability_id == FS]
    assert len(fs_events) == 1, f"expected 1 filesystem.write event on failure, got {len(fs_events)}"
    assert fs_events[0].outcome == ResolutionOutcome.SELECTED

    # 对照：成功路径也产生事件
    await reg_g.call_capability(
        FS, {"filename": "ok.txt", "content": "y"},
        permission_level=LEVEL_USER, caller=ALICE,
    )
    assert len([e for e in sink.events if e.capability_id == FS]) == 2


# ---------------------------------------------------------------------------
# C1：side-effect metadata pinning —— 诚实声明 side_effect=True / idempotent=False
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_c1_side_effect_metadata_pinned(monkeypatch, tmp_path):
    """C1：write provider 与治理契约都必须**显式**声明 side_effect=True / idempotent=False
    （不依赖默认值）。对照 read 的 side_effect=False，验证二者诚实区分——治理契约一致性门
    （resolver 校验 provider.side_effect == contract.side_effect）不被触发。
    """
    from anima.tools.capability_fs import (
        FileSystemWriteProvider,
        bootstrap_filesystem_write,
    )
    from anima.runtime.capability_resolver import CapabilityRegistry

    captured = {}

    def _ex(filename, execution_key):
        captured["called"] = (filename, execution_key)
        return "ok"

    provider = FileSystemWriteProvider(
        _ex, capability_id=FS, granted_scopes={"filesystem"}
    )
    # 显式诚实声明：写入有副作用、非幂等（同输入也可能覆写外部状态）
    assert provider.side_effect is True
    assert provider.idempotent is False
    assert provider.granted_scopes == {"filesystem"}

    registry = CapabilityRegistry()
    bootstrap_filesystem_write(registry, executor=_ex)
    # 注册不应因 side_effect/idempotent 不一致而拒绝（resolver 一致性门）
    providers = list(registry.providers_for(FS))
    assert len(providers) == 1
    # 治理契约也应显式 side_effect=True / idempotent=False
    contract = registry.contract_for(FS)
    assert contract.side_effect is True
    assert contract.idempotent is False
