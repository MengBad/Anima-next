# -*- coding: utf-8 -*-
"""入站复用合并门：同会话回复在途时吸收后续直接@, 上一回复完成后统一再回复。"""
import asyncio
from types import SimpleNamespace
from anima.llm.handler import LLMHandler


def _mk():
    h = LLMHandler.__new__(LLMHandler)
    h.core = SimpleNamespace(audit=None)
    h._reply_inflight = set()
    h._coalesced = {}
    h._handle_inner = (lambda e: None)  # 将被替换
    return h


def _ev(mid, text):
    return {'user_id': 1, 'group_id': 2, 'message_id': mid, 'text': text,
            'mentions_self': True, 'reply_to_self': False,
            'raw': {'message_type': 'group'}}


async def _scenario(inner, msgs):
    h = _mk()
    h._handle_inner = inner
    tasks = [asyncio.create_task(h.handle(m)) for m in msgs]
    await asyncio.gather(*tasks)
    return h


def test_followup_absorbed_while_first_inflight_then_drained_once():
    calls = []
    async def inner(event):
        await asyncio.sleep(0.02)   # 让 A 保持在途, B 并发到达
        calls.append(event.get('message_id'))
    h = asyncio.run(_scenario(inner, [_ev(10, '第一条'), _ev(11, '第二条补充')]))
    # 两条都进入但只产生一次真实回复?（A 处理,B 在途被吸收后 drain 会再触发一次）
    # 断言: A 被处理; B 被吸收并随 A 结束后的 drain 统一再处理一次
    assert 10 in calls
    assert sum(1 for m in calls if m == 11) == 1
    assert not h._reply_inflight and not h._coalesced


def test_multiple_followups_merged_into_one_drain():
    calls = []
    async def inner(event):
        await asyncio.sleep(0.02)
        calls.append({'mid': event.get('message_id'), 'text': event.get('text')})
    # A 处理时 B,C 并发到达并被吸收; A 结束后 drain 把 B,C 合并为一条
    h = asyncio.run(_scenario(inner, [_ev(10, 'A'), _ev(11, 'B'), _ev(12, 'C')]))
    mids = [c['mid'] for c in calls]
    assert mids.count(10) == 1
    # B 与 C 二者只触发一次 drain(取其一), 不再各自独立触发:
    assert sum(1 for m in mids if m in (11, 12)) == 1
    assert not h._reply_inflight and not h._coalesced


# ─────────── A1（2026-10-16）：合并必须覆盖多模态，不能只并 text ───────────
#
# 原实现 `_drain_coalesced` 只取 text：
#     texts = [str(e.get("text") or "").strip() for e in pending]
#     if not texts: return
#     synth = dict(pending[-1]); synth["text"] = merged
# ⇒ ① 前面各条的 images 被丢（`synth` 只继承最后一条）；
#    ② 纯富媒体连发（text 全空）**整批 return** ⇒ 一条都不回（静默丢弃）。
# V3 北极星四体验把这条列为 A1「P0·行为损失」（08-27 至今未修）。

def _ev_media(mid, text="", images=None):
    e = _ev(mid, text)
    if images:
        e["images"] = list(images)
    return e


async def _drain_capture(pending):
    """直接调 `_drain_coalesced`，捕获它交给 `handle` 的那条合成事件。"""
    h = _mk()
    box = {}

    async def fake_handle(ev):
        box["ev"] = ev

    h.handle = fake_handle
    h._coalesced[("group", 2, 1)] = pending
    await h._drain_coalesced(("group", 2, 1))
    return box.get("ev")


def test_coalesce_preserves_images_from_all_pending():
    """前面各条的图不能丢（`synth` 以最后一条为基底，须显式合并其余）。"""
    ev = asyncio.run(_drain_capture([
        _ev_media(10, "看这个", images=[{"file": "a.jpg"}]),
        _ev_media(11, "还有这个", images=[{"file": "b.jpg"}]),
    ]))
    assert ev is not None
    files = [i.get("file") for i in (ev.get("images") or [])]
    assert "a.jpg" in files, "第一条的图被丢弃"
    assert "b.jpg" in files, "最后一条的图应保留"


def test_coalesce_pure_media_not_dropped():
    """🔴 纯富媒体连发（无正文）不得整批静默丢弃 —— 用户 @ 了 bot 并连发图。"""
    ev = asyncio.run(_drain_capture([
        _ev_media(10, "", images=[{"file": "a.jpg"}]),
        _ev_media(11, "", images=[{"file": "b.jpg"}]),
    ]))
    assert ev is not None, "纯富媒体连发被整批丢弃（一条都不回）"
    files = [i.get("file") for i in (ev.get("images") or [])]
    assert len(files) == 2 and "a.jpg" in files and "b.jpg" in files


def test_coalesce_dedupes_identical_images():
    """同图重复发只保留一份（按 file/file_id/url 去重）。"""
    img = {"file": "same.jpg"}
    ev = asyncio.run(_drain_capture([
        _ev_media(10, "x", images=[img]),
        _ev_media(11, "y", images=[dict(img)]),
    ]))
    files = [i.get("file") for i in (ev.get("images") or [])]
    assert files.count("same.jpg") == 1


def test_coalesce_truly_empty_still_skipped():
    """反向对照：既无正文也无媒体 ⇒ 仍不触发（保持原语义）。"""
    ev = asyncio.run(_drain_capture([_ev(10, ""), _ev(11, "  ")]))
    assert ev is None


def test_coalesce_text_merge_unchanged():
    """行为不变：多条文本仍按「最后一条为主 + 其余作补充」合并。"""
    ev = asyncio.run(_drain_capture([
        _ev(10, "补充一"), _ev(11, "补充二"), _ev(12, "主句"),
    ]))
    assert ev is not None
    assert "主句" in ev["text"]
    assert "补充二" in ev["text"] and "补充一" in ev["text"]
