"""入站正文口径统一 + `SKIP_REPLY` 不空转（2026-09-25）。

事故链（09-25 出入站审核，逐行证到）
------------------------------------
1. `channels/onebot11.py:336` 只设 `event["message"] = raw_message`（**CQ 原文**），
   **从不设 `event["raw_message"]`**；真正的正文写在 `event["text"]`
   （text 段拼接 `:361` + 富媒体摘要 `:392`，**不含 CQ 码**）。
2. `handler.py` 的私聊合并 / 群防抖两条路径取 `raw_message or message`
   ⇒ 必落到 CQ 原文（`raw_message` 恒缺 ⇒ 永远取 `message`）。
3. 卡片/引用类消息的 CQ 原文被 `CQ_TRANSPORT_RE` **整段剥空** ⇒
   `memory.py:145-146` 判空返回 `SKIP_REPLY`：
   **静默丢弃（无日志 / 无计数 / 无出站）**。生产实证 09-24 14:18:17 私聊卡片。
4. 同一个变量还被写进 stickers `ctx_text` 等持久记忆 ⇒ CQ 码残留
   （stickers 682 处 / tool_learning 72 / episodes 39 / relation 33 / events 27 /
   contradictions 3，共 1,056 处）。
5. `pipeline/__init__.py:96-98` 对 `SKIP_REPLY` **只置 should_reply=False、不 break**
   ⇒ 后续 stage 照跑，`generation_stage` 不看该标记 ⇒ **白烧一次 ReAct**
   （生产实证 `ReAct: … text_len=0`）。

判据边界（不许悄悄放宽）
------------------------
* 取正文**只有一条路**：`constants.event_text()`（`text` 优先，缺失才回落 CQ）。
* 兼容字段 `raw_message` / `message` 的**语义不变**（仍是 CQ 原文），只是不再被当成正文。
* 已经决定不回复时**不得再向上游发请求**。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from anima.constants import CQ_TRANSPORT_RE, event_text
from anima.core.conversation_assembler import ConversationAssembler, GroupDebouncer
from anima.pipeline import PipelineContext, StageResult

_ROOT = Path(__file__).resolve().parents[1]

#: NapCat 推送的卡片消息：`message` 是这种 CQ 原文，会被 CQ_TRANSPORT_RE 整段吃掉。
_CARD_CQ = (
    '[CQ:json,data={"app":"com.tencent.structmsg","meta":{"detail_1":'
    '{"title":"某个分享","desc":"描述"}}}]'
)
#: 渠道层拼好的摘要（真实格式见 onebot11.format_rich_card_summary），不含 CQ 码。
_CARD_SUMMARY = "【QQ卡片·分享】某个分享（描述）"


def _card_event(**extra) -> dict:
    ev = {
        "message": _CARD_CQ,
        "text": _CARD_SUMMARY,
        "message_id": 1,
        "user_id": 7,
        "group_id": 100,
    }
    ev.update(extra)
    return ev


# ── 1. 唯一取值口径 ──────────────────────────────────────────────────────

def test_event_text_prefers_channel_text():
    assert event_text(_card_event()) == _CARD_SUMMARY


def test_event_text_falls_back_only_when_text_missing():
    assert event_text({"raw_message": "raw"}) == "raw"
    assert event_text({"message": "cq"}) == "cq"
    # 空串同样是「缺失」—— 不能因为 `text` 存在但为空就把正文丢掉
    assert event_text({"text": "", "message": "cq"}) == "cq"


def test_event_text_tolerates_junk():
    assert event_text({}) == ""
    assert event_text(None) == ""


def test_card_case_is_actually_discriminating():
    """**没有这条，上面几条在「谁赢」的实现上都能绿。**

    真正的判据不是「谁优先」，而是：按旧口径取出来的正文**必然被剥空**
    （⇒ 静默丢弃），按新口径取出来的正文**必须活着通过清洗**。
    """
    ev = _card_event()
    assert CQ_TRANSPORT_RE.sub("", ev["message"]).strip() == "", "旧口径必剥空"
    assert CQ_TRANSPORT_RE.sub("", event_text(ev)).strip() != "", "新口径必须留下正文"


# ── 2. handler 两条路径都走统一口径 ──────────────────────────────────────

def test_handler_takes_body_from_event_text():
    src = (_ROOT / "anima" / "llm" / "handler.py").read_text(encoding="utf-8-sig")
    assert src.count("text = event_text(event)") == 2, (
        "私聊合并 + 群防抖两处都必须走统一口径；漏一处，那条路径仍会静默丢消息"
    )
    assert 'event.get("raw_message") or event.get("message")' not in src


def test_handler_imports_the_single_source_of_truth():
    src = (_ROOT / "anima" / "llm" / "handler.py").read_text(encoding="utf-8-sig")
    imports = [ln for ln in src.splitlines()
               if ln.startswith("from ") or ln.startswith("import ")]
    assert any("event_text" in ln for ln in imports), \
        "event_text 必须是顶层导入（别在函数里现取常量表）"


# ── 3. 合并器必须产出 `text`（否则合并后只剩最后一条） ────────────────────

def _two_events() -> list[dict]:
    return [
        _card_event(),
        _card_event(
            message="[CQ:at,qq=5] 补一句",
            text="补一句",
            message_id=2,
        ),
    ]


@pytest.mark.parametrize("merge", [ConversationAssembler._merge, GroupDebouncer._merge])
def test_merge_produces_canonical_text(merge):
    merged = merge(_two_events())
    assert merged["text"] == _CARD_SUMMARY + "\n补一句"
    assert CQ_TRANSPORT_RE.sub("", event_text(merged)).strip() == merged["text"]


@pytest.mark.parametrize("merge", [ConversationAssembler._merge, GroupDebouncer._merge])
def test_merge_keeps_legacy_fields_as_transport(merge):
    """`raw_message` / `message` 仍是传输原文（兼容），只是不再被当正文用。"""
    merged = merge(_two_events())
    assert "[CQ:" in merged["raw_message"]
    assert merged["message"] == merged["raw_message"]


def test_debouncer_detects_command_after_a_mention():
    """指令判定也走统一口径：`@bot !anima …` 此前判不出指令（CQ 前缀挡在前）。"""
    d = GroupDebouncer()
    ev = {"text": "!anima agent approve t1",
          "message": "[CQ:at,qq=1] !anima agent approve t1"}
    assert d._is_command(ev) is True


# ── 4. should_reply=False ⇒ 不得向上游发请求 ─────────────────────────────

class _Boom:
    """任何属性访问都炸 —— 用来证明「短路发生在触碰 core 之前」。"""

    def __getattr__(self, item):
        raise AssertionError(f"should_reply=False 时不得触碰 core.{item}")


def test_generation_short_circuits_when_reply_already_decided_out():
    from anima.pipeline.stages.generation import generation_stage

    ctx = PipelineContext(event={}, text="", user_id=1, group_id=None,
                          detail_type="private", sender_name="tester")
    ctx.meta["core"] = _Boom()
    ctx.should_reply = False

    assert asyncio.run(generation_stage(ctx)) is StageResult.SKIP_REPLY


def test_generation_short_circuit_precedes_react_construction():
    src = (_ROOT / "anima" / "pipeline" / "stages" / "generation.py").read_text(
        encoding="utf-8-sig")
    assert "if not ctx.should_reply:" in src, "短路判据本身必须存在"
    assert src.index("if not ctx.should_reply:") < src.index("ReActLoop(")


def test_pipeline_skip_reply_still_does_not_break():
    """上游契约未被改动（SKIP_REPLY 仍是「继续但不回复」）—— 本轮的修法在 stage 侧。"""
    src = (_ROOT / "anima" / "pipeline" / "__init__.py").read_text(encoding="utf-8-sig")
    body = src.split("elif result == StageResult.SKIP_REPLY:")[1].split("except Exception")[0]
    assert "ctx.should_reply = False" in body
    assert "break" not in body
