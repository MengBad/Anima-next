"""RED：合并转发**内嵌图片**必须被识别（2026-10-13）。

## 生产事实
- 14 天窗口内 `[段结构]` 含 `forward(` 共 **47** 条，配对到闸门 41 条；
  其中 text_len 达 **549 / 352 / 133 / 122 / 110** ⇒ **转发正文展开是通的**
  （`_expand_forward_text` → `_extract_forward_text` → `parse_forward_messages`）。
- 但 `_forward_node_text` 只取 `type == "text"` 的段 ⇒ **转发节点里的
  image/bface/sface 段被整体丢弃**，`event["images"]` 永远看不到它们
  ⇒ bot 对「转发里的图」完全失明（用户报的 bug）。

## 判据（本文件锁三件事）
1. `parse_forward_images()` 能从 NapCat 三种节点形态里提出图片段（纯函数）；
2. 真实的 `_extract_forward_text` 会把这些图并入 `event["images"]`，
   并打 `from_forward=True`（转发别人的图 ≠ 自己发表情 ⇒ 不得进表情库）；
3. prefilter 的 `_expand_forward_text` 必须把合并结果**同步到
   `ctx.inbound_images`** —— ctx 在流水线之前就快照了 images，而转发展开
   发生在流水线内部（stage 0），不同步就白合并。
"""
from __future__ import annotations

import asyncio
import types

import pytest

from anima.channels import onebot11 as ob
from anima.llm.handler import LLMHandler, _merge_forward_images
from anima.pipeline import PipelineContext
from anima.pipeline.stages import prefilter as pf

# NapCat 已实测的三种 content 形态（见 parse_forward_messages docstring）
IMG_SEG = {
    "type": "image",
    "data": {
        "url": "https://multimedia.nt.qq.com.cn/x/1.jpg",
        "file": "AbCdEf123.jpg",
        "file_id": "fid-001",
        "subType": "0",
    },
}
IMG_SEG_2 = {
    "type": "image",
    "data": {
        "url": "https://multimedia.nt.qq.com.cn/x/2.jpg",
        "file": "XyZ789.jpg",
        "file_id": "fid-002",
        "subType": "0",
    },
}


def _napcat_shape():
    """形态 2：``{"content": {"messages": [节点]}}``（NapCat 包一层）。

    两个节点各带一张**不同**的图（刻意不同：同图应被去重，另有专测）。
    """
    return {
        "id": "99887766",
        "content": {
            "messages": [
                {"message": [{"type": "text", "data": {"text": "看这张"}}, IMG_SEG]},
                {"message": [IMG_SEG_2]},
            ]
        },
    }


# ---------------------------------------------------------------- 纯函数

def test_parse_forward_images_napcat_shape():
    """形态 2（NapCat 包一层）：两张不同的图都必须提出来。"""
    got = ob.parse_forward_images(_napcat_shape())
    assert len(got) == 2, got
    assert all(isinstance(g, dict) for g in got)
    first = got[0]
    assert first["file"] == "AbCdEf123.jpg"
    assert first["file_id"] == "fid-001"
    assert first["url"].endswith("1.jpg")
    assert got[1]["file"] == "XyZ789.jpg"


def test_parse_forward_images_dedupes_repeated_image():
    """同一张图在多个节点里重复出现 ⇒ 只算一张（按 file 去重）。"""
    data = {"content": {"messages": [
        {"message": [IMG_SEG]},
        {"message": [IMG_SEG]},          # 同一个 dict ⇒ 同 file
        {"message": [IMG_SEG_2]},
    ]}}
    got = ob.parse_forward_images(data)
    assert [g["file"] for g in got] == ["AbCdEf123.jpg", "XyZ789.jpg"], got


def test_parse_forward_images_standard_and_bare_shapes():
    """形态 3（标准 messages）与形态 1（裸 content 数组）都要认。"""
    std = {"messages": [{"message": [IMG_SEG]}]}
    bare = {"content": [{"message": [IMG_SEG]}]}
    assert len(ob.parse_forward_images(std)) == 1
    assert len(ob.parse_forward_images(bare)) == 1


def test_parse_forward_images_text_only_forward_is_empty():
    """纯文字转发 ⇒ 空列表（不得凭空造图）。"""
    data = {"content": {"messages": [{"message": [{"type": "text",
                                                  "data": {"text": "只有字"}}]}]}}
    assert ob.parse_forward_images(data) == []


def test_parse_forward_images_malformed_is_fail_closed():
    """畸形输入一律返回 []，绝不抛（与 parse_forward_messages 同口径）。"""
    for bad in (None, {}, {"content": None}, {"content": {}}, "字符串", 42):
        assert ob.parse_forward_images(bad) == []


def test_parse_forward_images_face_segment_excluded():
    """``face``（QQ 系统表情）不入库 —— 与顶层段口径一致（onebot11.py:564）。"""
    data = {"messages": [{"message": [{"type": "face", "data": {"id": "1"}}]}]}
    assert ob.parse_forward_images(data) == []


def test_parse_forward_images_capped():
    """上限保护：超量转发不得无限吐图。"""
    many = {"messages": [{"message": [dict(IMG_SEG,
                                           data=dict(IMG_SEG["data"],
                                                     file="f%d.jpg" % i,
                                                     file_id="id%d" % i))]}
                         for i in range(50)]}
    got = ob.parse_forward_images(many)
    assert 0 < len(got) <= ob._FORWARD_MAX_IMAGES, len(got)


# ---------------------------------------------------------------- 合并语义

def test_merge_forward_images_marks_from_forward():
    """合并必须打 `from_forward=True`（表情库据此跳过）。"""
    ev: dict = {}
    added = _merge_forward_images(ev, [dict(IMG_SEG["data"], file="a.jpg",
                                            file_id="a")])
    assert added == 1
    assert ev["images"][0]["from_forward"] is True
    assert ev["images"][0]["file"] == "a.jpg"


def test_merge_forward_images_dedup_against_existing():
    """与既有 images 按 file 去重，不重复入队。"""
    ev = {"images": [{"file": "a.jpg", "url": "", "sub_type": "", "file_id": "a"}]}
    added = _merge_forward_images(ev, [{"file": "a.jpg", "url": "",
                                        "sub_type": "", "file_id": "a"}])
    assert added == 0
    assert len(ev["images"]) == 1


# ---------------------------------------------------------------- 真实 handler 接线

class _FakeChannel:
    def __init__(self, resp):
        self._resp = resp
        self.calls = 0

    async def get_forward_msg(self, fid, retries=1):
        self.calls += 1
        return self._resp


def _stub_self(channel):
    return types.SimpleNamespace(core=types.SimpleNamespace(
        gateway=types.SimpleNamespace(channel=channel)))


def test_extract_forward_text_merges_images():
    """端到端：真实 `_extract_forward_text` 必须把图并入 event["images"]。"""
    ch = _FakeChannel(_napcat_shape())
    self = _stub_self(ch)
    event = {"forward_ids": ["99887766"]}

    text = asyncio.run(LLMHandler._extract_forward_text(self, event))

    assert "看这张" in text, "正文回归：文字仍要能取出"
    imgs = event.get("images") or []
    assert len(imgs) == 2, "转发内嵌图片没有并入 event['images']：%r" % (imgs,)
    assert all(i.get("from_forward") for i in imgs)


def test_extract_forward_text_no_images_no_key():
    """纯文字转发 ⇒ 不得凭空造出 event['images'] 键。"""
    ch = _FakeChannel({"content": {"messages": [
        {"message": [{"type": "text", "data": {"text": "纯文字"}}]}]}})
    event = {"forward_ids": ["1"]}
    asyncio.run(LLMHandler._extract_forward_text(_stub_self(ch), event))
    assert "images" not in event


# ---------------------------------------------------------------- ctx 快照同步

class _FakeHandler:
    """最小 handler 桩：模拟「真实 handler 会把图并入 event」的新行为。"""

    def __init__(self):
        self.core = types.SimpleNamespace(
            config=types.SimpleNamespace(
                autonomy=types.SimpleNamespace(
                    sleep_enabled=False, quiet_hours_enabled=False,
                    familiar_reply_min=0.85),
                permissions=types.SimpleNamespace(group_policies={}),
                persistence=types.SimpleNamespace(data_dir="data"),
                social=None),
            gateway=types.SimpleNamespace())

    async def _extract_forward_text(self, event: dict) -> str:
        _merge_forward_images(event, [dict(IMG_SEG["data"], file="a.jpg",
                                           file_id="a")])
        return "[合并转发内容]\n甲：看这张图"

    def _mark_wake(self, g, u):
        return None

    def _wake_session_active(self, g, u):
        return True

    def _passive_should_cooldown(self, g, u):
        return False

    def _ignore_store(self):
        return None


def _ctx_fwd(handler, *, forward_ids=None, text=""):
    event = {"post_type": "message", "message_type": "group",
             "group_id": 727631526, "user_id": 2038678896,
             "message_id": 1001, "self_id": 3889001246,
             "raw_message": "[CQ:forward,id=abc123]"}
    if forward_ids:
        event["forward_ids"] = list(forward_ids)
    return PipelineContext(event=event, text=text, user_id=2038678896,
                           group_id=727631526, detail_type="group",
                           sender_name="Ashymars",
                           meta={"_handler": handler, "trace": "t-fwd-img"})


def test_expand_forward_syncs_ctx_inbound_images(monkeypatch):
    """🔴 核心判据：prefilter 合并的图必须同步进 ctx.inbound_images。

    ctx 在流水线**之前**快照 `event["images"]`（handler.py:1137），而
    `_expand_forward_text` 跑在流水线**内部**（stage 0）⇒ 不同步就是白合并。
    """
    monkeypatch.setattr(
        "anima.llm.ignore_list.should_skip_ignored", lambda *a, **k: False,
        raising=True)
    h = _FakeHandler()
    ctx = _ctx_fwd(h, forward_ids=["abc123"], text="")

    asyncio.run(pf._expand_forward_text(ctx))

    imgs = ctx.inbound_images or []
    assert len(imgs) == 1, "ctx.inbound_images 没拿到转发内嵌图：%r" % (imgs,)
    assert imgs[0].get("from_forward") is True
    assert "看这张图" in ctx.text
