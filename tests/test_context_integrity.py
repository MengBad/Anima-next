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
