# -*- coding: utf-8 -*-
"""「bot 知道自己发过什么表情包」回归契约。

Grounding 结论（本文件的前提，均为代码事实）：
  - 表情包以 `{"type":"image","data":{"file":…,"subType":"1"}}` 段发出
    （`handler._build_reply_segments`，`subType="1"` = QQ 表情包渠道）；
  - 但 bot 自身出站只走 `ChatMemory.observe_group_bot_reply(gid, ctx.reply: str)`
    ⇒ **只记文本**，表情包对下一回合的自己完全不可见；
  - 且 `guardrail_stage` 仅在 `ctx.reply` 为空时补一句「喏，给你。」⇒ 纯表情回合
    在记忆里也只剩那句话。用户追问「你刚发的那张」时它会失忆。

本文件钉三件事：
  1. `StickerManager.describe_path(s)` 能从**文件路径**认出本库表情并给中文描述；
  2. `ChatMemory.observe_bot_media` 把留痕写进群短时缓冲 **与** 持久桶；
  3. `handler._note_sent_stickers` 只对**本库表情**留痕（生图不算），纯留痕不改行为。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from anima.cognitive.stickers import StickerManager
from anima.memory import chat_memory as cm_mod
from anima.memory.chat_memory import ChatMemory


# ---------------------------------------------------------------------------
# 夹具：一个最小可用的表情库（stickers.json 索引 + stickers/<hash><ext> 实体）
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_sticker_singleton():
    """`get_sticker_manager` 是**进程级单例、首次构造即定型**（后续传入的 data_dir 被忽略，
    见 `stickers.get_sticker_manager`）⇒ 用例之间必须复位，否则会读到上一个用例的表情库。
    生产只有一个 data_dir，故该语义在运行时无影响；测试必须显式隔离。"""
    from anima.cognitive.stickers import close_sticker_manager
    close_sticker_manager()
    yield
    close_sticker_manager()


def _make_library(tmp_path, entries: dict) -> StickerManager:
    stickers_dir = tmp_path / "stickers"
    stickers_dir.mkdir(parents=True, exist_ok=True)
    meta = {}
    for h, item in entries.items():
        ext = item.get("ext", ".jpg")
        (stickers_dir / f"{h}{ext}").write_bytes(b"sticker-bytes")
        meta[h] = {"hash": h, "ext": ext, **item}
    (tmp_path / "stickers.json").write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return StickerManager(str(tmp_path))


def _path(tmp_path, h: str, ext: str = ".jpg") -> str:
    return str((tmp_path / "stickers" / f"{h}{ext}").resolve())


# ---------------------------------------------------------------------------
# 1. describe_path / describe_paths —— 路径 → 中文描述
# ---------------------------------------------------------------------------

def test_describe_path_prefers_emotion(tmp_path):
    mgr = _make_library(tmp_path, {
        "aaa": {"emotion": "撒娇", "labels": ["永远爱你", "表白"], "category": "表情包"},
    })
    desc = mgr.describe_path(_path(tmp_path, "aaa"))
    assert desc, "本库表情必须能被描述"
    assert "撒娇" in desc, f"应优先用 emotion，实得 {desc!r}"


def test_describe_path_ignores_free_text_labels(tmp_path):
    """🔴 labels 是打标 LLM 生成的**自由文本**，不得进 bot 自己的上下文。

    生产实测存在「寻找炮友」这类粗俗标签 —— 一旦原样写进 prompt，就等于新开一条
    「未审用户文案 → prompt」通道。描述只允许取 `EMOTION_TAXONOMY` 受控词表。
    """
    mgr = _make_library(tmp_path, {
        "bbb": {"emotion": "", "labels": ["寻找炮友", "粗俗"], "category": "表情包"},
    })
    desc = mgr.describe_path(_path(tmp_path, "bbb"))
    assert desc == "未标注", f"无 emotion 时必须落「未标注」而不是标签，实得 {desc!r}"
    assert "寻找炮友" not in (desc or ""), "自由文本标签绝不可泄漏进描述"


def test_describe_path_ignores_free_text_category(tmp_path):
    """category 同样来自打标 LLM（自由文本）⇒ 也不得作为描述来源。"""
    mgr = _make_library(tmp_path, {
        "ccc": {"emotion": "", "labels": [], "category": "狗头"},
    })
    assert mgr.describe_path(_path(tmp_path, "ccc")) == "未标注"


def test_describe_path_rejects_out_of_taxonomy_emotion(tmp_path):
    """emotion 字段必须落在受控词表内，野值一律「未标注」。"""
    mgr = _make_library(tmp_path, {
        "ddd": {"emotion": "某种自由文本", "labels": []},
    })
    assert mgr.describe_path(_path(tmp_path, "ddd")) == "未标注"


def test_describe_path_unknown_file_returns_none(tmp_path):
    """非本库文件（如生图）必须返回 None —— 这是「认得出生图」的判据。"""
    mgr = _make_library(tmp_path, {"aaa": {"emotion": "撒娇", "labels": []}})
    assert mgr.describe_path(_path(tmp_path, "not-in-library")) is None


def test_describe_path_empty_or_none_returns_none(tmp_path):
    mgr = _make_library(tmp_path, {"aaa": {"emotion": "撒娇", "labels": []}})
    assert mgr.describe_path(None) is None
    assert mgr.describe_path("") is None


def test_describe_paths_keeps_order_and_skips_unknowns(tmp_path):
    mgr = _make_library(tmp_path, {
        "aaa": {"emotion": "撒娇", "labels": []},
        "bbb": {"emotion": "无语", "labels": []},
    })
    got = mgr.describe_paths([
        _path(tmp_path, "bbb"), _path(tmp_path, "gen-x"), _path(tmp_path, "aaa"),
    ])
    assert len(got) == 2, f"非本库项应被跳过，实得 {got!r}"
    assert "无语" in got[0] and "撒娇" in got[1], f"顺序必须保持，实得 {got!r}"


def test_describe_path_survives_corrupt_index(tmp_path):
    """索引损坏 ⇒ fail-soft 返回 None，绝不抛（留痕不能反过来搞挂出站）。"""
    (tmp_path / "stickers").mkdir(parents=True, exist_ok=True)
    (tmp_path / "stickers" / "aaa.jpg").write_bytes(b"x")
    (tmp_path / "stickers.json").write_text("{ 这不是 JSON", encoding="utf-8")
    mgr = StickerManager(str(tmp_path))
    assert mgr.describe_path(_path(tmp_path, "aaa")) is None


# ---------------------------------------------------------------------------
# 2. observe_bot_media —— 留痕落到模型真能看到的两处
# ---------------------------------------------------------------------------

def test_observe_bot_media_appends_group_assistant_row(tmp_path):
    mem = ChatMemory(persist_path=str(tmp_path / "chat_memory.json"))
    mem.observe_bot_media("group", 727631526, "（发了一张表情包：撒娇）",
                          group_id=727631526, user_id=1562290139)
    rows = list(mem._group_recent[727631526])
    assert rows, "群短时缓冲必须有留痕（模型每轮的 group_history 来源）"
    assert rows[-1]["role"] == "assistant"
    assert "表情包" in rows[-1]["content"] and "撒娇" in rows[-1]["content"]
    assert rows[-1]["content"].startswith("[哈基米]"), "应与 observe_group_bot_reply 同格式"


def test_observe_bot_media_visible_in_group_context(tmp_path):
    """端到端可读性：走模型真实入口 get_recent_group_context 能读到。"""
    mem = ChatMemory(persist_path=str(tmp_path / "chat_memory.json"))
    mem.observe_bot_media("group", 727631526, "（发了一张表情包：无语）",
                          group_id=727631526, user_id=1562290139)
    ctx = mem.get_recent_group_context(727631526, limit=12)
    assert any("表情包" in m["content"] for m in ctx), f"模型必须看得见，实得 {ctx!r}"


def test_observe_bot_media_writes_persisted_bucket(tmp_path):
    """持久桶（get_context 的输入）同样要有，群/私聊皆然。"""
    mem = ChatMemory(persist_path=str(tmp_path / "chat_memory.json"))
    mem.observe_bot_media("group", 727631526, "（发了一张表情包：撒娇）",
                          group_id=727631526, user_id=1562290139)
    hist = mem.get_context("group", 727631526, user_id=1562290139)
    assert hist, "持久桶必须有留痕"
    assert any(m["role"] == "assistant" and "表情包" in m["content"] for m in hist), hist


def test_observe_bot_media_private_writes_bucket_but_not_group_buffer(tmp_path):
    mem = ChatMemory(persist_path=str(tmp_path / "chat_memory.json"))
    mem.observe_bot_media("private", 1562290139, "（发了一张表情包：得意）",
                          user_id=1562290139)
    hist = mem.get_context("private", 1562290139, user_id=1562290139)
    assert any("表情包" in m["content"] for m in hist), "私聊也要能记住"
    assert not list(mem._group_recent.get(1562290139, ())), "私聊不得污染群缓冲"


def test_observe_bot_media_ignores_empty_note(tmp_path):
    mem = ChatMemory(persist_path=str(tmp_path / "chat_memory.json"))
    mem.observe_bot_media("group", 727631526, "", group_id=727631526, user_id=1)
    mem.observe_bot_media("group", 727631526, "   ", group_id=727631526, user_id=1)
    assert not list(mem._group_recent.get(727631526, ())), "空留痕不得写入"


# ---------------------------------------------------------------------------
# 2b. 🔴 二阶风险：留痕行不得削弱「跨用户污染」归属标注
#    `get_recent_group_context(annotate=True)` 用「其后有无 assistant 行」判「是否已答」。
#    「文本为空 + 被动附带一张表情」的回合会因此多出一条 assistant 行
#    ⇒ 若不排除，别人尚未被答的提问会被误判成「已答」（09-21 事故的修复被削弱）。
# ---------------------------------------------------------------------------

def test_media_row_does_not_suppress_pending_annotation(tmp_path):
    from anima.memory.chat_memory import _BYSTAND_PENDING_PREFIX
    mem = ChatMemory(persist_path=str(tmp_path / "chat_memory.json"))
    mem.observe_group_message(727631526, 111, "小明", "哈基米在吗",
                              message_id="m1", addressed_to_bot=True)
    mem.observe_bot_media("group", 727631526, "（发了一张表情包：撒娇）",
                          group_id=727631526, user_id=111)
    ctx = mem.get_recent_group_context(727631526, limit=12, annotate=True)
    hit = [m for m in ctx if m["role"] == "user"]
    assert hit and hit[0]["content"].startswith(_BYSTAND_PENDING_PREFIX), \
        f"纯媒体行不得把他人待答标记成已答，实得 {ctx!r}"


def test_text_reply_still_suppresses_pending_annotation(tmp_path):
    """反向对照：真实的文本回复仍应让标注消失（既有行为不得被改坏）。"""
    from anima.memory.chat_memory import _BYSTAND_PENDING_PREFIX
    mem = ChatMemory(persist_path=str(tmp_path / "chat_memory.json"))
    mem.observe_group_message(727631526, 111, "小明", "哈基米在吗",
                              message_id="m1", addressed_to_bot=True)
    mem.observe_group_bot_reply(727631526, "在的喵")
    ctx = mem.get_recent_group_context(727631526, limit=12, annotate=True)
    hit = [m for m in ctx if m["role"] == "user"]
    assert hit and not hit[0]["content"].startswith(_BYSTAND_PENDING_PREFIX), \
        f"已有文本回复的提问不该再标注，实得 {ctx!r}"


# ---------------------------------------------------------------------------
# 3. handler 侧：只对「本库表情」且「确实发成功」的留痕
# ---------------------------------------------------------------------------

def test_sent_sticker_note_from_library_path(tmp_path):
    from anima.llm.handler import _sent_sticker_note
    _make_library(tmp_path, {"aaa": {"emotion": "撒娇", "labels": ["永远爱你"]}})
    note = _sent_sticker_note([_path(tmp_path, "aaa")], str(tmp_path))
    assert note, "本库表情必须产出留痕"
    assert "表情包" in note and "撒娇" in note, note


def test_sent_sticker_note_ignores_generated_image(tmp_path):
    """生图不是表情包 —— 不得把它记成「我发了表情包」。"""
    from anima.llm.handler import _sent_sticker_note
    _make_library(tmp_path, {"aaa": {"emotion": "撒娇", "labels": []}})
    gen = str((tmp_path / "generated" / "pic.png").resolve())
    assert _sent_sticker_note([gen], str(tmp_path)) == ""


def test_sent_sticker_note_counts_multiple(tmp_path):
    from anima.llm.handler import _sent_sticker_note
    _make_library(tmp_path, {
        "aaa": {"emotion": "撒娇", "labels": []},
        "bbb": {"emotion": "无语", "labels": []},
    })
    note = _sent_sticker_note(
        [_path(tmp_path, "aaa"), _path(tmp_path, "bbb")], str(tmp_path))
    assert "2" in note, f"多张应报数量，实得 {note!r}"
    assert "撒娇" in note and "无语" in note, note


def test_sent_sticker_note_empty_input(tmp_path):
    from anima.llm.handler import _sent_sticker_note
    assert _sent_sticker_note([], str(tmp_path)) == ""
    assert _sent_sticker_note([], "") == ""


def test_note_sent_stickers_writes_memory(tmp_path, monkeypatch):
    """编排层：给了本库表情 ⇒ 记忆被写入；给生图 ⇒ 什么都不写。"""
    from anima.llm import handler as handler_mod
    _make_library(tmp_path, {"aaa": {"emotion": "撒娇", "labels": []}})
    mem = ChatMemory(persist_path=str(tmp_path / "chat_memory.json"))
    monkeypatch.setattr(cm_mod, "get_chat_memory", lambda: mem)

    core = SimpleNamespace(config=SimpleNamespace(
        persistence=SimpleNamespace(data_dir=str(tmp_path))))
    ctx = SimpleNamespace(meta={"chat_id": 727631526})

    handler_mod._note_sent_stickers(
        core, ctx, "group", [_path(tmp_path, "aaa")],
        group_id=727631526, user_id=1562290139, target_id=727631526)
    assert any("表情包" in m["content"]
               for m in mem.get_recent_group_context(727631526, limit=8)), "本库表情必须留痕"

    # 反向对照：生图不得留下「表情包」字样
    mem2 = ChatMemory(persist_path=str(tmp_path / "chat_memory2.json"))
    monkeypatch.setattr(cm_mod, "get_chat_memory", lambda: mem2)
    handler_mod._note_sent_stickers(
        core, ctx, "group", [str((tmp_path / "generated" / "p.png").resolve())],
        group_id=727631526, user_id=1562290139, target_id=727631526)
    assert not any("表情包" in m["content"]
                   for m in mem2.get_recent_group_context(727631526, limit=8)), "生图不得被当成表情包"


def test_note_sent_stickers_is_fail_soft(tmp_path, monkeypatch):
    """留痕失败绝不能反向搞挂出站。"""
    from anima.llm import handler as handler_mod

    def _boom():
        raise RuntimeError("memory down")

    monkeypatch.setattr(cm_mod, "get_chat_memory", _boom)
    core = SimpleNamespace(config=SimpleNamespace(
        persistence=SimpleNamespace(data_dir=str(tmp_path))))
    ctx = SimpleNamespace(meta={"chat_id": 1})
    # 不抛即通过
    handler_mod._note_sent_stickers(
        core, ctx, "group", ["/nonexistent/x.jpg"],
        group_id=1, user_id=1, target_id=1)


def test_send_loop_wires_sticker_note():
    """接线守卫：发送循环必须真的调用留痕（防只写实现不接线）。"""
    import inspect
    from anima.llm.handler import LLMHandler
    src = inspect.getsource(LLMHandler._handle_inner)
    assert "_note_sent_stickers" in src, "发送路径未接留痕 ⇒ 表情包依旧不可见"
