# -*- coding: utf-8 -*-
"""图片引导「分流 → 注入」端到端契约（2026-10-01）。

上一份 `test_image_semantic_routing.py` 只锁纯函数 `_image_guidance_kind` 的三态判定；
本文件锁**它下游**：判定结果是否真的把对应文案写进唯一 system，且
`## 身份锁定（最高优先级）` 仍在末尾（`_inject_runtime_tool_guidance` 的护栏契约）。

回归目标（生产实证）：语义级改图请求（如「把一只浣熊加入到图片里面」）
必须端到端得到「注入含 edit_image 的引导」——
修复前它落到只讲 run_python 的 PRESENT 分支 ⇒ 生产 7 天 edit_image 0 调用。
"""

from anima.pipeline import PipelineContext
from anima.pipeline.stages.generation import (
    _IDENTITY_LOCK_HEADING,
    _IMAGE_MANIP_GUIDANCE,
    _IMAGE_PRESENT_GUIDANCE,
    _IMAGE_SEMANTIC_GUIDANCE,
    _image_guidance_kind,
    _inject_runtime_tool_guidance,
)

RACCOON = "把一只浣熊加入到图片里面"          # 本次线上指令
FAKE_IMAGE = [{"file": "/opt/anima/data/stickers/x.jpg", "url": ""}]

BASE_SYSTEM = "你是 Anima。\n\n## 群聊语境\n今天群里聊了天气。\n\n" + _IDENTITY_LOCK_HEADING + "\n你是 Anima。"


def _mk_ctx(text: str, images=None) -> PipelineContext:
    img = FAKE_IMAGE if images is None else images
    return PipelineContext(
        event={}, text=text, user_id=10001, group_id=727631526,
        detail_type="group", sender_name="群友",
        messages=[{"role": "system", "content": BASE_SYSTEM}],
        inbound_images=img,
    )


def _inject(ctx: PipelineContext) -> tuple[str, bool]:
    """按 generation 阶段的真实分派顺序注入，返回 (kind, injected)。"""
    kind = _image_guidance_kind(ctx.text or "", bool(ctx.inbound_images))
    table = {
        "semantic": _IMAGE_SEMANTIC_GUIDANCE,
        "manip": _IMAGE_MANIP_GUIDANCE,
        "present": _IMAGE_PRESENT_GUIDANCE,
    }
    if kind is None:
        return "", False
    return kind, _inject_runtime_tool_guidance(ctx, table[kind])


def _system(ctx: PipelineContext) -> str:
    return next(m for m in ctx.messages if m["role"] == "system")["content"]


# ---------------------------------------------------------------------------
# ① 语义级请求端到端拿到 edit_image 引导（回归主项）
# ---------------------------------------------------------------------------

def test_raccoon_request_end_to_end_gets_edit_image_guidance():
    ctx = _mk_ctx(RACCOON)
    kind, ok = _inject(ctx)
    assert kind == "semantic"
    assert ok is True
    assert "edit_image" in _system(ctx)


def test_raccoon_guidance_explicitly_forbids_run_python_only():
    """新引导必须点名 run_python 不做语义级，否则模型会退回像素工具。"""
    ctx = _mk_ctx(RACCOON)
    _inject(ctx)
    body = _system(ctx)
    assert "run_python" in body
    assert "不要用 run_python" in body


def test_identity_lock_still_last_after_injection():
    ctx = _mk_ctx(RACCOON)
    _inject(ctx)
    body = _system(ctx)
    assert body.rstrip().endswith("你是 Anima。")
    assert body.rindex(_IDENTITY_LOCK_HEADING) > body.index("edit_image")


def test_system_prompt_mirror_updated():
    ctx = _mk_ctx(RACCOON)
    _inject(ctx)
    assert ctx.system_prompt == _system(ctx)
    assert "edit_image" in ctx.system_prompt


# ---------------------------------------------------------------------------
# ② 另外两态与无图态不得注入错文案
# ---------------------------------------------------------------------------

def test_pixel_request_gets_run_python_guidance():
    ctx = _mk_ctx("把这张图翻转一下")
    kind, ok = _inject(ctx)
    assert kind == "manip"
    assert ok is True
    body = _system(ctx)
    assert "run_python" in body


def test_plain_image_message_gets_present_guidance_without_forcing_edit():
    ctx = _mk_ctx("你在哪得到的这个图，你的目的是什么")
    kind, ok = _inject(ctx)
    assert kind == "present"
    assert ok is True
    body = _system(ctx)
    assert "只有用户明确要求改动时才动工具，不要主动改图" in body


def test_present_guidance_mentions_both_tools():
    """修复点：PRESENT 也不能只讲 run_python。"""
    ctx = _mk_ctx("这是什么意思")
    _inject(ctx)
    body = _system(ctx)
    assert "edit_image" in body and "run_python" in body


def test_no_image_means_no_injection_at_all():
    ctx = _mk_ctx(RACCOON, images=[])
    kind, ok = _inject(ctx)
    assert kind == ""
    assert ok is False
    assert _system(ctx) == BASE_SYSTEM


# ---------------------------------------------------------------------------
# ③ 注入器自身护栏（幂等 / 缺锚不注入）
# ---------------------------------------------------------------------------

def test_injection_is_idempotent():
    ctx = _mk_ctx(RACCOON)
    _inject(ctx)
    once = _system(ctx)
    assert _inject_runtime_tool_guidance(ctx, _IMAGE_SEMANTIC_GUIDANCE) is False
    assert _system(ctx) == once


def test_missing_identity_anchor_skips_injection():
    ctx = _mk_ctx(RACCOON)
    ctx.messages[0]["content"] = "没有身份锁定锚的 system"
    assert _inject_runtime_tool_guidance(ctx, _IMAGE_SEMANTIC_GUIDANCE) is False
    assert _system(ctx) == "没有身份锁定锚的 system"


def test_no_system_message_skips_injection():
    ctx = _mk_ctx(RACCOON)
    ctx.messages = [{"role": "user", "content": "hi"}]
    assert _inject_runtime_tool_guidance(ctx, _IMAGE_SEMANTIC_GUIDANCE) is False
