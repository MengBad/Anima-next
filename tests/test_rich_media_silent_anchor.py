"""R3 补强：富媒体静默埋点必须位于「真实拦截点之前」。

背景（2026-10-16 全量复核发现）：埋点原本只装在 ``memory_stage``，但
``prefilter_stage`` 排在 memory **之前**，纯视频/文件消息在回复闸门就 ``STOP``
⇒ memory 永不执行 ⇒ 埋点 19 小时零命中（同期确有 3 条 video）。
本测试锁定：埋点必须在 prefilter 的 STOP 出口也打出来。

判据形式锚「代码行为」不锚「文本子串」：真实驱动 stage，捕获日志。
"""

from __future__ import annotations

import asyncio
import io
import logging
from unittest.mock import MagicMock

import pytest

from anima.pipeline import PipelineContext, StageResult
from anima.pipeline.stages._shared import log_rich_media_silent
from anima.pipeline.stages.prefilter import prefilter_stage

ANCHOR = "[富媒体静默]"


@pytest.fixture
def cap_logger():
    """捕获 root logger 输出（新进程默认 WARNING，须显式降门槛）。"""
    buf = io.StringIO()
    root = logging.getLogger()
    old_level = root.level
    handler = logging.StreamHandler(buf)
    handler.setLevel(logging.INFO)
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    try:
        yield buf
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)


def _ctx(text: str, images=None, segs=None, detail_type: str = "group") -> PipelineContext:
    ctx = PipelineContext(
        event={
            "post_type": "message",
            "message_type": "group",
            "group_id": 727631526,
            "user_id": 3509574818,
            "message_segments": segs or [],
        },
        text=text,
        user_id=3509574818,
        group_id=727631526,
        detail_type=detail_type,
        sender_name="probe",
    )
    ctx.inbound_images = images or []
    ctx.meta["trace"] = "pytest"
    return ctx


def _fake_handler():
    """最小可用 handler：真实 config + 其余方法由 MagicMock 兜底。

    MagicMock 的任意属性访问返回 MagicMock（truthy），足以让各可选块走完；
    回复闸门用真实的 ``classify_group_reply_skip`` + 真实默认 config 判定。
    """
    from anima.core.config import AnimaConfig

    h = MagicMock()
    h.core.config = AnimaConfig()
    # 关掉时间相关闸门，避免测试在凌晨 1–5 点/免打扰时段被睡眠分支短路。
    h.core.config.autonomy.sleep_enabled = False
    h.core.config.autonomy.quiet_hours_enabled = False
    h.reply_quality = None
    return h


# ────────────────────────── 1. helper 单元行为 ──────────────────────────

def test_helper_file_only(cap_logger):
    ctx = _ctx("", segs=[{"type": "file", "data": {"file": "a.txt"}}])
    assert log_rich_media_silent(ctx, where="unit") is True
    assert ANCHOR in cap_logger.getvalue()
    assert "file" in cap_logger.getvalue()


def test_helper_video_only(cap_logger):
    ctx = _ctx("", segs=[{"type": "video", "data": {}}])
    assert log_rich_media_silent(ctx, where="unit") is True
    assert "video" in cap_logger.getvalue()


def test_helper_negative_has_text(cap_logger):
    """反向对照：有正文 ⇒ 不是富媒体静默。"""
    ctx = _ctx("你好", segs=[{"type": "text", "data": {"text": "你好"}}])
    assert log_rich_media_silent(ctx, where="unit") is False
    assert ANCHOR not in cap_logger.getvalue()


def test_helper_negative_image_only(cap_logger):
    """反向对照：有图无正文 ⇒ 走「[用户发送了图片]」路径，不算静默。"""
    ctx = _ctx("", images=[{"url": "http://x/y.jpg"}],
               segs=[{"type": "image", "data": {}}])
    assert log_rich_media_silent(ctx, where="unit") is False
    assert ANCHOR not in cap_logger.getvalue()


def test_helper_negative_text_seg_present(cap_logger):
    """反向对照：段里含 text（正文被剥空的转发/空串）⇒ 不算富媒体。"""
    ctx = _ctx("", segs=[{"type": "text", "data": {"text": ""}}])
    assert log_rich_media_silent(ctx, where="unit") is False
    assert ANCHOR not in cap_logger.getvalue()


def test_helper_negative_no_segments(cap_logger):
    """反向对照：无段信息（纯控制事件）⇒ 不打。"""
    ctx = _ctx("", segs=[])
    assert log_rich_media_silent(ctx, where="unit") is False


def test_helper_never_raises(cap_logger):
    """埋点自身必须失败隔离，绝不能阻断主流程。"""
    ctx = _ctx("", segs=[{"type": "video"}])
    ctx.event["message_segments"] = "不是列表"  # 恶意结构
    assert log_rich_media_silent(ctx, where="unit") is False


# ───────── 1b. 🔴 真实输入形态：ctx.text 不是空串，而是 CQ 传输原文 ─────────

def test_helper_cq_transport_text_still_counts(cap_logger):
    """🔴 2026-10-16 第二层根因：`event_text()` 用 `or` 链回落。

    `constants.event_text` = `event["text"] or event["raw_message"] or event["message"]`。
    纯富媒体消息 `event["text"]` 为空串（falsy）⇒ **回落到 `event["message"]`（CQ 原文）**
    ⇒ `ctx.text` 形如 `'[CQ:video,file=x]'`，**非空** ⇒ 判据「正文为空」不成立 ⇒ 永不命中。

    实测：04:00 部署后 19h 零命中，而同期 `[段结构]` 有 11 条 video。
    修法：判空前先 `_clean_transport_text()` 剥掉 CQ 残留（与 memory_stage 同口径）。
    """
    ctx = _ctx("[CQ:video,file=abc.video,url=http://x/y]",
               segs=[{"type": "video", "data": {}}])
    assert log_rich_media_silent(ctx, where="unit") is True, (
        "ctx.text 为 CQ 传输原文时被判成「有正文」⇒ 纯富媒体静默永远不可观测"
    )
    assert ANCHOR in cap_logger.getvalue()


def test_helper_cq_transport_mixed_with_real_text_not_counted(cap_logger):
    """反向对照：CQ 残留里**夹着真实文本** ⇒ 剥后非空 ⇒ 不是静默。"""
    ctx = _ctx("[CQ:at,qq=1] 你好啊", segs=[{"type": "at"}, {"type": "text"}])
    assert log_rich_media_silent(ctx, where="unit") is False
    assert ANCHOR not in cap_logger.getvalue()


def test_helper_cq_empty_payload_not_counted(cap_logger):
    """反向：剥完仍剩内容（如只有 at）⇒ 段里含 text 时不由 helper 负责。"""
    ctx = _ctx("[CQ:at,qq=1]", segs=[{"type": "text", "data": {"text": ""}}])
    assert log_rich_media_silent(ctx, where="unit") is False


def test_prefilter_stop_with_cq_text_only(cap_logger):
    """端到端：真实链路形态（text = CQ 原文）下，prefilter STOP 仍须留下埋点。"""
    ctx = _ctx("[CQ:video,file=abc.video]",
               segs=[{"type": "video", "data": {"file": "a.mp4"}}])
    ctx.meta["_handler"] = _fake_handler()

    res = asyncio.run(prefilter_stage(ctx))

    if res is StageResult.STOP:
        out = cap_logger.getvalue()
        assert ANCHOR in out, (
            "真实链路（ctx.text=CQ 原文）下 prefilter STOP 未打点 ⇒ R3 埋点无效"
        )
        assert "video" in out


# ─────────────────── 2. prefilter STOP 出口必须打埋点 ───────────────────

def test_prefilter_stop_logs_rich_media(cap_logger):
    """核心判据：纯 video 消息在 prefilter 被闸门 STOP ⇒ 必须留下埋点。

    这是 10-16 复核发现的缺口——修复前此断言 FAIL（埋点只装在 memory）。
    """
    ctx = _ctx("", segs=[{"type": "video", "data": {"file": "a.mp4"}}])
    ctx.meta["_handler"] = _fake_handler()

    res = asyncio.run(prefilter_stage(ctx))

    assert res is StageResult.STOP, "纯视频消息应被闸门拦下（否则本用例无意义）"
    out = cap_logger.getvalue()
    assert ANCHOR in out, "prefilter STOP 出口未打富媒体静默埋点"
    assert "video" in out
    assert "prefilter" in out, "埋点须带来源标记，便于区分 prefilter / memory"


def test_prefilter_stop_negative_has_text(cap_logger):
    """反向对照：被闸门拦下但**有正文** ⇒ 不打埋点（那是普通闲聊，非静默）。"""
    ctx = _ctx("这是一条正常闲聊内容足够长", segs=[{"type": "text", "data": {}}])
    ctx.meta["_handler"] = _fake_handler()

    res = asyncio.run(prefilter_stage(ctx))

    assert res is StageResult.STOP
    assert ANCHOR not in cap_logger.getvalue()


def test_prefilter_stop_negative_image(cap_logger):
    """反向对照：纯图片消息被拦 ⇒ 有 inbound_images，不算静默。"""
    ctx = _ctx("", images=[{"url": "http://x/y.jpg"}],
               segs=[{"type": "image", "data": {}}])
    ctx.meta["_handler"] = _fake_handler()

    res = asyncio.run(prefilter_stage(ctx))

    assert res is StageResult.STOP
    assert ANCHOR not in cap_logger.getvalue()


def test_prefilter_continue_never_logs(cap_logger):
    """反向对照：CONTINUE 时 prefilter 不打埋点（交给 memory），避免重复计数。"""
    ctx = _ctx("", segs=[{"type": "video"}])
    ctx.meta["_handler"] = _fake_handler()

    # 强制放行：闸门返回 None
    import anima.pipeline.stages.prefilter as pf

    original = pf.classify_group_reply_skip
    pf.classify_group_reply_skip = lambda *a, **k: None
    try:
        res = asyncio.run(prefilter_stage(ctx))
    finally:
        pf.classify_group_reply_skip = original

    if res is StageResult.CONTINUE:
        assert ANCHOR not in cap_logger.getvalue()
