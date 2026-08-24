# -*- coding: utf-8 -*-
"""一生时间线 只读投影 (B2) tests。"""
from anima.webui.life_timeline import label_life_stage, build_life_timeline, LIFE_STAGES


def test_stages_closed_union():
    assert set(LIFE_STAGES) == {"inbound", "deliberation", "tool", "outbound", "degrade", "drive", "governance"}


def test_label_inbound_outbound_tool():
    assert label_life_stage("messages.inbound") == "inbound"
    assert label_life_stage("messages.outbound") == "outbound"
    assert label_life_stage("tool_calls") == "tool"


def test_label_degrade_drive_deliberation_governance():
    assert label_life_stage("runtime_anomaly_detected") == "degrade"
    assert label_life_stage("config_auto_rolled_back") == "degrade"
    assert label_life_stage("sylann_tick") == "drive"
    assert label_life_stage("workflow_cancelled_from_webui") == "deliberation"
    assert label_life_stage("webui_token_changed") == "governance"


def test_build_projection_read_only():
    evs = [{"ts": 1.0, "type": "messages.inbound", "message": "你好"}]
    out = build_life_timeline(evs)
    assert out[0]["stage"] == "inbound"
    assert out[0]["summary"] == "你好"
    assert out[0]["ts"] == 1.0 and out[0]["type"] == "messages.inbound"


def test_build_empty():
    assert build_life_timeline([]) == []
