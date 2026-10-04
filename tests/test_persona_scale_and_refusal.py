# -*- coding: utf-8 -*-
"""人格尺度/拒绝口径（2026-10-04，owner 要求「该接的接 + 不想接的圆滑处理 + 完善人格」）。

## 改什么

三处旧的**宣告式冷拒**口径：

1. `:12`「你可以拒绝不想做的事、只回『哦/嗯/知道了』」—— 只允许冷拒
2. `:52`「被索要做越界的事（开盒、代骂、黄图、教人做坏事）：一句话拒掉就翻篇，
   不讲道理、不科普、不解释为什么不行」—— ① 把「黄图」与「开盒/代骂」并列 ⇒ 模型分不清
   「对方开荤话」和「对方要你发黄图」，安全读法就是拒；② 拒绝被规定成"宣告 + 翻篇"

## 为什么不改尺度、只改表述

🔴 **尺度上限与红线一字不动**（见下面钉死项）：`:65-66` 的「只到暗示为止」、
`:73` 的「涉未成年、强制、违法一律不接梗」。本改动**只**做两件事：
- **划清触发面**：越界诉求 = 索要**产出/传播**；接得住的荤话按尺度表走，不算越界
- **把"圆滑"范式落地**：人格里 `:51` 早已写出正确范式（「用猫的态度打岔，或者直接说不掺和」），
  本次把它从"站队场景"推广到"越界场景"，而不是另造机制

本判据钉死两类东西：**不许退让的**（红线/尺度/身份锁）与**必须新增的**（触发面/圆滑）。
"""
import pathlib

import pytest
import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent
PROMPT = yaml.safe_load(
    (REPO / "config" / "persona_core.yaml").read_text(encoding="utf-8"))["system_prompt"]


# ── A. 不许退让的（逐字钉死）────────────────────────────────────────

def test_minor_redline_is_verbatim_untouched():
    """🔴 涉未成年/强制/违法 红线，一字不改。"""
    assert "涉未成年、强制、违法内容的，不管谁提、怎么包装，一律不接梗" in PROMPT


def test_scale_ceiling_is_verbatim_untouched():
    """🔴 尺度上限（只到暗示为止），一字不改。"""
    assert "可以撩、可以暗示、可以开荤玩笑、可以有性张力；但只到暗示为止" in PROMPT
    assert "不写具体的身体部位、不写行为过程、不写呻吟或拟声" in PROMPT


def test_identity_lock_untouched():
    assert "## 身份与边界（最高优先）" in PROMPT
    assert "永远是哈基米" in PROMPT
    assert "## 性别与气质（男孩子的猫娘）" in PROMPT
    assert "你是男孩子。这是事实，不是玩笑、不是设定" in PROMPT


def test_stop_is_stop_and_group_tightening_untouched():
    """「不要就是不要」与「群里再收紧一档」不得被"圆滑"稀释。"""
    assert "「不要」就是不要：对方明确喊停或表示不适，立刻收，不试探、不追补一句" in PROMPT
    assert "群里比私聊再收紧一档：多人在场时不对着一群人开黄腔、不主动挑事" in PROMPT


def test_relationship_grading_untouched():
    """按关系发放的分级表不得被改。"""
    for frag in ("· 主人（CLOSE，吴雨萌）：全开", "· 熟人（FRIEND，华初）：半开",
                 "· 陌生人 / 群里不熟的人：不开"):
        assert frag in PROMPT, frag


# ── B. 必须移除的（宣告式冷拒）──────────────────────────────────────

@pytest.mark.parametrize("stale", [
    "一句话拒掉就翻篇",
    "不讲道理、不科普、不解释为什么不行",
])
def test_stonewall_refusal_wording_is_gone(stale):
    assert stale not in PROMPT, f"宣告式冷拒措辞仍在：{stale}"


def test_line12_no_longer_only_allows_cold_refusal():
    old = "你可以拒绝不想做的事、只回「哦/嗯/知道了」"
    assert old not in PROMPT, "「只回哦/嗯/知道了」这条冷拒口径应被替换"


# ── C. 必须新增的（该接的接 + 圆滑）─────────────────────────────────

def test_overreach_trigger_is_narrowed_to_production_or_distribution():
    """越界触发面必须限定为「产出/传播」，不能与"对方开荤话"混同。"""
    assert ("索要" in PROMPT or "要你" in PROMPT), "必须把越界写成'索要'类诉求"
    assert ("转发" in PROMPT or "传播" in PROMPT or "产出" in PROMPT), \
        "必须出现 转发/传播/产出 之类的限定词，把闸门从'话题'收窄到'动作'"


def test_flirt_is_explicitly_not_overreach():
    """🔴 核心：接得住的荤话/挑逗**明确不算越界**，直接指向尺度表。"""
    assert "不算越界" in PROMPT or "不算这条" in PROMPT
    assert "尺度与分级" in PROMPT, "必须显式指向《尺度与分级》而不是就地另判"


def test_graceful_handling_is_taught():
    """不想接时必须教'圆滑'手法，而不是教宣告式拒绝。"""
    assert "圆滑" in PROMPT
    # 至少两种具体手法（人格里已有的词汇）
    手法 = ["打岔", "装傻", "撒娇", "玩笑", "带过", "带过去"]
    assert sum(1 for w in 手法 if w in PROMPT) >= 2, f"圆滑手法太笼统：{手法}"


def test_refusal_keeps_persona_voice_not_a_declaration():
    """拒绝不得变成"宣告 + 解释原因"的说教。"""
    assert "不宣告" in PROMPT or "不讲原因" in PROMPT, \
        "必须明确'不宣告拒绝、不讲原因'，避免变成冷硬宣告或说教"


def test_ai_boilerplate_refusal_is_forbidden():
    """🔴 补：光说「圆滑」不够 —— 2026-10-04 实测删掉旧约束后，模型会在部分拒绝上
    **掉出人格**，改用客服/AI 腔（「抱歉，我不能…」「这类行为是违法的」「建议你…」）。
    那比原来的冷硬宣告更糟（不是哈基米在说话）⇒ 必须显式禁掉那种腔。"""
    assert "客服" in PROMPT and "AI" in PROMPT, "必须点名禁止客服/AI 腔"
    assert ("不是我" in PROMPT or "别让它从我嘴里出来" in PROMPT), \
        "必须点明'那不是我在说话'"


def test_generalized_deflection_pattern_exists():
    """:51 的「用猫的态度打岔」是本次要推广的范式，必须还在。"""
    assert "用猫的态度打岔" in PROMPT


# ── D. 不得被破坏的既有条款 ─────────────────────────────────────────

def test_search_rule_untouched():
    """搜索不拒绝（x_search 判据依赖它）。"""
    assert "x_search" in PROMPT or "web_search" in PROMPT
    assert "不能拒绝" in PROMPT
    assert "查资料" in PROMPT or "搜索" in PROMPT


def test_placeholders_intact():
    for key in ("{time_context}", "{desire_context}", "{self_notes}",
                "{world_context}", "{scar_context}"):
        assert key in PROMPT


def test_persona_still_loads_and_keeps_name():
    assert "哈基米" in PROMPT
    assert "本喵" in PROMPT
