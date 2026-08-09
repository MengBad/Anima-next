"""F8.5 —— Capability Gateway 闭环证明 + code.execute bootstrap + coverage 真实化。

纪律（F8.5 授权范围，冻结边界外一律不碰）：
- 仅测 Gateway（新建）+ code.execute bootstrap（注册进 Line B CapabilityRegistry）
  + F8.4 coverage 真实化；
- 不替换 Line A、不改 Line B resolver、不改 execution_key/ledger/fallback、
  不改 capability_mode 默认值；
- governance 包零 runtime import 泄漏（gateway 以惰性 import 守住）。

头号测试 ``test_production_code_execute_request_produces_governed_resolution_event``
是**闭环证明测试**：它必须因「生产 capability request 无法产生 governed
resolution event」而失败（RED 阶段因 gateway 模块尚不存在 → ImportError），
而非普通单测。GREEN 后它证明治理链第一次接住真实流量。
"""
from __future__ import annotations

import pytest

from anima.capability.governance import (
    CapabilityGovernanceView,
    GovernanceReachability,
    InMemoryAuditSink,
    ResolutionOutcome,
)
from anima.capability.governance.gateway import (
    CapabilityGateway,
    CodeExecuteProvider,
    bootstrap_code_execute,
)
from anima.runtime.capability_resolver import (
    CapabilityNeed,
    CapabilityRegistry,
)
from anima.runtime.capability_resolver import CapabilityNotFound
from anima.runtime.providers.base import ProviderResult


def _real_executor(code: str, execution_key: tuple) -> dict:
    """真实执行（F8.5 证明用）：实跑代码并返回结构化结果，证明治理链接的是真流量。

    这里刻意用真实执行而非 stub——闭环证明的价值正在于「接住的是真实能力请求」。
    生产接线（把真实 run_python 注入 executor）属后续 slice，本测试只证明治理闭环
    本身可端到端接住一次真实 code.execute 请求。
    """
    namespace: dict = {}
    exec(f"__anima_out__ = {code}", namespace)  # noqa: S102 - 受控测试执行
    return {"result": namespace.get("__anima_out__")}


def _governed_code_execute() -> tuple[CapabilityRegistry, CapabilityGateway, InMemoryAuditSink]:
    sink = InMemoryAuditSink()
    registry = CapabilityRegistry()
    bootstrap_code_execute(registry, executor=_real_executor)
    gateway = CapabilityGateway(registry, audit_sink=sink)
    return registry, gateway, sink


# ==========================================================================
# 头号：闭环证明测试（F8 完成判据）
# ==========================================================================
def test_production_code_execute_request_produces_governed_resolution_event():
    """F8 完成判据：一条真实 code.execute 请求经 Gateway 走完整治理闭环
    declare→canonicalize→validate→enforce→resolve→audit→execute，
    并产生可审计的 ResolutionEvent（outcome=SELECTED, selected_provider 非空）。

    此测试是闭环证明而非普通单测：RED 阶段它因 gateway 不存在而 ImportError 失败；
    GREEN 后它证明「治理系统存在且生产执行路径经过治理系统」这一 F8 真正阻塞已被解除。
    """
    registry, gateway, sink = _governed_code_execute()
    need = CapabilityNeed("code.execute", args={"code": "1 + 1"}, context={})
    result = gateway.handle(need, ("run-1", "task-1", 0))

    # 1) 执行真的跑通了（真实流量，非 stub 旁路）
    assert isinstance(result, ProviderResult)
    assert result.success is True
    assert result.data == {"result": 2}

    # 2) 治理链产出了可审计的 resolution event（闭环：resolve 之后 audit 出口）
    assert len(sink) == 1, "治理闭环必须恰好产生 1 条 ResolutionEvent"
    event = sink.events[0]
    assert event.capability_id == "code.execute"
    assert event.outcome == ResolutionOutcome.SELECTED
    assert event.selected_provider is not None
    assert event.execution_key == ("run-1", "task-1", 0)
    # CapabilityNeed 无 version 字段 —— 诚实记 None，不替调用方编造
    assert event.requested_version is None


# ==========================================================================
# F8.4 coverage 真实化
# ==========================================================================
def test_governed_capability_counts_as_governed_coverage():
    """F8.4 coverage 真实化：code.execute 注册进 Line B 后，治理读面如实报 GOVERNED=1。

    这正是「coverage 真实性」——此前 code.execute 仅挂 Line A（UNGOVERNED），
    bootstrap 进 Line B 后变为 GOVERNED，读面如实计数，不伪造、不补零。
    """
    registry, _gateway, _sink = _governed_code_execute()
    view = CapabilityGovernanceView(registry)
    counts = view.coverage(["code.execute"])
    assert counts[GovernanceReachability.GOVERNED] == 1
    assert counts[GovernanceReachability.UNGOVERNED] == 0
    # 单能力快照也确认 GOVERNED + 1 个 provider
    snap = view.describe("code.execute")
    assert snap.reachability == GovernanceReachability.GOVERNED
    assert snap.provider_count == 1


# ==========================================================================
# Gateway fan-in + fail-open 委托回退
# ==========================================================================
def test_gateway_fail_open_delegates_to_fallback_when_ungoverned():
    """fail-open：Line B 解析不到的能力（未注册），若有 fallback 则委托回退而非崩。"""
    sink = InMemoryAuditSink()
    registry = CapabilityRegistry()  # 空：search.web 未治理
    delegated: list[tuple] = []

    def _fb(need: CapabilityNeed, key: tuple) -> ProviderResult:
        delegated.append((need.capability_id, key))
        return ProviderResult(data="fallback", success=True)

    gateway = CapabilityGateway(registry, audit_sink=sink, fallback=_fb)
    need = CapabilityNeed("search.web", args={"q": "x"}, context={})
    result = gateway.handle(need, ("r", "t", 0))
    assert result.success is True
    assert result.data == "fallback"
    assert delegated == [("search.web", ("r", "t", 0))]
    # 治理如实记录「未治理」这一次尝试（CAPABILITY_NOT_FOUND），随后 fail-open 回退——
    # 这正是 F8.3「连失败也要解释」的落点：未治理能力不是静默消失，而是被观测后委托。
    assert len(sink) == 1
    assert sink.events[0].outcome == ResolutionOutcome.CAPABILITY_NOT_FOUND
    assert sink.events[0].capability_id == "search.web"


def test_gateway_no_fallback_reraises_resolution_error():
    """无 fallback 时，未治理能力的 ResolutionError 原样上抛（不静默吞）。"""
    registry = CapabilityRegistry()
    gateway = CapabilityGateway(registry)
    need = CapabilityNeed("filesystem.read", args={}, context={})
    with pytest.raises(CapabilityNotFound):
        gateway.handle(need, ("r", "t", 0))


# ==========================================================================
# bootstrap 诚实性钉死（F7.1 契约/Provider 一致）
# ==========================================================================
def test_code_execute_provider_profile_matches_contract():
    """诚实性钉死：Provider 的 side_effect/idempotent 必须与契约一致（F7.1 门）。"""
    registry = CapabilityRegistry()
    bootstrap_code_execute(registry, executor=_real_executor)
    provider = registry.providers_for("code.execute")[0]
    contract = registry.contract_for("code.execute")
    assert provider.side_effect is True
    assert contract.side_effect is True
    assert provider.idempotent is False
    assert contract.idempotent is False
    assert "exec" in provider.granted_scopes
