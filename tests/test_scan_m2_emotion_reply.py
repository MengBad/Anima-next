# -*- coding: utf-8 -*-
"""M2.2 回归：回复被 &&情绪&& 标签剥空后不再静默不发送。"""
from types import SimpleNamespace

from anima.llm.handler import _build_reply_segments, _extract_emotion_tag


def _core():
    return SimpleNamespace(config=SimpleNamespace(
        sticker=SimpleNamespace(always_atmosphere=("humorous", "casual")),
        persistence=SimpleNamespace(data_dir="data"),
    ))


def _ctx(reply):
    return SimpleNamespace(reply=reply, sticker_to_send=None,
        generated_image_paths=[], atmosphere_style="neutral")


def test_emotion_tag_only_reply_not_swallowed():
    ctx = _ctx("&&开心&&")
    segs = _build_reply_segments(ctx, "private", 123, 456, _core())
    texts = [s["data"].get("text") for s in segs if s.get("type") == "text"]
    assert texts and any((t or "").strip() for t in texts), "剥空后没兜底: %s" % (segs,)
    joined = "".join(texts)
    assert "&&" not in joined, "标签泄漏到出站文本: %r" % (joined,)


def test_normal_reply_strips_tag_keeps_text():
    ctx = _ctx("今天天气不错喵 &&期待&&")
    segs = _build_reply_segments(ctx, "private", 123, 456, _core())
    joined = "".join(s["data"].get("text", "") for s in segs if s.get("type") == "text")
    assert "&&" not in joined
    assert "今天天气不错喵" in joined


def test_extract_emotion_tag_returns_cleaned_and_emotion():
    emotion, cleaned = _extract_emotion_tag("&&好奇&&你说呢？")
    assert cleaned == "你说呢？"
    assert emotion == "好奇"


def test_no_tag_reply_unchanged():
    ctx = _ctx("普通回复")
    segs = _build_reply_segments(ctx, "private", 123, 456, _core())
    joined = "".join(s["data"].get("text", "") for s in segs if s.get("type") == "text")
    assert joined == "普通回复"
