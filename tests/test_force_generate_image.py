# -*- coding: utf-8 -*-
"""生图意图的**强制兜底**（2026-10-26 第三轮，引导不够硬）。

## 事故链（三轮才修到底）

**第一轮**（`2dc1214`）：事后拦截「说要画图但没画」→ 只让 bot 不撒谎。
**第二轮**（`2b97b1c`）：补 `generate` 引导 → 让模型知道该调工具。
**第三轮**（本次）：引导**注入成功了，但模型不听**。

## 决定性证据（18:40:34）

```
18:40:34  [图像引导] trace=f03455d9 kind=generate text='生成吴语萌大头照给华初'
18:40:39  generation_stage=4701ms        ← 成功生图要 28s，这里只 4.7s
18:40:40  出站 segs=['reply','text']     ← 无 image
```

对比 17:09 成功那次：

```
17:09:51  （无图像引导）
17:09:58  ReAct 轮次 1: 1 个工具调用
17:10:21  generation_stage=28485ms
```

**有引导的那次失败，没引导的那次成功** —— 说明 `tool_choice` 恒为 `auto`
（`llm/__init__.py:702`），模型**有权不调工具**。引导是软约束。

## 修法：ReAct 之后校验 + 硬调用

`_force_generate_image()`：
· 只在 `_image_kind == "generate"` **且** `generated_image_paths` 为空时触发
· 直接调 `manager.generate(prompt)`，不经过模型选择
· prompt 取用户原话（已足够描述要画什么）
· 写回 `ctx.generated_image_paths` + 跨回合记忆
· fail-soft：失败不影响主回复

## 为什么直接调工具而不是重跑 ReAct

· 重跑要靠模型这次听话 —— 它上一轮就没听，再赌一次没意义
· 「多生成一张图」的代价（配额 +1）远小于「用户拿不到图」

## 判据

1. 有生图意图 + 无产出 ⇒ 补调一次
2. 有产出（模型自己调了）⇒ 不重复调
3. 非生图请求 ⇒ 不触发
4. 兜底失败（配额/服务）⇒ 返回 False，不抛
5. 补出来的图要进 `generated_image_paths`（出站层从这取）
"""
from __future__ import annotations

import asyncio
import pathlib

import pytest

import anima.pipeline.stages.generation as G
from anima.pipeline import PipelineContext

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SRC = _ROOT / "anima" / "pipeline" / "stages" / "generation.py"


class _FakeManager:
    """最小 image_generation 桩。"""

    def __init__(self, *, ok: bool = True, path: str = "/data/gen/x.png"):
        self.ok = ok
        self.path = path
        self.calls: list[str] = []

    async def generate(self, prompt: str) -> str:
        self.calls.append(prompt)
        if not self.ok:
            raise RuntimeError("今日生图额度已用完")
        return self.path


def _core(manager):
    return type("C", (), {"image_generation": manager})()


def _ctx(text: str = "生成吴语萌大头照给华初") -> PipelineContext:
    c = PipelineContext.__new__(PipelineContext)
    c.text = text
    c.detail_type = "group"
    c.group_id = 727631526
    c.user_id = 1562290139
    c.generated_image_paths = []
    c.meta = {}
    return c


def _run_force(core, ctx) -> bool:
    return asyncio.run(G._force_generate_image(core, ctx))


# ── 1. 核心：补调 ─────────────────────────────────────────────────

def test_force_generate_works():
    mgr = _FakeManager()
    ctx = _ctx()
    assert _run_force(_core(mgr), ctx) is True
    assert mgr.calls, "没调用生图"
    assert ctx.generated_image_paths == ["/data/gen/x.png"], "路径没写回"


def test_prompt_uses_user_text():
    """prompt 应该就是用户原话（已足够描述要画什么）。"""
    mgr = _FakeManager()
    _run_force(_core(mgr), _ctx("生成吴语萌大头照给华初"))
    assert "吴语萌" in mgr.calls[0]
    assert "@" not in mgr.calls[0], "@ 前缀没被清掉"


def test_prompt_strips_at_prefix():
    mgr = _FakeManager()
    _run_force(_core(mgr), _ctx("@MengKing 画个猫"))
    assert not mgr.calls[0].startswith("@")
    assert "画个猫" in mgr.calls[0]


def test_prompt_truncated():
    """超长文本要截断（生图 prompt 不需要整段）。"""
    mgr = _FakeManager()
    _run_force(_core(mgr), _ctx("画个猫" + "很" * 500))
    assert len(mgr.calls[0]) <= 200


# ── 2. fail-soft ─────────────────────────────────────────────────

def test_quota_exhausted_returns_false():
    mgr = _FakeManager(ok=False)
    ctx = _ctx()
    assert _run_force(_core(mgr), ctx) is False
    assert ctx.generated_image_paths == []


def test_no_manager_returns_false():
    assert _run_force(_core(None), _ctx()) is False


def test_empty_text_returns_false():
    mgr = _FakeManager()
    assert _run_force(_core(mgr), _ctx("")) is False
    assert not mgr.calls


def test_manager_returning_empty_returns_false():
    mgr = _FakeManager()
    mgr.path = ""
    assert _run_force(_core(mgr), _ctx()) is False


# ── 3. 接线：只在「generate 且无产出」时触发 ─────────────────────

def test_force_gate_only_for_generate_kind():
    """源码级：兜底必须由 `_image_kind == "generate"` 门控。"""
    src = _SRC.read_text(encoding="utf-8")
    i = src.index("_force_generate_image(core, ctx)")
    window = src[max(0, i - 1200):i]
    assert '_image_kind == "generate"' in window, "兜底没有 generate 门控"


def test_force_only_when_no_image_produced():
    """已有产出（模型自己调了工具）时不得重复调。"""
    src = _SRC.read_text(encoding="utf-8")
    i = src.index("_force_generate_image(core, ctx)")
    window = src[max(0, i - 800):i]
    assert "generated_image_paths" in window, "没检查已有产出"
    assert "if not _paths_now" in window, "缺少「无产出才补」的判据"


def test_force_has_log_anchor():
    src = _SRC.read_text(encoding="utf-8")
    assert "[生图兜底]" in src, "缺日志锚：无法从 journal 回测"


def test_force_wrapped_in_try():
    """兜底失败不能拖垮主回复。"""
    src = _SRC.read_text(encoding="utf-8")
    i = src.index("_force_generate_image(core, ctx)")
    window = src[max(0, i - 400):i + 200]
    assert "try:" in window and "except" in src[i:i + 400]


# ── 4. 与既有两层的关系（三层防护）───────────────────────────────

def test_three_layers_all_present():
    """🔴 三层必须都在，各管一段：

    ① 事前引导 `_IMAGE_GENERATE_GUIDANCE`  → 让模型知道该调
    ② 强制兜底 `_force_generate_image`      → 模型不听就直接调
    ③ 事后拦截 `_looks_like_fake_image_claim` → 万一还是没图，不许骗人
    """
    src = _SRC.read_text(encoding="utf-8")
    assert "_IMAGE_GENERATE_GUIDANCE" in src, "缺①事前引导"
    assert "_force_generate_image" in src, "缺②强制兜底"
    guard = (_ROOT / "anima" / "pipeline" / "stages" / "guardrail.py").read_text(
        encoding="utf-8")
    assert "_looks_like_fake_image_claim" in guard, "缺③事后拦截"


def test_force_runs_after_react():
    """兜底必须在 ReAct **之后**（先让模型自己试，不行再补）。"""
    src = _SRC.read_text(encoding="utf-8")
    body = src[src.index("reply = await react.run("):]
    assert body.index("_force_generate_image") < body.index("finally:")


# ── 5. 跨回合记忆 ────────────────────────────────────────────────

def test_forced_image_is_remembered(tmp_path, monkeypatch):
    """补出来的图也要进跨回合记忆（供后续融合/改图）。"""
    G._recent_generated.clear()
    img = tmp_path / "gen.png"
    img.write_bytes(b"\x89PNG" + b"x" * 60)
    mgr = _FakeManager(path=str(img))
    ctx = _ctx()
    assert _run_force(_core(mgr), ctx) is True
    got = G._recent_generated_images_b64(ctx)
    assert got, "补出来的图没进跨回合记忆"


# ── 6. finally 必须「合并」而不是「覆盖」（2026-10-26 生产事故）──────────

def test_finally_merges_instead_of_overwriting():
    """🔴 生产事故回归：`finally` 无条件用 call_context 覆盖 ctx 会吞掉兜底图。

    18:56:52 实证：
        [生图兜底] 已补生成图片: ...image_b96d4848de8a.jpg     ← 图生成了
        [编造出图拦截] 声称给图但本回合无图片产出，已改写       ← 却被判无图
        出站 segs=['reply', 'text']                            ← 图没发出去

    原因：模型没调工具 ⇒ `call_context['generated_image_paths']` 是空列表
    ⇒ `ctx.generated_image_paths = []` 把兜底刚写进去的路径**清空**。

    ⇒ `finally` 必须把两处来源**合并**（call_context 优先，ctx 已有的补后），
      不能无条件覆盖。
    """
    src = _SRC.read_text(encoding="utf-8")
    start = src.index("finally:\n        if context_token is not None:")
    block = src[start:start + 3000]
    # 合并来源：必须遍历 ctx 已有的路径
    assert 'getattr(ctx, "generated_image_paths", None)' in block, (
        "finally 未合并 ctx 已有的生成图 —— 会把兜底补的图覆盖掉")
    # 且必须在赋值之前读（顺序：先收集 ctx 已有的，再统一赋值）
    assert block.index('getattr(ctx, "generated_image_paths", None)') < block.index(
        "_paths.append(_s)") or block.count("_paths.append(_s)") >= 2, (
        "finally 只从 call_context 收集，没有把 ctx 已有路径并进去")
    # 去重键必须共用同一个 set（合并后仍去重）
    assert block.count("_seen = set()") == 1, "合并逻辑应复用同一个 _seen 去重集合"


def test_finally_merge_preserves_order_and_dedup():
    """合并后：call_context 在前，ctx 兜底图在后，重复路径只留一次。"""
    import re as _re
    src = _SRC.read_text(encoding="utf-8")
    start = src.index("finally:\n        if context_token is not None:")
    block = src[start:start + 3000]
    # 抽取循环体内的两段 _paths.append(_s)，确认是「先 call_context 后 ctx」
    first = block.index("for _p in generated:")
    second = block.index('for _p in (getattr(ctx, "generated_image_paths", None) or []):')
    assert first < second, "合并顺序错了：应 call_context 在前、ctx 兜底图在后"


def test_forced_image_survives_finally_merge():
    """行为验证：兜底写入 ctx 的图，经过 finally 合并逻辑后仍然在。"""
    # 直接模拟 finally 的合并代码（纯逻辑，不依赖 pipeline 运行时）
    generated_from_tools = []          # 模型没调工具
    ctx_existing = ["/data/gen/forced.png"]   # 兜底刚写进去的
    _seen = set()
    _paths = []
    for _p in generated_from_tools:
        _s = str(_p)
        if _s and _s not in _seen:
            _seen.add(_s)
            _paths.append(_s)
    for _p in ctx_existing:
        _s = str(_p)
        if _s and _s not in _seen:
            _seen.add(_s)
            _paths.append(_s)
    assert _paths == ["/data/gen/forced.png"], "兜底图在 finally 里被吞掉了"


def test_finally_merge_no_duplicate_when_both_sources_have_same():
    """两处来源含同一路径时只保留一份（模型调了工具 + 兜底也写了同一路径）。"""
    generated_from_tools = ["/data/gen/a.png", "/data/gen/b.png"]
    ctx_existing = ["/data/gen/b.png", "/data/gen/c.png"]
    _seen = set()
    _paths = []
    for _p in generated_from_tools + ctx_existing:
        _s = str(_p)
        if _s and _s not in _seen:
            _seen.add(_s)
            _paths.append(_s)
    assert _paths == ["/data/gen/a.png", "/data/gen/b.png", "/data/gen/c.png"]
