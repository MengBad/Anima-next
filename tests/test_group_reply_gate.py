"""群聊直接呼叫闸门回归：只在 @、回复或喊「哈基米」时进入生成。"""
from __future__ import annotations

import types

import pytest

from anima.llm.handler import (
    LLMHandler,
    is_direct_group_call,
    resolve_group_reply_mode,
    should_reply_in_group,
)


def test_direct_group_call_accepts_at_reply_or_name():
    assert is_direct_group_call({"mentions_self": True}, "随便说什么")
    assert is_direct_group_call({"reply_to_self": True}, "随便说什么")
    assert is_direct_group_call({}, "哈基米你怎么看")
    assert not is_direct_group_call({}, "大家晚上吃什么")


def test_resolve_group_reply_mode_prefers_explicit_policy():
    autonomy = types.SimpleNamespace(reply_only_when_mentioned=True, familiar_reply_min=0.35)
    assert resolve_group_reply_mode({"reply_mode": "familiar"}, autonomy) == "familiar"
    assert resolve_group_reply_mode({"reply_only_when_mentioned": False}, autonomy) == "open"
    assert resolve_group_reply_mode({}, autonomy) == "mention"


def test_familiar_mode_allows_known_user_without_mention(tmp_path, monkeypatch):
    from anima.cognitive import relation as relation_mod
    from anima.cognitive.relation import RelationEngine

    data_dir = str(tmp_path)
    monkeypatch.setattr(relation_mod, "_relation_engines", {})
    rs = RelationEngine(data_dir)
    rel = rs.touch(10001, "你好呀")
    # 多 touch 几次推高亲密度到 familiar 门槛以上
    for _ in range(30):
        rs.touch(10001, "又见面了")
    assert rel.familiarity >= 0.35
    relation_mod._relation_engines[data_dir] = rs

    autonomy = types.SimpleNamespace(reply_only_when_mentioned=True, familiar_reply_min=0.35)
    policy = {"reply_mode": "familiar", "familiar_min": 0.35}
    assert should_reply_in_group(
        {}, "大家晚上吃什么好呢这道菜怎么做",
        user_id=10001, group_policy=policy, autonomy=autonomy, data_dir=data_dir,
    )
    assert not should_reply_in_group(
        {}, "大家晚上吃什么",
        user_id=99999, group_policy=policy, autonomy=autonomy, data_dir=data_dir,
    )

@pytest.mark.asyncio
async def test_handler_ignores_unaddressed_group_message_but_answers_name_call():
    """名字文本触发与未呼叫静默必须同时成立，避免 bot 插入他人对话。"""
    calls = []

    class _Gateway:
        sent = []

        async def send(self, msg):
            self.sent.append(msg)
            return 1

    class _Core:
        config = types.SimpleNamespace(
            compat=types.SimpleNamespace(astrbot_enabled=False),
            crossgroup=types.SimpleNamespace(enabled=False),
            autonomy=types.SimpleNamespace(reply_only_when_mentioned=True, enabled=True),
            sticker=types.SimpleNamespace(enabled=False, auto_collect=False, collect_min_interval=0,
                                          always_atmosphere=[]),
            conversation=types.SimpleNamespace(enabled=False),
        )
        gateway = _Gateway()
        llm = None
        group_presence = types.SimpleNamespace(observe=lambda *a, **k: types.SimpleNamespace(state="quiet"))

    handler = LLMHandler(_Core())

    async def fake_run(ctx):
        calls.append(ctx.text)
        ctx.reply = "在，咋了"
        return ctx

    handler.pipeline = types.SimpleNamespace(run=fake_run)
    base = {
        "user_id": 10001,
        "group_id": 20001,
        "raw": {"message_type": "group", "sender": {"nickname": "测试用户"}},
        "mentions_self": False,
        "reply_to_self": False,
    }

    await handler.handle({**base, "text": "你们继续聊"})
    assert calls == []
    assert _Core.gateway.sent == []

    await handler.handle({**base, "text": "哈基米你在吗"})
    assert calls == ["哈基米你在吗"]
    assert len(_Core.gateway.sent) == 1


@pytest.mark.asyncio
async def test_group_policy_can_override_reply_gate_without_affecting_other_groups():
    calls = []

    class _Gateway:
        sent = []

        async def send(self, msg):
            self.sent.append(msg)
            return 1

    class _Core:
        config = types.SimpleNamespace(
            compat=types.SimpleNamespace(astrbot_enabled=False),
            crossgroup=types.SimpleNamespace(enabled=False),
            autonomy=types.SimpleNamespace(reply_only_when_mentioned=True, enabled=True),
            permissions=types.SimpleNamespace(group_policies={"20001": {"reply_only_when_mentioned": False}}),
            sticker=types.SimpleNamespace(enabled=False, auto_collect=False, collect_min_interval=0,
                                          always_atmosphere=[]),
            conversation=types.SimpleNamespace(enabled=False),
        )
        gateway = _Gateway()
        llm = None
        group_presence = types.SimpleNamespace(observe=lambda *a, **k: types.SimpleNamespace(state="quiet"))

    handler = LLMHandler(_Core())

    async def fake_run(ctx):
        calls.append(ctx.group_id)
        ctx.reply = "在呢"
        return ctx

    handler.pipeline = types.SimpleNamespace(run=fake_run)
    base = {
        "user_id": 10001, "text": "正常群聊", "mentions_self": False, "reply_to_self": False,
        "raw": {"message_type": "group", "sender": {"nickname": "测试用户"}},
    }
    await handler.handle({**base, "group_id": 20001})
    await handler.handle({**base, "group_id": 20002})
    assert calls == [20001]


@pytest.mark.asyncio
async def test_reply_id_only_resolves_original_sender_before_gate():
    """reply 段只有 id 时，get_msg 发现原消息来自 bot 也必须触发回复。"""
    calls = []

    class _Channel:
        async def get_msg(self, message_id):
            assert message_id == 7788
            return {
                "message_id": 7788,
                "user_id": 88888,
                "message": [{"type": "text", "data": {"text": "本喵刚说的话"}}],
            }

    class _Gateway:
        channel = _Channel()
        sent = []

        async def send(self, msg):
            self.sent.append(msg)
            return 1

    class _Core:
        config = types.SimpleNamespace(
            qq=types.SimpleNamespace(self_id=88888),
            compat=types.SimpleNamespace(astrbot_enabled=False),
            crossgroup=types.SimpleNamespace(enabled=False),
            autonomy=types.SimpleNamespace(reply_only_when_mentioned=True, enabled=True),
            sticker=types.SimpleNamespace(enabled=False, auto_collect=False, collect_min_interval=0,
                                          always_atmosphere=[]),
            conversation=types.SimpleNamespace(enabled=False),
        )
        gateway = _Gateway()
        llm = None
        group_presence = types.SimpleNamespace(observe=lambda *a, **k: types.SimpleNamespace(state="quiet"))

    handler = LLMHandler(_Core())

    async def fake_run(ctx):
        calls.append(ctx.text)
        ctx.reply = "接着说"
        return ctx

    handler.pipeline = types.SimpleNamespace(run=fake_run)
    event = {
        "text": "然后呢",
        "user_id": 10001,
        "group_id": 20001,
        "self_id": 88888,
        "reply_message_id": 7788,
        "mentions_self": False,
        "reply_to_self": False,
        "raw": {"message_type": "group", "self_id": 88888,
                "sender": {"nickname": "测试用户"}},
    }

    await handler.handle(event)

    assert event["reply_to_self"] is True
    assert calls == ["然后呢"]
    assert len(_Core.gateway.sent) == 1
