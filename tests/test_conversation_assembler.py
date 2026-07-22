import asyncio

import pytest

from anima.core.conversation_assembler import ConversationAssembler
from anima.llm.handler import LLMHandler


@pytest.mark.asyncio
async def test_consecutive_private_messages_become_one_turn():
    assembler = ConversationAssembler(wait_sec=0.02, max_wait_sec=0.1)
    first = asyncio.create_task(
        assembler.ingest({"user_id": 42, "message_id": 1, "raw_message": "第一句"})
    )
    await asyncio.sleep(0.005)
    second = await assembler.ingest(
        {"user_id": 42, "message_id": 2, "raw_message": "第二句"}
    )
    merged = await first

    assert second is None
    assert merged is not None
    assert merged["raw_message"] == "第一句\n第二句"
    assert merged["assembled_message_ids"] == [1, 2]
    await assembler.close()


@pytest.mark.asyncio
async def test_different_users_do_not_share_a_turn():
    assembler = ConversationAssembler(wait_sec=0.01, max_wait_sec=0.1)
    first = asyncio.create_task(
        assembler.ingest({"user_id": 1, "message_id": 1, "raw_message": "甲"})
    )
    second = asyncio.create_task(
        assembler.ingest({"user_id": 2, "message_id": 2, "raw_message": "乙"})
    )
    a, b = await asyncio.gather(first, second)

    assert a["raw_message"] == "甲"
    assert b["raw_message"] == "乙"
    await assembler.close()


@pytest.mark.asyncio
async def test_private_image_then_question_keeps_media_and_reply_source():
    assembler = ConversationAssembler(wait_sec=0.02, max_wait_sec=0.1)
    first = asyncio.create_task(assembler.ingest({
        "user_id": 42, "message_id": 1, "raw_message": "",
        "images": [{"file": "a.jpg", "url": "https://example.invalid/a.jpg"}],
        "reply_message_id": 99,
    }))
    await asyncio.sleep(0.005)
    await assembler.ingest({
        "user_id": 42, "message_id": 2, "raw_message": "这张图是什么",
        "images": [{"file": "a.jpg"}, {"file": "b.jpg"}],
    })
    merged = await first

    assert merged["raw_message"] == "这张图是什么"
    assert [item["file"] for item in merged["images"]] == ["a.jpg", "b.jpg"]
    assert merged["reply_message_id"] == 99
    assert merged["assembled_count"] == 2
    await assembler.close()


@pytest.mark.asyncio
async def test_handler_turn_lock_serializes_only_same_session():
    handler = object.__new__(LLMHandler)
    handler._turn_locks = {}
    same_first = handler._turn_lock("private", None, 42)
    same_second = handler._turn_lock("private", None, 42)
    other_user = handler._turn_lock("private", None, 43)
    group_session = handler._turn_lock("group", 100, 42)

    assert same_first is same_second
    assert same_first is not other_user
    assert same_first is not group_session

    order: list[str] = []

    async def first() -> None:
        async with same_first:
            order.append("first-start")
            await asyncio.sleep(0.02)
            order.append("first-end")

    async def second() -> None:
        await asyncio.sleep(0.002)
        async with same_second:
            order.append("second")

    await asyncio.gather(first(), second())
    assert order == ["first-start", "first-end", "second"]
