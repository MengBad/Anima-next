"""出站表情段构建测试 —— 锁定「LLM 已选表情在群聊也必须发出」的修复。"""
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, ".")

from anima.llm.handler import _build_reply_segments, _split_reply_messages, _extract_emotion_tag


def _ctx(sticker=None, atm="neutral", reply="嗨"):
    return SimpleNamespace(
        reply=reply,
        sticker_to_send=sticker,
        generated_image_paths=[],
        atmosphere_style=atm,
    )


def _core(always=("humorous", "casual")):
    return SimpleNamespace(
        config=SimpleNamespace(
            sticker=SimpleNamespace(always_atmosphere=list(always)),
            persistence=SimpleNamespace(data_dir="data"),
        )
    )


def test_group_with_picked_sticker_sends_image():
    """修复核心：群聊 LLM 主动 pick_sticker 选了图，出站必须带 image 段。"""
    ctx = _ctx(sticker="/opt/anima/data/stickers/x.jpg", atm="neutral")
    segs = _build_reply_segments(ctx, "group", 123, 456, _core())
    types = [s["type"] for s in segs]
    assert "at" in types and "text" in types
    assert "image" in types, f"群聊已选表情却没带图！segs={segs}"
    img = [s for s in segs if s["type"] == "image"][0]
    # 跨平台：resolve() 在 Windows 上会把 /opt/... 当相对路径，只比对文件名。
    assert img["data"]["file"].endswith("x.jpg")


def test_private_with_picked_sticker_sends_image():
    ctx = _ctx(sticker="/opt/anima/data/stickers/y.png")
    segs = _build_reply_segments(ctx, "private", 123, 456, _core())
    assert any(s["type"] == "image" for s in segs)


def test_group_no_sticker_calm_atmosphere_no_image():
    """群聊没选表情且氛围不在白名单 -> 不带图（避免强行刷表情）。"""
    ctx = _ctx(sticker=None, atm="formal")
    segs = _build_reply_segments(ctx, "group", 123, 456, _core())
    assert not any(s["type"] == "image" for s in segs)


def test_extract_emotion_tag_pure():
    emo, left = _extract_emotion_tag("哈哈&&开心&&来了")
    assert emo == "开心" and left == "哈哈来了"
    emo2, left2 = _extract_emotion_tag("无标签")
    assert emo2 is None and left2 == "无标签"


def test_emotion_tag_stripped_from_reply_text():
    ctx = _ctx(reply="哈哈&&开心&&来了", atm="casual")
    segs = _build_reply_segments(ctx, "private", 123, 456, _core())
    text = next(s for s in segs if s["type"] == "text")["data"]["text"]
    assert "&&" not in text and "开心" not in text and "哈哈" in text

def test_group_no_sticker_but_atmosphere_whitelist_auto_picks():
    """被动自动：群聊无 sticker 但氛围命中 always_atmosphere -> 随机带一张。"""
    ctx = _ctx(sticker=None, atm="humorous")

    class _Mgr:
        def random_one(self, group_id=None, allow_cross_group=False):
            return "/opt/anima/data/stickers/auto.jpg"

    with patch("anima.cognitive.stickers.get_sticker_manager", return_value=_Mgr()):
        segs = _build_reply_segments(ctx, "group", 123, 456, _core())
    assert any(s["type"] == "image" for s in segs)


def test_generated_image_appended():
    ctx = _ctx(sticker=None, atm="formal", reply="看图")
    ctx.generated_image_paths = ["/opt/anima/data/gen/z.webp"]
    segs = _build_reply_segments(ctx, "group", 123, 456, _core())
    imgs = [s for s in segs if s["type"] == "image"]
    assert imgs and imgs[0]["data"]["file"] == "/opt/anima/data/gen/z.webp"


def test_split_text_and_sticker_into_separate_messages():
    """「消息是消息、表情是表情」：正文（含 @）与 image 必须拆成两条独立消息。"""
    ctx = _ctx(sticker="/opt/anima/data/stickers/x.jpg")
    segs = _build_reply_segments(ctx, "group", 123, 456, _core())
    msgs = _split_reply_messages(segs)
    assert len(msgs) == 2, f"应拆成 2 条，实际={msgs}"
    # 第一条：@ + 文本
    assert {s["type"] for s in msgs[0]} == {"at", "text"}
    # 第二条：单独的 image
    assert msgs[1] == [{"type": "image", "data": {"file": msgs[1][0]["data"]["file"]}}]
    assert msgs[1][0]["type"] == "image"


def test_split_no_image_is_single_message():
    """无表情/生图时退化为单条，保持旧行为。"""
    ctx = _ctx(sticker=None, atm="formal")
    segs = _build_reply_segments(ctx, "group", 123, 456, _core())
    msgs = _split_reply_messages(segs)
    assert len(msgs) == 1
    assert {s["type"] for s in msgs[0]} == {"at", "text"}


def test_group_passive_reply_no_at():
    """被动熟人接话：对方未 @ / 未回复 / 未点名时，出站不应前插 @ 段。

    修复热群「每条回复都 @ 对方」刷屏：familiar 模式的主动接话不 @。
    """
    ctx = _ctx(sticker=None, atm="formal")
    segs = _build_reply_segments(ctx, "group", 123, 456, _core(), at_trigger=False)
    assert not any(s["type"] == "at" for s in segs)

def test_intent_gate_skips_passive_on_serious_atmosphere(monkeypatch):
    """意图门控：严肃氛围命中 intent_gate_serious_atmospheres -> 不触碰选图，直接不带图。"""
    ctx = _ctx(sticker=None, atm="support")
    sc = SimpleNamespace(always_atmosphere=("support",), intent_gate=True,
                         intent_gate_serious_atmospheres=("support",))
    core = SimpleNamespace(config=SimpleNamespace(
        sticker=sc, persistence=SimpleNamespace(data_dir="data")))

    def _boom(*a, **k):
        raise AssertionError("意图门控未拦截：不应触发选图/取管理器")

    with patch("anima.cognitive.stickers.get_sticker_manager", side_effect=_boom):
        segs = _build_reply_segments(ctx, "group", 123, 456, core)
    assert not any(s["type"] == "image" for s in segs)


def test_intent_gate_disabled_still_attempts_pick():
    """意图门控关闭：严肃氛围仍会尝试被动选图（触碰 get_sticker_manager）。"""
    ctx = _ctx(sticker=None, atm="support")
    sc = SimpleNamespace(always_atmosphere=("support",), intent_gate=False,
                         intent_gate_serious_atmospheres=("support",))
    core = SimpleNamespace(config=SimpleNamespace(
        sticker=sc, persistence=SimpleNamespace(data_dir="data")))

    calls = []

    def _rec(*a, **k):
        calls.append(1)
        return None

    with patch("anima.cognitive.stickers.get_sticker_manager", side_effect=_rec):
        segs = _build_reply_segments(ctx, "group", 123, 456, core)
    assert calls, "门控关闭时仍应尝试被动选图取管理器"
    assert not any(s["type"] == "image" for s in segs)
    assert any(s["type"] == "text" for s in segs)


def test_group_direct_trigger_keeps_at():
    """对方真正点名（@/回复/喊名）时，「@ 触发者」仍应保留。"""
    ctx = _ctx(sticker=None, atm="formal")
    segs = _build_reply_segments(ctx, "group", 123, 456, _core(), at_trigger=True)
    assert any(s["type"] == "at" and s["data"]["qq"] == "123" for s in segs)


def test_private_never_at():
    """私聊从不前插 @ 段（QQ 私聊无 @ 语义）。"""
    ctx = _ctx(sticker=None, atm="formal")
    segs = _build_reply_segments(ctx, "private", 123, 456, _core(), at_trigger=True)
    assert not any(s["type"] == "at" for s in segs)


def test_split_generated_image_also_separate():
    """表情 + 生图都应各自独立成条（共 3 条：@文本 / 表情 / 生图）。"""
    ctx = _ctx(sticker="/opt/anima/data/stickers/x.jpg")
    ctx.generated_image_paths = ["/opt/anima/data/gen/z.webp"]
    segs = _build_reply_segments(ctx, "group", 123, 456, _core())
    msgs = _split_reply_messages(segs)
    assert len(msgs) == 3
    assert msgs[0][0]["type"] == "at"
    assert all(len(m) == 1 and m[0]["type"] == "image" for m in msgs[1:])


def test_sticker_manager_returns_absolute_path():
    """根因回归：mgr._dir 必须绝对化，否则 pick/random_one 返回相对路径，
    被 _extract_sticker_path 拒绝 -> LLM 主动选图却发不出（群聊/私聊都失效）。"""
    import tempfile
    from pathlib import Path
    from anima.cognitive.stickers import get_sticker_manager

    with tempfile.TemporaryDirectory() as td:
        mgr = get_sticker_manager(td)
        assert mgr._dir.is_absolute(), f"sticker 目录必须是绝对路径，实际={mgr._dir}"
        # 即使传入相对 data_dir，_dir 也应解析为绝对
        rel = str(Path(td) / "rel_data")
        mgr2 = get_sticker_manager(rel)
        assert mgr2._dir.is_absolute(), f"相对 data_dir 也应解析为绝对，实际={mgr2._dir}"


def test_group_quote_segment_injected_when_direct_trigger():
    # 直接点到 bot 且有 message_id 时, 出站前插 reply 引用段(不只 @)
    ctx = _ctx(reply='喵')
    segs = _build_reply_segments(ctx, 'group', 123, 456, _core(), at_trigger=True, message_id=999)
    assert segs[0]['type'] == 'reply' and segs[0]['data']['id'] == '999'
    assert 'at' in [s['type'] for s in segs]
    segs2 = _build_reply_segments(ctx, 'group', 123, 456, _core(), at_trigger=True, message_id=None)
    assert all(s['type'] != 'reply' for s in segs2)
