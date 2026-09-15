"""第三轮体验测试修复回归（B1-B5，2026-09-06）。"""
import asyncio

import pytest

from anima.core.config import AnimaConfig
from anima.core.anima_core import AnimaCore
from anima.channels.onebot11 import parse_onebot_event
from anima.llm.handler import LLMHandler
from anima.observation.feedback_detect import detect_engineering_feedback
from anima.pipeline.stages.guardrail import _strip_fixation_sentences
from anima.core.autonomy import _is_ungrounded_proactive


# ── B1 接地网关 ─────────────────────────────────────────────
def test_ungrounded_proactive_detected():
    assert _is_ungrounded_proactive(
        "输入数据解析异常：当前消息片段缺乏明确的语境与语义主干。"
        "若需要查询实时气象数据，请提供具体的地理坐标或城市名称喵。")
    assert _is_ungrounded_proactive("工具执行错误：接口暂时不可用")
    assert not _is_ungrounded_proactive("今天天气不错喵，本喵想去晒太阳～")
    assert _is_ungrounded_proactive("请提供一下城市名称喵")


# ── B3 旧梗句剥离 ───────────────────────────────────────────
def test_strip_fixation_keeps_other_sentences():
    reply = ("本喵是哈基米呀。本喵才不叫沙琪玛，那名字怪怪的。"
             "你问的模型问题本喵不能说喵。沙琪玛是小甜点。")
    out = _strip_fixation_sentences(reply)
    assert "沙琪玛" not in out
    assert "本喵是哈基米呀" in out
    assert "不能说喵" in out


def test_strip_fixation_all_match_falls_back_empty():
    assert _strip_fixation_sentences("沙琪玛是小甜点。本喵才不叫沙琪玛。") == ""


# ── B5 工程反馈误报收窄 ─────────────────────────────────────
def test_feedback_pure_emotion_complaint_excluded():
    assert detect_engineering_feedback("而且你上次答错了还嘴硬，真让人失望。") is None


def test_feedback_concrete_fault_still_detected():
    fb = detect_engineering_feedback("你把我的名字记错了，我不叫这个")
    assert fb is not None and fb.kind == "memory"
    fb2 = detect_engineering_feedback("搜索结果链接打不开，有问题")
    assert fb2 is not None


# ── B2/B4 处理器层 ──────────────────────────────────────────
def _grp_raw(text, uid=31001, mid=1, at=True):
    segs = ([{"type": "at", "data": {"qq": "88888"}}] if at else []) + [
        {"type": "text", "data": {"text": text}}]
    return parse_onebot_event({
        "post_type": "message", "message_type": "group", "message_id": mid,
        "user_id": uid, "group_id": 990003,
        "raw_message": (f"[CQ:at,qq=88888]" if at else "") + text,
        "message": segs,
        "sender": {"user_id": uid, "nickname": "小测", "role": "member"},
        "self_id": 88888,
    })


def _prv_raw(text, uid=31001, mid=2):
    return parse_onebot_event({
        "post_type": "message", "message_type": "private", "message_id": mid,
        "user_id": uid, "raw_message": text,
        "message": [{"type": "text", "data": {"text": text}}],
        "sender": {"user_id": uid, "nickname": "小测"},
        "self_id": 88888,
    })


class FakeGateway:
    def __init__(self):
        self.channel = None
        self.sent = []

    async def on_event(self, event):
        return None

    async def send(self, msg):
        self.sent.append(msg)
        return len(self.sent)

    async def send_file(self, msg):
        self.sent.append(msg)
        return len(self.sent)


@pytest.mark.asyncio
async def test_private_remind_guidance_for_non_owner():
    cfg = AnimaConfig.model_validate({"autonomy": {"sleep_enabled": False}})
    core = AnimaCore(cfg)
    core.llm = type("L", (), {"model": "m"})()
    core.gateway = FakeGateway()
    from anima.tools import create_default_registry
    core.tools = create_default_registry(core)
    h = LLMHandler(core)
    reply = await h._try_verbal_request(
        "明天早上7点提醒我喂猫", 31001, None, "private", _prv_raw("明天早上7点提醒我喂猫"))
    assert reply and "在群里" in reply and "提醒" in reply
    # 非 remind 的私聊闲聊仍回落 None
    reply2 = await h._try_verbal_request(
        "在吗", 31001, None, "private", _prv_raw("在吗"))
    assert reply2 is None


@pytest.mark.asyncio
async def test_non_owner_anima_command_refused():
    cfg = AnimaConfig.model_validate({"autonomy": {"sleep_enabled": False}})
    core = AnimaCore(cfg)
    core.llm = type("L", (), {"model": "m"})()
    gw = FakeGateway()
    core.gateway = gw
    from anima.tools import create_default_registry
    core.tools = create_default_registry(core)
    h = LLMHandler(core)
    consumed = await h._handle_command(
        _grp_raw("!anima status"), "!anima status", 31001, 990003, "group")
    assert consumed is True
    assert gw.sent and "只有主人能用" in str(gw.sent[-1].message)


@pytest.mark.asyncio
async def test_non_owner_forget_me_still_self_service():
    cfg = AnimaConfig.model_validate({"autonomy": {"sleep_enabled": False}})
    core = AnimaCore(cfg)
    core.llm = type("L", (), {"model": "m"})()
    gw = FakeGateway()
    core.gateway = gw
    from anima.tools import create_default_registry
    core.tools = create_default_registry(core)
    h = LLMHandler(core)
    consumed = await h._handle_command(
        _prv_raw("!anima forget me"), "!anima forget me", 31001, None, "private")
    assert consumed is True
    assert gw.sent and "已清除" in str(gw.sent[-1].message)
