"""2026-11-27 P0 四项收口的守卫测试。

覆盖：
1. gateway 出站结构约束（段类型白名单 + 内容净化）
2. 交互漏斗（record_inbound / funnel）
3. D-41 台账改判
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


# ── 1. 网关出站结构约束 ─────────────────────────────────────────────

def test_gateway_strips_placeholder_and_local_path():
    """直发路径（主动发言 / Agent 交付 / owner 通知）此前只过滤内心独白，
    占位符与本地路径会原样漏到 QQ。2026-11-27 三起生产事故的直接原因。"""
    from anima.core.gateway import _filter_outbound_message

    out = _filter_outbound_message("别怕～ （附带表情）")
    assert isinstance(out, str)
    assert "附带表情" not in out
    assert "别怕" in out

    out2 = _filter_outbound_message('看这个 <img src="file://D:/x/a.jpg" /> 好看吗')
    assert "img" not in str(out2).lower()
    assert "file://" not in str(out2).lower()
    assert "好看吗" in str(out2)


def test_gateway_rejects_unknown_segment_type():
    """段类型白名单：未知类型段必须丢弃，防止内部结构漏到 QQ。"""
    from anima.core.gateway import _filter_outbound_message

    segs = [
        {"type": "text", "data": {"text": "正常文本"}},
        {"type": "internal_scaffold", "data": {"x": 1}},
        {"type": "image", "data": {"file": "/tmp/a.jpg"}},
    ]
    out = _filter_outbound_message(segs)
    types = [s["type"] for s in out]
    assert "internal_scaffold" not in types, "未授权段类型必须被丢弃"
    assert "text" in types
    assert "image" in types


def test_gateway_drops_non_dict_segment():
    from anima.core.gateway import _filter_outbound_message
    out = _filter_outbound_message([{"type": "text", "data": {"text": "a"}}, "裸字符串段"])
    assert all(isinstance(s, dict) for s in out)


# ── 2. 交互漏斗 ─────────────────────────────────────────────────────

def test_funnel_computes_rates(tmp_path):
    """漏斗必须给出 inbound/replied/gated 三段与占比，供判断
    「交互量低」是流量稀疏还是闸门过紧。"""
    from anima.cognitive.reply_quality import ReplyQuality

    q = ReplyQuality(str(tmp_path))
    for _ in range(10):
        q.record_inbound()
    q.record_reply("回复一", detail_type="group", target_id=1)
    q.record_gate_skip("idle")
    q.record_gate_skip("idle")
    q.record_gate_skip("unfamiliar")

    f = q.funnel()
    assert f["inbound"] == 10
    assert f["replied"] == 1
    assert f["gated"] == 3
    assert f["other"] == 6
    assert 0 <= f["reply_rate"] <= 1
    assert f["gate_breakdown"].get("idle") == 2
    assert f["gate_breakdown"].get("unfamiliar") == 1


def test_inbound_total_persists(tmp_path):
    from anima.cognitive.reply_quality import ReplyQuality
    q = ReplyQuality(str(tmp_path))
    q.record_inbound()
    q.record_inbound()
    raw = json.loads((tmp_path / "reply_quality.json").read_text(encoding="utf-8"))
    day = next(iter(raw["days"]))
    assert raw["days"][day]["inbound_total"] == 2


# ── 3. D-41 台账 ───────────────────────────────────────────────────

def test_d41_marked_fixed():
    """D-41 三条验收已由代码满足（5 处私聊出站 + 6 处 fail-closed +
    owner_notify 节流），台账必须改判，否则持续误导排查方向。"""
    led = Path("ENGINEERING_DEBT_LEDGER.md").read_text(encoding="utf-8")
    assert "只能发群 —— `FIXED`" in led
    assert "2026-11-27 复核：三条验收全部达成" in led


def test_funnel_flags_window_misalignment():
    """分母截断时不能输出误导性比率。

    生产实证（11-28）：inbound=57 而 gated=130 ⇒ gate_rate=228%，逻辑上不可能。
    根因是 inbound_total 当天中途才上线，而 gate_skip 从 00:00 就在累计。
    """
    from anima.cognitive.reply_quality import ReplyQuality
    import tempfile

    q = ReplyQuality(tempfile.mkdtemp())
    for _ in range(57):
        q.record_inbound()
    q.record_reply("x", detail_type="group", target_id=1)
    for _ in range(130):
        q.record_gate_skip("idle")

    f = q.funnel()
    assert f["window_misaligned"] is True
    assert f["reply_rate"] is None
    assert f["gate_rate"] is None


def test_funnel_normal_window_keeps_rates():
    """窗口正常时必须照常给比率（不能让上一项的保护把正常数据也屏蔽）。"""
    from anima.cognitive.reply_quality import ReplyQuality
    import tempfile

    q = ReplyQuality(tempfile.mkdtemp())
    for _ in range(10):
        q.record_inbound()
    q.record_reply("x", detail_type="group", target_id=1)
    q.record_gate_skip("idle")

    f = q.funnel()
    assert f["window_misaligned"] is False
    assert f["reply_rate"] == 0.1
    assert f["gate_rate"] == 0.1
