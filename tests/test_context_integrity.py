"""上下文隔离与群会话连续性回归。"""
from __future__ import annotations

import pytest

from anima.memory.chat_memory import ChatMemory
from anima.memory.vector_memory import VectorMemory
from anima.pipeline import PipelineContext
from anima.pipeline.stages import guardrail_stage


def test_bot_history_uses_openai_assistant_role(tmp_path):
    memory = ChatMemory(persist_path=str(tmp_path / "chat.json"))
    memory.add("private", 7, "bot", "Anima", "上一句", user_id=7)

    history = memory.get_context("private", 7, user_id=7)

    assert history[0]["role"] == "assistant"
    assert history[0]["content"].endswith("上一句")


def test_group_recent_context_is_isolated_and_chronological(tmp_path):
    memory = ChatMemory(persist_path=str(tmp_path / "chat.json"))
    memory.observe_group_message(100, 1, "甲", "第一句", 1)
    memory.observe_group_bot_reply(100, "本喵的回复")
    memory.observe_group_message(100, 2, "乙", "第二句", 2)
    memory.observe_group_message(200, 3, "丙", "别的群", 3)

    context = memory.get_recent_group_context(100, exclude_message_id=2)

    assert [item["role"] for item in context] == ["user", "assistant"]
    assert "第一句" in context[0]["content"]
    assert "本喵的回复" in context[1]["content"]
    assert all("别的群" not in item["content"] for item in context)


@pytest.mark.asyncio
async def test_vector_recall_strictly_scopes_user_and_group(tmp_path):
    memory = VectorMemory(db_path=str(tmp_path / "memory.db"), embedding=None)
    await memory.add("共同关键词 私密甲", source_uin=1, source_gid=100)
    await memory.add("共同关键词 私密乙", source_uin=2, source_gid=100)
    await memory.add("共同关键词 另一群", source_uin=1, source_gid=200)

    hits = await memory.search(
        "共同关键词", top_k=5, source_uin=1, source_gid=100, scoped=True,
    )

    assert [item["text"] for item in hits] == ["共同关键词 私密甲"]


@pytest.mark.asyncio
async def test_guardrail_rewrite_is_stored_as_final_reply(tmp_path, monkeypatch):
    import anima.memory.chat_memory as chat_memory_mod

    memory = ChatMemory(persist_path=str(tmp_path / "chat.json"))
    monkeypatch.setattr(chat_memory_mod, "get_chat_memory", lambda: memory)
    ctx = PipelineContext(
        event={}, text="敏感话题", user_id=7, group_id=100,
        detail_type="group", sender_name="甲", meta={"chat_id": 100},
    )
    ctx.reply = "-----BEGIN RSA PRIVATE KEY-----"

    await guardrail_stage(ctx)

    own_history = memory.get_context("group", 100, user_id=7)
    group_history = memory.get_recent_group_context(100)
    assert own_history[-1]["content"].endswith("（这条回复被护栏拦截了）")
    assert group_history[-1]["role"] == "assistant"
    assert "护栏拦截" in group_history[-1]["content"]


def test_active_speak_does_not_pollute_personal_buckets(tmp_path):
    """主动发言（群级可见）只进短时群上下文，不广播进每个用户的个人持久桶。

    回归背景：旧 add_group_bot_reply 把一条主动发言写入该群所有 (群,用户) 桶，
    造成 N 倍存储冗余 + 跨用户个人上下文污染（个人桶混入群公开发言）。
    """
    memory = ChatMemory(persist_path=str(tmp_path / "chat.json"))

    # 两个用户各自有个人桶（各自发过一条消息，形成独立 (群,用户) 桶）
    memory.observe_group_message(100, 1, "甲", "甲的消息", 1)
    memory.observe_group_message(100, 2, "乙", "乙的消息", 2)

    # 主动发言：群级可见，走短时群上下文
    memory.observe_group_bot_reply(100, "本喵的主动发言")

    # 短时群上下文应包含主动发言
    recent = memory.get_recent_group_context(100)
    assert any("主动发言" in m["content"] for m in recent),         "主动发言应进入短时群上下文"

    # 个人持久桶不应被广播写入主动发言
    for uid in (1, 2):
        own = memory.get_context("group", 100, user_id=uid)
        texts = [m["content"] for m in own]
        assert all("主动发言" not in t for t in texts),             f"用户 {uid} 的个人桶不应混入群级主动发言"
        # 个人桶只保留该用户自己的一对一消息
        assert any(("甲的消息" if uid == 1 else "乙的消息") in t for t in texts),             f"用户 {uid} 的个人桶应保留自己的消息"
