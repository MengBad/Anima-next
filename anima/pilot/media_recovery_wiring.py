"""P10.4-R2.5 — flag-gated activation wiring for the MediaRecoveryController.

Activation contract (controlled observation, NOT full media autonomy):
    * Engages ONLY when ``LivePilotConfig.media_mode == "observe"`` AND the
      group is in ``media_pilot_groups``.
    * Otherwise returns a DISABLED deny decision — the caller keeps its
      normal (pre-activation) behaviour.  Fail closed, zero behaviour change.
    * Never sends.  It only gates an already-selected local asset path and
      returns a :class:`MediaDecision`; the caller attaches the path to an
      already-authorised reply.
    * Capability gate (QQIdentityPolicy) + SafetyFilter + explicit size limit
      + JSONL audit.  Every outcome is audited with a one-way subject hash.

Forbidden by design (P10.4-R2.5): no execution, no sandbox.write, no
permission-level change, no kill-switch change, no production full release,
no autonomous-agent upgrade.  This module only decides + audits.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Callable, Optional

from anima.pilot.identity_policy import QQIdentityPolicy
from anima.pilot.media_recovery import MediaDecision, MediaRecoveryController
from anima.pilot.safety_filter import SafetyFilter

logger = logging.getLogger("anima.pilot.media_recovery_wiring")

#: size ceilings (mirror MediaRecoveryController defaults)
DEFAULT_MAX_STICKER_BYTES = 700 * 1024
GIF_MAX_STICKER_BYTES = 3 * 1024 * 1024

#: capability name used for sticker / meme / media responses
MEDIA_STICKER_CAPABILITY = "media.sticker"


def _resolve_pilot_config(core: Any) -> Any:
    """P10.4-R2.5 wiring fix: the live ``LivePilotConfig`` lives on the pilot
    coordinator's runtime (``core.pilot_coordinator._runtime._config``), NOT on
    ``core.config.pilot`` (which the pydantic ``AnimaConfig`` does not expose).

    Falls back to ``core.config.pilot`` so existing test fixtures keep working;
    returns ``None`` when neither exists (caller fails closed to "disabled").
    """
    coord = getattr(core, "pilot_coordinator", None)
    rt = getattr(coord, "_runtime", None) if coord is not None else None
    cfg = getattr(rt, "_config", None) if rt is not None else None
    if cfg is not None and hasattr(cfg, "media_mode"):
        return cfg
    return getattr(getattr(core, "config", None), "pilot", None)


def make_audit_sink(data_dir: str) -> Callable[[dict], None]:
    """JSONL audit sink mirroring the ``memory_read_audit`` convention.

    Never raises — audit must never break a media decision.
    """
    path = Path(data_dir) / "logs" / "media_recovery_audit.jsonl"

    def sink(record: dict) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            record = dict(record)
            record.setdefault("ts", time.time())
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            pass

    return sink


def build_controller(
    policy: QQIdentityPolicy,
    safety: SafetyFilter,
    *,
    audit: Optional[Callable[[dict], None]] = None,
    selected_path: Optional[str] = None,
    capability: str = MEDIA_STICKER_CAPABILITY,
    max_sticker_bytes: int = DEFAULT_MAX_STICKER_BYTES,
    gif_max_bytes: int = GIF_MAX_STICKER_BYTES,
) -> MediaRecoveryController:
    """Build a controller whose picker just returns ``selected_path``.

    The actual sticker selection already happened in the caller (the sticker
    tool); this controller adds capability + safety + size + audit gating on
    the pre-selected local path.
    """
    captured = selected_path

    def _picker(query: str, group_id: object, allow_cross_group: bool) -> Optional[str]:
        return captured

    return MediaRecoveryController(
        policy,
        safety,
        {capability: _picker},
        audit=audit,
        max_sticker_bytes=max_sticker_bytes,
        gif_max_bytes=gif_max_bytes,
    )


def request_media(
    core: Any,
    *,
    user_id: object,
    group_id: object,
    capability: str = MEDIA_STICKER_CAPABILITY,
    query: str = "",
    selected_path: Optional[str] = None,
    allow_cross_group: bool = False,
    policy: Optional[QQIdentityPolicy] = None,
    safety: Optional[SafetyFilter] = None,
    audit: Optional[Callable[[dict], None]] = None,
) -> MediaDecision:
    """Gate an already-selected media asset. Fail-closed.

    Returns ``MediaDecision(False, "disabled")`` when activation is off or the
    group is outside the pilot scope — the caller should keep its normal
    behaviour (attach the sticker as before).  When engaged and DENIED, the
    caller must suppress the sticker.
    """
    cfg = _resolve_pilot_config(core)
    mode = str(getattr(cfg, "media_mode", "off") or "off").strip().lower()
    pilot_groups = set(str(g) for g in (getattr(cfg, "media_pilot_groups", ()) or ()))
    if mode != "observe" or str(group_id) not in pilot_groups:
        return MediaDecision(False, "disabled")

    if policy is None:
        policy = _policy_from_core(core)
    if safety is None:
        safety = SafetyFilter()
    if audit is None:
        data_dir = getattr(getattr(getattr(core, "config", None), "persistence", None), "data_dir", None)
        if data_dir:
            audit = make_audit_sink(data_dir)

    controller = build_controller(
        policy, safety, audit=audit, selected_path=selected_path, capability=capability
    )
    return controller.request(
        user_id, group_id, capability, query, allow_cross_group=allow_cross_group
    )


def _policy_from_core(core: Any) -> QQIdentityPolicy:
    """D1 (P10.4-R2.5): read the REAL runtime QQIdentityPolicy.

    The live policy is built by ``cli.py`` from the raw ``identity_policy``
    section and stored on ``core.pilot_coordinator._runtime._policy``.  The old
    ``core.config.identity`` attribute does NOT exist on ``AnimaConfig`` (the
    pydantic config has no ``identity`` field), so reading it always returned
    ``None`` -> a disabled (fail-closed) policy and ``media.sticker`` could
    never be authorised.

    This change is a PURE POLICY-SOURCE CORRECTION.  It does NOT alter the
    QQIdentityPolicy decision logic (same ``can()`` / ``decide()`` used by
    P10.2-C for ``chat.answer`` / ``sandbox.write``); it only points at the
    policy object that actually exists at runtime.  A ``media.sticker`` grant
    is still required (capability must be declared in ``capability_levels``) —
    see ``request_media`` / the deployment config delta.

    Falls back to a disabled policy if the runtime is absent, and keeps a
    legacy ``core.config.identity`` branch only for old test fixtures.
    """
    coord = getattr(core, "pilot_coordinator", None)
    rt = getattr(coord, "_runtime", None) if coord is not None else None
    policy = getattr(rt, "_policy", None) if rt is not None else None
    if isinstance(policy, QQIdentityPolicy):
        return policy
    # legacy fallback (kept only for old test fixtures that set core.config.identity)
    cfg = getattr(getattr(core, "config", None), "identity", None)
    if cfg is None:
        return QQIdentityPolicy(None)
    from anima.pilot.identity_policy import IdentityPolicyConfig

    if isinstance(cfg, dict):
        return QQIdentityPolicy(IdentityPolicyConfig.from_dict(cfg))
    data = {
        "enabled": getattr(cfg, "enabled", False),
        "group_allowlist": tuple(getattr(cfg, "group_allowlist", ()) or ()),
        "group_denylist": tuple(getattr(cfg, "group_denylist", ()) or ()),
        "user_allowlist": tuple(getattr(cfg, "user_allowlist", ()) or ()),
        "user_denylist": tuple(getattr(cfg, "user_denylist", ()) or ()),
        "admin_users": tuple(getattr(cfg, "admin_users", ()) or ()),
        "user_levels": dict(getattr(cfg, "user_levels", {}) or {}),
        "capability_levels": dict(getattr(cfg, "capability_levels", {}) or {}),
    }
    return QQIdentityPolicy(IdentityPolicyConfig.from_dict(data))
