# -*- coding: utf-8 -*-
"""出站 emoji / 颜文字剥离（2026-10-25）。

## 为什么 prompt 层不够

人格正本这回补了「不用 emoji，也不用符号画的表情」，但那是**软约束**。
本项目反复验证过「prompt 层约束会被无视」——时段词纠偏（`fix_period_words`）
就是因为模型不听 prompt 才做的第二道闸，注释里写得很清楚。

生产实证（10-25 13:51 出站，人格补丁部署前）：

```
才不是什么量产的笨蛋模型呢~ (￣^￣)
```

这是**颜文字**，不是 emoji。字符全在 CJK 符号区，`_EMOJI_RE` 一个都拦不住
⇒ 光禁 emoji 不够，必须两类都处理。

## 判据

1. emoji 被剥离（🐾 🎉 等）；
2. 颜文字被剥离（`(￣^￣)` / `(｡･ω･｡)` / `QAQ`）；
3. **正文不被破坏**：普通句子原样返回；
4. 剥离后不留双空格/孤立波浪号；
5. 幂等；
6. 护栏真的调用它（源码级判据 —— 函数写了不接线等于没做）。
"""
from __future__ import annotations

import pathlib

import pytest

from anima.cognitive.intent_phrase import strip_kinetic_decorations

_ROOT = pathlib.Path(__file__).resolve().parents[1]


# ── 1. 纯函数 ────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expect", [
    # 🔴 句尾单波浪号**保留** —— 那是人的打字习惯（生产 28% 出站带它），
    #    不是装饰。此前误删导致 3 个既有测试失败。
    ("才不是什么量产的笨蛋模型呢~ (￣^￣)", "才不是什么量产的笨蛋模型呢~"),
    ("好耶🐾", "好耶"),
    ("我超 这个也太离谱了吧 QAQ", "我超 这个也太离谱了吧"),
    ("笑死 (｡･ω･｡)", "笑死"),
])
def test_strips_decorations(raw, expect):
    got, hits = strip_kinetic_decorations(raw)
    assert got == expect, f"{raw!r} → {got!r}（期望 {expect!r}）"
    assert hits >= 1, "命中处数必须 > 0（否则日志锚失效）"


@pytest.mark.parametrize("raw", [
    "天呐这也太抽象了吧，蚌埠住了~",
    "是这样的喵~",
    "笑死~",
    "普通的句子。",
])
def test_single_tilde_is_preserved(raw):
    """🔴 回归：单波浪号是人的打字习惯，不是装饰。

    生产实证：约 28% 的出站在句尾带 `~`。上一版规则把
    `(?<=汉字)\\s*[~～]\\s*` 全部删掉 ⇒ 3 个既有测试（含
    `test_casual_reply_writes_nothing`）立刻失败。
    **把人的说话习惯当装饰清掉，比留下装饰更不像人。**
    """
    got, hits = strip_kinetic_decorations(raw)
    assert got == raw, f"单波浪号被误删：{raw!r} → {got!r}"
    assert hits == 0


@pytest.mark.parametrize("raw", [
    "普通的一句话，没有装饰",
    "唔？怎么又丢过来一张图……",
    "哼，本喵才不当点唱机呢",
])
def test_plain_text_untouched(raw):
    got, hits = strip_kinetic_decorations(raw)
    assert got == raw
    assert hits == 0


def test_emoji_in_middle_is_removed_without_breaking_sentence():
    got, _ = strip_kinetic_decorations("今天🐾天气不错")
    assert got == "今天天气不错"


def test_action_descriptor_is_preserved():
    """「（发了一张表情包：开心）」是**出站动作描述**，不是装饰 —— 不许清掉。"""
    raw = "（发了一张表情包：开心）"
    got, _ = strip_kinetic_decorations(raw)
    assert "表情包" in got, "括号里有汉字 ⇒ 是动作描述，不能被当颜文字删掉"


def test_no_double_space_after_strip():
    got, _ = strip_kinetic_decorations("好  🐾  的")
    assert "  " not in got


def test_blank_line_structure_is_preserved():
    """🔴 回归：`\\n\\n`（空行分段）是排版，不是装饰。

    上一版规则里有 `\\s*\\n\\s*` → `\\n`，把编号列表的段落黏成一坨。
    生产实测踩过：`test_still_detected_and_sampled` 要求
    「样本必须是完整原文」，折叠换行直接把它改了。
    **本函数只处理视觉装饰，不负责重排文本。**
    """
    raw = "A段\n\n1. 第一点\n\n2. 第二点"
    got, hits = strip_kinetic_decorations(raw)
    assert got == raw, f"换行结构被改了：{raw!r} → {got!r}"
    assert hits == 0


def test_empty_and_none_safe():
    assert strip_kinetic_decorations("")[0] == ""
    assert strip_kinetic_decorations(None)[0] == ""


# ── 2. 幂等 ──────────────────────────────────────────────────────────

def test_idempotent_on_production_samples():
    samples = [
        "才不是什么量产的笨蛋模型呢~ (￣^￣)",
        "好耶🐾🎉",
        "哼，本喵才不当点唱机呢……或者拿罐头来求求本大爷嘛~",
    ]
    for s in samples:
        once, _ = strip_kinetic_decorations(s)
        twice, _ = strip_kinetic_decorations(once)
        assert once == twice, f"非幂等：{s!r} → {once!r} → {twice!r}"


# ── 3. 接线（源码级判据）────────────────────────────────────────────

def test_guardrail_calls_decoration_strip():
    """🔴 函数写了不接线 = 等于没做（本项目反复踩过）。"""
    src = (_ROOT / "anima" / "pipeline" / "stages" / "guardrail.py").read_text(
        encoding="utf-8")
    assert "from anima.cognitive.intent_phrase import strip_kinetic_decorations" in src
    assert "strip_kinetic_decorations(" in src
    # 必须在 guardrail_stage 里调用，而不是随便哪处
    body = src.split("async def guardrail_stage", 1)[-1]
    assert "strip_kinetic_decorations(" in body


def test_guardrail_has_log_anchor():
    """剥离必须留痕 —— 否则「到底有没有生效」永远无法从 journal 回测。"""
    src = (_ROOT / "anima" / "pipeline" / "stages" / "guardrail.py").read_text(
        encoding="utf-8")
    body = src.split("async def guardrail_stage", 1)[-1]
    assert "[装饰剥离]" in body, "缺日志锚：无法证明这条护栏跑过"


def test_decoration_strip_precedes_other_rewrites():
    """装饰必须先清 —— 后续骨架/字数规则要基于**真实正文**判断。"""
    src = (_ROOT / "anima" / "pipeline" / "stages" / "guardrail.py").read_text(
        encoding="utf-8")
    body = src.split("async def guardrail_stage", 1)[-1]
    # 用「调用形态」定位，不能用裸函数名 —— `fix_period_words` 也在 import/定义段出现
    _strip_at = body.index("strip_kinetic_decorations(_before)")
    _period_at = body.index("fix_period_words(ctx.reply)")
    assert _strip_at < _period_at, "装饰剥离必须在时段纠偏之前"
