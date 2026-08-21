"""P8.2 — Real User Entry Gate (RED contract).

Every real message passes: Scope -> Permission -> Rate Limit -> Identity Bind.
Fail closed: empty allowed_groups -> NOTHING admitted.  PASSIVE: only
triggered messages (@bot / reply-to-bot) are admitted for reply.

Acceptance:
    A  empty allowed_groups (default) -> deny all (fail closed)
    B  allowed group + triggered -> admitted, fresh request_id bound
    C  allowed group + untriggered -> denied (passive_not_triggered)
    D  disallowed group -> denied even when triggered
    E  user whitelist: outside -> denied; empty user list -> members OK
    F  rate limit: second message within min_interval -> denied
    G  content cap: over max_content_len -> denied
    H  identity bind: request_id is fresh per message, never qq ids
    I  static boundary: no executor / planning / System B
"""
from __future__ import annotations

import pathlib
import time

from anima.pilot.entry_gate import (
    EntryGate,
    GateConfig,
    GateDecision,
    PASSIVE_MODE,
)


def _gate(**overrides):
    cfg = dict(
        allowed_groups=("123456",),
        allowed_users=(),
        reply_mode=PASSIVE_MODE,
        min_interval_sec=3.0,
        max_content_len=2000,
    )
    cfg.update(overrides)
    return EntryGate(GateConfig(
        allowed_groups=tuple(cfg["allowed_groups"]),
        allowed_users=tuple(cfg["allowed_users"]),
        reply_mode=cfg["reply_mode"],
        min_interval_sec=cfg["min_interval_sec"],
        max_content_len=cfg["max_content_len"],
    ))


def _args(**overrides):
    base = dict(
        group_id="123456", user_id="10001", message_id="m-1",
        content="你好", triggered=True,
    )
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# A - empty allowed_groups -> deny all (fail closed)
# ---------------------------------------------------------------------------
def test_a_empty_groups_deny_all():
    g = _gate(allowed_groups=())
    d = g.admit(**_args())
    assert d.admitted is False and d.reason == "group_not_allowed"


# ---------------------------------------------------------------------------
# B - allowed group + triggered -> admitted with fresh request_id
# ---------------------------------------------------------------------------
def test_b_admitted_with_request_id():
    g = _gate()
    d1 = g.admit(**_args())
    assert d1.admitted is True and d1.reason == "admitted"
    assert d1.request_id.startswith("req-")
    # a second message (outside rate window) gets a DIFFERENT request_id
    time.sleep(3.1)
    d2 = g.admit(**_args())
    assert d2.admitted is True
    assert d2.request_id != d1.request_id
    assert d2.group_id == "123456" and d2.user_id == "10001"


# ---------------------------------------------------------------------------
# C - allowed group + untriggered -> denied (PASSIVE)
# ---------------------------------------------------------------------------
def test_c_untriggered_denied():
    g = _gate()
    d = g.admit(**_args(triggered=False))
    assert d.admitted is False and d.reason == "passive_not_triggered"


# ---------------------------------------------------------------------------
# D - disallowed group -> denied even when triggered
# ---------------------------------------------------------------------------
def test_d_disallowed_group():
    g = _gate(allowed_groups=("789012",))
    d = g.admit(**_args(group_id="999999"))
    assert d.admitted is False and d.reason == "group_not_allowed"


# ---------------------------------------------------------------------------
# E - user whitelist
# ---------------------------------------------------------------------------
def test_e_user_whitelist():
    g = _gate(allowed_users=("10001",))
    assert g.admit(**_args()).admitted is True
    d = g.admit(**_args(user_id="20002"))
    assert d.admitted is False and d.reason == "user_not_allowed"
    # empty user whitelist = any member of an allowed group is eligible
    g2 = _gate(allowed_users=())
    assert g2.admit(**_args(user_id="20002")).admitted is True


# ---------------------------------------------------------------------------
# F - rate limit
# ---------------------------------------------------------------------------
def test_f_rate_limit():
    g = _gate(min_interval_sec=5.0)
    assert g.admit(**_args()).admitted is True
    d2 = g.admit(**_args())  # immediately after -> rate limited
    assert d2.admitted is False and d2.reason == "rate_limited"


# ---------------------------------------------------------------------------
# G - content cap
# ---------------------------------------------------------------------------
def test_g_content_cap():
    g = _gate(max_content_len=10)
    d = g.admit(**_args(content="x" * 11))
    assert d.admitted is False and d.reason == "content_too_long"


# ---------------------------------------------------------------------------
# H - identity bind: request_id is fresh, never qq ids
# ---------------------------------------------------------------------------
def test_h_identity_bind():
    g = _gate()
    d = g.admit(**_args())
    assert d.admitted is True
    assert d.request_id != d.group_id and d.request_id != d.user_id
    assert d.request_id != d.message_id


# ---------------------------------------------------------------------------
# I - static boundary
# ---------------------------------------------------------------------------
def test_i_static_boundary():
    src = pathlib.Path("anima/pilot/entry_gate.py").read_text(encoding="utf-8")
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
