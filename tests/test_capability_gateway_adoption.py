"""F8.6 —— Capability Governance Adoption（pilot production path）。

闭环证明 + 验收门（Routing / Security / Result / Audit / Rollback）。

最小修改边界（已裁定）：实现仅在 ``anima/tools/__init__.py``（adapter / bridge /
pilot gate / audit sink wiring）+ ``anima/capability/governance/``（gateway adapter
support，本文件不依赖其改动）。``runtime/capability_resolver``、provider contracts、
execution ledger、workflow/orchestrator/recovery、constitution 一律不碰。

RED 阶段：本文件首测必须失败于——

    真实入口 ``call_capability("code.execute")`` 在
    ``capability_mode=ON / gateway_mode=ON / allowlist 放行`` 下，
    **没有产生任何 ResolutionEvent**（入口尚未把请求路由进 Governance Event 流）。

GREEN 后：同调用恰好产生 1 条 ``ResolutionEvent(SELECTED)``，且结果与 legacy 一致。
"""
from __future__ import annotations

import pytest

from anima.capability.governance.audit import (
    InMemoryAuditSink,
    ResolutionOutcome,
)
from anima.tools import (
    LEVEL_OWNER,
    LEVEL_USER,
    _SAFE_TOOL_FAILURE,
    create_default_registry,
)

PILOT = "code.execute"
ALICE = "user:alice"
BOB = "user:bob"


def _stub_run_python(registry, recorder: list):
    """把 run_python 的 handler 换成确定性替身：记录 ``_invoke`` 实际下发的 args。

    只替换沙箱本体，整条 ``call → _invoke``（capability-only 守卫 / exec scope 门 /
    ctx 权限覆盖 / execution 审计）仍然真实执行——因此两条路径的比对是忠实的，
    同时不依赖宿主沙箱可用性。
    """
    async def handler(code: str, **kw):
        recorder.append({"code": code, **kw})
        return f"STUB:{code}"

    registry.tools["run_python"].handler = handler


def _fake_executor(data: str):
    """返回一个 0-arg 工厂：固定返回 data，不走真实 run_python（确定性）。"""
    def factory() -> "Callable[[str, tuple], str]":
        def executor(code: str, execution_key: tuple) -> str:
            return data
        return executor
    return factory


def _spy_call(recorder: list, returns: str = "RESULT"):
    """返回一个 instance-bound ``call`` 替身：记录 (name, args, level) 并返回确定性值。"""
    async def spy(name, args, permission_level=LEVEL_USER, *, _via_capability=False):
        recorder.append((name, dict(args), permission_level))
        return returns
    return spy


# ---------------------------------------------------------------------------
# 闭环证明（RED 首测）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_entry_produces_governed_resolution_event():
    """真实入口 code.execute 必须进入 Governance Event 流，产出恰好 1 条 SELECTED 事件。"""
    sink = InMemoryAuditSink()
    registry = create_default_registry(
        None,
        capability_mode=True,
        pilot_capability_allowlist=[ALICE],
        capability_gateway_mode=True,
        governance_audit_sink=sink,
    )
    registry._make_code_executor = _fake_executor("2")  # 确定性，不依赖沙箱
    result = await registry.call_capability(PILOT, {"code": "1+1"}, caller=ALICE)
    # RED: 入口未接治理 -> 0 事件 -> 失败。GREEN: 恰好 1 条。
    assert len(sink) == 1, f"expected exactly 1 governed ResolutionEvent, got {len(sink)}"
    ev = sink.events[0]
    assert ev.capability_id == PILOT
    assert ev.outcome == ResolutionOutcome.SELECTED
    assert ev.selected_provider is not None
    assert ev.execution_key[1] == PILOT
    assert result == "2"


# ---------------------------------------------------------------------------
# Routing 门
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_gateway_mode_off_keeps_line_a_path():
    """gateway_mode=OFF：code.execute 走 Line A legacy，不产生 governed 事件。"""
    sink = InMemoryAuditSink()
    registry = create_default_registry(
        None, capability_mode=True, pilot_capability_allowlist=[ALICE],
        capability_gateway_mode=False, governance_audit_sink=sink,
    )
    registry._make_code_executor = _fake_executor("legacy-fake")
    rec: list = []
    _stub_run_python(registry, rec)
    result = await registry.call_capability(PILOT, {"code": "1+1"}, caller=ALICE)
    assert len(sink) == 0                      # 未进入治理闭环
    assert result == "STUB:1+1"                # Line A 真实抵达执行基底
    assert result != "legacy-fake"             # 证明走的是 Line A，而非 gateway fake


@pytest.mark.asyncio
async def test_gateway_mode_on_enters_gateway():
    """gateway_mode=ON + code.execute：进入 Gateway（治理闭环）。"""
    sink = InMemoryAuditSink()
    registry = create_default_registry(
        None, capability_mode=True, pilot_capability_allowlist=[ALICE],
        capability_gateway_mode=True, governance_audit_sink=sink,
    )
    registry._make_code_executor = _fake_executor("ok")
    await registry.call_capability(PILOT, {"code": "1+1"}, caller=ALICE)
    assert len(sink) == 1
    assert sink.events[0].outcome == ResolutionOutcome.SELECTED


# ---------------------------------------------------------------------------
# Security 门：principal authorization 在 gateway governance 之前
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_principal_gate_before_gateway():
    """不在 allowlist -> 即便 gateway_mode=ON 也 deny，且不产生 ResolutionEvent。"""
    sink = InMemoryAuditSink()
    registry = create_default_registry(
        None, capability_mode=True, pilot_capability_allowlist=[ALICE],
        capability_gateway_mode=True, governance_audit_sink=sink,
    )
    registry._make_code_executor = _fake_executor("x")
    result = await registry.call_capability(PILOT, {"code": "1+1"}, caller=BOB)
    assert len(sink) == 0                                   # 未进入治理闭环
    assert "未获治理放行" in result                          # 走 Line A deny 文案


# ---------------------------------------------------------------------------
# Result 门：legacy output == governed output
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_result_parity_legacy_vs_governed():
    """Result 门：同 code + 同 caller，legacy 与 governed 的**输出**与**下发参数**都一致。

    两条路径都真实走完 ``registry.call → _invoke``（exec gate / ctx 覆盖 / 执行审计
    全部生效），只把最底层的沙箱本体换成确定性替身——因此断言既忠实又不受宿主沙箱
    环境影响（真实 run_python 在本仓已有已知的宿主级 temp-file 编码缺陷）。
    """
    code = "print(1+1)"
    # legacy（gateway OFF → Line A → code_exec_handler → run_python）
    rec_l: list = []
    reg_l = create_default_registry(
        None, capability_mode=True, pilot_capability_allowlist=[ALICE],
        capability_gateway_mode=False,
    )
    _stub_run_python(reg_l, rec_l)
    res_l = await reg_l.call_capability(PILOT, {"code": code}, caller=ALICE)

    # governed（gateway ON → Gateway → Provider → bridge → run_python）
    rec_g: list = []
    sink = InMemoryAuditSink()
    reg_g = create_default_registry(
        None, capability_mode=True, pilot_capability_allowlist=[ALICE],
        capability_gateway_mode=True, governance_audit_sink=sink,
    )
    _stub_run_python(reg_g, rec_g)
    res_g = await reg_g.call_capability(PILOT, {"code": code}, caller=ALICE)

    # 行为级相等（验收门核心）
    assert res_l == res_g == f"STUB:{code}", f"legacy {res_l!r} != governed {res_g!r}"
    # 结构级：下发给执行基底的参数逐字节一致（含 _invoke 覆盖后的 permission_level）
    assert len(rec_l) == 1 and len(rec_g) == 1
    assert rec_l == rec_g, f"legacy args {rec_l} != governed args {rec_g}"
    assert rec_g[0]["code"] == code
    assert rec_g[0]["permission_level"] == LEVEL_USER
    assert len(sink) == 1


@pytest.mark.asyncio
async def test_call_context_survives_bridge_for_sandbox_tier():
    """R3（context fidelity）：可信 ctx 必须穿过桥接线程，否则 sandbox tier 静默降级。

    ``_invoke`` 对 run_python 会**无条件用 call context 覆盖** permission_level
    （M0.4.3-R1 反特权注入）。桥接若在裸线程里跑协程，ContextVar 为空 → owner 级请求
    会被降级成 user 级沙箱，与 legacy 分叉。本测试对两条路径同时施加 owner ctx 比对。
    """
    code = "print(1+1)"
    rec_l: list = []
    reg_l = create_default_registry(
        None, capability_mode=True, pilot_capability_allowlist=[ALICE],
        capability_gateway_mode=False,
    )
    _stub_run_python(reg_l, rec_l)
    tok = reg_l.set_call_context(user_id="alice", permission_level=LEVEL_OWNER)
    try:
        await reg_l.call_capability(PILOT, {"code": code}, caller=ALICE)
    finally:
        reg_l.reset_call_context(tok)

    rec_g: list = []
    reg_g = create_default_registry(
        None, capability_mode=True, pilot_capability_allowlist=[ALICE],
        capability_gateway_mode=True,
    )
    _stub_run_python(reg_g, rec_g)
    tok = reg_g.set_call_context(user_id="alice", permission_level=LEVEL_OWNER)
    try:
        await reg_g.call_capability(PILOT, {"code": code}, caller=ALICE)
    finally:
        reg_g.reset_call_context(tok)

    assert rec_l[0]["permission_level"] == LEVEL_OWNER
    assert rec_g[0]["permission_level"] == LEVEL_OWNER, "桥接丢失可信 ctx：沙箱分级被降级"
    assert rec_l == rec_g


# ---------------------------------------------------------------------------
# Audit 门：one request = one ResolutionEvent（禁止 0 / 2+）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_exactly_one_event_no_duplicate():
    sink = InMemoryAuditSink()
    registry = create_default_registry(
        None, capability_mode=True, pilot_capability_allowlist=[ALICE],
        capability_gateway_mode=True, governance_audit_sink=sink,
    )
    registry._make_code_executor = _fake_executor("ok")
    await registry.call_capability(PILOT, {"code": "1+1"}, caller=ALICE)
    assert len(sink) == 1
    assert sink.events[0].outcome == ResolutionOutcome.SELECTED


@pytest.mark.asyncio
async def test_execution_key_stable_through_pipeline():
    """E 不变量：adapter 生成的 request_id 原样透传进 ResolutionEvent（I3）。"""
    sink = InMemoryAuditSink()
    registry = create_default_registry(
        None, capability_mode=True, pilot_capability_allowlist=[ALICE],
        capability_gateway_mode=True, governance_audit_sink=sink,
    )
    registry._make_code_executor = _fake_executor("ok")
    registry._next_governed_request_id = lambda: "req-123"  # type: ignore[assignment]
    await registry.call_capability(PILOT, {"code": "1+1"}, caller=ALICE)
    assert len(sink) == 1
    assert sink.events[0].execution_key == ("req-123", PILOT, 0)


# ---------------------------------------------------------------------------
# R2：async/sync bridge —— 线程生命周期 + 异常传播
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_bridge_runs_async_execution_in_thread():
    """bridge 在独立线程跑 async run_python 并返回结果（线程生命周期正常）。"""
    sink = InMemoryAuditSink()
    registry = create_default_registry(
        None, capability_mode=True, pilot_capability_allowlist=[ALICE],
        capability_gateway_mode=True, governance_audit_sink=sink,
    )
    captured: dict = {}
    async def spy(name, args, permission_level=LEVEL_USER, *, _via_capability=False):
        captured["name"] = name
        return "BRIDGED"
    registry.call = spy  # type: ignore[assignment]
    result = await registry.call_capability(PILOT, {"code": "1+1"}, caller=ALICE)
    assert result == "BRIDGED"
    assert captured["name"] == "run_python"
    assert len(sink) == 1


@pytest.mark.asyncio
async def test_bridge_propagates_exception_as_safe_failure():
    """bridge 中 async 执行抛异常 -> 传播到 adapter -> 安全失败字符串，不崩溃。"""
    sink = InMemoryAuditSink()
    registry = create_default_registry(
        None, capability_mode=True, pilot_capability_allowlist=[ALICE],
        capability_gateway_mode=True, governance_audit_sink=sink,
    )
    async def spy(name, args, permission_level=LEVEL_USER, *, _via_capability=False):
        raise RuntimeError("boom")
    registry.call = spy  # type: ignore[assignment]
    result = await registry.call_capability(PILOT, {"code": "1+1"}, caller=ALICE)
    assert isinstance(result, str)
    assert result == _SAFE_TOOL_FAILURE     # 安全失败，不崩溃
    assert len(sink) == 0                   # 失败中途抛出，无 SELECTED 事件


# ---------------------------------------------------------------------------
# Rollback 门：feature gate OFF 是有效回退路径（非死代码）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_rollback_gate_off_is_valid_fallback():
    """gateway_mode=OFF 下 code.execute 仍可经 Line A 正常执行（gate 是活回退）。"""
    sink = InMemoryAuditSink()
    registry = create_default_registry(
        None, capability_mode=True, pilot_capability_allowlist=[ALICE],
        capability_gateway_mode=False, governance_audit_sink=sink,
    )
    registry._make_code_executor = _fake_executor("ignored")
    rec: list = []
    _stub_run_python(registry, rec)
    result = await registry.call_capability(PILOT, {"code": "1+1"}, caller=ALICE)
    # 走 Line A：真实抵达执行基底（gate OFF 不是死代码，业务仍可执行）。
    assert result == "STUB:1+1"
    assert result != "ignored"
    assert len(rec) == 1
    assert len(sink) == 0
