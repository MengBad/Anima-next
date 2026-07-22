"""Owner-gated weekly digest of aggregate ops/quality/memory signals.

Read-only over existing metrics JSON; never mutates production code or config.
Does not store chat message bodies.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from anima.core.persistence import PersistenceProvider

_BJ = timezone(timedelta(hours=8))
_DIGEST_DIR = "digests"
_LATEST = "weekly_digest_latest.json"


def _now_iso() -> str:
    return datetime.now(_BJ).isoformat(timespec="seconds")


def _day_keys(days: int = 7) -> list[str]:
    today = datetime.now(_BJ).date()
    return [(today - timedelta(days=i)).isoformat() for i in range(days)]


def build_weekly_digest(core: Any, *, days: int = 7) -> dict[str, Any]:
    """Assemble a L1 summary from live modules; pure read of aggregates."""
    days = max(1, min(int(days or 7), 31))
    data_dir = str(getattr(getattr(core, "config", None), "persistence", None)
                   and core.config.persistence.data_dir or "data")

    quality_days: list[dict[str, Any]] = []
    quality = getattr(core, "reply_quality", None)
    totals = {
        "total_replies": 0,
        "unavailable_replies": 0,
        "empty_should_reply": 0,
        "tool_spins": 0,
        "react_max_rounds": 0,
        "space_link_ok": 0,
        "space_link_fail": 0,
        "tool_failures": 0,
        "tool_calls": 0,
        "outbound_failures": 0,
        "identity_leaks": 0,
        "label_leaks": 0,
        "repeated_replies": 0,
    }
    if quality is not None:
        for key in _day_keys(days):
            report = quality.report(key)
            quality_days.append(report)
            for name in totals:
                totals[name] += int(report.get(name, 0) or 0)

    memory: dict[str, Any] = {}
    try:
        from anima.memory.chat_memory import get_chat_memory
        cm = get_chat_memory()
        sessions = getattr(cm, "_sessions", None) or getattr(cm, "sessions", {}) or {}
        if not isinstance(sessions, dict):
            sessions = {}
        memory["chat_sessions"] = len(sessions)
        memory["chat_messages_est"] = sum(
            len(v) if hasattr(v, "__len__") else 0 for v in sessions.values()
        )
    except Exception:
        memory["chat_sessions"] = None
        memory["chat_messages_est"] = None

    try:
        from anima.crossgroup import get_crossgroup_manager
        cgm = get_crossgroup_manager()
        facts = getattr(cgm, "user_facts", None) or getattr(cgm, "_user_facts", {}) or {}
        memory["crossgroup_users"] = len(facts) if isinstance(facts, dict) else None
    except Exception:
        memory["crossgroup_users"] = None

    try:
        vm = getattr(core, "vector_memory", None)
        if vm is not None and hasattr(vm, "count"):
            c = vm.count()
            memory["vector_count"] = int(c) if c is not None else None
        else:
            memory["vector_count"] = None
    except Exception:
        memory["vector_count"] = None

    subs: dict[str, Any] = {"count": 0, "kinds": {}}
    try:
        monitors = getattr(core, "monitors", None)
        store = getattr(monitors, "store", None) if monitors is not None else None
        raw_items = getattr(store, "items", {}) if store is not None else {}
        items = list(raw_items.values()) if isinstance(raw_items, dict) else list(raw_items or [])
        subs["count"] = len(items)
        for item in items:
            kind = str(
                getattr(item, "platform", None)
                or (item.get("platform") if isinstance(item, dict) else "unknown")
            )
            subs["kinds"][kind] = subs["kinds"].get(kind, 0) + 1
    except Exception:
        pass

    channel_connected = None
    try:
        ch = getattr(getattr(core, "gateway", None), "channel", None)
        if ch is not None and hasattr(ch, "health"):
            h = ch.health() or {}
            channel_connected = bool(h.get("connected") or h.get("channel_connected"))
    except Exception:
        pass

    selfcode_pending = 0
    try:
        sc = getattr(core, "selfcode", None)
        if sc is not None and hasattr(sc, "list_installed"):
            tools = sc.list_installed() or []
            if isinstance(tools, list):
                selfcode_pending = sum(
                    1 for t in tools
                    if str((t.get("status") if isinstance(t, dict) else getattr(t, "status", "")) or "")
                    in {"pending", "awaiting_review", "draft", "proposed"}
                )
    except Exception:
        pass

    highlights: list[str] = []
    if totals["empty_should_reply"] > 0:
        highlights.append(f"应回却空 {totals['empty_should_reply']} 次")
    if totals["tool_spins"] > 0:
        highlights.append(f"工具空转 {totals['tool_spins']} 次")
    if totals["space_link_fail"] > 0:
        highlights.append(f"主页链接失败 {totals['space_link_fail']} 次")
    if totals["outbound_failures"] > 0:
        highlights.append(f"出站失败 {totals['outbound_failures']} 次")
    if totals["identity_leaks"] > 0:
        highlights.append(f"身份泄漏 {totals['identity_leaks']} 次")
    if not highlights:
        highlights.append("近窗质量信号平稳，无突出告警")

    digest = {
        "generated_at": _now_iso(),
        "window_days": days,
        "day_keys": _day_keys(days),
        "quality_totals": totals,
        "quality_by_day": quality_days,
        "memory": memory,
        "subscriptions": subs,
        "channel_connected": channel_connected,
        "selfcode_pending_review": selfcode_pending,
        "highlights": highlights,
        "notes": [
            "本摘要只读聚合指标，不改配置、不自动装 SelfCode、不写聊天正文。",
            "如需动作：人工审阅后通过 Owner 命令 / WebUI 审批执行。",
        ],
        "version": 1,
    }
    return digest


def format_digest_text(digest: dict[str, Any]) -> str:
    """Owner-facing short Chinese summary (QQ command / API text)."""
    t = digest.get("quality_totals") or {}
    mem = digest.get("memory") or {}
    subs = digest.get("subscriptions") or {}
    lines = [
        f"周报摘要（近 {digest.get('window_days', 7)} 天 · {digest.get('generated_at', '')}）",
        f"回复 {t.get('total_replies', 0)}，不可用 {t.get('unavailable_replies', 0)}，"
        f"应回却空 {t.get('empty_should_reply', 0)}，"
        f"工具空转 {t.get('tool_spins', 0)}，ReAct 触顶 {t.get('react_max_rounds', 0)}，"
        f"主页链接 ok/fail {t.get('space_link_ok', 0)}/{t.get('space_link_fail', 0)}，"
        f"出站失败 {t.get('outbound_failures', 0)}。",
        f"记忆：会话 {mem.get('chat_sessions', '?')}，跨群用户 {mem.get('crossgroup_users', '?')}，"
        f"向量 {mem.get('vector_count', '?')}。",
        f"订阅 {subs.get('count', 0)} 条；频道连接 {digest.get('channel_connected')}；"
        f"SelfCode 待审 {digest.get('selfcode_pending_review', 0)}。",
        "要点：" + "；".join(digest.get("highlights") or ["无"]),
        "（只读，不自动改生产）",
    ]
    return "\n".join(lines)


def save_digest(core: Any, digest: dict[str, Any] | None = None, *, days: int = 7) -> dict[str, Any]:
    """Generate (if needed) and persist under data/digests/ — no code/config mutation."""
    if digest is None:
        digest = build_weekly_digest(core, days=days)
    data_dir = Path(str(core.config.persistence.data_dir))
    persistence = PersistenceProvider(str(data_dir))
    stamp = datetime.now(_BJ).strftime("%Y%m%d-%H%M%S")
    rel_dir = _DIGEST_DIR
    (data_dir / rel_dir).mkdir(parents=True, exist_ok=True)
    archive_name = f"{rel_dir}/weekly_{stamp}.json"
    persistence.write_json(archive_name, digest)
    persistence.write_json(_LATEST, digest)
    digest = dict(digest)
    digest["saved_as"] = archive_name
    digest["latest_path"] = _LATEST
    return digest


def load_latest_digest(core: Any) -> dict[str, Any] | None:
    data_dir = str(core.config.persistence.data_dir)
    persistence = PersistenceProvider(data_dir)
    raw = persistence.read_json(_LATEST, None)
    return raw if isinstance(raw, dict) else None
