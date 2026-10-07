"""r28 P1-1（RED）：合并转发正文必须在「回复闸门」**之前**拉回。

生产事实（2026-10-06 01:00 → 2026-10-07 01:00，24h）：

- 入站含 ``forward`` 段共 **5** 条：3 条被 ``prefilter:unfamiliar`` 拦下、2 条进了 REACT；
- 四个锚点全为 0：``forward_ids`` 无拉取日志、``get_forward_msg`` 从未调用、
  ``[合并转发内容]`` 从未出现、``合并转发`` 只出现在 NapCat 原始行；
- 唯一活样本 23:13:06 被 ``prefilter:unfamiliar`` 拦下，``text_len=39``
  （39 = 卡片死标题「合并转发消息」+ 空壳，正文一个字都没有）。

根因：r24 把 ``_extract_forward_text`` 挂在 ``attachment_text_stage``，而该
stage 注册在 ``prefilter_stage`` **之后**（``handler.py:288``）。闸门一旦
``STOP``，流水线不再往下走 ⇒ 转发正文**结构性拉不到**。

判据只锁一件事：**闸门看到的 ``text`` 必须已经含转发正文**。
"""
from __future__ import annotations

import asyncio
import types

import pytest

from anima.pipeline import PipelineContext
from anima.pipeline.stages import prefilter as pf

FORWARD_BODY = (
    "[合并转发内容]\n"
    "甲：明天下午三点老地方集合，别迟到\n"
    "乙：收到，我把投影仪带上\n"
    "丙：我可能晚十分钟，地铁口堵住了"
)


class _FakeGateway:
    def __init__(self):
        self.sent = []


class _FakeHandler:
    """最小 handler 桩：只提供 prefilter 真正摸得到的属性。"""

    def __init__(self, forward_text: str = FORWARD_BODY, session_active: bool = True):
        self.forward_calls = 0
        self._forward_text = forward_text
        self._session_active = session_active
        self.core = types.SimpleNamespace(
            config=types.SimpleNamespace(
                autonomy=types.SimpleNamespace(
                    sleep_enabled=False,
                    quiet_hours_enabled=False,
                    familiar_reply_min=0.85,
                ),
                permissions=types.SimpleNamespace(group_policies={}),
                persistence=types.SimpleNamespace(data_dir="data"),
                social=None,
            ),
            gateway=_FakeGateway(),
        )

    async def _extract_forward_text(self, event: dict) -> str:
        self.forward_calls += 1
        return self._forward_text

    def _mark_wake(self, group_id: int, user_id: int) -> None:
        return None

    def _wake_session_active(self, group_id: int, user_id: int) -> bool:
        return self._session_active

    def _passive_should_cooldown(self, group_id, user_id) -> bool:
        return False

    def _ignore_store(self):
        return None


def _ctx(handler: _FakeHandler, *, forward_ids=None, text: str = "") -> PipelineContext:
    event = {
        "post_type": "message",
        "message_type": "group",
        "group_id": 727631526,
        "user_id": 2038678896,
        "message_id": 1001,
        "self_id": 3889001246,
        "raw_message": "[CQ:forward,id=abc123]",
    }
    if forward_ids:
        event["forward_ids"] = list(forward_ids)
    return PipelineContext(
        event=event,
        text=text,
        user_id=2038678896,
        group_id=727631526,
        detail_type="group",
        sender_name="Ashymars",
        meta={"_handler": handler, "trace": "t-forward"},
    )


def test_gate_sees_forward_body(monkeypatch, tmp_path):
    """闸门拿到的 text 必须已含转发正文 —— 这是 P1-1 的唯一判据。"""
    seen: dict = {}

    def _capture(event, text, **kwargs):
        seen["text"] = text
        return None  # 不拦，让流程走完

    monkeypatch.setattr(pf, "classify_group_reply_skip", _capture, raising=True)
    monkeypatch.setattr(
        "anima.llm.ignore_list.should_skip_ignored",
        lambda *a, **k: False,
        raising=True,
    )

    h = _FakeHandler()
    ctx = _ctx(h, forward_ids=["abc123"], text="")
    out = asyncio.run(pf.prefilter_stage(ctx))

    assert h.forward_calls >= 1, "闸门之前没有拉取转发正文 ⇒ P1-1 未修"
    assert FORWARD_BODY.splitlines()[1] in (seen.get("text") or ""), (
        "闸门看到的 text 里没有转发正文（text=%r）" % (seen.get("text"),)
    )
    assert out.name != "STOP"


def test_pure_forward_not_stopped_as_idle(monkeypatch, tmp_path):
    """端到端：纯转发（正文为空壳）不得再被当成「纯图/空正文」判 idle。

    走真实 ``classify_group_reply_skip`` + 唤醒窗内接续路径：
    未修时 text="" ⇒ ``_is_session_thread_idle`` 命中空正文 ⇒ ``prefilter:idle``。
    """
    monkeypatch.setattr(
        "anima.llm.ignore_list.should_skip_ignored",
        lambda *a, **k: False,
        raising=True,
    )
    h = _FakeHandler()
    h.core.config.persistence.data_dir = str(tmp_path)

    ctx = _ctx(h, forward_ids=["abc123"], text="")
    asyncio.run(pf.prefilter_stage(ctx))

    assert ctx.stop_reason == "", f"纯转发消息被拦下了：{ctx.stop_reason}"
    assert ctx.should_reply is True
    assert "老地方集合" in ctx.text


def test_no_double_fetch(monkeypatch, tmp_path):
    """闸门之前拉过一次后，attachment_text_stage 不得重复拉取（幂等）。"""
    monkeypatch.setattr(
        "anima.llm.ignore_list.should_skip_ignored",
        lambda *a, **k: False,
        raising=True,
    )
    h = _FakeHandler()
    h.core.config.persistence.data_dir = str(tmp_path)
    ctx = _ctx(h, forward_ids=["abc123"], text="")

    asyncio.run(pf.prefilter_stage(ctx))
    asyncio.run(pf.attachment_text_stage(ctx))

    assert h.forward_calls == 1, f"转发正文被重复拉取 {h.forward_calls} 次"
    assert ctx.text.count("[合并转发内容]") == 1
