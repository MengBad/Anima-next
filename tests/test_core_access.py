"""M0.1 Commit 3b —— DeprecatedCoreAccess 契约测试。

不依赖业务运行时（用 FakeCore 鸭子类型注入），仅验证：
1. raw.require 必须带 caller 并产生审计事件；forbidden / 非 allowlist 拒绝。
2. Facade 是底层 core 的忠实投影（flag 等价性：Facade 模式 vs Legacy 直接访问结果一致）。
3. 每个 facade 满足对应 Protocol（Facade → Protocol → Implementation，非换皮）。
4. permission 真实委托到 config.permissions（V1 无独立子系统）。
5. facade_enabled() flag 决策默认开、可关。

对应 3b 验收：同调用 Facade / Legacy 两模式结果一致；
raw 必须记录 caller；fake 门面与真实门面形状一致。
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from anima.contracts.access import (
    DeprecatedCoreAccess,
    RawAccessForbidden,
    RawAccessNotAllowed,
    facade_enabled,
)
from anima.contracts.facades import (
    AgendaProtocol,
    ConfigProtocol,
    LegacyToolAccessProtocol,
    MemoryProtocol,
    OutboundMsg,
    OutboundProtocol,
    PermissionProtocol,
    TelemetryProtocol,
)


# OutboundMsg 是 Protocol（不可实例化）；测试用具体信封。
@dataclass
class FakeOutboundMsg:
    detail_type: str
    target_id: object
    message: str


# ---- Fake 真实子系统（鸭子类型，不 import 业务模块） ----
class FakeGateway:
    def __init__(self):
        self.calls = []
    def send(self, msg):
        self.calls.append(msg)
        return 1


class FakeVectorMemory:
    def __init__(self):
        self.search_calls = []
        self.add_calls = []
    def search(self, query, **kw):
        self.search_calls.append((query, kw))
        return [{"score": 0.9}]
    def add(self, text, **kw):
        self.add_calls.append((text, kw))
        return "ok"


class FakeReplyQuality:
    def __init__(self):
        self.fixations = 0
        self.unavail = 0
    def record_guard_fixation(self):
        self.fixations += 1
    def record_unavailable(self):
        self.unavail += 1


class FakeAudit:
    def __init__(self):
        self.events = []
    def emit(self, event_type, **fields):
        self.events.append((event_type, fields))


class FakePermissions:
    def __init__(self):
        self.group_policies = {"123": {"mute": True}}


class FakeConfig:
    def __init__(self):
        self.persistence = object()
        self.sticker = object()
        self.security = object()
        self.permissions = FakePermissions()
        self.qzone = object()
        self.voice = object()
        self.meme = object()


class FakeTools:
    def __init__(self):
        self.tools = {"search": object()}
    def get(self, name):
        return self.tools.get(name)


class FakeCore:
    def __init__(self):
        self.vector_memory = FakeVectorMemory()
        self.config = FakeConfig()
        self.gateway = FakeGateway()
        self.reply_quality = FakeReplyQuality()
        self.audit = FakeAudit()
        self.llm = object()
        self.tools = FakeTools()
    def propose_agenda(self, group_id, text, *, priority=0.5, **kw):
        return [("agenda", group_id, text, priority)]


def _access(audit=None):
    core = FakeCore()
    return DeprecatedCoreAccess(core, audit=audit), core


# ---- 1. raw 审计 ----
def test_raw_require_audits_caller():
    events = []
    acc, core = _access(audit=events.append)
    out = acc.raw.require("llm", caller="generation")
    assert out is core.llm
    assert events == [{"capability": "llm", "caller": "generation"}]


def test_raw_require_forbidden_raises():
    acc, _ = _access()
    for name in ("memory", "tools", "gateway", "permission", "config"):
        with pytest.raises(RawAccessForbidden):
            acc.raw.require(name, caller="x")


def test_raw_require_not_in_allowlist_raises():
    acc, _ = _access()
    for name in ("vector_memory", "sylann", "bus", "audit"):
        with pytest.raises(RawAccessNotAllowed):
            acc.raw.require(name, caller="x")


# ---- 2. Facade 忠实投影（flag 等价性） ----
def test_outbound_facade_matches_direct_core():
    acc, core = _access()
    msg = FakeOutboundMsg("group", 123, "hi")
    r1 = acc.outbound().send(msg)          # Facade 模式
    r2 = core.gateway.send(msg)            # Legacy 直接访问
    assert r1 == r2 == 1
    assert all(m is msg for m in core.gateway.calls)  # 同一 provider / 同一参数


def test_memory_facade_matches_direct_core():
    acc, core = _access()
    r1 = acc.memory().search("q", top_k=3)
    r2 = core.vector_memory.search("q", top_k=3)
    assert r1 == r2                                    # 结果一致
    assert ("q", {"top_k": 3}) in core.vector_memory.search_calls  # 同一参数透传


def test_config_facade_matches_direct_core():
    acc, core = _access()
    assert acc.config().permissions is core.config.permissions
    assert acc.config().persistence is core.config.persistence


def test_telemetry_facade_matches_direct_core():
    acc, core = _access()
    acc.telemetry().record_guard_fixation()
    assert core.reply_quality.fixations == 1
    acc.telemetry().emit("evt", trace="t")
    assert core.audit.events == [("evt", {"trace": "t"})]


def test_facade_vs_legacy_produce_same_result():
    """用户验收：同一调用 Facade 模式 vs Legacy 模式结果一致（安全网）。"""
    core = FakeCore()
    legacy = core.gateway.send(FakeOutboundMsg("group", 1, "x"))
    facade = DeprecatedCoreAccess(core).outbound().send(FakeOutboundMsg("group", 1, "x"))
    assert legacy == facade


# ---- 3. 每个 facade 满足对应 Protocol ----
def test_all_facades_conform_to_protocol():
    acc, _ = _access()
    assert isinstance(acc.memory(), MemoryProtocol)
    assert isinstance(acc.config(), ConfigProtocol)
    assert isinstance(acc.outbound(), OutboundProtocol)
    assert isinstance(acc.telemetry(), TelemetryProtocol)
    assert isinstance(acc.permission(), PermissionProtocol)
    assert isinstance(acc.agenda(), AgendaProtocol)
    assert isinstance(acc.tools(), LegacyToolAccessProtocol)


# ---- 4. permission 真实委托 ----
def test_permission_facade_delegates_to_config_permissions():
    acc, core = _access()
    assert acc.permission().group_policy_for(123) == {"mute": True}
    assert acc.permission().role_for(1, 123) == ""  # V1 无独立 role_for，返回 "" 非假架构


# ---- 5. flag 决策 ----
def test_facade_enabled_default_true(monkeypatch):
    monkeypatch.delenv("ANIMA_CORE_FACADE_ENABLED", raising=False)
    assert facade_enabled() is True


def test_facade_enabled_false_disables(monkeypatch):
    monkeypatch.setenv("ANIMA_CORE_FACADE_ENABLED", "false")
    assert facade_enabled() is False


# ---- 6. 边界守护：access.py 不得反向依赖业务模块 ----
def test_access_has_no_reverse_business_dependency():
    """DeprecatedCoreAccess 是边界组装层，若反向 import 业务模块即成新耦合点。"""
    import ast
    import pathlib

    src = pathlib.Path(__file__).parent.parent / "anima" / "contracts" / "access.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    forbidden = {
        "anima.pipeline", "anima.core", "anima.agent", "anima.llm",
        "anima.tools", "anima.memory", "anima.cognitive",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for f in forbidden:
                assert not node.module.startswith(f), (
                    f"access.py 反向依赖业务模块 {node.module}"
                )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                for f in forbidden:
                    assert not alias.name.startswith(f), (
                        f"access.py 反向依赖业务模块 {alias.name}"
                    )
