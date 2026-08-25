"""P7.4 — Single-Provider Execution Bridge: sandbox.write (RED contract).

First F5-domain SIDE-EFFECT provider: writes inside a controlled sandbox root
with the execution_key as the external idempotency token (F5.7 C2/C4).

Acceptance (all GREEN):
    A  Provider write          - invoke writes within the sandbox
    B  Sandbox traversal block - ../ or absolute escapes are refused
    C  C2 external idempotency - same execution_key -> written=False (token hit)
    D  Failure normalization   - missing args -> ProviderResult(success=False)
    E  RealTaskExecutor chain  - resolver + executor + provider + observation
    F  Ledger gate no double   - ledger HIT skips the second provider call
    G  Non-escalation          - observation never becomes authorization
    H  Boundary scan           - no System B / tools / gateway / production
"""
from __future__ import annotations

import os
import pathlib
import re

import pytest

from anima.runtime.execution_ledger import ExecutionLedger
from anima.runtime.executor import ExecutionStep
from anima.runtime.real_task_executor import RealTaskExecutor


def _step(path="out.txt", content="hello", **overrides):
    defaults = dict(
        id="t-w",
        title="sandbox write",
        intent="persist derived note",
        depends_on=[],
        capability_need={
            "capability": "sandbox.write",
            "args": {"path": path, "content": content},
            "context": {},
        },
        estimates={},
    )
    defaults.update(overrides)
    return ExecutionStep(**defaults)


def _make_provider(sandbox_root):
    from anima.runtime.providers.sandbox_write_provider import SandboxWriteProvider

    return SandboxWriteProvider(sandbox_root=str(sandbox_root))


def _make_executor(sandbox_root):
    """RealTaskExecutor injected with a resolver serving sandbox.write."""
    from anima.runtime.capability_resolver import (
        CapabilityContract,
        CapabilityRegistry,
        CapabilityResolver,
    )

    registry = CapabilityRegistry()
    registry.register(
        CapabilityContract(
            capability_id="sandbox.write", side_effect=True, idempotent=True
        ),
        _make_provider(sandbox_root),
    )
    return RealTaskExecutor(resolver=CapabilityResolver(registry))


# ---------------------------------------------------------------------------
# A - Provider write (within sandbox)
# ---------------------------------------------------------------------------
def test_a_provider_write(tmp_path):
    provider = _make_provider(tmp_path)
    result = provider.invoke(
        "sandbox.write", {"path": "out.txt", "content": "hello"}, ("run-1", "t1", 0)
    )
    assert result.success is True
    assert result.data.get("written") is True
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "hello"


# ---------------------------------------------------------------------------
# B - Sandbox traversal block
# ---------------------------------------------------------------------------
def test_b_traversal_block(tmp_path):
    provider = _make_provider(tmp_path)
    # 反斜杠在非 Windows 是合法文件名字符而非路径分隔符：按平台组候选
    candidates = ["../evil.txt", str(tmp_path.parent / "escape.txt")]
    if os.name == "nt":
        candidates.append("..\\evil.txt")
    for evil in candidates:
        result = provider.invoke(
            "sandbox.write", {"path": evil, "content": "x"}, ("run-1", "t2", 0)
        )
        assert result.success is False, f"traversal not blocked: {evil!r}"
        assert "outside" in result.error.lower() or "escape" in result.error.lower()
    assert not (tmp_path.parent / "evil.txt").exists()
    assert not (tmp_path.parent / "escape.txt").exists()


# ---------------------------------------------------------------------------
# C - C2 external idempotency token (same execution_key -> no re-write)
# ---------------------------------------------------------------------------
def test_c_external_idempotency(tmp_path):
    provider = _make_provider(tmp_path)
    key = ("run-1", "t3", 0)
    first = provider.invoke(
        "sandbox.write", {"path": "dup.txt", "content": "v1"}, key
    )
    second = provider.invoke(
        "sandbox.write", {"path": "dup.txt", "content": "v2"}, key
    )
    assert first.success is True and first.data.get("written") is True
    assert second.success is True and second.data.get("written") is False
    # the file still holds the FIRST write (external authoritative dedupe)
    assert (tmp_path / "dup.txt").read_text(encoding="utf-8") == "v1"


# ---------------------------------------------------------------------------
# D - Failure normalization
# ---------------------------------------------------------------------------
def test_d_failure_normalization(tmp_path):
    provider = _make_provider(tmp_path)
    missing_path = provider.invoke(
        "sandbox.write", {"content": "x"}, ("run-1", "t4", 0)
    )
    assert missing_path.success is False
    assert "path" in missing_path.error.lower()
    missing_content = provider.invoke(
        "sandbox.write", {"path": "x.txt"}, ("run-1", "t5", 0)
    )
    assert missing_content.success is False
    assert "content" in missing_content.error.lower()


# ---------------------------------------------------------------------------
# E - RealTaskExecutor chain (resolver + executor + provider + observation)
# ---------------------------------------------------------------------------
def test_e_real_executor_chain(tmp_path):
    executor = _make_executor(tmp_path)
    obs = executor.execute(_step(), ("run-1", "t6", 0))
    assert obs.success is True
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "hello"
    assert obs.metadata.get("provider_id") is not None


# ---------------------------------------------------------------------------
# F - Ledger gate: HIT skips the second provider call
# ---------------------------------------------------------------------------
def test_f_ledger_gate(tmp_path):
    executor = _make_executor(tmp_path)
    ledger = ExecutionLedger()  # F5.1 memory-only
    key = ("run-1", "t7", 0)
    first = executor.execute(_step(), key, ledger)
    # second execute: ledger HIT -> cached observation, provider not called again
    second = executor.execute(_step(path="out.txt", content="changed"), key, ledger)
    assert second == first
    assert second.success is True
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "hello"  # unchanged


# ---------------------------------------------------------------------------
# G - Non-escalation (observation never becomes authorization)
# ---------------------------------------------------------------------------
def test_g_non_escalation(tmp_path):
    executor = _make_executor(tmp_path)
    obs = executor.execute(_step(), ("run-1", "t8", 0))
    payload = " ".join((obs.task_id, str(obs.result), obs.error, str(obs.metadata))).lower()
    for token in ("approved", "authorized", "deployed", "production"):
        assert token not in payload


# ---------------------------------------------------------------------------
# H - Boundary scan (no System B / tools / gateway / production)
# ---------------------------------------------------------------------------
def test_h_boundary_scan():
    src = pathlib.Path(
        "anima/runtime/providers/sandbox_write_provider.py"
    ).read_text(encoding="utf-8")
    parts = src.split('"""')
    code_body = parts[2] if len(parts) >= 3 else src
    forbidden = [
        "CapabilityGateway",
        "gateway",
        "tools",
        "approve_and_execute",
        "capability_mode",
        "webui",
        "napcat",
        r"\brequests\b",
        "httpx",
        "subprocess",
        r"\bssh\b",
        "paramiko",
    ]
    for needle in forbidden:
        assert not re.search(needle, code_body), f"forbidden token in code: {needle}"
    # imports stay inside the F5 runtime domain
    allowed_prefixes = ("anima.runtime.",)
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith(("from ", "import ")):
            target = stripped.split(" import ")[0].replace("from ", "").strip()
            if target.startswith("anima."):
                assert target.startswith(allowed_prefixes), (
                    f"import escapes F5 domain: {target!r}"
                )
