"""生图 / X 配图出站回归测试。

修复前 bug：generation 阶段把工具下载的多张图截断为 [:1]，出站层又 [:1]，
导致 x_search 拿多张图时永远只发第 1 张（X 相似 query 排第一的图常相同 ->
表现为「一直发同一张」）。

本测试锁定：
- generation 阶段同步 ctx.generated_image_paths 应去重保序、保留全部（不再 [:1]）
- 出站 _build_reply_segments 应逐张发出所有去重后的图（上限 MAX_GENERATED_IMAGES_PER_REPLY）
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, ".")

from anima.llm.handler import (
    MAX_GENERATED_IMAGES_PER_REPLY,
    _build_reply_segments,
)
from anima.pipeline import PipelineContext, StageResult
from anima.pipeline.stages import generation_stage


# ---------------------------------------------------------------------------
# 1. generation 阶段同步工具下载的多张图（去重保序，不再 [:1]）
# ---------------------------------------------------------------------------
class _FakeTools:
    """模拟工具运行时：本回合 x_search 下载了 5 张图（含 1 张重复）。"""

    def __init__(self, downloaded):
        self._downloaded = downloaded

    tools = {"x_search": object()}

    def set_call_context(self, **kw):
        self._ctx = dict(kw)
        return object()

    def get_call_context(self):
        return {"generated_image_paths": list(self._downloaded)}

    def reset_call_context(self, token):
        self._ctx = {}


def _fake_core(downloaded):
    class _FakeLLM:
        model = "fake-model"

    class _Sticker:
        share_across_groups = True

    class _Config:
        sticker = _Sticker()

    class _FakeCore:
        llm = _FakeLLM()
        config = _Config()
        vector_memory = None
        tools = _FakeTools(downloaded)

    return _FakeCore()


@pytest.mark.asyncio
async def test_generation_sync_keeps_all_images_deduped(monkeypatch):
    """工具下载了 5 张（含重复）-> ctx.generated_image_paths 去重后应为 4 张，顺序保持。"""
    import anima.agent.react as react_mod

    class _FakeReAct:
        def __init__(self, *a, **k):
            pass

        async def run(self, messages, **k):
            return "好滴"

    monkeypatch.setattr(react_mod, "ReActLoop", _FakeReAct)

    downloaded = [
        "/data/x_browser/downloaded/a.jpg",
        "/data/x_browser/downloaded/a.jpg",  # 重复
        "/data/x_browser/downloaded/b.jpg",
        "/data/x_browser/downloaded/c.jpg",
        "/data/x_browser/downloaded/d.jpg",
    ]
    from anima.pipeline.stages.generation import _IDENTITY_LOCK_HEADING

    ctx = PipelineContext(
        event={},
        text="去 X 拿几张男娘图",
        user_id=200,
        group_id=727631526,
        detail_type="group",
        sender_name="小明",
        meta={"core": _fake_core(downloaded)},
    )
    nl = chr(10)
    ctx.messages = [{"role": "system", "content": f"系统{nl}{nl}{_IDENTITY_LOCK_HEADING}{nl}你是哈基米。"}]

    await generation_stage(ctx)
    result_paths = getattr(ctx, "generated_image_paths", [])
    assert result_paths, "应有生图路径"
    assert result_paths == [
        "/data/x_browser/downloaded/a.jpg",
        "/data/x_browser/downloaded/b.jpg",
        "/data/x_browser/downloaded/c.jpg",
        "/data/x_browser/downloaded/d.jpg",
    ], f"应去重保序保留全部 4 张，实际={result_paths}"


# ---------------------------------------------------------------------------
# 2. 出站层逐张发出所有去重后的图（受上限约束）
# ---------------------------------------------------------------------------
def _ctx_with_gen(paths, sticker=None, atm="neutral", reply="看图"):
    return SimpleNamespace(
        reply=reply,
        sticker_to_send=sticker,
        generated_image_paths=list(paths),
        atmosphere_style=atm,
        user_id=200,
        group_id=727631526,
    )


def _core():
    return SimpleNamespace(
        config=SimpleNamespace(
            sticker=SimpleNamespace(always_atmosphere=("humorous", "casual")),
            persistence=SimpleNamespace(data_dir="data"),
        )
    )


def test_outbound_emits_all_distinct_images():
    """3 张不同图（< 上限）-> 出站带 3 个 image 段，全发不丢。"""
    paths = [f"/data/gen/{c}.jpg" for c in "abc"]
    ctx = _ctx_with_gen(paths)
    segs = _build_reply_segments(ctx, "group", 200, 727631526, _core(), at_trigger=False)
    imgs = [s for s in segs if s["type"] == "image"]
    assert len(imgs) == 3, f"应发 3 张，实际={len(imgs)}"


def test_outbound_caps_at_max():
    """超过上限 -> 只发 MAX_GENERATED_IMAGES_PER_REPLY 张。"""
    many = [f"/data/gen/{i}.jpg" for i in range(20)]
    ctx = _ctx_with_gen(many)
    segs = _build_reply_segments(ctx, "group", 200, 727631526, _core(), at_trigger=False)
    imgs = [s for s in segs if s["type"] == "image"]
    assert len(imgs) == MAX_GENERATED_IMAGES_PER_REPLY, len(imgs)


def test_outbound_dedupes_repeats():
    """同一张图重复出现多次 -> 只发一次（修「一直发同一张」）。"""
    repeated = ["/data/gen/same.jpg"] * 5
    ctx = _ctx_with_gen(repeated)
    segs = _build_reply_segments(ctx, "group", 200, 727631526, _core(), at_trigger=False)
    imgs = [s for s in segs if s["type"] == "image"]
    assert len(imgs) == 1 and imgs[0]["data"]["file"] == "/data/gen/same.jpg"


def test_outbound_generated_images_split_separate_messages():
    """多张生图各自独立成条消息（与表情一致）。"""
    from anima.llm.handler import _split_reply_messages

    paths = [f"/data/gen/{c}.jpg" for c in "ab"]
    ctx = _ctx_with_gen(paths)
    segs = _build_reply_segments(ctx, "group", 200, 727631526, _core(), at_trigger=False)
    msgs = _split_reply_messages(segs)
    # 1 条文本 + 2 条图
    assert len(msgs) == 3, msgs
    assert all(len(m) == 1 and m[0]["type"] == "image" for m in msgs[1:])
