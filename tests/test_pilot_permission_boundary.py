"""P8.6 — Execution Permission Boundary (RED contract).

QQ group messages default to READ ONLY.  Answering is allowed; executing
requires explicit command + authorized scope + execution approval.

Acceptance:
    A  read-only question -> ANSWER
    B  "帮我写个代码" -> EXECUTE (bounded to sandbox.write)
    C  "删除服务器文件" -> DENY (dangerous, needs_approval=True)
    D  explicit execute markers (执行/运行/run/write) -> EXECUTE
    E  dangerous verbs (删除/重启/部署/kill/delete) -> DENY + approval
    F  empty content -> DENY
    G  allowed capability set is exactly (sandbox.write,)
    H  static boundary: no System B / gateway / executor import
"""
from __future__ import annotations

import pathlib

from anima.pilot.permission_boundary import (
    ACTION_ANSWER,
    ACTION_DENY,
    ACTION_EXECUTE,
    PermissionBoundary,
)


def _b():
    return PermissionBoundary()


# ---------------------------------------------------------------------------
# A - read-only question -> ANSWER
# ---------------------------------------------------------------------------
def test_a_read_only_answer():
    r = _b().classify("什么是量子计算？")
    assert r.action == ACTION_ANSWER and r.reason == "read_only_answer"
    assert r.capability == ""


# ---------------------------------------------------------------------------
# B - "帮我写个代码" -> EXECUTE (bounded)
# ---------------------------------------------------------------------------
def test_b_write_code_execute():
    r = _b().classify("帮我写个代码")
    assert r.action == ACTION_EXECUTE
    assert r.capability == "sandbox.write"
    assert r.needs_approval is False


# ---------------------------------------------------------------------------
# C - "删除服务器文件" -> DENY (dangerous)
# ---------------------------------------------------------------------------
def test_c_delete_denied():
    r = _b().classify("删除服务器文件")
    assert r.action == ACTION_DENY
    assert r.needs_approval is True
    assert "dangerous" in r.reason


# ---------------------------------------------------------------------------
# D - explicit execute markers -> EXECUTE
# ---------------------------------------------------------------------------
def test_d_execute_markers():
    for text in ("请执行这个任务", "帮我运行一下", "write a file", "run the plan",
                 "帮我保存这个文件", "帮我生成一份报告"):
        r = _b().classify(text)
        assert r.action == ACTION_EXECUTE, text
        assert r.capability == "sandbox.write"


# ---------------------------------------------------------------------------
# E - dangerous verbs -> DENY + approval
# ---------------------------------------------------------------------------
def test_e_dangerous_verbs():
    for text in ("删除文件", "重启服务", "部署到服务器", "kill process",
                 "delete the file", "restart the server"):
        r = _b().classify(text)
        assert r.action == ACTION_DENY, text
        assert r.needs_approval is True


# ---------------------------------------------------------------------------
# F - empty -> DENY
# ---------------------------------------------------------------------------
def test_f_empty_denied():
    r = _b().classify("")
    assert r.action == ACTION_DENY and r.reason == "empty_request"


# ---------------------------------------------------------------------------
# G - allowed capability set exactly sandbox.write
# ---------------------------------------------------------------------------
def test_g_allowed_capabilities():
    assert _b().allowed_capabilities() == ("sandbox.write",)


# ---------------------------------------------------------------------------
# H - static boundary
# ---------------------------------------------------------------------------
def test_h_static_boundary():
    src = pathlib.Path("anima/pilot/permission_boundary.py").read_text(encoding="utf-8")
    parts = src.split('"""')
    code_body = parts[2] if len(parts) >= 3 else src
    forbidden = [
        "executor", "Executor", "PlanOrchestrator", "pilot_rollout",
        "CapabilityGateway", "gateway", "tools", "approve_and_execute",
        "capability_mode", "webui", "napcat", "subprocess", "os.environ",
        "getenv", "requests", "httpx", "ssh", "paramiko",
    ]
    for needle in forbidden:
        assert needle not in code_body, f"forbidden token: {needle}"
    allowed_prefixes = ("anima.pilot.", "anima.runtime.", "anima.constitution.")
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith(("from ", "import ")) and "anima." in stripped:
            target = stripped.split(" import ")[0].replace("from ", "").strip()
            if target.startswith("anima."):
                assert target.startswith(allowed_prefixes), f"import escapes: {target!r}"
