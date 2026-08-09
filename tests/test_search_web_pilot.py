"""F8.9.3 —— search.web Pilot Expansion（第四条治理路径：external dependency + hidden side effect）。

闭环证明 + 验收门（M1 identity / M2 mixed-mode preservation / M3 no capability_only
regression / Q2 failure parity / C1 side-effect metadata pinning / C2 async bridge /
C3 hidden storage 不进 audit）。

最小修改边界（已裁定）：
- 仅改 ``anima/tools/__init__.py`` 的 ``_GATEWAY_PILOT_CAPABILITIES`` 加 ``"search.web"``
  （Q5 精确豁免，F8.9.1 模式）+ 新增 ``_make_search_web_executor``（C2：复用 code.execute
  的 ``_run_in_thread`` async 桥）+ 在 ``_build_capability_gateway`` 挂
  ``bootstrap_search_web``；
- 新文件 ``anima/tools/capability_web.py``：``SearchWebProvider`` + ``bootstrap_search_web``
  （C1 显式 pin metadata：side_effect=True / idempotent=False / permission=network /
  timeout=60.0 / retry=0）；
- runtime/capability_resolver、providers/base、governance/*、audit/、ops/、
  execution_ledger、workflow/orchestrator/recovery、constitution、ARCHITECTURE、
  builtin/web.py、memory/web_knowledge.py 一律不碰（C3：audit schema 不扩展）。

RED 阶段：本文件首测必须失败于——

    真实入口 ``call_capability("search.web")`` 在
    ``capability_mode=ON / gateway_mode=ON / allowlist 放行`` 下，
    **没有产生任何 search.web 的 ResolutionEvent**（入口尚未把 search.web 路由进
    Governance Event 流）。

Q5 裁定：**禁止真实网络测试**——网络可用性不是 capability governance 的测试变量。
本文件全部用例经 stub handler（``monkeypatch.setattr("anima.tools.builtin.tool_web_search",
fake)``，注册时从 builtin 包命名空间取引用，patch 后 create_default_registry 即得 fake），
成功/失败两路 fake 确定性断言 parity，与 F8.6 fake executor 同模式。

GREEN 后证明：成功路径 legacy==governed + 恰好 1 条 SELECTED 事件；失败路径
legacy 错误字符串==governed 错误字符串（``搜索时出错了：<ExceptionType>``，handler 吞
异常为成功文本——Q4 事实「failure != resolution failure」在测试中钉死）+ 事件保留。
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

FS = "search.web"
ALICE = "user:alice"


def _stub_search_success(query: str, num_results: int = 5) -> str:
    """成功 stub：确定性文本，不含网络。"""
    return f"关于「{query}」的搜索结果（共 2 条，展示前 1 条）：\n\n【1】测试标题\nhttps://example.com\n摘要: 测试摘要"


def _stub_search_failure(query: str, num_results: int = 5) -> str:
    """失败 stub：镜像真实 handler 的吞异常语义（web.py:202-204）——
    内部捕获异常并返回 ``搜索时出错了：<ExceptionType>``，而非向上抛。
    """
    try:
        raise TimeoutError("network blip")
    except Exception as exc:
        return f"搜索时出错了：{type(exc).__name__}"


def _governed_registry(monkeypatch, sink, stub=_stub_search_success):
    """governed 路径：gateway_mode=ON + stub handler（真实执行 = stub 文本）。"""
    monkeypatch.setattr("anima.tools.builtin.tool_web_search", stub)
    return create_default_registry(
        None,
        capability_mode=True,
        pilot_capability_allowlist=[ALICE],
        capability_gateway_mode=True,
        governance_audit_sink=sink,
    )


def _legacy_registry(monkeypatch, stub=_stub_search_success):
    """legacy 路径：gateway_mode=OFF，仍走 Line A（web_search stub 执行）。"""
    monkeypatch.setattr("anima.tools.builtin.tool_web_search", stub)
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
async def test_search_web_governed_emits_resolution_event_and_parity(monkeypatch):
    """真实入口 search.web 必须进入 Governance Event 流，产出恰好 1 条 SELECTED
    事件，且结果与 legacy 逐字节一致（stub 确定性文本，经过 Gateway 后仍是同一个工具）。"""
    sink = InMemoryAuditSink()
    reg_g = _governed_registry(monkeypatch, sink)
    reg_l = _legacy_registry(monkeypatch)

    legacy = await reg_l.call_capability(
        FS, {"query": "测试"},
        permission_level=LEVEL_USER, caller=ALICE,
    )
    governed = await reg_g.call_capability(
        FS, {"query": "测试"},
        permission_level=LEVEL_USER, caller=ALICE,
    )

    # RED: 入口未接 search.web 治理 -> 0 事件 -> 失败。GREEN: 恰好 1 条。
    fs_events = [e for e in sink.events if e.capability_id == FS]
    assert len(fs_events) == 1, f"expected 1 search.web ResolutionEvent, got {len(fs_events)}"
    ev = fs_events[0]
    assert ev.outcome == ResolutionOutcome.SELECTED
    assert ev.selected_provider is not None
    assert ev.execution_key[1] == FS
    # 行为级相等（验收门核心）：经过 Gateway 后仍是同一个工具（stub 文本一致）
    assert governed == legacy
    assert "关于「测试」的搜索结果" in governed


# ---------------------------------------------------------------------------
# M1：capability identity —— 复用既有 id，不重新声明
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_m1_capability_identity_reused_not_redeclared(monkeypatch):
    """M1：search.web 复用既有 capability id（Line A 早已 declare + web_search 挂
    capability_ids），F8.9.3 不应重新声明一个新 capability。"""
    reg = _legacy_registry(monkeypatch)
    assert FS in reg.capability_index.capabilities()
    tool = reg.tools.get("web_search")
    assert tool is not None
    assert FS in tool.capability_ids
    assert reg.capability_index.capabilities().count(FS) >= 1


# ---------------------------------------------------------------------------
# M2：mixed-mode preservation —— OFF legacy 不变；ON governed 一致
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_m2_mixed_mode_preservation(monkeypatch):
    """M2：gateway OFF 时 legacy web_search 完全不变；gateway ON 时 governed 结果一致。"""
    reg_off = _legacy_registry(monkeypatch)
    off = await reg_off.call_capability(
        FS, {"query": "甲"}, permission_level=LEVEL_USER, caller=ALICE
    )
    assert "关于「甲」的搜索结果" in off

    sink = InMemoryAuditSink()
    reg_on = _governed_registry(monkeypatch, sink)
    on = await reg_on.call_capability(
        FS, {"query": "甲"}, permission_level=LEVEL_USER, caller=ALICE
    )
    assert on == off
    assert any(e.capability_id == FS for e in sink.events)


# ---------------------------------------------------------------------------
# M3：no capability_only regression —— web_search 保持 mixed-mode
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_m3_web_search_stays_mixed_mode(monkeypatch):
    """M3：web_search 不得被隐式改为 capability_only=True（保持 mixed-mode，与
    code.execute 的 capability_only=True 语义区分；禁止让 capability_only=True 成为
    隐式要求）。"""
    reg = _legacy_registry(monkeypatch)
    tool = reg.tools.get("web_search")
    assert tool is not None
    assert tool.capability_only is False


# ---------------------------------------------------------------------------
# Q2：failure parity —— legacy 与 governed 用户可见行为一致（handler 吞异常语义）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_q2_failure_parity_error_string(monkeypatch):
    """Q2：search.web 失败（stub 抛 TimeoutError 并吞为错误字符串，镜像 web.py:202-204）
    时，legacy 与 governed 用户可见行为一致（均返回 ``搜索时出错了：TimeoutError``）；
    governed 失败路径仍产生 search.web ResolutionEvent（Q4 事实：failure != resolution
    failure——handler 吞异常为成功文本，治理事件流看不到网络失败，SELECTED 而非失败
    分支；本测试钉死该 gap，不修）。"""
    # 失败 stub 换装
    reg_l = _legacy_registry(monkeypatch, stub=_stub_search_failure)
    legacy = await reg_l.call_capability(
        FS, {"query": "x"}, permission_level=LEVEL_USER, caller=ALICE,
    )
    sink = InMemoryAuditSink()
    reg_g = _governed_registry(monkeypatch, sink, stub=_stub_search_failure)
    governed = await reg_g.call_capability(
        FS, {"query": "x"}, permission_level=LEVEL_USER, caller=ALICE,
    )
    assert legacy == governed
    assert legacy == "搜索时出错了：TimeoutError"
    fs_events = [e for e in sink.events if e.capability_id == FS]
    assert len(fs_events) == 1, f"expected 1 search.web event on failure, got {len(fs_events)}"
    # Q4 gap 钉死：吞异常 → 工具层成功 → SELECTED（失败对治理事件流不可见）
    assert fs_events[0].outcome == ResolutionOutcome.SELECTED
    assert fs_events[0].error is None


# ---------------------------------------------------------------------------
# C1：metadata pinning —— 诚实声明 side_effect=True / idempotent=False / network / 60.0
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_c1_side_effect_metadata_pinned(monkeypatch):
    """C1：search provider 与治理契约都必须**显式**声明 side_effect=True /
    idempotent=False / permission=network / timeout=60.0 / retry=0（不依赖默认值）。
    对照 read（False/True）与 write（True/False），三样本诚实区分——治理契约一致性门
    （resolver 校验 provider.side_effect == contract.side_effect）不被触发。
    C3：audit schema 不扩展——ResolutionEvent 字段集保持不变。
    """
    from anima.tools.capability_web import (
        SearchWebProvider,
        bootstrap_search_web,
    )
    from anima.runtime.capability_resolver import CapabilityRegistry

    captured = {}

    def _ex(query, execution_key):
        captured["called"] = (query, execution_key)
        return "ok"

    provider = SearchWebProvider(
        _ex, capability_id=FS, granted_scopes={"network"}
    )
    # 显式诚实声明：搜索写知识库（hidden persistence side effect）→ 有副作用、非幂等
    assert provider.side_effect is True
    assert provider.idempotent is False
    assert provider.granted_scopes == {"network"}

    registry = CapabilityRegistry()
    bootstrap_search_web(registry, executor=_ex)
    # 注册不应因 side_effect/idempotent 不一致而拒绝（resolver 一致性门）
    providers = list(registry.providers_for(FS))
    assert len(providers) == 1
    # 治理契约也应显式 side_effect=True / idempotent=False / timeout=60.0 / retry=0
    contract = registry.contract_for(FS)
    assert contract.side_effect is True
    assert contract.idempotent is False
    assert contract.permission == "network"
    assert contract.timeout == 60.0
    assert contract.retry == 0

    # C3：audit schema 不扩展——ResolutionEvent 字段集保持冻结（timestamp..error）
    from anima.capability.governance.audit import ResolutionEvent
    import dataclasses
    field_names = {f.name for f in dataclasses.fields(ResolutionEvent)}
    assert field_names == {
        "timestamp", "capability_id", "execution_key", "requested_version",
        "candidates", "selected_provider", "metadata_snapshot", "outcome", "error",
    }, f"ResolutionEvent 结构被扩展: {field_names}"
