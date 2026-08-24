from __future__ import annotations

from typing import Any

LIFE_STAGES = ("inbound", "deliberation", "tool", "outbound", "degrade", "drive", "governance")


def label_life_stage(event_type: str) -> str:
    t = (event_type or "").lower()
    if "inbound" in t or "received" in t:
        return "inbound"
    if "outbound" in t or "sent" in t or "proactive_expression" in t or t.startswith("reply"):
        return "outbound"
    if "tool" in t:
        return "tool"
    if any(k in t for k in ("anomaly", "fault", "degrad", "rollback", "rolled_back", "unavailable", "error")):
        return "degrade"
    if any(k in t for k in ("drive", "autonom", "sylann", "desire", "active_speech", "curiosity")):
        return "drive"
    if any(k in t for k in ("workflow", "agenda", "intent", "deliberat", "subscription")):
        return "deliberation"
    return "governance"


def _summary(ev: dict[str, Any]) -> str:
    for key in ("message", "text", "summary", "action", "intent", "tool", "detail", "event_type"):
        val = ev.get(key)
        if val:
            return str(val)[:160]
    return str(ev.get("type", "event"))


def build_life_timeline(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for ev in events:
        typ = str(ev.get("type", ""))
        out.append({"ts": ev.get("ts"), "type": typ,
                    "stage": label_life_stage(typ), "summary": _summary(ev)})
    return out
