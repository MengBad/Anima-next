# -*- coding: utf-8 -*-
"""capabilities 片段瘦身：注入时只给「会什么 + 何时用」，执行细节留给 execute

## 为什么（2026-10-05 实测）
`capabilities` 在 `system_token_budget=11000` 里 **100% 命中丢弃**（24h 24/138 回合）。
量化后：
```
base(persona)      = 3368 token
required 合计      = 3384 token
可选池             = 11000 − 3384 = 7616 token
capabilities 片段  =  701 token   ← 只占可选池 9%，但 priority=20（全表最低）⇒ 必然第一个被丢
```

## 🔴 为什么不抬 priority（**零和，且实测是净负**）
| | capabilities | external_exploration |
|---|---|---|
| 24h 效果 | 3 条技能关键词命中 **0/0/0** | `exploration` **33** 次、`x_search` **7 次实调** + `search_web` 6 |
抬 `capabilities`(20→30) 会挤掉 `external_exploration`(25) ⇒ **拿零效果的换有效果的 = 净负**。

## 修法：瘦身，不抬 priority
- 删掉 3 行固定开场白（~60 token）——「遇到合适的场景我会主动调用」这种话
  对模型是废话，真正的信号是能力清单本身。
- 每条能力**不再带 `how_to_use`**（最长 110 字 × N 条 = 大头）
  ⇒ 那是 `execute_capability` 执行时才 reread 的内容，**注入时不需要**。
- 保留：名字 + 触发场景描述 + 置信度（模型据此判断"该不该用"）。

## 判据
1. token 必须显著下降（**自证**：老形态 vs 新形态同一份数据）。
2. 🔴 **能力名与触发条件必须仍在**（丢光了等于片段没用）。
3. 🔴 `how_to_use` 的内容**不得丢失** —— 它在 `execute_capability` 里仍要用（只是不在注入里）。
4. 不动 `top_k` 语义、不动排序（`value_score`）。
5. 空能力列表仍返回空串。
"""

from __future__ import annotations

import json

import pytest

from anima.cognitive.capabilities import CapabilityManager
from anima.pipeline.context import estimate_text_tokens


CAPS = {
    "capabilities": [
        {
            "name": "帮群友翻塔罗牌",
            "description": "群里两脚兽反复喊着要抽塔罗牌时，帮他们翻牌并用大白话解读牌面和运势",
            "how_to_use": "1. 认领两脚兽指定的牌号或随机抓一张大阿卡纳；2. 报出牌名与正逆位；"
                          "3. 用通俗好懂的猫话解释寓意，顺便给两脚兽一句当下的喵式小建议。",
            "confidence": 0.46,
        },
        {
            "name": "丢出今日份猪猪",
            "description": "群里有人喊“/今日猪猪”暗号或者刷搞笑图的时候，本猫猫负责给他们塞一张最新鲜可爱的小猪猪或小动物梗图",
            "how_to_use": "只要看到有人打出“/今日猪猪”或者大家开始跟风甩动物表情包，"
                          "我就去图库里掏出一张搞怪可爱的小猪/动物图回复他们。",
            "confidence": 0.38,
        },
    ]
}


def _mgr(tmp_path, caps=None):
    # 🔴 真实文件名是 `personal_capabilities.json`（`CapabilityManager._path`），
    #    写错名会得到空 manager ⇒ 判据假红。
    (tmp_path / "personal_capabilities.json").write_text(
        json.dumps(caps if caps is not None else CAPS, ensure_ascii=False), encoding="utf-8")
    return CapabilityManager(str(tmp_path))


# ── 1. token 显著下降 ──────────────────────────────────────────────

def test_fragment_is_much_shorter(tmp_path):
    m = _mgr(tmp_path)
    frag = m.build_fragment()
    assert frag, "片段不得为空"

    # 🔴 旧形态 = 3 行开场白 + 每条带 how_to_use（上限 140+110 字）
    old_lines = ["【我的个人技能与方法论】",
                 "以下是我通过自己的经历、研究和思考，一点一点学会并创造出来的方法。",
                 "遇到合适的场景，我会主动调用其中最贴合的那一个。"]
    for cap in CAPS["capabilities"]:
        old_lines.append("\n• %s（我的置信度 %d%%）" % (cap["name"], cap["confidence"] * 100))
        old_lines.append("  " + cap["description"][:140])
        old_lines.append("  我通常这样用：" + cap["how_to_use"][:110])
    old = "\n".join(old_lines)
    old_tok = estimate_text_tokens(old)

    new_tok = estimate_text_tokens(frag)
    assert new_tok < old_tok * 0.65, f"token 未显著下降：old={old_tok} new={new_tok}"
    assert new_tok <= 320, f"目标 ≤320 token，实测 {new_tok}"


def test_how_to_use_not_injected(tmp_path):
    """🔴 `how_to_use` 是执行时才 reread 的内容，**不得**进 system 注入
    （它是最长的一段，且对"该不该用这个能力"的判断无帮助）。"""
    frag = _mgr(tmp_path).build_fragment()
    assert "我通常这样用" not in frag, "how_to_use 引导语仍在注入里"
    assert "认领两脚兽指定的牌号" not in frag, "how_to_use 正文仍在注入里"
    assert "大阿卡纳" not in frag, "how_to_use 细节泄漏进注入"


# ── 2. 🔴 信号必须保留（丢光 = 片段无用）─────────────────────────

def test_capability_names_and_triggers_kept(tmp_path):
    frag = _mgr(tmp_path).build_fragment()
    for cap in CAPS["capabilities"]:
        assert cap["name"] in frag, f"能力名丢失：{cap['name']}"
        # 触发条件的关键信息不得丢
        assert cap["description"][:20] in frag, f"触发描述丢失：{cap['name']}"
    assert "塔罗" in frag and "猪猪" in frag


def test_confidence_kept(tmp_path):
    """置信度是模型判断"敢不敢用"的依据 ⇒ 必须留。"""
    frag = _mgr(tmp_path).build_fragment()
    assert "46%" in frag and "38%" in frag, f"置信度丢失：{frag}"


def test_no_long_filler_prologue(tmp_path):
    """🔴 三行固定开场白是纯废话（~60 token）⇒ 必须去掉。"""
    frag = _mgr(tmp_path).build_fragment()
    for junk in ("以下是我通过自己的经历", "一点一点学会并创造出来的方法",
                 "遇到合适的场景，我会主动调用"):
        assert junk not in frag, f"开场白仍在：{junk}"


# ── 3. 🔴 how_to_use 本身不得丢失（执行时还要用）─────────────────

def test_how_to_use_still_available_for_execute(tmp_path):
    """瘦身只是**不注入**，`execute_capability` 仍要能拿到 how_to_use。"""
    m = _mgr(tmp_path)
    caps = m._data.get("capabilities", [])
    assert caps, "能力数据本身不得被改动"
    for cap in caps:
        assert cap.get("how_to_use"), f"how_to_use 被误删：{cap.get('name')}"
        assert cap.get("description"), f"description 被误删：{cap.get('name')}"


# ── 4. 既有语义不动 ───────────────────────────────────────────────

def test_top_k_and_order_unchanged(tmp_path):
    """🔴 `top_k` 与排序依据是 `value_score` = **使用次数×2 + 修正×0.5 + 新近度**
    （`capabilities.py:234`）—— **刻意不含 `confidence`**
    （用得多的更有价值，而不是"自称置信度高"的）。
    ⇒ 判据必须按 `usage_count` 排序，**不用 confidence**（我第一版写错了）。
    """
    caps = {
        "capabilities": [
            {"name": "低价值", "description": "d" * 10, "how_to_use": "h" * 10,
             "confidence": 0.99, "usage_count": 0},
            {"name": "高价值", "description": "d" * 10, "how_to_use": "h" * 10,
             "confidence": 0.10, "usage_count": 9},
            {"name": "中价值", "description": "d" * 10, "how_to_use": "h" * 10,
             "confidence": 0.50, "usage_count": 3},
        ]
    }
    frag = _mgr(tmp_path, caps).build_fragment(top_k=2)
    assert "高价值" in frag, "top_k 未生效（按 usage_count 应入选）"
    assert "中价值" in frag, "top_k 未生效（按 usage_count 应入选）"
    assert "低价值" not in frag, "top_k 未生效（usage_count=0 应被淘汰）"
    # 顺序：value_score 高的在前 ⇒ usage_count 多的在前
    assert frag.index("高价值") < frag.index("中价值"), "排序被破坏（应按 value_score）"
    # 🔴 置信度低的也要入选 —— 证明排序**不依赖** confidence
    assert "10%" in frag, "置信度 10% 的高价值能力置信度丢失"


def test_empty_returns_empty_string(tmp_path):
    assert _mgr(tmp_path, {"capabilities": []}).build_fragment() == ""


def test_single_line_per_capability(tmp_path):
    """每条能力**一行**（多行会让片段结构松散、占更多 token）。"""
    frag = _mgr(tmp_path).build_fragment()
    bullets = [ln for ln in frag.splitlines() if ln.strip().startswith("•")]
    assert len(bullets) == 2, f"应每条能力一行，实得 {len(bullets)}"
    for b in bullets:
        assert "\n" not in b
