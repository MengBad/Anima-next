# -*- coding: utf-8 -*-
"""图像操作「语义级 vs 像素级」分流契约（2026-10-01）。

背景（详见 reports/2026-10-01_instruction-recognition-raccoon.md）：
「@MengKing 把一只浣熊加入到图片里面」在原实现下**不被识别为改图意图**
（`_IMAGE_MANIP_RE` 不含「加入」等语义动词），落进 `_IMAGE_PRESENT_GUIDANCE`；
而该文案通篇只讲 `run_python`，与 `edit_image` 的工具契约
（「语义级修改必须用 edit_image」）互相打架 —— 引导把模型指错了方向。
生产实证：`edit_image` 7 天 0 次调用。

本文件锁定修复后的分流契约：
- 语义级（加入/换主体/换背景/改画风…）→ 明确点名 `edit_image`
- 像素级（翻转/缩放/滤镜/加文字…）→ `run_python`
- 有图且无操作意图 → 仅告知「有图」，不诱导改图

additive 原则：`_IMAGE_MANIP_RE` **保持宽匹配不收窄**
（同名契约见 test_agentic_harness.py::test_image_manip_regex_is_intentionally_broad）。
"""
from __future__ import annotations

import anima.pipeline.stages.generation as gen


# ── helpers：模块属性访问，避免整模块 import 失败（RED 阶段可逐条报错）──
def _kind(text: str, has_image: bool = True):
    fn = getattr(gen, "_image_guidance_kind", None)
    assert fn is not None, "_image_guidance_kind 未实现（RED 预期）"
    return fn(text, has_image)


def _semantic_re():
    r = getattr(gen, "_IMAGE_SEMANTIC_RE", None)
    assert r is not None, "_IMAGE_SEMANTIC_RE 未定义（RED 预期）"
    return r


def _semantic_guidance() -> str:
    g = getattr(gen, "_IMAGE_SEMANTIC_GUIDANCE", None)
    assert g is not None, "_IMAGE_SEMANTIC_GUIDANCE 未定义（RED 预期）"
    return g


# ══════════════ §1 语义级改图：必须命中 ══════════════
SEMANTIC_HITS = [
    "把一只浣熊加入到图片里面",          # ← 本次事件原句
    "把一只浣熊加入图片",                # 无「到」
    "在图片里加一只猫",
    "给我加一个太阳",
    "帮我在图里加几朵云",
    "图里加一头牛",
    "把这只猫加进去",
    "往右侧加一张桌子",
    "放一只小狗在画面角落",
    "把这个人放进星空里",
    "塞进一只兔子",
    "合成到背景里",                       # 「合成进」
    "把这只猫P上去",
    "p只鸟上去",
    "帮我P一只猫上去",
    "把背景换成海边",
    "把这个人换成猫",
    "把风格改成二次元",
    "改成水墨画风",
    "整成油画的样子",
    "换画风",
    "换个风格",
    "风格迁移",
    "改一下人物表情",
    "换一下服装",
    "改背景",
    "把表情改成笑",
    "把发型换成短发",
]


def test_semantic_hits():
    r = _semantic_re()
    misses = [t for t in SEMANTIC_HITS if not r.search(t)]
    assert not misses, f"语义级改图漏判: {misses}"


def test_core_sentence_from_incident_routes_semantic():
    # 事件原句：必须是 semantic（修的就是这一条）
    assert _kind("把一只浣熊加入到图片里面") == "semantic"


# ══════════════ §2 反向对照：像素级/无关句不得误判为语义 ══════════════
SEMANTIC_MUST_NOT_HIT = [
    "把这张图翻转一下",       # 像素：应走 manip
    "给我放大这张照片",
    "这个滤镜怎么加",         # 含「加」但是像素滤镜
    "加个字",                 # 关键：加文字 = run_python 能做的像素级
    "加上一行字",
    "加个字幕",
    "帮我加个水印",           # 水印 = 贴图，像素级
    "看看这张图压缩一下",
    "编辑文档",               # 与图无关
    "处理一下",
    "这个背景真好看",         # 关键：孤立名词，无操作动词 ⇒ 绝不诱导改图
    "这是二次元图吗",         # 关键：孤立风格词
    "这图好写实",
    "抠个图",
    "调亮一点",
]


def test_semantic_does_not_over_match():
    r = _semantic_re()
    bad = [t for t in SEMANTIC_MUST_NOT_HIT if r.search(t)]
    assert not bad, f"语义级误判（应为像素/无关）: {bad}"


def test_pixel_ops_still_route_manip():
    # 像素级不得被语义吃掉（additive：MANIP 面保持覆盖）
    for t in ["把这张图翻转一下", "给我放大这张照片", "这个滤镜怎么加"]:
        assert _kind(t) == "manip", t


def test_add_text_stays_pixel():
    # 「加字」是 run_python 的活 → 绝不能走 semantic
    for t in ["加个字", "加上一行字", "帮我加个水印"]:
        assert _kind(t) != "semantic", t


# ══════════════ §3 分流优先级与三态 ══════════════
def test_no_image_returns_none():
    assert _kind("把一只浣熊加入到图片里面", has_image=False) is None


def test_no_operation_intent_returns_present():
    assert _kind("这张图真好看", has_image=True) == "present"
    assert _kind("这是谁", has_image=True) == "present"
    assert _kind("", has_image=True) == "present"


def test_semantic_takes_priority_over_manip():
    # 「换个背景」同时命中 _IMAGE_MANIP_RE（换[背个]→「换个」）与语义 ⇒ 语义优先
    assert _kind("换个背景") == "semantic"
    assert gen._IMAGE_MANIP_RE.search("换个背景"), "前提：MANIP 也命中该句"
    assert gen._IMAGE_SEMANTIC_RE.search("换个背景"), "前提：语义亦命中"


def test_kind_is_exhaustive():
    for t in ["把一只浣熊加入到图片里面", "翻转一下", "好看", ""]:
        assert _kind(t) in {"semantic", "manip", "present"}


# ══════════════ §4 引导文案：必须点名正确的工具 ══════════════
def test_semantic_guidance_names_edit_image():
    g = _semantic_guidance()
    assert "edit_image" in g, "语义级引导必须点名 edit_image"
    assert "run_python" in g, "需说明与 run_python 的分工"


def test_present_guidance_teaches_both_tools():
    g = gen._IMAGE_PRESENT_GUIDANCE
    assert "edit_image" in g, "有图引导必须让模型知道 edit_image 存在"
    assert "run_python" in g


def test_present_guidance_does_not_induce_edits():
    # 有图但无操作意图时不得主动改图
    g = gen._IMAGE_PRESENT_GUIDANCE
    assert "不要主动改图" in g or "只有用户明确要求" in g


def test_manip_guidance_still_names_run_python():
    g = gen._IMAGE_MANIP_GUIDANCE
    assert "run_python" in g
    assert "edit_image" in g, "像素引导亦需指出语义需求应改走 edit_image"


def test_semantic_guidance_requires_real_image():
    # 无原图时应如实索要，不得凭空生成
    g = _semantic_guidance()
    assert "图" in g


# ══════════════ §5 既有契约不被破坏（additive 回归）══════════════
def test_manip_regex_remains_broad():
    # 与 test_agentic_harness 的同名契约定向一致：不得收窄
    for t in ["处理一下", "编辑文档", "压缩文件", "把这张图翻转一下"]:
        assert gen._IMAGE_MANIP_RE.search(t), t
