"""Familiar-mode Gate B: skip ultra-short unaddressed idle chatter."""
from __future__ import annotations

import types

import pytest

from anima.llm.handler import (
    classify_group_reply_skip,
    is_addressed_to_other_bot,
    is_familiar_idle_unaddressed,
    should_reply_in_group,
)


def test_idle_detects_short_unaddressed_phrases():
    assert is_familiar_idle_unaddressed({}, "约吗")
    assert is_familiar_idle_unaddressed({}, "嗯")
    assert is_familiar_idle_unaddressed({}, "在吗")
    assert is_familiar_idle_unaddressed({}, "你好")
    assert is_familiar_idle_unaddressed({}, "？")
    assert is_familiar_idle_unaddressed({}, "哈哈哈")
    assert is_familiar_idle_unaddressed({}, "在干嘛")
    assert is_familiar_idle_unaddressed({}, "肺雾")
    assert is_familiar_idle_unaddressed({}, "煞笔")
    assert is_familiar_idle_unaddressed({}, "我没叫你")
    # ≤12 字未点名也 idle
    assert is_familiar_idle_unaddressed({}, "什么时候更新")
    # pure CQ / empty after strip
    assert is_familiar_idle_unaddressed({}, "[CQ:image,file=x.jpg]")
    # @ someone else, not bot
    assert is_familiar_idle_unaddressed(
        {"mentions_self": False},
        "[CQ:at,qq=12345] 约吗",
    )


def test_idle_allows_direct_calls():
    assert not is_familiar_idle_unaddressed({"mentions_self": True}, "约吗")
    assert not is_familiar_idle_unaddressed({"reply_to_self": True}, "嗯")
    assert not is_familiar_idle_unaddressed({}, "哈基米约吗")
    # longer addressable familiar chat is not idle
    assert not is_familiar_idle_unaddressed({}, "大家晚上吃什么好呢这道菜怎么做")


def test_skips_when_addressed_to_other_bot():
    """叫沙琪玛等其它 bot 时哈基米不得抢话；同时叫哈基米仍放行。"""
    assert is_addressed_to_other_bot({}, "沙琪玛，gay会传染吗")
    assert is_addressed_to_other_bot({}, "沙琪玛 今日运势")
    assert is_addressed_to_other_bot({}, "沙琪玛今日运势")
    assert is_addressed_to_other_bot({}, "aki，今日运势")
    assert not is_addressed_to_other_bot({}, "哈基米 今日运势")
    assert not is_addressed_to_other_bot({}, "沙琪玛和哈基米哪个可爱")
    assert not is_addressed_to_other_bot({"mentions_self": True}, "沙琪玛")
    assert not is_addressed_to_other_bot({}, "大家晚上吃什么")


def test_familiar_gate_skips_yue_ma_but_keeps_name_call(tmp_path, monkeypatch):
    from anima.cognitive import relation as relation_mod
    from anima.cognitive.relation import RelationEngine

    data_dir = str(tmp_path)
    monkeypatch.setattr(relation_mod, "_relation_engines", {})
    rs = RelationEngine(data_dir)
    for _ in range(30):
        rs.touch(10001, "又见面了")
    relation_mod._relation_engines[data_dir] = rs

    autonomy = types.SimpleNamespace(
        reply_only_when_mentioned=True,
        familiar_reply_min=0.35,
        other_bot_names=["沙琪玛", "aki"],
    )
    policy = {"reply_mode": "familiar", "familiar_min": 0.35}

    # 熟人但未点名短句 → 不进流水线
    assert not should_reply_in_group(
        {}, "约吗",
        user_id=10001, group_policy=policy, autonomy=autonomy, data_dir=data_dir,
    )
    assert not should_reply_in_group(
        {}, "嗯",
        user_id=10001, group_policy=policy, autonomy=autonomy, data_dir=data_dir,
    )
    # 直接呼叫仍放行
    assert should_reply_in_group(
        {"mentions_self": True}, "约吗",
        user_id=10001, group_policy=policy, autonomy=autonomy, data_dir=data_dir,
    )
    assert should_reply_in_group(
        {}, "哈基米你怎么看",
        user_id=10001, group_policy=policy, autonomy=autonomy, data_dir=data_dir,
    )
    # 有足够实质内容的熟人闲聊仍可插话（>12 字）
    assert should_reply_in_group(
        {}, "大家晚上吃什么好呢这道菜怎么做",
        user_id=10001, group_policy=policy, autonomy=autonomy, data_dir=data_dir,
    )
    # 短闲聊/骂人未点名 → 不接
    assert not should_reply_in_group(
        {}, "在干嘛",
        user_id=10001, group_policy=policy, autonomy=autonomy, data_dir=data_dir,
    )
    # 点名其它 bot → 绝不抢话（即使熟人、即使 open）
    assert not should_reply_in_group(
        {}, "沙琪玛，gay会传染吗",
        user_id=10001, group_policy=policy, autonomy=autonomy, data_dir=data_dir,
    )
    assert not should_reply_in_group(
        {}, "沙琪玛 今日运势",
        user_id=10001, group_policy={"reply_mode": "open"}, autonomy=autonomy, data_dir=data_dir,
    )
    # 同时喊到哈基米仍回
    assert should_reply_in_group(
        {}, "沙琪玛和哈基米都在吗",
        user_id=10001, group_policy=policy, autonomy=autonomy, data_dir=data_dir,
    )
    # 原因分类可观测
    assert classify_group_reply_skip(
        {}, "沙琪玛 今日运势",
        user_id=10001, group_policy=policy, autonomy=autonomy, data_dir=data_dir,
    ) == "other_bot"
    assert classify_group_reply_skip(
        {}, "在干嘛",
        user_id=10001, group_policy=policy, autonomy=autonomy, data_dir=data_dir,
    ) == "idle"
    assert classify_group_reply_skip(
        {}, "大家晚上吃什么好呢这道菜怎么做",
        user_id=10001, group_policy={"reply_mode": "mention"}, autonomy=autonomy, data_dir=data_dir,
    ) == "mention_only"


@pytest.mark.asyncio
async def test_handler_skips_yue_ma_in_familiar_group(tmp_path, monkeypatch):
    from anima.cognitive import relation as relation_mod
    from anima.cognitive.relation import RelationEngine
    from anima.llm.handler import LLMHandler

    data_dir = str(tmp_path)
    monkeypatch.setattr(relation_mod, "_relation_engines", {})
    rs = RelationEngine(data_dir)
    for _ in range(30):
        rs.touch(10001, "又见面了")
    relation_mod._relation_engines[data_dir] = rs

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
            autonomy=types.SimpleNamespace(reply_only_when_mentioned=True, enabled=True,
                                          familiar_reply_min=0.35),
            permissions=types.SimpleNamespace(group_policies={
                "20001": {"reply_mode": "familiar", "familiar_min": 0.35},
            }),
            sticker=types.SimpleNamespace(enabled=False, auto_collect=False, collect_min_interval=0,
                                          always_atmosphere=[]),
            conversation=types.SimpleNamespace(enabled=False),
            persistence=types.SimpleNamespace(data_dir=data_dir),
        )
        gateway = _Gateway()
        llm = None
        group_presence = types.SimpleNamespace(observe=lambda *a, **k: types.SimpleNamespace(state="quiet"))

    handler = LLMHandler(_Core())

    async def fake_run(ctx):
        calls.append(ctx.text)
        ctx.reply = "不该到这里"
        return ctx

    handler.pipeline = types.SimpleNamespace(run=fake_run)
    base = {
        "user_id": 10001,
        "group_id": 20001,
        "raw": {"message_type": "group", "sender": {"nickname": "测试用户"}},
        "mentions_self": False,
        "reply_to_self": False,
    }

    await handler.handle({**base, "text": "约吗"})
    assert calls == []
    assert _Core.gateway.sent == []

    await handler.handle({**base, "text": "哈基米约吗"})
    assert calls == ["哈基米约吗"]
    assert len(_Core.gateway.sent) == 1
