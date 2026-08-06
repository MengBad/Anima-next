"""M0.4.6 Provider Implementation —— capability provider 回归（分层落地）。

按 Review Package §5 的 T1–T8 分层补齐。本阶段（D1 落地）仅包含 **T1**：
P1 Audit Taxonomy Normalization 验证。T2–T8 在 D2/D3 挂载 provider 后于 Step 5 补齐。

纪律（与 Review Package 一致）：
- 本文件不创建 provider、不挂载能力、不激活 capability_mode；
- D1 仅归一审计事件名，不触碰 legacy 执行工具 / filesystem / RFC-0001 / ARCHITECTURE。
"""

import asyncio

from anima.capability.scopes import PermissionScopes
from anima.tools import LEVEL_USER, Tool, ToolRegistry


class _CaptureAudit:
    """捕获 emit 的事件类型与字段，供断言使用。"""

    def __init__(self):
        self.events = []

    def emit(self, event_type: str, **fields):
        self.events.append((event_type, fields))

    def types(self):
        return [e for e, _ in self.events]


class _FakeCore:
    def __init__(self):
        self.audit = _CaptureAudit()


def test_t1_p1_audit_normalization():
    """D1：缺少 exec scope 的执行工具必须发 execution_denied{reason:missing_exec_scope}，
    且绝不发 legacy execution_scope_denied（闭合 contract v0.2 F1）。"""
    core = _FakeCore()
    reg = ToolRegistry(core=core)
    # 一个名字在 EXECUTION_TOOLS 中、但未获 exec scope 授权的工具
    noexec = Tool(
        name="run_python",
        description="no-exec variant for test",
        parameters={"code": {"type": "string"}},
        handler=lambda **kw: "SHOULD_NOT_RUN",
        min_level=LEVEL_USER,
        granted_scopes=None,  # 无 exec scope -> exec_permitted() == False
    )
    reg.register(noexec)
    result = asyncio.run(reg.call("run_python", {"code": "1+1"}, permission_level=LEVEL_USER))

    # legacy 名必须消失
    assert "execution_scope_denied" not in core.audit.types(), \
        "legacy execution_scope_denied must be gone after D1"
    # 归一后的事件必须出现，且 reason 正确
    denied = [f for evt, f in core.audit.events if evt == "execution_denied"]
    assert denied, "execution_denied must be emitted on exec-scope gate failure"
    assert any(f.get("reason") == "missing_exec_scope" for f in denied), \
        "execution_denied reason must be 'missing_exec_scope'"
    # gate 必须提前返回，handler 不得执行
    assert "SHOULD_NOT_RUN" not in result
    # 不应留下正常执行审计轨迹
    assert "execution_started" not in core.audit.types()
