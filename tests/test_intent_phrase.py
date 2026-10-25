# -*- coding: utf-8 -*-
"""意图压缩与人格三条（2026-10-25）。

## 这一批修的是什么

生产实证（214 条真实出站 + 13 条 Agent 交付记忆）：

| # | 病 | 证据 |
|---|---|---|
| B1 | 欲望原文被**原样**贴进 system prompt | `desires.json` 存的是「喵呜~ 阿姨天天给你推什么奇怪的东西呀？快跟本喵讲讲嘛！🐾」，`desire.prompt_fragment` 直接拼进去 ⇒ 模型照抄改两字发出去 ⇒ 平均 58.5 字、26% 达标率、出站出现 🐾（人格里根本没有 emoji） |
| B2 | Agent 交付摘要带【步骤N】脚手架 | `（Agent 任务 34281ee486 已完成并交付 275 字：【步骤1】分析主人最近的回复内容…）` 写进 chat_memory，hydrate 时当作「我说过的话」喂回 prompt |
| B3 | 成长快照 values 落的是整句台词 | `"主人是在说咱的毛色像吗？还是想看咱穿兽装呀喵？"` —— 快照该记「我在意什么」，记成了「我打算说什么」 |
| B4 | 人格缺三个硬约束 | 字数有了，但**句数**没有（模型可以拆句绕过字数）；**emoji** 完全没有禁令；**敷衍档**缺失（只有「安静」没有「嗯/6」） |

## 为什么压缩点只能有一个

`compress_intent` 是 B1 与 B3 的**唯一**实现。本项目反复踩过「同一修法在多处
各写一份、只改一半」（见 `cognition.py` 的 `_answer_length_budget_fragment`
docstring：「2026-10-08 修的 bug 正是这个」）。故本测试同时钉住
「三个调用点都走同一个函数」。

## 幂等是硬要求

「源端压一次（desire）+ 展示端再压一次（growth）」的场景真实存在，
若 `compress(compress(x)) != compress(x)` 就会二次失真。Hypothesis 钉住。
"""
import pathlib
import re

import pytest
import yaml
from hypothesis import given, settings, strategies as st

from anima.cognitive.intent_phrase import (
    compress_intent,
    compress_intents,
    strip_emoji,
)

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF]")


@pytest.fixture(scope="module")
def persona() -> str:
    return yaml.safe_load(
        (_ROOT / "config" / "persona_core.yaml").read_text(encoding="utf-8")
    )["system_prompt"]


# ── B1: 意图压缩 ────────────────────────────────────────────────────

def test_compresses_production_desire_verbatim():
    """🔴 生产原文（`desires.json` 实测）必须被压成短语。"""
    raw = "喵呜~ 阿姨天天给你推什么奇奇怪怪的东西呀？快跟本喵讲讲嘛！🐾"
    got = compress_intent(raw)
    assert got == "阿姨天天给你推什么奇奇怪怪的东西"
    assert "🐾" not in got
    assert "？" not in got
    assert len(got) <= 24


def test_compression_drops_emoji():
    assert strip_emoji("好耶🐾🎉") == "好耶"
    assert compress_intent("想跟大家聊天🐾") == "想跟大家聊天"


def test_compression_keeps_plain_intent_untouched():
    """已经是短语的不许改（幂等的前提）。"""
    for s in ("想跟大家聊天", "想自己探索点新东西", "想了解量子纠缠到底是什么"):
        assert compress_intent(s) == s


def test_compression_takes_first_clause_only():
    """复读的典型形态是「一个问题 + 一句催」，取首句即拆掉两拍结构。"""
    got = compress_intent("主人是在说咱的毛色像吗？还是想看咱穿兽装呀喵？")
    assert got == "主人是在说咱的毛色像吗"
    assert "兽装" not in got


def test_compression_keeps_subject_when_it_is_a_noun():
    """🔴 不带分隔符时「主人」是**主语**，不能当称呼语剥掉。"""
    assert compress_intent("主人是在说咱的毛色像吗？") .startswith("主人")


def test_compression_drops_leading_noise_with_separator():
    assert compress_intent("喵呜~ 阿姨天天推奇怪的东西") == "阿姨天天推奇怪的东西"
    assert compress_intent("哼，本喵才懒得理你") == "本喵才懒得理你"


def test_pure_action_descriptor_yields_empty():
    """「（发了一张表情包：撒娇）」不是话题，不许进 prompt。"""
    assert compress_intent("（发了一张表情包：撒娇）") == ""
    assert compress_intent("") == ""
    assert compress_intent("   ") == ""


def test_compression_caps_length():
    long = "这句话非常非常长" * 20
    assert len(compress_intent(long)) <= 24


@settings(max_examples=300, deadline=None)
@given(st.text(alphabet="喵呜啊哈主人呀吧呢？，。！🐾 abc123哼切了的哦啦呗", max_size=45))
def test_compression_is_idempotent(text):
    """🔴 源端压 + 展示端再压，结果必须一致。"""
    once = compress_intent(text)
    assert compress_intent(once) == once


@settings(max_examples=200, deadline=None)
@given(st.text(max_size=60))
def test_compression_output_is_clean(text):
    """输出不得含 emoji / 问号 / 换行。"""
    out = compress_intent(text)
    assert not _EMOJI_RE.search(out)
    assert "？" not in out and "?" not in out
    assert "\n" not in out


def test_compress_intents_dedups_and_limits():
    got = compress_intents(
        ["想跟大家聊天", "想跟大家聊天🐾", "想了解量子纠缠", "", "（发了一张表情包：撒娇）"],
        limit=5,
    )
    assert got == ["想跟大家聊天", "想了解量子纠缠"]


def test_compress_intents_limit():
    got = compress_intents([f"想法{i}" for i in range(10)], limit=3)
    assert len(got) == 3


# ── B1: desire.prompt_fragment 接线 ─────────────────────────────────

def _mk_desire_system(tmp_path):
    from anima.cognitive.desire import DesireSystem
    return DesireSystem(str(tmp_path))


def test_desire_prompt_fragment_compresses(tmp_path):
    """🔴 核心判据：注入的**不得**是原句。"""
    ds = _mk_desire_system(tmp_path)
    ds.add("喵呜~ 阿姨天天给你推什么奇奇怪怪的东西呀？快跟本喵讲讲嘛！🐾",
           intensity=0.5, kind="chat", source_group_id=123,
           expression_scope="internal")
    frag = ds.prompt_fragment(123)
    assert "喵呜" not in frag, "开头的拟声词壳必须被剥掉"
    assert "🐾" not in frag, "emoji 不得进 prompt"
    assert "快跟本喵讲讲嘛" not in frag, "第二个分句必须被丢掉"
    assert "阿姨天天给你推什么奇奇怪怪的东西" in frag


def test_desire_prompt_fragment_skips_action_only_desires(tmp_path):
    """只有动作描述时，整节不注入（不留只有标题的空壳）。"""
    ds = _mk_desire_system(tmp_path)
    ds.add("（发了一张表情包：撒娇）", intensity=0.9, kind="chat",
           source_group_id=123, expression_scope="internal")
    frag = ds.prompt_fragment(123)
    assert "你最近想聊的话题" not in frag


def test_desire_prompt_fragment_renumbers_after_skip(tmp_path):
    """跳过一条后编号必须连续（不能出现 1. 3.）。"""
    ds = _mk_desire_system(tmp_path)
    ds.add("（发了一张表情包：撒娇）", intensity=0.9, kind="chat",
           source_group_id=123, expression_scope="internal")
    ds.add("想跟大家聊天", intensity=0.6, kind="chat",
           source_group_id=123, expression_scope="internal")
    frag = ds.prompt_fragment(123)
    assert "1. 想跟大家聊天" in frag
    assert "2. " not in frag


def test_desire_prompt_fragment_uses_shared_compressor():
    """🔴 纪律：压缩点只有一个 —— 不得在 desire.py 里再写一套正则。"""
    src = (_ROOT / "anima" / "cognitive" / "desire.py").read_text(encoding="utf-8")
    assert "from anima.cognitive.intent_phrase import" in src
    assert "_EMOJI_RE" not in src, "desire.py 里不得自带 emoji 正则（重复实现）"


# ── B3: 成长快照 values ─────────────────────────────────────────────

def test_growth_uses_shared_compressor():
    src = (_ROOT / "anima" / "constitution" / "growth.py").read_text(encoding="utf-8")
    assert "from anima.cognitive.intent_phrase import" in src
    assert "compress_intents" in src


def test_growth_values_no_longer_raw_lines():
    """源码级判据：`values` 必须经过 compress_intents，不能直接 list(...)。"""
    src = (_ROOT / "anima" / "constitution" / "growth.py").read_text(encoding="utf-8")
    assert 'snap["values"] = list(ident.values or [])[:10]' not in src, \
        "仍把 ident.values 原样落盘（整句台词会进快照）"
    assert "compress_intents(" in src


# ── B2: Agent 交付摘要去脚手架 ──────────────────────────────────────

def test_delivery_gist_strips_step_scaffold():
    from anima.agent.owner_agent import OwnerAgent
    body = ("【步骤1】读取文件 data/learning_state.json 的内容\n\n"
            "读到了 3 条学习状态，其中最近一条是关于摄影构图的。")
    got = OwnerAgent._delivery_gist(body)
    assert "【步骤" not in got
    assert "读到了 3 条学习状态" in got


def test_delivery_gist_prefers_tail():
    """末段是定稿；中间产物不该成为摘要。"""
    from anima.agent.owner_agent import OwnerAgent
    body = "【步骤1】解析语料\n语料一共 16 句。\n\n【步骤2】定稿\n最终这版脚本把节奏放慢了。"
    got = OwnerAgent._delivery_gist(body)
    assert "节奏放慢" in got


def test_delivery_gist_empty_when_only_scaffold():
    from anima.agent.owner_agent import OwnerAgent
    assert OwnerAgent._delivery_gist("【步骤1】解析语料 ✓\n【步骤2】编排顺序 ✓") != ""
    assert OwnerAgent._delivery_gist("") == ""
    assert OwnerAgent._delivery_gist("   \n  ") == ""


def test_delivery_gist_caps_length():
    from anima.agent.owner_agent import OwnerAgent
    got = OwnerAgent._delivery_gist("很长的产物" * 100)
    assert len(got) <= OwnerAgent._GIST_MAX_CHARS + 1  # +1 是省略号


def test_chat_memory_note_has_no_task_id_or_step():
    """🔴 核心：写进记忆的摘要**不得**含任务 id / 字数 / 步骤号。"""
    src = (_ROOT / "anima" / "agent" / "owner_agent.py").read_text(encoding="utf-8")
    assert "已完成并交付 %d 字" not in src, "旧的脚手架摘要还在"
    assert "_delivery_gist(body)" in src


# ── B4: 人格三条 ────────────────────────────────────────────────────

def test_persona_has_sentence_count_limit(persona):
    """字数管不住拆句 —— 必须有**句数**上限。"""
    assert "默认一句，最多两句" in persona


def test_persona_forbids_emoji(persona):
    """出站实测出现 🐾，而人格里此前**完全没有** emoji 禁令。"""
    assert "不用 emoji" in persona


def test_persona_has_minimal_reply_tier(persona):
    """缺「敷衍档」时会每句都问/点评。"""
    assert "允许只回一个字" in persona


def test_persona_itself_has_no_emoji_characters(persona):
    """🔴 U2 定律：prompt 里出现的符号会被照搬 —— 禁令本身不许带样例。"""
    assert not _EMOJI_RE.search(persona), \
        "人格里出现了真 emoji 字符（会被模型原样搬进出站）"


def test_persona_keeps_previous_constraints(persona):
    """上几轮的约束不得回退。"""
    assert "25 字" in persona
    assert "不要连续抬杠" in persona
    assert "不需要较劲" in persona
    assert "不要居高临下" in persona
    for w in ("本大爷", "老子", "老娘", "本座", "朕"):
        assert w in persona
    for w in ("两脚兽", "铲屎官"):
        assert w in persona
