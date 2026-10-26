# -*- coding: utf-8 -*-
"""多图融合（2026-10-26 生产事故）。

## 事故

```
17:09:48  「帮我生成吴雨萌在天上飞的图」   → generate_image(prompt)  → 出图
17:11:41  「帮我生成华初在天上飞的图」     → generate_image(prompt)  → 出图
17:12:21  华初发了一张表情包
17:12:44  「两张融合一下」                → generate_image(prompt)  → 出图 ← 与素材无关
```

**用户要的是「把这两张融成一张」，它给的是一张全新的、与素材无关的图。**

## 三层根因

### ① `edit_image` 只取第一张

```python
imgs = (ctx.get("input_images_b64") or [])
if imgs:
    input_b64 = imgs[0]      # ← 第二张被静默丢弃
```

### ② 输入图只来自**当前这条消息**

```python
# generation.py
if ctx.inbound_images:        # ← 只看本条消息
    for uri in uris: ...
```

「两张融合一下」这条消息**没带图** ⇒ 一张都拿不到 ⇒ 返回
「改图需要一张原始图片」⇒ 模型改走 `generate_image`（纯文本生图）。

### ③ 它记不住「自己刚生成过什么」

要融的素材之一，是它 30 秒前刚发的图。那份文件在磁盘上，
但下一回合的上下文里没有它 ⇒ 拿不到。

## 修法（五层）

1. `llm.__init__`：`input_image_b64` 接受 `str | list[str]`，
   chat 端点按数组 append 多个 `image_url` 段；`_as_image_list` 归一化
2. `media_generation`：`edit()` 接受多图；`_normalise_images` 归一化
3. `tools/builtin/owner.py`：收集**全部**入站图；「融合」类请求要求 ≥2 张，
   拿不到就**如实说明缺什么**（不让模型误改走生图）
4. `generation.py`：`remember_generated_image` 跨回合记住刚生成的图路径，
   下一回合作为多图输入补进去
5. 工具 schema：`image` 从 `string` 改为 `array`

## 判据

1. 多图能一路传到底（llm → media → 工具）
2. 融合请求缺图时**明确报错**，不静默降级
3. 单图改图行为不变（向后兼容）
4. 「记住自己生成过什么」可跨回合
5. 两个归一化函数语义一致（防「同一修法两处各写」）
"""
from __future__ import annotations

import base64
import pathlib
import time

import pytest

import anima.llm as _llm
import anima.media_generation as _mg

_ROOT = pathlib.Path(__file__).resolve().parents[1]


# ── 1. 归一化函数（两个包各一份，必须语义一致）────────────────────

_CASES = (
    (None, []),
    ("", []),
    ("   ", []),
    ("abc", ["abc"]),
    (["a", "b"], ["a", "b"]),
    (["a", "a", ""], ["a"]),
    (("x", "y"), ["x", "y"]),
    (123, []),
    ({"a": 1}, []),
    ([1, "b", None, "c"], ["b", "c"]),
)


@pytest.mark.parametrize("value,expect", _CASES)
def test_as_image_list(value, expect):
    assert _llm._as_image_list(value) == expect


@pytest.mark.parametrize("value,expect", _CASES)
def test_normalise_images_matches(value, expect):
    """🔴 两个归一化函数必须**语义完全一致**。

    它们分别住在 `anima.llm` 与 `anima.media_generation`
    （`media_generation` 刻意不依赖 `llm` 的内部工具函数）——
    这正是本项目反复踩过的「同一修法两处各写、只改一半」风险点。
    """
    assert _mg._normalise_images(value) == expect
    assert _mg._normalise_images(value) == _llm._as_image_list(value)


# ── 2. LLM 层：多图 → 多个 image_url 段 ────────────────────────────

def test_chat_content_has_one_image_url_per_image():
    """源码级：chat 请求必须**逐个** append，不是只 append 第一张。"""
    src = (_ROOT / "anima" / "llm" / "__init__.py").read_text(encoding="utf-8")
    i = src.index("def _request_image_via_chat")
    body = src[i:i + 2500]
    assert "for uri_raw in _as_image_list(input_image_b64)" in body, \
        "没有遍历多图"
    assert 'content.append({"type": "image_url"' in body


def test_generations_endpoint_warns_on_multi_image():
    """`/images/generations` 只认单张 —— 多图时必须**记警告**，不静默丢弃。"""
    src = (_ROOT / "anima" / "llm" / "__init__.py").read_text(encoding="utf-8")
    i = src.index("def _request_image_via_generations")
    body = src[i:i + 2000]
    assert "只支持单张输入图" in body
    assert "logger.warning" in body


def test_edit_image_signature_accepts_list():
    src = (_ROOT / "anima" / "llm" / "__init__.py").read_text(encoding="utf-8")
    i = src.index("async def edit_image")
    body = src[i:i + 400]
    assert "str | list[str] | None" in body


# ── 3. media_generation 层 ─────────────────────────────────────────

def test_edit_accepts_multiple():
    src = (_ROOT / "anima" / "media_generation.py").read_text(encoding="utf-8")
    i = src.index("async def edit")
    body = src[i:i + 500]
    assert "str | list[str] | None" in body


def test_render_passes_list_when_multiple():
    src = (_ROOT / "anima" / "media_generation.py").read_text(encoding="utf-8")
    i = src.index("def _render")
    body = src[i:i + 3000]
    assert "_imgs if len(_imgs) > 1 else _imgs[0]" in body, \
        "多图没有整份传给 LLM 层"


# ── 4. 工具层：融合要求多张，缺图如实报 ───────────────────────────

def _make_tool(manager=None):
    from anima.tools.builtin.owner import make_edit_image

    class Core:
        image_generation = manager
    return make_edit_image(Core())


class _FakeManager:
    def __init__(self):
        self.calls = []

    async def edit(self, prompt, imgs=None):
        self.calls.append((prompt, imgs))
        return "/tmp/fake_generated.png"


@pytest.mark.asyncio
async def test_fusion_without_images_reports_missing():
    """🔴 「融合」但一张图都没有 ⇒ **明说缺图**，不静默降级成生图。"""
    mgr = _FakeManager()
    tool = _make_tool(mgr)
    out = await tool("把这两张融合一下")
    assert "两张" in out or "需要" in out, f"没有明确报缺图：{out!r}"
    assert not mgr.calls, "缺图时不该调用生图"


@pytest.mark.asyncio
async def test_fusion_with_one_image_refuses():
    """只拿到 1 张也说清楚，不假装做了融合。"""
    mgr = _FakeManager()
    tool = _make_tool(mgr)
    out = await tool("两张融合一下", image=["only_one_base64"])
    assert "只拿到 1 张" in out or "两张" in out, f"没说明只有一张：{out!r}"
    assert not mgr.calls, "单张不该当融合做"


@pytest.mark.asyncio
async def test_fusion_with_two_images_works():
    """两张齐了 ⇒ 传 list 给 manager。"""
    mgr = _FakeManager()
    tool = _make_tool(mgr)
    out = await tool("两张融合一下", image=["img_a", "img_b"])
    assert mgr.calls, "没调用生图"
    prompt, imgs = mgr.calls[0]
    assert isinstance(imgs, list) and len(imgs) == 2, f"没传两张：{imgs!r}"
    assert "2 张" in out, f"没报告用了两张：{out!r}"


@pytest.mark.asyncio
async def test_single_image_edit_still_works():
    """向后兼容：单图改图行为不变。"""
    mgr = _FakeManager()
    tool = _make_tool(mgr)
    await tool("把背景改成星空", image="single_b64")
    prompt, imgs = mgr.calls[0]
    assert imgs == "single_b64", f"单图时应传 str：{imgs!r}"


@pytest.mark.asyncio
async def test_no_image_non_fusion_reports_need():
    mgr = _FakeManager()
    tool = _make_tool(mgr)
    out = await tool("把背景改成星空")
    assert "需要" in out


# ── 5. 跨回合记住「自己生成过什么」────────────────────────────────

def test_remember_and_recall(tmp_path, monkeypatch):
    """🔴 核心：上一回合生成的图，下一回合能被取到。"""
    import anima.pipeline.stages.generation as G

    G._recent_generated.clear()
    p = tmp_path / "gen.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 100)

    G.remember_generated_image("group", 727631526, str(p))
    ctx = type("C", (), {"detail_type": "group", "group_id": 727631526, "user_id": 1})()
    got = G._recent_generated_images_b64(ctx, limit=2)
    assert len(got) == 1, "没取回刚生成的图"
    assert base64.b64decode(got[0]).startswith(b"\x89PNG")


def test_recall_respects_limit(tmp_path):
    import anima.pipeline.stages.generation as G

    G._recent_generated.clear()
    for i in range(5):
        p = tmp_path / f"g{i}.png"
        p.write_bytes(b"\x89PNG" + bytes([i]) * 50)
        G.remember_generated_image("group", 999, str(p), now=time.time() + i)
    ctx = type("C", (), {"detail_type": "group", "group_id": 999, "user_id": 1})()
    assert len(G._recent_generated_images_b64(ctx, limit=2)) == 2


def test_recall_expires(tmp_path):
    """超过 TTL 的不算「刚生成过」。"""
    import anima.pipeline.stages.generation as G

    G._recent_generated.clear()
    p = tmp_path / "old.png"
    p.write_bytes(b"\x89PNG" + b"x" * 50)
    G.remember_generated_image("group", 555, str(p),
                               now=time.time() - G._RECENT_GEN_TTL_SEC - 60)
    ctx = type("C", (), {"detail_type": "group", "group_id": 555, "user_id": 1})()
    assert G._recent_generated_images_b64(ctx) == []


def test_recall_isolated_per_session(tmp_path):
    """不同群的「刚生成过」不能串。"""
    import anima.pipeline.stages.generation as G

    G._recent_generated.clear()
    p = tmp_path / "a.png"
    p.write_bytes(b"\x89PNG" + b"x" * 50)
    G.remember_generated_image("group", 111, str(p))
    ctx = type("C", (), {"detail_type": "group", "group_id": 222, "user_id": 1})()
    assert G._recent_generated_images_b64(ctx) == []


def test_remember_is_fail_soft():
    """记录失败不能抛（生图本身不能被它拖垮）。"""
    import anima.pipeline.stages.generation as G
    G.remember_generated_image("", 0, "")          # 空路径
    G.remember_generated_image("group", None, "x")  # None target
    # 不抛即通过


def test_missing_file_is_skipped(tmp_path):
    """记录的路径文件没了 ⇒ 跳过，不抛。"""
    import anima.pipeline.stages.generation as G

    G._recent_generated.clear()
    G.remember_generated_image("group", 777, str(tmp_path / "nonexistent.png"))
    ctx = type("C", (), {"detail_type": "group", "group_id": 777, "user_id": 1})()
    assert G._recent_generated_images_b64(ctx) == []


# ── 6. 接线 ────────────────────────────────────────────────────────

def test_generation_calls_remember_on_success():
    src = _ROOT / "anima" / "pipeline" / "stages" / "generation.py"
    body = src.read_text(encoding="utf-8")
    assert "remember_generated_image(" in body
    assert "_recent_generated_images_b64(ctx)" in body


def test_generation_injects_recent_generated():
    src = (_ROOT / "anima" / "pipeline" / "stages" / "generation.py").read_text(
        encoding="utf-8")
    i = src.index("_input_images_b64: list[str] = []")
    window = src[i:i + 2500]
    assert "_recent_generated_images_b64(ctx)" in window, "没把最近生成的图补进上下文"


def test_tool_schema_image_is_array():
    """工具描述必须告诉模型「融合要传数组」。"""
    src = (_ROOT / "anima" / "tools" / "builtin" / "__init__.py").read_text(
        encoding="utf-8")
    i = src.index('"edit_image"')
    body = src[i:i + 2000]
    assert '"type": "array"' in body, "schema 仍是 string"
    assert "多图融合必须传 2 张及以上" in body
