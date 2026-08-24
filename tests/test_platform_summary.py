# -*- coding: utf-8 -*-
"""B4 — 底座收拢 platform_summary 纯函数 tests。"""
from anima.webui.platform_summary import capability_layer_status, security_audit_map, build_platform_summary


def test_capability_layer_inventory():
    s = capability_layer_status()
    assert isinstance(s["layer_present"], bool)
    assert "rbac" in s["present"] and "scopes" in s["present"]


def test_security_audit_map_counts():
    evs = [{"type": "config_applied"}, {"type": "webui_token_changed"},
           {"type": "webui_admin_added"}, {"type": "runtime_anomaly_detected"},
           {"type": "config_auto_rolled_back"}]
    m = security_audit_map(evs)
    assert m["config_events"] == 2
    assert m["token_events"] == 1
    assert m["admin_events"] == 1
    assert m["anomaly_count"] == 2


def test_build_read_only_aggregation():
    out = build_platform_summary({"modules": [1]}, [])
    assert out["read_only"] is True
    assert out["runtime"]["modules"] == [1]
    assert "capability" in out and "security" in out
