"""P1-3 Owner 受控浏览器（agent_browser 进常规 ReAct，但群聊不暴露）。

验收口径：
- Owner 私聊：agent_browser 可见、可调用（动作受白名单约束）
- 群聊（即便是 Owner）：agent_browser 不可见、调用被拒并写审计
"""
from __future__ import annotations

import asyncio

import pytest

from anima.tools import LEVEL_OWNER, Tool, ToolRegistry
from anima.tools.builtin.agent_tools import agent_browser, register_agent_tools


class _Cfg:
    def __init__(self, enabled=True, private_only=True, actions="navigate,extract"):
        self.owner_browser = type("B", (), {
            "enabled": enabled,
            "private_only": private_only,
            "allowed_actions": actions,
        })()
        self.persistence = type("P", (), {"data_dir": "data"})()


class _Core:
    def __init__(self, **kw):
        self.config = _Cfg(**kw)
        self.audit = None

    def _auditor(self):
        return None


def _names(schemas):
    return [s["function"]["name"] for s in schemas]


def _registry(core):
    reg = ToolRegistry(core=core)
    register_agent_tools(reg, core)
    return reg


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# 可见性
# ---------------------------------------------------------------------------
def test_browser_visible_to_owner_in_private_chat():
    reg = _registry(_Core())
    token = reg.set_call_context(detail_type="private", group_id=None, user_id=1)
    try:
        assert "agent_browser" in _names(reg.get_schemas(LEVEL_OWNER))
    finally:
        reg.reset_call_context(token)


def test_browser_hidden_in_group_even_for_owner():
    """Owner 在群里同样拿到 permission_level=4 —— 仅靠 min_level 挡不住。"""
    reg = _registry(_Core())
    token = reg.set_call_context(detail_type="group", group_id=727631526, user_id=1)
    try:
        names = _names(reg.get_schemas(LEVEL_OWNER))
        assert "agent_browser" not in names
        # 其余 Owner 工具不受影响（本项只收窄浏览器）
        assert "agent_read_file" in names
    finally:
        reg.reset_call_context(token)


def test_browser_hidden_when_only_group_id_present():
    """没有 detail_type 只有 group_id 时也必须按群聊处理（不靠单一字段）。"""
    reg = _registry(_Core())
    token = reg.set_call_context(group_id=123)
    try:
        assert "agent_browser" not in _names(reg.get_schemas(LEVEL_OWNER))
    finally:
        reg.reset_call_context(token)


def test_browser_not_visible_to_normal_user_anywhere():
    reg = _registry(_Core())
    token = reg.set_call_context(detail_type="private", group_id=None)
    try:
        assert "agent_browser" not in _names(reg.get_schemas(2))
    finally:
        reg.reset_call_context(token)


# ---------------------------------------------------------------------------
# 调用门（schema 隐藏之外的第二道门）
# ---------------------------------------------------------------------------
def test_call_denied_in_group_even_with_owner_level():
    reg = _registry(_Core())
    token = reg.set_call_context(detail_type="group", group_id=999, user_id=1)
    try:
        out = _run(reg.call("agent_browser", {"action": "navigate", "url": "https://a.com"},
                            LEVEL_OWNER))
    finally:
        reg.reset_call_context(token)
    assert "仅限私聊" in out


def test_call_allowed_in_private_for_owner():
    reg = _registry(_Core())
    token = reg.set_call_context(detail_type="private", group_id=None, user_id=1)
    try:
        out = _run(reg.call("agent_browser", {"action": "navigate", "url": "https://a.com"},
                            LEVEL_OWNER))
    finally:
        reg.reset_call_context(token)
    # 环境无 playwright → 应命中「未安装」提示，而不是被权限/场景拒绝
    assert "仅限私聊" not in out and "权限不足" not in out


# ---------------------------------------------------------------------------
# 动作白名单
# ---------------------------------------------------------------------------
def test_click_denied_by_default_allowlist():
    out = _run(agent_browser(_Core(), "click", url="https://a.com", selector="#x"))
    assert "不在允许列表" in out


def test_navigate_passes_allowlist():
    out = _run(agent_browser(_Core(), "navigate", url="https://a.com"))
    assert "不在允许列表" not in out


def test_allowlist_configurable():
    out = _run(agent_browser(_Core(actions="navigate,extract,click"),
                             "click", url="https://a.com", selector="#x"))
    assert "不在允许列表" not in out


def test_unsafe_url_rejected():
    out = _run(agent_browser(_Core(), "navigate", url="file:///etc/passwd"))
    assert "拒绝非 http/https" in out


# ---------------------------------------------------------------------------
# 开关
# ---------------------------------------------------------------------------
def test_enabled_false_not_registered():
    reg = _registry(_Core(enabled=False))
    token = reg.set_call_context(detail_type="private", group_id=None)
    try:
        assert "agent_browser" not in _names(reg.get_schemas(LEVEL_OWNER))
    finally:
        reg.reset_call_context(token)


def test_private_only_false_keeps_old_behavior():
    """private_only=false → 回退到「群里也能看到」的旧行为（Owner 可显式选择）。"""
    reg = _registry(_Core(private_only=False))
    token = reg.set_call_context(detail_type="group", group_id=123)
    try:
        assert "agent_browser" in _names(reg.get_schemas(LEVEL_OWNER))
    finally:
        reg.reset_call_context(token)


def test_other_tools_unaffected_by_private_only_flag():
    """private_only 默认 False，既有工具行为不变（注册/可见性逐字节兼容）。"""
    reg = ToolRegistry(core=_Core())
    reg.register(Tool("plain", "p", {}, lambda **k: "ok"))
    token = reg.set_call_context(detail_type="group", group_id=123)
    try:
        assert _names(reg.get_schemas(LEVEL_OWNER)) == ["plain"]
    finally:
        reg.reset_call_context(token)
