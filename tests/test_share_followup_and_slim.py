# -*- coding: utf-8 -*-
"""分享→追问链路 + 短句问句豁免 + 健康历史瘦身（2026-10-26）。

## 三个生产问题，一份测试

### 事故 A（09:02:26 ~ 09:02:47）

```
09:02:26  它分享「诶本喵刚刷到一个好玩的，法国那边又开始抗议了，抓了一千多人呢。」
09:02:40  诹访子：「@MengKing 发」      ← 想看详情
09:02:47  它回：「发什么发，当本喵是自动复读机呢喵……」
```

**根因两层**：
1. `_maybe_share` 的 intent 只有 `summary[:60]` ⇒ 它分享时手上只有 60 字；
2. 主动发言路径**不注入探索笔记** ⇒ 它说那句话时自己也不知道细节。

### 事故 B（同类，更普遍）

群友问「你看了什么视频」，它**根本没收到**：

```
prefilter → classify_group_reply_skip → is_familiar_idle_unaddressed
  if len(plain) <= 12:  return True   ← 一刀切
```

实测：
```
 9字「发点人类的视频好吗」      → idle（实际是请求）
 7字「你看了什么视频」          → idle（实际是提问）
10字「视频太长了你自己看去」    → idle（这条确实该拦 ✅）
```

### 问题 C：健康历史 49MB

`upstream_cap.groups`（106 个模型明细）占单条样本 93%，每 30 秒存一份，
环形只保条数不保体积 ⇒ 一年 900MB。

## 判据

1. `_maybe_share` 的 intent 带完整摘要（≥200 字）+ 来源 + 「记住细节」指示；
2. agenda 带 `note_id`，可按 id 取回原文；
3. `followup_fragment` 能按 note_id / 最近分享取回完整笔记；
4. 追问兜底只对「被呼叫 + ≤6 字」生效；
5. 短句**在问它/求它**时不判 idle；纯闲聊仍判；
6. `_slim_cap` 压掉 95%+ 体积且保留趋势字段。
"""
from __future__ import annotations

import json
import pathlib

import pytest

from anima.cognitive.agenda import AgendaItem
from anima.core.health_history import _slim_cap
from anima.llm.group_gate import (
    _looks_like_question_or_request,
    is_familiar_idle_unaddressed,
)

_ROOT = pathlib.Path(__file__).resolve().parents[1]


# ── 1. agenda 带 note_id ────────────────────────────────────────────

def test_agenda_item_has_note_id_field():
    item = AgendaItem(727631526, "测试意图", note_id="explore_abc123")
    assert item.note_id == "explore_abc123"


def test_agenda_item_note_id_defaults_empty():
    """旧 agenda.json 缺该键也不能炸（尾部带默认值）。"""
    item = AgendaItem(727631526, "测试意图")
    assert item.note_id == ""


def test_propose_agenda_accepts_note_id():
    src = (_ROOT / "anima" / "core" / "anima_core.py").read_text(encoding="utf-8")
    i = src.index("def propose_agenda")
    window = src[i:i + 1200]
    code = "\n".join(ln for ln in window.splitlines()
                     if not ln.strip().startswith("#"))
    assert "note_id: str = \"\"" in code, "propose_agenda 没有 note_id 参数"
    assert "note_id=str(note_id or \"\")" in code, "note_id 没传进 AgendaItem"


# ── 2. 分享时给足细节 ───────────────────────────────────────────────

def _maybe_share_code() -> str:
    """取 `_maybe_share` 的**代码行**（去掉注释，避免文档里的旧值误判）。"""
    src = (_ROOT / "anima" / "cognitive" / "exploration.py").read_text(encoding="utf-8")
    i = src.index("def _maybe_share")
    window = src[i:i + 4000]
    return "\n".join(ln for ln in window.splitlines()
                     if not ln.strip().startswith("#"))


def test_share_intent_carries_full_summary():
    """🔴 不能只给 60 字 —— 那正是它答不上细节的原因。"""
    code = _maybe_share_code()
    assert "summary[:60]" not in code, "仍是 60 字摘要（会答不上追问）"
    assert "[:400]" in code, "没有给足摘要长度"


def test_share_intent_tells_model_to_remember_details():
    """必须明确告知「被追问时讲这些，不要当成让你复读」。"""
    code = _maybe_share_code()
    assert "记住" in code
    assert "复读" in code


def test_share_passes_note_id_to_agenda():
    code = _maybe_share_code()
    assert "note_id=" in code, "没有把 note_id 传给 propose_agenda"


# ── 3. followup_fragment 取回原文 ───────────────────────────────────

class _FakeExploration:
    """最小桩：只测 followup_fragment 的取值逻辑。"""

    def __init__(self, notes):
        self.state = {"notes": notes}

    from anima.cognitive.exploration import ExplorationManager as _EM
    by_id = _EM.by_id
    last_shared_note = _EM.last_shared_note
    followup_fragment = _EM.followup_fragment


def _note(nid, shared=True, ts=100.0):
    return {
        "id": nid,
        "title": "法國再爆大規模示威",
        "summary": "法国周六爆发大规模全国抗议，数十万民众参与，53名警员受伤，"
                   "全国超过1300人被捕。该事件反映了当地持续存在的社会矛盾。",
        "source": "BBC 中文",
        "url": "https://www.bbc.com/zhongwen/articles/x",
        "shared": shared,
        "observed_at": ts,
    }


def test_followup_by_id_returns_full_note():
    ex = _FakeExploration([_note("explore_a", ts=1.0), _note("explore_b", ts=2.0)])
    got = ex.followup_fragment("explore_a")
    assert "法國再爆大規模示威" in got
    assert "1300人被捕" in got, "摘要被截断了"
    assert "BBC" in got


def test_followup_falls_back_to_last_shared():
    """旧数据没 note_id 时，退到最近分享的那条。"""
    ex = _FakeExploration([
        _note("old", ts=1.0),
        _note("newer", ts=99.0),
    ])
    got = ex.followup_fragment("")
    assert "1300人被捕" in got


def test_followup_prefers_shared_notes():
    """未分享过的笔记不当成"刚才分享的那条"。"""
    ex = _FakeExploration([
        {"id": "x", "title": "未分享", "summary": "内容", "shared": False,
         "observed_at": 999.0},
    ])
    assert ex.followup_fragment("") == ""


def test_followup_unknown_id_falls_back():
    ex = _FakeExploration([_note("explore_a")])
    got = ex.followup_fragment("完全不存在的id")
    assert "1300人被捕" in got, "未知 id 应退到最近分享"


def test_followup_empty_state_returns_empty():
    ex = _FakeExploration([])
    assert ex.followup_fragment("") == ""
    assert ex.followup_fragment("explore_a") == ""


def test_followup_tells_model_it_is_a_followup():
    """必须写明「对方在追问，不是让你复读」—— 这正是事故的误解点。"""
    ex = _FakeExploration([_note("a")])
    got = ex.followup_fragment("a")
    assert "追问" in got
    assert "复读" in got


# ── 4. 追问兜底接线 ────────────────────────────────────────────────

def test_cognition_wires_followup_fallback():
    src = (_ROOT / "anima" / "pipeline" / "stages" / "cognition.py").read_text(
        encoding="utf-8")
    i = src.index("自主探索笔记")
    window = src[i:i + 2500]
    assert "followup_fragment" in window, "追问兜底没接线"
    assert "is_direct_group_call" in window, "没判「是否被呼叫」"


def test_followup_judgement_is_narrow():
    """判据必须窄：被呼叫 + ≤6 字。放宽会把普通对话都当追问。"""
    src = (_ROOT / "anima" / "pipeline" / "stages" / "cognition.py").read_text(
        encoding="utf-8")
    i = src.index("followup_fragment")
    window = src[max(0, i - 900):i]
    assert "<= 6" in window, "追问长度判据缺失或过宽"


# ── 5. 短句问句豁免 ────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "发点人类的视频好吗",
    "你看了什么视频",
    "你最近刷到什么好玩的",
    "详细说说",
    "哪看的",
    "你觉得呢",
    "你推荐点",
])
def test_question_or_request_not_idle(text):
    """🔴 在问它/求它 ⇒ 不判 idle（否则群友以为它不会回答）。"""
    assert is_familiar_idle_unaddressed({}, text) is False, \
        f"{text!r} 被误判为闲聊"


@pytest.mark.parametrize("text", [
    "视频太长了你自己看去",   # 含「你」但是陈述
    "哈哈哈",
    "在吗",
    "约吗",
    "嗯",
    "你干嘛",
])
def test_pure_chatter_still_idle(text):
    """纯闲聊/陈述仍要拦（熟人乱插话是真实问题）。"""
    assert is_familiar_idle_unaddressed({}, text) is True, \
        f"{text!r} 应仍判 idle"


def test_question_helper_is_pure_function():
    assert _looks_like_question_or_request("") is False
    assert _looks_like_question_or_request("什么") is True


def test_long_text_unaffected_by_exemption():
    """超过 12 字的本来就不判 idle，豁免不影响它们。"""
    long = "今天天气真不错啊我们去公园散步吧好不好呀"
    assert is_familiar_idle_unaddressed({}, long) is False


# ── 6. 健康历史瘦身 ────────────────────────────────────────────────

def _big_cap(n_models=106):
    return {
        "reachable": True, "range": "24h", "captured_at": 1792972735.0,
        "error": "", "retained_since": None, "last_used": None,
        "latency_ms": 6, "summary": None, "configured": True,
        "groups": [
            {"provider": f"p{i}", "model": f"m{i}", "alias": f"a{i}",
             "reasoning_effort": "", "requests": 2, "failed_requests": 2,
             "failure_status": 400, "success_rate": 0.0, "all_failed": True,
             "average_latency_ms": 91.4}
            for i in range(n_models)
        ],
        "broken_groups": [],
        "failed_models": [f"m{i}" for i in range(5)],
    }


def test_slim_cap_shrinks_drastically():
    """🔴 核心：必须压掉 95%+ 体积。"""
    cap = _big_cap()
    before = len(json.dumps(cap, ensure_ascii=False))
    after = len(json.dumps(_slim_cap(cap), ensure_ascii=False))
    assert after < before * 0.05, f"只压到 {after}/{before}，不够"


def test_slim_cap_keeps_trend_fields():
    """趋势字段必须保留（WebUI 与判读在用）。"""
    got = _slim_cap(_big_cap())
    for k in ("reachable", "range", "latency_ms", "groups_count",
              "all_failed_count", "failed_models", "upstream_reachable"):
        assert k in got, f"丢了 {k}"


def test_slim_cap_drops_groups_detail_only():
    """🔴 只删 `groups` 大数组（25KB/条，占 93%）。

    `failed_models` 是**小字典**（模型名 → 次数，约 538 字节），
    它是「哪个模型在挂」的直接证据 ⇒ 必须保留。
    """
    got = _slim_cap(_big_cap())
    assert "groups" not in got, "groups 明细没删（体积元凶）"
    assert "failed_models" in got, "failed_models 被误删（判读要用）"


def test_slim_cap_counts_are_correct():
    got = _slim_cap(_big_cap(n_models=7))
    assert got["groups_count"] == 7
    assert got["all_failed_count"] == 7
    assert got["failed_models"] == [f"m{i}" for i in range(5)]


def test_slim_cap_fail_closed_on_bad_input():
    """非 dict ⇒ reachable=None，绝不伪造健康。"""
    for bad in (None, [], "x", 42):
        assert _slim_cap(bad) == {"reachable": None}


def test_record_uses_slim_cap():
    src = (_ROOT / "anima" / "core" / "health_history.py").read_text(encoding="utf-8")
    assert '"upstream_cap": _slim_cap(cap)' in src, "record() 没用瘦身版"
