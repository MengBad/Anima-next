"""引用（quote）归属契约（2026-09-28）。

上游诊断：`reports/2026-09-28_quote-attribution-diagnosis.md`

根因一句话：用户**引用别人的话/图**调侃 bot 时，bot 把内容算到**引用者**头上 ——
因为被引用者的身份全仓只用来算 `reply_to_self` 布尔，从不落成「谁说的」；
组帧文案 `（你引用的上一条消息内容是：X）` 只给内容、不给来源。

实录（2026-09-28 20:47-20:50，owner 提供）：诹访子引用**bot 自己 20:45:55 发的图**
说「你看你烧的」，bot 咬定「自己发这种怪图加 100 心情还倒打一耙！」
「992934160！不就是你自己吗喂！」—— 它连自己发的东西都不认。

本文件钉四层：
  ① 入站落字段（reply_sender_id / reply_sender_name / reply_is_unknown）
  ② 引用图计数 + `from_reply` 标记，并把**表情收集**从引用图上摘出去
  ③ 组帧归属文案（自己／别人／纯图／未知；**无引用时字节不变**）
  ④ P1：群短时缓冲留 `[引用 X]`，让下一轮也看得出这是引用
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import types

import pytest

import anima.memory.chat_memory as cm_mod
import anima.llm.handler as handler_mod
from anima.llm.handler import LLMHandler
from anima.memory.chat_memory import ChatMemory
from anima.pipeline import PipelineContext, StageResult
from anima.pipeline.stages import generation_stage
import anima.pipeline.stages.generation as gen_mod

GROUP = 727631526
QUOTER = 992934160          # 诹访子
QUOTED = 111111111          # 被引用者（另一人）
BOT_ID = 3889000000         # bot 自己的 QQ


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

def _napcat_replied(user_id=QUOTED, card="诹访子", nickname="zsf",
                    text="你看你烧的", images=None) -> dict:
    """模拟 `get_msg` 返回值。

    🔴 `_call_action`（`channels/onebot11.py:1239-1242`）已把 `{"status":..,"data":..}`
    剥到 data 层 ⇒ `sender` / `user_id` / `message` 都在**顶层**。
    生产佐证：`_fetch_reply_images` 读顶层 `msg["message"]` 且真的拉到过图
    （24h `[引用回复取图]` 24 次）—— 形状若嵌套则它永远返回空。
    """
    segs = []
    if text:
        segs.append({"type": "text", "data": {"text": text}})
    for i, im in enumerate(images or []):
        segs.append({"type": "image", "data": {"file": im, "url": f"http://x/{im}",
                                               "subType": "0", "file_id": im}})
    return {
        "message_id": 52669979,
        "real_id": 52669979,
        "user_id": user_id,
        "group_id": GROUP,
        "message_type": "group",
        "sender": {"user_id": user_id, "card": card, "nickname": nickname},
        "message": segs,
        "raw_message": text or "[图片]",
    }


# ---------------------------------------------------------------------------
# ① 入站落字段
# ---------------------------------------------------------------------------

def test_reply_sender_fields_are_recorded():
    """引用别人的话 ⇒ 被引用者 QQ 与昵称必须落进 event。"""
    from anima.llm.handler import _record_reply_attribution

    event = {"reply_message_id": 52669979}
    _record_reply_attribution(event, _napcat_replied())

    assert event.get("reply_sender_id") == str(QUOTED)
    assert event.get("reply_sender_name") == "诹访子"
    assert event.get("reply_is_unknown") is False


def test_reply_sender_name_uses_group_card_over_nickname():
    """群里用的一直是群名片；只读 nickname 会叫错人（09-20 深审）。"""
    from anima.llm.handler import _record_reply_attribution

    event = {}
    _record_reply_attribution(event, _napcat_replied(card="诹访子", nickname="Rezon"))
    assert event["reply_sender_name"] == "诹访子", "应读群名片而非 QQ 昵称"


def test_attribution_marks_unknown_when_fetch_fails():
    """get_msg 失败（replied=None）⇒ 只标「来源未知」，不得抛、不得伪造 id。"""
    from anima.llm.handler import _record_reply_attribution

    event = {"reply_message_id": 1}
    _record_reply_attribution(event, None)

    assert event.get("reply_is_unknown") is True
    assert "reply_sender_id" not in event
    assert "reply_sender_name" not in event


def test_attribution_tolerates_nested_data_shape():
    """个别实现没剥 data ⇒ 归属提取须兜一层，不能静默变 None。"""
    from anima.llm.handler import _record_reply_attribution

    nested = {"status": "ok", "data": _napcat_replied()}
    event = {}
    _record_reply_attribution(event, nested)
    assert event.get("reply_sender_id") == str(QUOTED)


def test_attribution_only_wired_inside_reply_block():
    """普通消息不得携带归属字段（防泄漏）—— 接线必须在 reply 分支内。"""
    src = inspect.getsource(LLMHandler._handle_inner)
    idx = src.find("_record_reply_attribution(")
    assert idx != -1, "handler 未接线引用归属"
    guard = src.rfind('if detail_type == "group" and event.get("reply_message_id")', 0, idx)
    assert guard != -1, "归属落字段必须包在 reply_message_id 分支内"


def test_reply_to_self_reuses_recorded_sender_id():
    """闸门布尔与归属字段必须同源（单一提取原语），不许两份取号逻辑分叉。"""
    src = inspect.getsource(LLMHandler._handle_inner)
    assert "reply_sender = event.get(\"reply_sender_id\")" in src, \
        "reply_to_self 应复用已落的 reply_sender_id"


# ---------------------------------------------------------------------------
# ② 引用图计数 + from_reply 标记
# ---------------------------------------------------------------------------

def test_merge_reply_images_counts_only_fetched():
    event = {"images": [{"file": "own.jpg", "url": "u"}]}
    extra = [{"file": "q1.jpg", "url": "u1"}, {"file": "q2.jpg", "url": "u2"}]
    added = handler_mod._merge_reply_images(event, extra)

    assert added == 2
    assert event["reply_image_count"] == 2
    assert len(event["images"]) == 3, "原有图必须保留"


def test_merged_reply_images_carry_from_reply_flag():
    event = {"images": [{"file": "own.jpg"}]}
    handler_mod._merge_reply_images(event, [{"file": "q1.jpg"}])

    flags = {i["file"]: i.get("from_reply") for i in event["images"]}
    assert flags["q1.jpg"] is True
    assert flags["own.jpg"] is None, "本就存在的图不得被标成引用图"


def test_own_images_not_flagged_and_dedupe_preserved():
    """既有按 file 去重语义必须保留；重复项不得计数。"""
    event = {"images": [{"file": "same.jpg"}]}
    added = handler_mod._merge_reply_images(event, [{"file": "same.jpg"}, {"file": ""}])

    assert added == 1, "同名应去重；file 为空的按既有语义仍并入"
    assert event["reply_image_count"] == 1
    assert len(event["images"]) == 2


def test_merge_reply_images_never_raises_on_garbage():
    event = {"images": "not-a-list"}
    assert handler_mod._merge_reply_images(event, [None, 123, "x"]) == 0
    assert handler_mod._merge_reply_images({}, None) == 0


# ---------------------------------------------------------------------------
# ③ 组帧归属文案
# ---------------------------------------------------------------------------

def test_quote_hint_names_other_sender():
    ev = {"reply_message_id": 1, "reply_text": "你看你烧的",
          "reply_sender_name": "诹访子", "reply_to_self": False}
    hint = gen_mod._compose_quote_hint(ev, "你懂什么叫引用吗")
    assert "诹访子" in hint
    assert "你看你烧的" in hint
    assert "你自己" not in hint


def test_quote_hint_says_self_when_quoting_bot():
    ev = {"reply_message_id": 1, "reply_text": "喵",
          "reply_sender_id": str(BOT_ID), "reply_to_self": True}
    hint = gen_mod._compose_quote_hint(ev, "这啥")
    assert "你自己" in hint


def test_quote_hint_for_image_only_quote():
    """🧩 生产实录核心场景：引用一张**纯图**（无正文）也必须给出归属。"""
    ev = {"reply_message_id": 52669979, "reply_text": "",
          "reply_image_count": 1, "reply_to_self": True}
    hint = gen_mod._compose_quote_hint(ev, "你看你烧的")
    assert "图" in hint and "你自己" in hint, f"实得 {hint!r}"

    ev2 = {"reply_message_id": 1, "reply_image_count": 1,
           "reply_sender_name": "哈基岸", "reply_to_self": False}
    hint2 = gen_mod._compose_quote_hint(ev2, "看看")
    assert "哈基岸" in hint2 and "图" in hint2


def test_quote_hint_unknown_sender_falls_back_to_legacy_wording():
    """发送者未知时**退回旧文案**（保证任何未落字段的路径字节不变）。"""
    ev = {"reply_message_id": 1, "reply_text": "被引用的上一条消息"}
    hint = gen_mod._compose_quote_hint(ev, "你怎么看")
    assert hint == "（你引用的上一条消息内容是：被引用的上一条消息） "


def test_quote_hint_empty_without_reply():
    """无引用 ⇒ 空串（与旧版逐字节一致，不吃 prompt 预算）。"""
    assert gen_mod._compose_quote_hint({}, "直接说的话") == ""
    assert gen_mod._compose_quote_hint({"reply_text": "   "}, "直接说的话") == ""


def test_quote_hint_skipped_when_quote_already_in_text():
    """既有去重语义：正文已含被引内容时不重复注入。"""
    ev = {"reply_message_id": 1, "reply_text": "你看你烧的", "reply_sender_name": "诹访子"}
    assert gen_mod._compose_quote_hint(ev, "你看你烧的？我没发啊") == ""


def test_quote_hint_never_raises_on_garbage():
    for ev in (None, "x", [], {"reply_image_count": "abc"}, {"reply_text": 12345}):
        gen_mod._compose_quote_hint(ev, None)  # 不抛即通过


def test_quote_hint_unknown_quote_fetch_failure_is_announced():
    """拉取失败（拿了 id 但没内容）也要说一句，而不是假装没引用。"""
    ev = {"reply_message_id": 1, "reply_is_unknown": True}
    hint = gen_mod._compose_quote_hint(ev, "")
    assert hint, "纯引用且拉取失败时应给出「没拿到」提示"


# ---------------------------------------------------------------------------
# ⑤ 空正文呼吁分支
# ---------------------------------------------------------------------------

def _fake_core():
    class _FakeLLM:
        model = "fake-model"
    class _FakeCore:
        llm = _FakeLLM()
        config = None
        vector_memory = None
        tools = None
    return _FakeCore()


@pytest.mark.asyncio
async def test_empty_text_with_image_quote_skips_light_prompt(monkeypatch):
    """空正文 + 引用了一张图 ⇒ 不得回「叫本喵干嘛呀喵？」（那是最伤体验的答非所问）。"""
    class _FakeReAct:
        def __init__(self, *a, **k):
            pass
        async def run(self, messages, **k):
            return "这图不是我发的喵"
    import anima.agent.react as react_mod
    monkeypatch.setattr(react_mod, "ReActLoop", _FakeReAct)

    ctx = PipelineContext(
        event={"reply_message_id": 52669979, "reply_image_count": 1,
               "reply_to_self": True},
        text="", user_id=QUOTER, group_id=GROUP, detail_type="group",
        sender_name="诹访子", meta={"core": _fake_core()},
    )
    ctx.messages = []
    await generation_stage(ctx)
    assert ctx.reply != "叫本喵干嘛呀喵？"
    user_msgs = [m for m in ctx.messages if m.get("role") == "user"]
    assert user_msgs, "应正常进入生成，而不是走轻量追问"
    assert "图" in user_msgs[-1]["content"]


@pytest.mark.asyncio
async def test_empty_text_without_any_quote_still_light_prompt(monkeypatch):
    """反向对照：真的什么都没引用时，轻量追问仍要保留（不许被顺手改没）。"""
    class _FakeReAct:
        def __init__(self, *a, **k):
            pass
        async def run(self, messages, **k):
            return "不该走到这里"
    import anima.agent.react as react_mod
    monkeypatch.setattr(react_mod, "ReActLoop", _FakeReAct)

    ctx = PipelineContext(
        event={"mentions_self": True}, text="", user_id=QUOTER, group_id=GROUP,
        detail_type="group", sender_name="诹访子", meta={"core": _fake_core()},
    )
    ctx.messages = []
    result = await generation_stage(ctx)
    assert result == StageResult.CONTINUE
    assert ctx.reply == "叫本喵干嘛呀喵？"


# ---------------------------------------------------------------------------
# ③-b 表情收集去污
# ---------------------------------------------------------------------------

class _StickerCfg:
    enabled = True
    auto_collect = True
    collect_min_interval = 0
    collect_cooldown_sec = 0
    collect_mode = "cooldown"
    collect_probability = 1.0
    collect_allowlist: list = []
    collect_blocklist: list = []


class _Mgr:
    def runtime_auto_collect(self):
        return True


class _Mem:
    def get_group_context(self, gid, recent_rounds=3):
        return [{"role": "user", "content": "最近聊了猫"}]


def _sticker_handler(monkeypatch, captured):
    monkeypatch.setattr("anima.cognitive.stickers.get_sticker_manager",
                        lambda *a, **k: _Mgr())
    monkeypatch.setattr(cm_mod, "get_chat_memory", lambda: _Mem())
    # 不真调度后台收集（避免悬挂协程），只捕获传进去的图
    monkeypatch.setattr(asyncio, "create_task", lambda coro, *a, **k: None)

    class _Core:
        config = types.SimpleNamespace(sticker=_StickerCfg())

    h = LLMHandler.__new__(LLMHandler)
    h.core = _Core()
    h._sticker_last = {}

    def _stub(imgs, uid, gid, ctx):
        captured["imgs"] = imgs
        return None

    h._collect_stickers = _stub
    return h


@pytest.mark.asyncio
async def test_stickers_skipped_for_reply_images(monkeypatch):
    """引用别人的图 ≠ 自己发了这张表情 — 必须跳过（09-28 实况污染）。"""
    captured = {}
    h = _sticker_handler(monkeypatch, captured)

    event = {"images": [{"file": "b.jpg", "url": "u", "from_reply": True}]}
    await h._observe_stickers(event, "你看你烧的", QUOTER, GROUP, "group")

    assert captured.get("imgs") in (None, []), \
        f"引用图不得进表情库，实得 {captured.get('imgs')!r}"
    assert len(event["images"]) == 1, "只许过滤，不得改动 event（vision 还要用）"


@pytest.mark.asyncio
async def test_stickers_still_collected_for_own_images(monkeypatch):
    """反向对照：普通入站图（无 from_reply）照旧收集，不许一刀切。"""
    captured = {}
    h = _sticker_handler(monkeypatch, captured)

    event = {"images": [{"file": "a.jpg", "url": "u"}]}
    await h._observe_stickers(event, "发个图", QUOTER, GROUP, "group")

    assert captured.get("imgs"), "正常图应照旧收集"
    assert captured["imgs"][0]["file"] == "a.jpg"


@pytest.mark.asyncio
async def test_mixed_images_only_reply_ones_skipped(monkeypatch):
    captured = {}
    h = _sticker_handler(monkeypatch, captured)

    event = {"images": [{"file": "mine.jpg", "url": "u"},
                        {"file": "quoted.jpg", "url": "u2", "from_reply": True}]}
    await h._observe_stickers(event, "看看", QUOTER, GROUP, "group")

    files = [i["file"] for i in captured.get("imgs") or []]
    assert files == ["mine.jpg"], f"应只收自己的图，实得 {files}"


def test_reply_images_still_flow_into_inbound_images():
    """🔴 回归：被引图**必须**继续进 `ctx.inbound_images`（vision/沙箱要用）。

    表情去污只允许在 `_observe_stickers` 内部过滤；`event["images"]` 是
    `inbound_images`（`handler.py:1030`）的唯一来源，砍它就是砍 vision。
    """
    src = inspect.getsource(LLMHandler._handle_inner)
    assert 'inbound_images=event.get("images") or []' in src

    event = {}
    handler_mod._merge_reply_images(event, [{"file": "q.jpg", "url": "u"}])
    assert [i["file"] for i in event["images"]] == ["q.jpg"]


# ---------------------------------------------------------------------------
# ⑥ 埋点
# ---------------------------------------------------------------------------

def test_quote_attribution_log_emitted(caplog):
    caplog.set_level(logging.INFO, logger="anima.llm.handler")
    event = {"reply_message_id": 1, "reply_text": "你看你烧的",
             "reply_sender_id": str(QUOTED), "reply_sender_name": "诹访子",
             "reply_to_self": False}
    handler_mod._merge_reply_images(event, [{"file": "q.jpg"}])

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "[引用归属]" in text, f"缺活体判据锚，实得 {text!r}"
    assert "诹访子" in text
    assert "imgs=1" in text


def test_quote_attribution_log_emitted_even_when_fetch_failed(caplog):
    """拉取失败也必须留痕 —— 否则「归属缺失」在活体上不可观测。"""
    caplog.set_level(logging.INFO, logger="anima.llm.handler")
    ev = {"reply_message_id": 1, "reply_is_unknown": True}
    handler_mod._merge_reply_images(ev, [])
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "[引用归属]" in text


# ---------------------------------------------------------------------------
# ④ P1：群缓冲留引用来源
# ---------------------------------------------------------------------------

def _mem(tmp_path) -> ChatMemory:
    return ChatMemory(persist_path=str(tmp_path / "chat_memory.json"))


def test_group_buffer_records_quote_from(tmp_path):
    m = _mem(tmp_path)
    m.observe_group_message(GROUP, QUOTER, "诹访子", "你看你烧的", 52669980,
                            quote_from="哈基米")
    rows = list(m._group_recent[GROUP])
    assert rows, "未写入群缓冲"
    assert "[引用 哈基米]" in rows[-1]["content"], f"实得 {rows[-1]['content']!r}"


def test_group_buffer_quote_from_is_optional(tmp_path):
    """向后兼容：不传 `quote_from` 时内容与旧版逐字节一致。"""
    m = _mem(tmp_path)
    m.observe_group_message(GROUP, QUOTER, "诹访子", "普通一句话", 1)
    assert "[引用" not in list(m._group_recent[GROUP])[-1]["content"]


def test_group_history_renders_quote_from(tmp_path):
    """下一轮回看历史时也要看得出这是引用（否则「当场认错、下轮又忘」）。"""
    m = _mem(tmp_path)
    m.observe_group_message(GROUP, QUOTER, "诹访子", "你看你烧的", 2,
                            quote_from="哈基米", addressed_to_bot=True)
    rows = m.get_recent_group_context(GROUP, limit=5, annotate=True)
    joined = " ".join(r["content"] for r in rows)
    assert "[引用 哈基米]" in joined, f"历史未渲染引用来源：{joined!r}"


# ---------------------------------------------------------------------------
# 生产原句回放（§4 验证矩阵）
# ---------------------------------------------------------------------------

def test_production_replay_quote_of_own_image():
    """回放 2026-09-28 20:47 实录：诹访子引用 **bot 自己 20:45:55 发的纯图**。

    实录中 bot 的错答是「自己发这种怪图加 100 心情还倒打一耙！」——
    即它以为图是诹访子发的。修复后组帧必须明确告知「那是你自己发的图」。
    """
    # 入站：@MengKing + 引用 msg_id=52669979（bot 自己那张图）
    event = {"reply_message_id": 52669979,
             "mentions_self": True,
             "reply_text": ""}                       # 被引消息是纯图 ⇒ 无语段
    # ① 落归属：get_msg 拉回来的是 bot 自己的消息
    from anima.llm.handler import _record_reply_attribution
    _record_reply_attribution(event, _napcat_replied(
        user_id=BOT_ID, card="MengKing", nickname="MengKing", text=""))
    event["reply_to_self"] = True                    # 闸门布尔（既有逻辑算出）
    # ② 拉回被引图并标记
    handler_mod._merge_reply_images(event, [{"file": "5f2b074a.jpg", "url": "u"}])

    # ③ 组帧
    hint = gen_mod._compose_quote_hint(event, "你看你烧的")
    assert "图" in hint, f"纯图引用未给出归属：{hint!r}"
    assert "你自己" in hint, f"未指明是 bot 自己的图：{hint!r}"

    line = f"[群聊消息] 诹访子({QUOTER}): {hint}你看你烧的".rstrip()
    assert "你自己发的图" in line, f"最终进 prompt 的行未纠正归属：{line!r}"


# ---------------------------------------------------------------------------
# ⑤ 防抖合并：归属字段必须能穿过 GroupDebouncer / ConversationAssembler
#    根因（2026-09-29 复核）：两份 `_merge` 都是 `deepcopy(events[-1])` +
#    **显式并集清单**（mentions_self / reply_to_self / text / images / records /
#    reply_message_id），而接线点在合并器**之前**（handler 先落归属、之后才 ingest）
#    ⇒ 「先引用、再补一句」的回合里 `reply_sender_*` / `reply_text` 全被丢掉。
#    实测后果：引用**别人**的回合，组帧归属直接退化成空串。
# ---------------------------------------------------------------------------

from anima.core.conversation_assembler import (  # noqa: E402
    ConversationAssembler,
    GroupDebouncer,
)


def _ev(mid, text, **kw):
    d = {"message_id": mid, "user_id": QUOTER, "group_id": GROUP,
         "text": text, "raw_message": text, "message": text, "images": []}
    d.update(kw)
    return d


def _quoting_then_appending():
    """第一条：诹访子引用**别人**（哈基岸）的一句话；第二条：补一句 @bot。"""
    e1 = _ev("m1", "你看你烧的", reply_message_id=52669979,
             reply_to_self=False, reply_sender_id="992934160",
             reply_sender_name="哈基岸", reply_is_unknown=False,
             reply_text="今晚月亮真圆")
    e2 = _ev("m2", "@MengKing 说话")
    return e1, e2


def test_group_merge_keeps_reply_sender_name():
    """引用在第一条、补句在第二条 ⇒ 归属名必须仍然在。"""
    e1, e2 = _quoting_then_appending()
    merged = GroupDebouncer._merge([e1, e2])
    assert merged.get("reply_sender_name") == "哈基岸", (
        f"合并后丢了归属名 ⇒ 组帧会把被引内容算到引用者头上：{merged.get('reply_sender_name')!r}")


def test_group_merge_keeps_reply_sender_id_and_unknown_flag():
    e1, e2 = _quoting_then_appending()
    merged = GroupDebouncer._merge([e1, e2])
    assert str(merged.get("reply_sender_id")) == "992934160"
    assert merged.get("reply_is_unknown") is False


def test_group_merge_keeps_reply_text():
    """被引正文同样要穿过合并 —— 否则 bot 看不到「被引用的那句话」。"""
    e1, e2 = _quoting_then_appending()
    merged = GroupDebouncer._merge([e1, e2])
    assert merged.get("reply_text") == "今晚月亮真圆"


def test_private_merge_keeps_reply_sender_name():
    """私聊合并器同源（两份 `_merge` 必须成对，否则两条路径悄悄分叉）。"""
    e1, e2 = _quoting_then_appending()
    merged = ConversationAssembler._merge([e1, e2])
    assert merged.get("reply_sender_name") == "哈基岸"
    assert merged.get("reply_text") == "今晚月亮真圆"


def test_merge_attribution_follows_reply_message_id():
    """归属必须与 `reply_message_id`（= 最后一个带引用的那条）**同源**。"""
    e1 = _ev("m1", "A", reply_message_id=111, reply_to_self=False,
             reply_sender_name="甲", reply_text="x")
    e2 = _ev("m2", "B", reply_message_id=222, reply_to_self=False,
             reply_sender_name="乙", reply_text="y")
    merged = GroupDebouncer._merge([e1, e2])
    assert str(merged.get("reply_message_id")) == "222"
    assert merged.get("reply_sender_name") == "乙", "归属与 reply_message_id 不同源"
    assert merged.get("reply_text") == "y"


def test_merge_without_quote_does_not_invent_attribution():
    """反向对照：没有引用的合并不得凭空产生归属字段。"""
    e1, e2 = _ev("m1", "A"), _ev("m2", "B")
    merged = GroupDebouncer._merge([e1, e2])
    assert not merged.get("reply_sender_name")
    assert "reply_sender_id" not in merged or merged.get("reply_sender_id") in (None, "")


def test_merge_single_event_unchanged():
    """反向对照：单条时 `deepcopy(events[-1])` 本就带全字段，行为不得改变。"""
    e1, _ = _quoting_then_appending()
    for merged in (GroupDebouncer._merge([e1]), ConversationAssembler._merge([e1])):
        assert merged.get("reply_sender_name") == "哈基岸"
        assert merged.get("reply_text") == "今晚月亮真圆"


def test_merged_event_end_to_end_names_other_sender():
    """端到端：合并后的 event 组帧仍须写出「哈基岸发的内容」。"""
    e1, e2 = _quoting_then_appending()
    merged = GroupDebouncer._merge([e1, e2])
    hint = gen_mod._compose_quote_hint(merged, merged.get("text") or "")
    assert "哈基岸" in hint, f"合并回合的归属消失/错误：{hint!r}"


def test_real_order_merge_then_images_then_hint():
    """按 handler 真实执行顺序端到端：882 落归属 → 1017 合并 → 1066 拉图 → 组帧。

    这条是「合并场景」的守卫：`reply_image_count` 由 `_merge_reply_images` 在
    **合并之后**重算，故合并器丢它无害；但 `reply_sender_*` / `reply_text`
    只能靠 `_carry_reply_attribution` 搬运 —— 少了它，本用例会退化成 `''`。
    """
    ev = {"message_id": "m1", "user_id": QUOTER, "group_id": GROUP,
          "text": "你看你烧的", "raw_message": "你看你烧的", "message": "你看你烧的",
          "images": [], "reply_message_id": 52669979,
          "mentions_self": True, "self_id": BOT_ID, "reply_text": ""}
    handler_mod._record_reply_attribution(
        ev, _napcat_replied(user_id=992934160, nickname="诹访子"))
    ev["reply_to_self"] = str(ev.get("reply_sender_id")) == BOT_ID
    merged = GroupDebouncer._merge([ev, _ev("m2", "@MengKing 说话")])
    handler_mod._merge_reply_images(merged, [{"file": "abc.jpg", "url": "u"}])
    hint = gen_mod._compose_quote_hint(merged, merged.get("text") or "")
    assert "诹访子" in hint and "图" in hint, f"合并回合组帧丢了归属：{hint!r}"


def test_real_order_merged_quote_images_still_excluded_from_stickers():
    """合并场景下，被引用图仍须被表情收集排除（`from_reply` 标记不因合并而丢）。"""
    ev = {"message_id": "m1", "user_id": QUOTER, "group_id": GROUP,
          "text": "你看你烧的", "raw_message": "你看你烧的", "message": "你看你烧的",
          "images": [], "reply_message_id": 52669979,
          "mentions_self": True, "self_id": BOT_ID, "reply_text": ""}
    handler_mod._record_reply_attribution(
        ev, _napcat_replied(user_id=BOT_ID, nickname="MengKing"))
    merged = GroupDebouncer._merge([ev, _ev("m2", "@MengKing 说话")])
    handler_mod._merge_reply_images(merged, [{"file": "own.jpg", "url": "u"}])
    visible = [i for i in (merged.get("images") or []) if not i.get("from_reply")]
    assert visible == [], f"引用图未被排除：{visible}"
    assert merged.get("reply_image_count") == 1
