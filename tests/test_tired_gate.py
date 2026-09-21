"""社交疲劳 gate 的罐头回复 —— 第 14 个兜底出口（2026-09-21 审计新增）。

背景
----
`llm/handler.py` 的社交疲劳 gate（同群 5 分钟被 @ 超过 `autonomy.fatigue_mention_hard`）
会 `random.choice` 一句罐头直接出站并 `return`。2026-09-21 深审发现它：

1. **无冷却** —— 实测 21:37:29 与 21:39:23 各发一次同一句
   「本喵要午睡一会儿…别戳了喵…」（中间群友仍在 @）；
2. **无计数** —— `handler.py` 全局 `_record_fallback` 出现 **0** 次 ⇒
   旧的"13 出口 / 12 埋点"口径**不全**，机器话术出站占比被低估。

本文件钉住：池化、计数、冷却三件事。
"""
from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

from anima.cognitive.reply_quality import ReplyQuality
from anima.llm.handler import (
    LLMHandler,
    _TIRED_COOLDOWN_SEC,
    _TIRED_REPLIES,
    _record_tired_fallback,
)

_ROOT = Path(__file__).resolve().parents[1]
_HANDLER_SRC = (_ROOT / "anima" / "llm" / "handler.py").read_text(encoding="utf-8-sig")


# ── 1. 池化 ─────────────────────────────────────────────────────────────
def test_tired_pool_has_multiple_variants():
    assert len(set(_TIRED_REPLIES)) >= 2, "疲劳罐头池不足 2 句变体"
    assert len(set(_TIRED_REPLIES)) == len(_TIRED_REPLIES), "池内有重复句"


def test_tired_pool_keeps_persona_voice():
    for text in _TIRED_REPLIES:
        assert "本喵" in text, f"丢了人格口吻: {text!r}"


def test_source_uses_module_level_pool_not_local_list():
    """源码级：调用点不得再内联定义列表（那样本文件的池守卫就看不到它）。"""
    assert "_tired_replies = [" not in _HANDLER_SRC, "本地列表又回来了（守卫看不到池）"
    assert "random.choice(_TIRED_REPLIES)" in _HANDLER_SRC


def test_source_wires_cooldown_and_telemetry():
    """源码级：两个新行为必须在调用点上真的接上，而不是只定义了函数。"""
    assert "self._tired_gate_allow(group_id)" in _HANDLER_SRC
    assert "_record_tired_fallback(self.core)" in _HANDLER_SRC


# ── 2. 计数 ─────────────────────────────────────────────────────────────
def test_record_tired_fallback_lands_in_its_own_bucket(tmp_path):
    rq = ReplyQuality(str(tmp_path))
    _record_tired_fallback(SimpleNamespace(reply_quality=rq))
    rep = rq.report()
    assert rep["fallback_tired"] == 1
    assert rep["fallback_total"] == 1


def test_record_tired_fallback_is_fail_soft():
    """遥测绝不允许影响回复路径：core 形状异常时必须静默返回。"""
    _record_tired_fallback(SimpleNamespace())          # 没有 reply_quality
    _record_tired_fallback(object())                    # 任意对象

    def _boom(kind):
        raise RuntimeError("boom")

    _record_tired_fallback(SimpleNamespace(
        reply_quality=SimpleNamespace(record_fallback=_boom)))


# ── 3. 冷却 ─────────────────────────────────────────────────────────────
def _handler() -> LLMHandler:
    return LLMHandler.__new__(LLMHandler)


def test_tired_cooldown_blocks_immediate_repeat():
    h = _handler()
    assert h._tired_gate_allow(727631526) is True
    assert h._tired_gate_allow(727631526) is False, "同群紧邻的第二次必须被冷却挡住"


def test_tired_cooldown_is_per_group():
    h = _handler()
    assert h._tired_gate_allow(111) is True
    assert h._tired_gate_allow(222) is True, "A 群疲劳不应冷却 B 群"


def test_tired_cooldown_expires():
    h = _handler()
    h._last_tired_at = {1: time.time() - _TIRED_COOLDOWN_SEC - 1}
    assert h._tired_gate_allow(1) is True, "冷却窗过后应恢复"


def test_tired_cooldown_is_fail_open():
    """异常时放行 —— 宁可多发一句，也不可把回复路径卡住。"""
    h = _handler()
    h._last_tired_at = None  # 让 `.get` 抛 AttributeError
    assert h._tired_gate_allow(1) is True
