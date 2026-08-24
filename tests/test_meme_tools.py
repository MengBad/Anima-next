"""LLM 三工具 search_meme / send_meme / steal_meme 单测（对标 astrbot_plugin_stealer）。"""
from __future__ import annotations

import asyncio
import base64
import pathlib

import pytest

from anima import tools as _T
from anima.tools.builtin.sticker import make_sticker_tools


def _core(tmp):
    class _S:
        enabled = True
        recent_window = 4
        gif_min_interval_sec = 1800
        smart_select = False
        llm_select = False
        select_model = ""
        share_across_groups = False
        require_owner_review = True
    class _P:
        def __init__(self, d):
            self.data_dir = d
    class _Cfg:
        def __init__(self, d):
            self.sticker = _S()
            self.persistence = _P(d)
    class _Core:
        def __init__(self, d):
            self.config = _Cfg(d)
    return _Core(str(tmp))


def _tool(tools, name):
    return next(t for t in tools if t.name == name)


def test_make_sticker_tools_has_three_meme_tools(tmp_path):
    tools = make_sticker_tools(_core(tmp_path))
    names = {t.name for t in tools}
    assert {"search_meme", "send_meme", "steal_meme"} <= names


def test_search_meme_returns_matches(tmp_path):
    from anima.cognitive.stickers import get_sticker_manager
    mgr = get_sticker_manager(str(tmp_path))
    asyncio.run(mgr.import_uploads(
        [("a.png", b"IMGDATA")], category="狗头",
        approval_required=False, auto_analyze=False))
    tool = _tool(make_sticker_tools(_core(tmp_path)), "search_meme")
    res = asyncio.run(tool.handler(query="狗头"))
    assert "找到" in res and "狗头" in res
    # 无命中
    res2 = asyncio.run(tool.handler(query="完全不存在的梗zz"))
    assert "没有匹配" in res2


def test_send_meme_returns_existing_path(tmp_path):
    from anima.cognitive.stickers import get_sticker_manager
    mgr = get_sticker_manager(str(tmp_path))
    asyncio.run(mgr.import_uploads(
        [("a.png", b"IMGDATA")], category="dog",
        approval_required=False, auto_analyze=False))
    tool = _tool(make_sticker_tools(_core(tmp_path)), "send_meme")
    res = asyncio.run(tool.handler(query="dog"))
    assert res.startswith("已选图：")
    p = pathlib.Path(res.split("：", 1)[1])
    assert p.exists()


def test_steal_meme_no_image(tmp_path):
    tool = _tool(make_sticker_tools(_core(tmp_path)), "steal_meme")
    res = asyncio.run(tool.handler(desc="", category=""))
    assert "没得偷" in res


def test_steal_meme_with_image_goes_pending(tmp_path):
    from anima.cognitive.stickers import get_sticker_manager
    b64 = base64.b64encode(b"PSTICKERBYTES").decode()
    tok = _T._CALL_CONTEXT.set(
        {"input_images_b64": [b64], "share_stickers": False})
    try:
        tool = _tool(make_sticker_tools(_core(tmp_path)), "steal_meme")
        res = asyncio.run(tool.handler(desc="好图", category="梗图"))
    finally:
        _T._CALL_CONTEXT.reset(tok)
    assert "待审核" in res or "图库" in res
    mgr = get_sticker_manager(str(tmp_path))
    assert mgr.pending()


def test_send_meme_is_in_generation_autotag_scanner():
    src = pathlib.Path(
        "anima/pipeline/stages/generation.py").read_text(encoding="utf-8")
    assert '("pick_sticker", "random_sticker", "send_meme")' in src