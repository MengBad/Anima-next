"""M0.1 Commit 3a — Facade Protocol 契约测试（RED→GREEN）。

只验证「协议形状」：每个 facade 的 fake 实现满足对应 Protocol，且 raw 允许列表
与禁用列表互斥、覆盖用户拍板的边界。不依赖任何业务模块（contracts 层零反向依赖）。
"""
from __future__ import annotations

from anima.contracts import facades as F


class FakeMemory:
    def search(self, query, **kwargs):
        return []

    def add(self, text, **kwargs):
        return None


class FakeAgenda:
    def propose_agenda(self, group_id, text, *, priority=0.5, **kwargs):
        return []


class FakeTelemetry:
    def record_guard_fixation(self):
        return None

    def emit(self, event, **kwargs):
        return None


class FakeConfig:
    persistence = None
    sticker = None
    security = None
    permissions = None
    qzone = None
    voice = None
    meme = None


class FakePermission:
    def group_policy_for(self, group_id, **kwargs):
        return {}

    def role_for(self, user_id, group_id, **kwargs):
        return "user"


class FakeOutboundMsg:
    def __init__(self):
        self.detail_type = "group"
        self.target_id = 123
        self.message = "hi"


class FakeOutbound:
    def send(self, message):
        return 1


class FakeLegacyToolAccess:
    @property
    def tools(self):
        return {}

    def get(self, name):
        return None


def test_memory_protocol():
    assert isinstance(FakeMemory(), F.MemoryProtocol)


def test_agenda_protocol():
    assert isinstance(FakeAgenda(), F.AgendaProtocol)


def test_telemetry_protocol():
    assert isinstance(FakeTelemetry(), F.TelemetryProtocol)


def test_config_protocol():
    assert isinstance(FakeConfig(), F.ConfigProtocol)


def test_permission_protocol():
    assert isinstance(FakePermission(), F.PermissionProtocol)


def test_outbound_protocol_and_msg():
    assert isinstance(FakeOutbound(), F.OutboundProtocol)
    assert isinstance(FakeOutboundMsg(), F.OutboundMsg)


def test_legacy_tool_access_protocol():
    assert isinstance(FakeLegacyToolAccess(), F.LegacyToolAccessProtocol)


def test_raw_allowlist_forbidden_are_disjoint():
    # 已建 facade 的能力绝不可出现在允许列表（否则迁移无意义）
    assert F.RAW_ACCESS_FORBIDDEN.isdisjoint(F.RAW_ACCESS_ALLOWLIST)
    for forbidden in ("memory", "tools", "gateway", "permission", "config"):
        assert forbidden in F.RAW_ACCESS_FORBIDDEN
        assert forbidden not in F.RAW_ACCESS_ALLOWLIST
    for allowed in ("llm", "danger", "exploration", "tool_learning"):
        assert allowed in F.RAW_ACCESS_ALLOWLIST
        assert allowed not in F.RAW_ACCESS_FORBIDDEN
