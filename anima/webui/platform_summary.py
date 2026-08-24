"""B4 底座收拢 —— 只读 /api/platform/summary 的数据投影。

聚合 runtime / 能力RFC-0001层 / 安全审计 三块为一屏, 纯函数可测, fail-closed。
"""
from __future__ import annotations

import importlib
from typing import Any

CAPABILITY_MODULES = ("rbac", "scopes", "contract", "schema", "resolver", "canonical", "evidence", "isolation")


def capability_layer_status() -> dict[str, Any]:
    """逐模块清点 anima.capability 层是否存在(只读; 缺失按 present=False)。"""
    present: dict[str, bool] = {}
    for name in CAPABILITY_MODULES:
        try:
            importlib.import_module(f"anima.capability.{name}")
            present[name] = True
        except Exception:  # noqa: BLE001
            present[name] = False
    return {"present": present, "layer_present": any(present.values())}


def security_audit_map(events: list[dict[str, Any]]) -> dict[str, Any]:
    """把最近 audit 事件归并成安全块(只读计数)。"""
    cfg = tok = perm = admin = drift = anomaly = 0
    for ev in events:
        typ = str(ev.get("type", ""))
        if typ.startswith("config_") or "change_set" in typ or "rolled_back" in typ:
            cfg += 1
        elif "webui_token" in typ or "login" in typ:
            tok += 1
        elif "permission" in typ or "drift" in typ:
            perm += 1
        elif "admin" in typ:
            admin += 1
        if "anomaly" in typ or "drift" in typ or "leak" in typ or "roll" in typ:
            anomaly += 1
    return {"config_events": cfg, "token_events": tok, "permission_events": perm,
            "admin_events": admin, "anomaly_count": anomaly}


def build_platform_summary(runtime_block: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    """聚合为一屏(只读, fail-closed: 都不授权动作)。"""
    return {"runtime": runtime_block,
            "capability": capability_layer_status(),
            "security": security_audit_map(events),
            "read_only": True}
