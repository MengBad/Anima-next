# -*- coding: utf-8 -*-
"""霸凌误判修复（2026-08-29）：提到骂人词但非攻击 bot 时不得沉默。

回归场景：owner 私聊「是不是觉得 727631526 群里的人太傻逼了」——
这是在问 bot 看法，不是骂 bot，却被霸凌检测判为 severe(sev=3) 触发完全沉默
（「私聊不理我」根因）。修复：①主人豁免 ②问句/谈论第三方 → 不触发霸凌沉默。
"""
from __future__ import annotations

import pytest
import types

from anima.llm.handler import LLMHandler


def _make_core(data_dir, owners=()):
    class _Gateway:
        def __init__(self):
            self.sent = []

        async def send(self, msg):
            self.sent.append(msg)
            return 1

    class _Core:
        config = types.SimpleNamespace(
            compat=types.SimpleNamespace(astrbot_enabled=False),
            crossgroup=types.SimpleNamespace(enabled=False),
            autonomy=types.SimpleNamespace(
                reply_only_when_mentioned=True, enabled=True, familiar_reply_min=0.35,
                wake_session_ttl_sec=60.0, passive_reply_cooldown_sec=0.0,
            ),
            permissions=types.SimpleNamespace(owners=set(owners), group_policies={}),
            sticker=types.SimpleNamespace(enabled=False, auto_collect=False,
                                          collect_min_interval=0, always_atmosphere=[]),
            conversation=types.SimpleNamespace(enabled=False),
            persistence=types.SimpleNamespace(data_dir=data_dir),
        )
        gateway = _Gateway()
        llm = None
        group_presence = types.SimpleNamespace(
            observe=lambda *a, **k: types.SimpleNamespace(state="quiet"))

    return _Core()


def _run_handle(core, text, uid, detail_type="private", owner=False):
    handler = LLMHandler(core)
    seen = {}

    async def fake_run(ctx):
        seen["bullying_detected"] = ctx.meta.get("bullying_detected", False)
        seen["severity"] = ctx.meta.get("bullying_severity", 0)
        ctx.reply = "（测试回复）"
        return ctx

    handler.pipeline = types.SimpleNamespace(run=fake_run)
    event = {
        "user_id": uid,
        "group_id": None if detail_type == "private" else 20001,
        "raw": {"message_type": detail_type, "sender": {"nickname": "tester"}},
        "text": text,
        "mentions_self": False,
        "reply_to_self": False,
    }
    try:
        import asyncio
        asyncio.run(handler.handle(event))
    except Exception as e:
        # pipeline 桩不完整可能抛错，但我们只关心 bullying 标记是否被置位。
        # 只要 fake_run 被调用且记录了 seen 即可。
        if "bullying_detected" not in seen:
            raise e
    return seen


def test_owner_never_silenced_by_insult_word():
    """主人发含辱骂词的消息，绝不标记霸凌、不沉默。"""
    data_dir = "/tmp/bully_test_owner"
    core = _make_core(data_dir, owners={1562290139})
    seen = _run_handle(core, "你是不是觉得群里的人太傻逼了", 1562290139, owner=True)
    # 主人豁免：不标记霸凌
    assert seen.get("bullying_detected", False) is False
    assert seen.get("severity", 0) == 0


def test_question_about_others_not_attack():
    """非主人发「问句谈他者」→ 非对 bot 攻击，不触发霸凌沉默。"""
    data_dir = "/tmp/bully_test_q"
    core = _make_core(data_dir, owners=set())
    seen = _run_handle(core, "你觉得群里的人是不是都太傻逼了", 12345, detail_type="group")
    assert seen.get("bullying_detected", False) is False
    assert seen.get("severity", 0) == 0


def test_direct_attack_still_silenced():
    """直接骂 bot 的仍应标记霸凌（不破坏原判）。"""
    data_dir = "/tmp/bully_test_direct"
    core = _make_core(data_dir, owners=set())
    seen = _run_handle(core, "你就是个傻逼废物", 12345, detail_type="private")
    # 直接攻击：应标记霸凌且 severity>0（generation 会据此沉默）
    assert seen.get("bullying_detected", False) is True
    assert seen.get("severity", 0) >= 3
