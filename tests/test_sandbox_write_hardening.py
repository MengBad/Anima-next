"""P7.8 — SandboxWriteProvider Production Hardening (RED contract).

Closes P7.7 gaps G1-G4 (G5 is a documented identity-policy decision, not code):

    G1  lifecycle    - provider carries an auditable lifecycle state (no governance wiring)
    G2  config       - default sandbox root derivable from data_dir (caller injection wins)
    G3  cleanup      - explicit retention cleanup of .tokens/ markers (primary file never deleted)
    G4  atomicity    - C2 idempotency window closed: marker-missing + content-match -> restore
                       marker (written=False, no re-write); content-mismatch -> conflict refused

P7.4 behavior must stay GREEN after hardening (C2 idempotency, traversal block, etc.).
"""
from __future__ import annotations

import pathlib
import re
import time

import pytest

from anima.runtime.providers.sandbox_write_provider import SandboxWriteProvider


def _make(sandbox_root=None, **kw):
    return SandboxWriteProvider(sandbox_root=str(sandbox_root), **kw) if sandbox_root else SandboxWriteProvider(sandbox_root=str(sandbox_root), **kw)


# ---------------------------------------------------------------------------
# G1 - lifecycle state (auditable, no governance wiring)
# ---------------------------------------------------------------------------
def test_g1_lifecycle_state(tmp_path):
    provider = _make(tmp_path)
    assert provider.lifecycle == "active"
    # the provider exposes no governance control surface
    for banned in ("approve", "activate", "suspend", "revoke", "dispatch"):
        assert not hasattr(provider, banned), f"leaks {banned!r}"


# ---------------------------------------------------------------------------
# G2 - config default (data_dir-derived sandbox root; caller injection wins)
# ---------------------------------------------------------------------------
def test_g2_config_default(tmp_path):
    provider = SandboxWriteProvider(sandbox_root=None, data_dir=str(tmp_path))
    assert provider.sandbox_root == (tmp_path / "sandbox_write")
    result = provider.invoke(
        "sandbox.write", {"path": "out.txt", "content": "hello"}, ("run-1", "t1", 0)
    )
    assert result.success is True
    assert (tmp_path / "sandbox_write" / "out.txt").read_text(encoding="utf-8") == "hello"
    # explicit sandbox_root still wins
    explicit = SandboxWriteProvider(sandbox_root=str(tmp_path / "explicit"))
    assert explicit.sandbox_root == (tmp_path / "explicit")


# ---------------------------------------------------------------------------
# G3 - cleanup (explicit retention, .tokens only, primary file never deleted)
# ---------------------------------------------------------------------------
def test_g3_cleanup(tmp_path):
    provider = _make(tmp_path)
    provider.invoke("sandbox.write", {"path": "a.txt", "content": "a"}, ("run-1", "t1", 0))
    provider.invoke("sandbox.write", {"path": "b.txt", "content": "b"}, ("run-2", "t1", 0))
    tokens_dir = tmp_path / ".tokens"
    assert tokens_dir.exists()
    n_before = len(list(tokens_dir.glob("*.token")))

    removed = provider.cleanup(retention_seconds=0)
    assert removed == n_before  # all expired markers removed
    assert not tokens_dir.exists() or len(list(tokens_dir.glob("*.token"))) == 0
    # primary data files survive
    assert (tmp_path / "a.txt").exists()
    assert (tmp_path / "b.txt").exists()


# ---------------------------------------------------------------------------
# G4a - atomic recovery: marker missing + content match -> restore, written=False
# ---------------------------------------------------------------------------
def test_g4a_marker_restore(tmp_path):
    provider = _make(tmp_path)
    key = ("run-1", "t9", 0)
    first = provider.invoke("sandbox.write", {"path": "r.txt", "content": "payload"}, key)
    assert first.data["written"] is True
    # simulate the crash window: target committed, marker lost
    for marker in (tmp_path / ".tokens").glob("*.token"):
        marker.unlink()
    second = provider.invoke("sandbox.write", {"path": "r.txt", "content": "payload"}, key)
    # restored: marker re-created, NOT re-written
    assert second.success is True
    assert second.data["written"] is False
    assert (tmp_path / "r.txt").read_text(encoding="utf-8") == "payload"
    assert len(list((tmp_path / ".tokens").glob("*.token"))) == 1


# ---------------------------------------------------------------------------
# G4b - conflict: marker missing + content mismatch -> refused, never overwritten
# ---------------------------------------------------------------------------
def test_g4b_conflict_refused(tmp_path):
    provider = _make(tmp_path)
    key = ("run-1", "t10", 0)
    provider.invoke("sandbox.write", {"path": "c.txt", "content": "v1"}, key)
    for marker in (tmp_path / ".tokens").glob("*.token"):
        marker.unlink()
    result = provider.invoke("sandbox.write", {"path": "c.txt", "content": "v2"}, key)
    assert result.success is False
    assert "conflict" in result.error.lower()
    assert (tmp_path / "c.txt").read_text(encoding="utf-8") == "v1"  # never overwritten


# ---------------------------------------------------------------------------
# P7.4 regression: C2 idempotency + traversal still hold after hardening
# ---------------------------------------------------------------------------
def test_p74_regression_c2_and_traversal(tmp_path):
    provider = _make(tmp_path)
    key = ("run-1", "t3", 0)
    first = provider.invoke("sandbox.write", {"path": "dup.txt", "content": "v1"}, key)
    second = provider.invoke("sandbox.write", {"path": "dup.txt", "content": "v2"}, key)
    assert first.data["written"] is True
    assert second.data["written"] is False
    assert (tmp_path / "dup.txt").read_text(encoding="utf-8") == "v1"
    # traversal still blocked
    evil = provider.invoke("sandbox.write", {"path": "../evil.txt", "content": "x"}, ("r", "t", 0))
    assert evil.success is False
    assert not (tmp_path.parent / "evil.txt").exists()


# ---------------------------------------------------------------------------
# Static boundary (no System B / gateway / capability_mode / production)
# ---------------------------------------------------------------------------
def test_static_boundary():
    src = pathlib.Path(
        "anima/runtime/providers/sandbox_write_provider.py"
    ).read_text(encoding="utf-8")
    parts = src.split('"""')
    code_body = parts[2] if len(parts) >= 3 else src
    forbidden = [
        "CapabilityGateway", "gateway", "tools", "approve_and_execute",
        "capability_mode", "webui", "napcat", r"\brequests\b", "httpx",
        "subprocess", r"\bssh\b", "paramiko",
    ]
    for needle in forbidden:
        assert not re.search(needle, code_body), f"forbidden token: {needle}"
    allowed_prefixes = ("anima.runtime.",)
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith(("from ", "import ")):
            target = stripped.split(" import ")[0].replace("from ", "").strip()
            if target.startswith("anima."):
                assert target.startswith(allowed_prefixes), f"import escapes F5: {target!r}"
