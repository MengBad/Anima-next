# -*- coding: utf-8 -*-
"""求答轮次的「说多少」指令（2026-10-08）

## 为什么存在这个开关
`max_tokens` 在本项目上游（CPA 代理）**被完全忽略** —— 实测：

| 参数 | 值 | 实际输出 |
|---|---|---|
| `max_tokens` | 16 | **6421 字** |
| `max_completion_tokens` | 16 | **4626 字** |
| system 里写「≤150 字」 | — | **117 字** ✅ |
| system 里写「≤80 字」 | — | **72 字** ✅ |

且第二条实测在**生产人格（2925 字）作 system** 时同样成立。
⇒ `soul.map_max_tokens` 是**坏掉的旋钮**；system 的字节指令才是唯一可用的长度旋钮。

## 判据
1. 默认（未配置）⇒ **不注入**（零行为变更，与从前逐字相同）。
2. 开启 + 求答 ⇒ 注入，且带上预算数字。
3. 开启 + 求答 + **用户自己说要详细** ⇒ 不注入
   （🔴 实测：不排除就会把「请详细讲讲」也压成 121 字）。
4. 非求答（闲聊/情绪/主动发言无文本）⇒ 不注入。
5. **防哑开关**：`AnswerLengthBudgetConfig` 必须是 `ContextConfig` 的显式字段 ——
   pydantic 在本项目是 `extra=ignore`，只写 yaml 不加字段会被**静默丢弃**。
6. 注入点必须是 `protected=True`（被预算器静默裁掉 = 规则没生效且无人知道）。
"""

from __future__ import annotations

import io
import re
from types import SimpleNamespace

import pytest

import anima.pipeline.stages.cognition as _cog
from anima.core.config import AnswerLengthBudgetConfig, ContextConfig
from anima.pipeline import PipelineContext
from anima.pipeline.stages.cognition import _answer_length_budget_fragment
from anima.pipeline.stages.guardrail import _user_wants_substantive

# 生产原句（10-03 私聊被截那条；既无问号也无疑问词，靠 ≥14 字陈述式兜底）
PROD_ASK = "医生给我开了思诺思和文拉法辛和富马酸喹硫平"
CASUAL = "哈哈哈哈"
WANTS_DETAIL = "帮我详细讲讲文拉法辛的作用机制，展开说一下"


def _core(enabled: bool, budget: int = 200, exclude: bool = True):
    cfg = ContextConfig(answer_length_budget={
        "enabled": enabled, "budget_chars": budget,
        "exclude_when_user_wants_detail": exclude,
    })
    return SimpleNamespace(config=SimpleNamespace(context=cfg))


def _ctx(text: str) -> PipelineContext:
    return PipelineContext(event={}, text=text, user_id=1, group_id=2,
                           detail_type="group", sender_name="A", meta={})


# ── 1. 默认关 ⇒ 零行为变更 ───────────────────────────────────────

def test_default_off_no_injection():
    assert AnswerLengthBudgetConfig().enabled is False, "默认必须是关"
    assert _answer_length_budget_fragment(_ctx(PROD_ASK), _core(False)) == ""


def test_default_off_even_for_ask():
    """默认配置对象（未显式传 enabled）同样不得注入。"""
    core = SimpleNamespace(config=SimpleNamespace(context=ContextConfig()))
    assert _answer_length_budget_fragment(_ctx(PROD_ASK), core) == ""


# ── 2. 开启 + 求答 ⇒ 注入且带预算 ────────────────────────────────

def test_enabled_and_ask_injects_budget():
    got = _answer_length_budget_fragment(_ctx(PROD_ASK), _core(True, 200))
    assert got, "开启后求答轮次必须注入"
    assert "200" in got, f"注入文本未带预算数字：{got!r}"


# ── 2b. 与人格「25 字」的分界必须写明（2026-10-25）──────────────────

def test_budget_declares_scope_against_persona_limit():
    """🔴 生产实证：同一份 system 里人格写「25 字」、本片段写「200 字」，
    两个数字打架，模型挑宽松的 ⇒ 实测平均 58.5 字（正好在两者之间）。

    真相是各有适用面（25 字管闲聊 / 200 字管求答），但 prompt 里没写分界。
    ⇒ 本片段必须显式声明它只覆盖「求答」，且不得被读成放宽日常长度。
    """
    got = _answer_length_budget_fragment(_ctx(PROD_ASK), _core(True, 200))
    assert "闲聊" in got, "必须点明这条不覆盖闲聊"
    assert "人格" in got, "必须显式让人格规则优先"
    assert "别把它当成放宽" in got, "必须禁止把它读成放宽日常长度的许可"


@pytest.mark.parametrize("budget", [120, 200, 350])
def test_budget_value_is_carried(budget):
    got = _answer_length_budget_fragment(_ctx(PROD_ASK), _core(True, budget))
    assert str(budget) in got


# ── 3. 用户自己要详细 ⇒ 不注入（实测教训）────────────────────────

def test_user_wants_detail_not_squeezed():
    """🔴 实测：人格 + ≤150 字时，用户说「请详细讲讲，越多越好」模型仍只写 121 字。

    不排除就等于「用户要详细也拿不到」。
    """
    got = _answer_length_budget_fragment(_ctx(WANTS_DETAIL), _core(True, 200))
    assert got == "", f"用户要详细时不得注入长度上限：{got!r}"


def test_exclude_can_be_disabled_explicitly():
    """开关可关掉排除逻辑（用于 A/B）—— 此时用户要详细也照样注入。"""
    got = _answer_length_budget_fragment(
        _ctx(WANTS_DETAIL), _core(True, 200, exclude=False)
    )
    assert got, "exclude_when_user_wants_detail=False 时应照常注入"


# ── 4. 非求答 ⇒ 不注入 ───────────────────────────────────────────

@pytest.mark.parametrize("text", [CASUAL, "", "今天真的好累啊感觉要猝死"])
def test_non_ask_not_injected(text):
    assert _answer_length_budget_fragment(_ctx(text), _core(True)) == "", \
        f"{text!r} 不是求答轮次，不得注入"


# ── 5. 防哑开关：字段必须显式存在于 ContextConfig ─────────────────

def test_config_field_is_declared():
    """pydantic `extra=ignore` ⇒ 只写 yaml 不加字段会被静默丢弃（F9.1 G-01 同类）。"""
    assert "answer_length_budget" in ContextConfig.model_fields, (
        "ContextConfig 缺 answer_length_budget 字段 ⇒ config.yaml 的写入会被静默丢弃"
    )


def test_config_roundtrip_from_mapping():
    """按 **yaml 加载的真实形态**（dict）解析后，值必须真的拿到。"""
    c = ContextConfig.model_validate(
        {"answer_length_budget": {"enabled": True, "budget_chars": 150}}
    )
    assert c.answer_length_budget.enabled is True, "enabled 被静默丢弃（哑开关）"
    assert c.answer_length_budget.budget_chars == 150


def test_budget_chars_bounds():
    assert AnswerLengthBudgetConfig(budget_chars=0).budget_chars == 0
    with pytest.raises(Exception):
        AnswerLengthBudgetConfig(budget_chars=-1)
    with pytest.raises(Exception):
        AnswerLengthBudgetConfig(budget_chars=99999)


def test_zero_budget_means_no_injection():
    assert _answer_length_budget_fragment(_ctx(PROD_ASK), _core(True, 0)) == ""


# ── 6. 注入点必须是 protected（不可裁）────────────────────────────

def test_injection_point_is_protected():
    """长度指令被预算器静默裁掉 = 规则没生效且无人知道（「判据锚不存在」同类）。"""
    src = io.open(_cog.__file__, encoding="utf-8", errors="replace").read()
    m = re.search(r'add_system_fragment\(\s*"answer_length_budget",[^)]*\)', src, re.S)
    assert m, "找不到 answer_length_budget 的注入调用"
    assert "protected=True" in m.group(0), (
        f"注入未设 protected=True ⇒ 预算压力下会被静默裁掉：{m.group(0)!r}"
    )


def test_has_log_anchor():
    """没有日志锚 ⇒ 「指令到底注入没有」无法从 journal 回测。"""
    src = io.open(_cog.__file__, encoding="utf-8", errors="replace").read()
    assert "[长度预算] 注入上限=" in src, "缺日志锚，无法回测是否生效"


# ── 7. 「要详细」词表覆盖率（本功能的排除项依赖它）─────────────────

#: 16 个**现实说法**：原词表只命中 5 个（漏网 11 个 = 69%）。
_DETAIL_PHRASES = [
    "详细讲讲", "展开讲讲", "展开说说", "多说点", "多讲讲", "细说一下",
    "详细说说", "展开讲", "解析一下", "帮我分析", "介绍一下", "多说几句",
    "讲详细点", "详细一点", "展开说说吧", "再多说点",
]


@pytest.mark.parametrize("phrase", _DETAIL_PHRASES)
def test_detail_phrases_recognized(phrase):
    """🔴 判据漏网 ⇒ 用户明确要详细却被压成短答（实测：模型只写 121 字）。"""
    assert _user_wants_substantive(phrase) is True, f"漏网：{phrase!r}"


#: 泛疑问 / 寒暄 —— 扩表**不得**把它们收进来。
_CHATTER = [
    "在吗", "怎么了", "哈哈哈哈哈", "什么鬼", "为什么啊",
    "今天好累啊", "你说得对", "本喵刚打了个嗝", "这个怎么说呢",
]


@pytest.mark.parametrize("text", _CHATTER)
def test_chatter_not_over_exempted(text):
    """🔴 扩表改了"要详细"就放宽，若把泛疑问/寒暄也收进来 = 给所有口语开后门。"""
    assert _user_wants_substantive(text) is False, f"过度豁免：{text!r}"


@pytest.mark.parametrize("phrase", ["详细讲讲", "多说点", "细说一下"])
def test_detail_phrase_blocks_budget_injection(phrase):
    """端到端：用户要详细 ⇒ 不注入长度上限。"""
    got = _answer_length_budget_fragment(_ctx(f"关于文拉法辛，{phrase}"), _core(True))
    assert got == "", f"{phrase!r} 是要详细，不该被压：{got!r}"
