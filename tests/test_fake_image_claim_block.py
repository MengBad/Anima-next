# -*- coding: utf-8 -*-
"""对话层「说要画图但没画」的拦截（2026-10-26 第二轮事故）。

## 事故

```
17:14:32  华初：@MengKing 帮我生成吴语萌大头照
17:14:41  它：哼，怎么又来找本喵变图……看本喵给你捏一个出来喵喵~
               segs=['reply','text']          ← 无 image 段
17:15:25  华初：在哪
17:15:35  它：在这儿呢，爪子刚伸出来，瞅啥呐喵~
               segs=['reply','text']          ← 又无 image 段
```

两次 `generation_stage` 里**零次工具调用**，`generated_image_paths` 为空。

## 为什么上一轮的 `_CLAIM_DONE_RE` 拦不住

上一轮（10-26 早）加的词表是**完成态**：

    「已经办妥 / 弄好了 / 做好了 / 完成了 / 这就去办」

而这次它说的是**将来时/进行时的承诺**：

    「看本喵给你**捏一个**出来」    ← 承诺
    「**在这儿呢**」               ← 假装已交付

语义上更隐蔽，对用户伤害相同：用户以为图在路上，实际什么都没有。

## 判据

1. 回复含「画/捏/做/生成」+ 交付语气（给你捏/在这儿呢/接好/喏…）
2. **且**本回合无图片交付证据：
   · `ctx.generated_image_paths` 空
   · `tool_trace` 无成功的生图类工具
   · `generation_tool_calls` 无生图类工具
   · `ctx.inbound_images` 也空（入站就有图 ⇒ 可能是"这就是你说的那张"）

## 处置：**改写**，不是拒发

与「编造任务完成」的处理不同：
· 编造完成 → 拒发（它会牵出「结果呢/在哪」的追问，越解释越假）
· 编造出图 → 改写为一句诚实的话（用户能理解，也知道怎么重试）
  「……本喵没画成，图没出来。你再说一次，本喵重试喵。」
  拒发会让用户觉得 bot 死了。
"""
from __future__ import annotations

import asyncio
import pathlib

import pytest

import anima.pipeline.stages.guardrail as _g
from anima.core.config import AnimaConfig
from anima.pipeline import PipelineContext, StageResult

_ROOT = pathlib.Path(__file__).resolve().parents[1]

#: 事故两条原话（逐字）
_FAKE_IMAGE_CLAIMS = (
    "哼，怎么又来找本喵变图……大头照是吧，瞧你那急性子，看本喵给你捏一个出来喵喵~",
    "在这儿呢，爪子刚伸出来，瞅啥呐喵~",
)

#: 诚实降级文案（改写后必须含的语义）
_HONEST_MARKERS = ("没画成", "没出来", "重试")


def _ctx(reply: str, *, meta=None, gen=None, inbound=None) -> PipelineContext:
    c = PipelineContext.__new__(PipelineContext)
    c.reply = reply
    c.text = "帮我生成吴语萌大头照"
    c.detail_type = "group"
    c.group_id = 727631526
    c.user_id = 1371704917
    c.sender_name = "华初"
    c.meta = dict(meta or {})
    c.event = {}
    c.image_paths = []
    c.generated_image_paths = list(gen or [])
    c.generated_text_files = []
    c.inbound_images = inbound
    c.core = type("C", (), {"config": AnimaConfig()})()
    return c


def _run(ctx) -> StageResult:
    return asyncio.run(_g.guardrail_stage(ctx))


# ── 1. 事故两条必须被改写 ─────────────────────────────────────────

@pytest.mark.parametrize("claim", _FAKE_IMAGE_CLAIMS)
def test_fake_image_claim_is_rewritten(claim):
    """🔴 核心：声称给图但没画 ⇒ 改写成诚实的话。"""
    ctx = _ctx(claim)
    result = _run(ctx)
    assert result == StageResult.CONTINUE, "不该拒发（会显得 bot 死了）"
    assert any(m in ctx.reply for m in _HONEST_MARKERS), \
        f"没改写成诚实文案：{ctx.reply!r}"
    assert ctx.meta.get("fake_image_claim_blocked") is True


def test_rewrite_has_log_anchor(caplog):
    import logging
    with caplog.at_level(logging.ERROR, logger="anima.pipeline.stages"):
        _run(_ctx(_FAKE_IMAGE_CLAIMS[0]))
    assert any("[编造出图拦截]" in r.getMessage() for r in caplog.records)


# ── 2. 有图片证据时不得改写 ───────────────────────────────────────

def test_generated_paths_allow_claim():
    ctx = _ctx("看本喵给你捏一个出来喵喵~", gen=["/tmp/x.png"])
    _run(ctx)
    assert ctx.reply == "看本喵给你捏一个出来喵喵~", "有图还被改写"
    assert ctx.meta.get("fake_image_claim_blocked") is None


def test_tool_trace_allows_claim():
    ctx = _ctx("看本喵给你捏一个出来喵喵~",
               meta={"tool_trace": [{"name": "generate_image", "ok": True}]})
    _run(ctx)
    assert ctx.reply == "看本喵给你捏一个出来喵喵~"


def test_tool_trace_failed_does_not_count():
    """生图工具**失败**了不算交付证据 —— 这时说「给你捏好了」仍是编造。"""
    ctx = _ctx("看本喵给你捏一个出来喵喵~",
               meta={"tool_trace": [{"name": "generate_image", "ok": False}]})
    _run(ctx)
    assert any(m in ctx.reply for m in _HONEST_MARKERS), \
        "工具失败却说出图了，没被拦"


def test_generation_tool_calls_allows_claim():
    ctx = _ctx("在这儿呢", meta={"generation_tool_calls": [{"name": "edit_image"}]})
    _run(ctx)
    assert ctx.reply == "在这儿呢"


def test_inbound_images_allow_claim():
    """入站就有图 ⇒ 可能是在说「这就是你说的那张」，不判编造。"""
    ctx = _ctx("在这儿呢", inbound=[{"file": "x"}])
    _run(ctx)
    assert ctx.reply == "在这儿呢"


# ── 3. 普通回复不得误伤 ───────────────────────────────────────────

@pytest.mark.parametrize("reply", [
    "哼，怎么又骂人……信不信本喵一爪子拍过去喵！",
    "这个显卡要 2000 块",
    "哈哈笑死",
    "东北狗？那是什么奇怪的品种……买来干嘛，当传家宝吗喵？",
    "好家伙，这杯子成精了还带跨群连线的？本喵的鱼干都要笑喷了喵~",
    "才不是什么量产的笨蛋模型呢~",
])
def test_normal_replies_pass(reply):
    """🔴 误伤比漏拦更贵 —— 正常聊天不能被改成「没画成」。

    ⚠️ 刻意**不用**含时段词的语料（如「大晚上」）：
    那样会被既有的 `fix_period_words` 改动（与本次闸门无关），
    测试会因为「别的规则动了」而假失败 —— 本轮实测踩过。
    """
    ctx = _ctx(reply)
    _run(ctx)
    assert ctx.reply == reply, f"误伤：{reply!r} → {ctx.reply!r}"


def test_talking_about_images_without_promise_passes():
    """评论别人的图不算「承诺给图」。"""
    ctx = _ctx("这图的构图还行吧，就是光有点平")
    _run(ctx)
    assert ctx.reply == "这图的构图还行吧，就是光有点平"


# ── 4. 与「编造完成」两道闸的关系 ─────────────────────────────────

def test_both_gates_coexist():
    src = (_ROOT / "anima" / "pipeline" / "stages" / "guardrail.py").read_text(
        encoding="utf-8")
    assert "_looks_like_fake_completion" in src
    assert "_looks_like_fake_image_claim" in src


def test_image_gate_runs_after_completion_gate():
    """顺序：先判「任务完成」编造（拒发），再判「出图」编造（改写）。"""
    src = (_ROOT / "anima" / "pipeline" / "stages" / "guardrail.py").read_text(
        encoding="utf-8")
    body = src.split("async def guardrail_stage", 1)[-1]
    assert body.index("_looks_like_fake_completion") < body.index(
        "_looks_like_fake_image_claim")


def test_agent_layer_guard_still_exists():
    src = (_ROOT / "anima" / "agent" / "executor.py").read_text(encoding="utf-8")
    assert "_looks_like_fake_action" in src


# ── 5. 判据细节 ───────────────────────────────────────────────────

def test_detects_promise_forms():
    """承诺句式必须命中。"""
    for t in ("看本喵给你捏一个", "帮你画好了一张", "接好喵", "喏，给你",
              "马上画", "这就给你画"):
        assert _g._IMAGE_CLAIM_RE.search(t), f"没识别出承诺句式：{t!r}"


def test_no_false_positive_on_plain_chat():
    """没有承诺/交付语气的不命中。"""
    for t in ("我不太会画画", "这图挺好看的", "你说的那个图我见过", "喵"):
        assert not _g._IMAGE_CLAIM_RE.search(t), f"误命中：{t!r}"
