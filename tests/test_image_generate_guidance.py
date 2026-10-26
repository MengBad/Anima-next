# -*- coding: utf-8 -*-
"""无图生图请求必须注入 generate_image 引导（2026-10-26 根因修复）。

## 事故

```
17:14:32  华初：@MengKing 帮我生成吴语萌大头照
17:14:41  它：看本喵给你捏一个出来喵喵~
               segs=['reply','text']   ← 无 image，零工具调用
```

上一轮我加了「说要画图但没画」的**事后拦截**（`_looks_like_fake_image_claim`），
那只让 bot 不再骗人，**没解决为什么不画**。

## 真根因：`_image_guidance_kind` 无图时直接返回 None

```python
def _image_guidance_kind(text, has_image):
    if not has_image:
        return None          # ← 纯生图请求**完全没有引导**
    ...
```

实测（修复前）：

    「帮我生成吴语萌大头照」        → None
    「帮我生成吴语萌在天上飞的图」   → None

**模型收不到任何「要调 generate_image」的提示** ⇒ 当普通聊天回 ⇒
第一次 ReAct 就返回 content 而非 tool_calls。

（17:09 那两次能出图，是模型**自己想到了** —— 概率性的，不是引导保证的。）

## 修法

补 `generate` 分支：无图 + 生图意图 ⇒ 注入 `_IMAGE_GENERATE_GUIDANCE`。

## 判据（三类形态，缺一个就漏）

A. 动词 + 图类名词：「生成…大头照」「来张壁纸」「捏一个头像」
B. 强生图动词 + **量词** + 对象：「画个猫」「画一只鲸鱼」
   🔴 量词是必需的 —— 缺了会误命中「我不太会画画」「这个画风真好」
C. 「帮我/给我/替我」+ 生图动词 + 量词：「帮我画一个」
"""
from __future__ import annotations

import pathlib

import pytest

from anima.pipeline.stages.generation import (
    _IMAGE_GENERATE_GUIDANCE,
    _IMAGE_GENERATE_RE,
    _image_guidance_kind,
)

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SRC = _ROOT / "anima" / "pipeline" / "stages" / "generation.py"


# ── 1. 核心：无图 + 生图意图 ⇒ generate ──────────────────────────

@pytest.mark.parametrize("text", [
    "帮我生成吴语萌大头照",          # 事故原句
    "帮我生成吴语萌在天上飞的图",     # 事故原句
    "给我来张壁纸",
    "捏一个头像",
    "生成一张海报",
    "画个猫",
    "画一只狗",
    "帮我画一只鲸鱼",
    "帮我画一个",
])
def test_bare_generation_request_gets_guidance(text):
    """🔴 核心：无图 + 生图意图必须返回 generate（否则模型只回文字）。"""
    assert _image_guidance_kind(text, False) == "generate", \
        f"{text!r} 没拿到生图引导"


# ── 2. 不得误判（量词是关键）─────────────────────────────────────

@pytest.mark.parametrize("text", [
    "今天天气不错",
    "这个画风真好",       # 评论画风，不是要图
    "我不太会画画",       # 自述能力，不是要图
    "这画得真好看",
    "画风好奇怪",
    "你好",
    "做个计划",
    "来点想法",
    "生成一个总结",       # 生成的是总结不是图
])
def test_non_generation_request_not_flagged(text):
    """🔴 误判比漏判更贵 —— 每句闲聊都注引导会让 prompt 膨胀且带偏。"""
    assert _image_guidance_kind(text, False) is None, \
        f"{text!r} 被误判成生图请求"


def test_quantifier_is_required():
    """量词是 B 类判据的必要条件。"""
    # 有量词 → 命中
    assert _IMAGE_GENERATE_RE.search("画个猫")
    assert _IMAGE_GENERATE_RE.search("画一只鲸鱼")
    # 无量词 → 不命中（「我不太会画画」里的「画」后面没量词）
    assert not _IMAGE_GENERATE_RE.search("我不太会画画")
    assert not _IMAGE_GENERATE_RE.search("这个画风真好")


# ── 3. 有图时行为完全不变（向后兼容）────────────────────────────

@pytest.mark.parametrize("text,expect", [
    ("两张融合一下", "present"),
    ("把背景改成星空", "semantic"),
    ("翻转一下", "manip"),
    ("这图好看吗", "present"),
])
def test_with_image_unchanged(text, expect):
    """🔴 有图的三态分流不得被新分支影响。"""
    assert _image_guidance_kind(text, True) == expect


def test_generate_branch_only_when_no_image():
    """同一句话，有图时不该走 generate。"""
    t = "帮我生成吴语萌大头照"
    assert _image_guidance_kind(t, False) == "generate"
    # 有图时会落到 present（无改动意图）或 semantic —— 反正不是 generate
    assert _image_guidance_kind(t, True) != "generate"


# ── 4. 引导文案本身 ──────────────────────────────────────────────

def test_guidance_forbids_lying():
    """文案必须明确禁止「说要发图却没发」。"""
    assert "必须调用 generate_image" in _IMAGE_GENERATE_GUIDANCE
    assert "如实说没画成" in _IMAGE_GENERATE_GUIDANCE
    assert "绝不能说" in _IMAGE_GENERATE_GUIDANCE


def test_guidance_mentions_prompt_content():
    """必须告诉模型 prompt 该写什么，否则它可能传空串。"""
    assert "主体" in _IMAGE_GENERATE_GUIDANCE or "prompt" in _IMAGE_GENERATE_GUIDANCE


# ── 5. 接线 ─────────────────────────────────────────────────────

def test_generate_branch_is_wired_in_stage():
    src = _SRC.read_text(encoding="utf-8")
    i = src.index("_image_kind = _image_guidance_kind(")
    window = src[i:i + 1600]
    assert '_image_kind == "generate"' in window, "generate 分支没接线"
    assert "_IMAGE_GENERATE_GUIDANCE" in window


def test_generate_branch_has_log_anchor():
    """没有日志锚 ⇒「引导到底有没有注入」无法从 journal 回测。"""
    src = _SRC.read_text(encoding="utf-8")
    i = src.index('_image_kind == "generate"')
    window = src[i:i + 500]
    assert "kind=generate" in window


def test_old_semantic_manip_present_intact():
    """三道旧分支不得因新增 generate 而删除。"""
    src = _SRC.read_text(encoding="utf-8")
    for kind in ('"semantic"', '"manip"', '"present"'):
        assert f"_image_kind == {kind}" in src, f"{kind} 分支被删了"


# ── 6. 与事后拦截的关系（双保险）────────────────────────────────

def test_both_prevention_and_detection_exist():
    """🔴 事前引导 + 事后拦截都要在。

    事前：`_image_guidance_kind` 的 generate 分支（让它该调工具时调）
    事后：`_looks_like_fake_image_claim`（万一还是没调，不许骗人）
    """
    src = _SRC.read_text(encoding="utf-8")
    assert '_image_kind == "generate"' in src, "缺事前引导"
    guard = (_ROOT / "anima" / "pipeline" / "stages" / "guardrail.py").read_text(
        encoding="utf-8")
    assert "_looks_like_fake_image_claim" in guard, "缺事后拦截"
