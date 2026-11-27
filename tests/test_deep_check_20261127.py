"""2026-11-27 深度检测三项修复的守卫测试。

覆盖：
1. iterate 输出预算闸门（避免每天 10 次 × 50s / 32k token 的无效调用）
2. 出站告警落盘（通道全灭时证据不丢）
3. embedding 缓存上限下调到 6000
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


def test_iterate_has_output_budget_gate():
    """补丁生成必须有输出预算闸门。

    生产实证：iterate 连续 20 次 output 恒定 12007 token（撞 max_tokens=12000），
    说明它每次都试图输出整个文件正文被截断，解析必然失败 ——
    owner_tasks 里 76 条 core_patch 全部「LLM 未生成可用白名单补丁」。
    """
    import anima.cognitive.controlled_iteration as ci

    src = Path(ci.__file__).read_text(encoding="utf-8")
    assert "_PATCH_INPUT_CHAR_BUDGET" in src, "缺少输出预算闸门常量"
    assert "_prompt_chars > _PATCH_INPUT_CHAR_BUDGET" in src, "闸门没有被真正使用"
    # 跳过后必须推进冷却，否则每 tick 都会重试同一批超大文件
    assert "自动补丁跳过" in src


def test_outbound_alert_is_persisted():
    """出站失败告警必须先落盘 —— 通道全灭时 urgent 私聊也发不出去。"""
    import anima.channels.onebot11 as ob

    src = Path(ob.__file__).read_text(encoding="utf-8")
    assert "_persist_alert" in src, "缺少落盘方法"
    assert "pending_outbound_alerts.json" in src, "缺少落盘文件名"
    assert "_flush_persisted_alerts" in src, "缺少恢复后补发"


def test_channel_down_alert_exists():
    """NapCat 长时间连不上要告警（生产实证：11 小时、689 次重试无人知晓）。"""
    import anima.channels.onebot11 as ob

    assert hasattr(ob.OneBot11Adapter, "_alert_channel_down")
    assert ob.OneBot11Adapter._WS_ALERT_AFTER_FAILURES >= 3
    src = Path(ob.__file__).read_text(encoding="utf-8")
    assert "_ws_fail_streak" in src


def test_embedding_cache_limit_lowered():
    """缓存上限 8000 -> 6000（满额体积 174MB -> 约 130MB）。"""
    import anima.memory.vector_memory as vm

    src = Path(vm.__file__).read_text(encoding="utf-8")
    assert "_MAX_CACHE_ENTRIES = 6000" in src


def test_alert_persist_roundtrip(tmp_path):
    """落盘 -> 取出 的往返正确，且同文去重。"""
    import anima.channels.onebot11 as ob

    class _P:
        data_dir = str(tmp_path)

    class _Cfg:
        persistence = _P()

    class _Core:
        config = _Cfg()

    inst = object.__new__(ob.OneBot11Adapter)
    inst.core = _Core()

    inst._persist_alert("告警 A")
    inst._persist_alert("告警 A")  # 去重
    inst._persist_alert("告警 B")

    path = tmp_path / "pending_outbound_alerts.json"
    assert path.is_file()
    items = json.loads(path.read_text(encoding="utf-8"))
    assert len(items) == 2, f"应去重后剩 2 条，实得 {len(items)}"

    got = inst._drain_persisted_alerts()
    assert got == ["告警 A", "告警 B"]
    assert not path.exists(), "取出后应清空"
