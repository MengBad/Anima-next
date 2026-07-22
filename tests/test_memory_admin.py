"""Owner 记忆审查、纠正与删除能力。"""
from __future__ import annotations

import types

import pytest

from anima.crossgroup.memory.manager import CrossGroupManager
from anima.llm.handler import LLMHandler


def test_crossgroup_fact_replace_delete_and_forget(tmp_path):
    manager = CrossGroupManager(str(tmp_path / "crossgroup"))
    manager.add_user_fact("42", "喜欢红茶", "100")
    manager.add_user_fact("42", "住在火星", "100")
    assert manager.replace_user_fact("42", 2, "喜欢天文")
    assert manager.get_user("42").facts[1].content == "喜欢天文"
    assert manager.delete_user_fact("42", 1)
    assert [fact.content for fact in manager.get_user("42").facts] == ["喜欢天文"]
    assert manager.forget_user("42") == 1
    assert "42" not in manager._users


@pytest.mark.asyncio
async def test_owner_memory_show_and_replace_commands(tmp_path, monkeypatch):
    manager = CrossGroupManager(str(tmp_path / "crossgroup"))
    manager.add_user_fact("42", "住在火星", "100")
    monkeypatch.setattr("anima.crossgroup.get_crossgroup_manager", lambda: manager)

    class _Vector:
        async def export_by_source_uin(self, user_id):
            return [{"rowid": 7, "text": "旧的长期记录"}]

    class _Gateway:
        sent = []

        async def send(self, msg):
            self.sent.append(msg)
            return 1

    core = types.SimpleNamespace(
        config=types.SimpleNamespace(
            permissions=types.SimpleNamespace(owners=[1]),
            persistence=types.SimpleNamespace(data_dir=str(tmp_path)),
        ),
        vector_memory=_Vector(),
        gateway=_Gateway(),
    )
    handler = LLMHandler.__new__(LLMHandler)
    handler.core = core

    event = {"raw": {"sender": {"nickname": "主人"}}}
    assert await handler._handle_command(event, "!anima memory show 42", 1, None, "private")
    assert "F1. 住在火星" in core.gateway.sent[-1].message
    assert "V7. 旧的长期记录" in core.gateway.sent[-1].message

    assert await handler._handle_command(
        event, "!anima memory replace 42 1 喜欢天文", 1, None, "private",
    )
    assert manager.get_user("42").facts[0].content == "喜欢天文"


@pytest.mark.asyncio
async def test_non_owner_cannot_manage_others_memory(tmp_path):
    class _Gateway:
        sent = []

        async def send(self, msg):
            self.sent.append(msg)

    handler = LLMHandler.__new__(LLMHandler)
    handler.core = types.SimpleNamespace(
        config=types.SimpleNamespace(permissions=types.SimpleNamespace(owners=[1])),
        gateway=_Gateway(),
    )
    consumed = await handler._handle_command(
        {"raw": {}}, "!anima memory show 42", 2, None, "private",
    )
    assert consumed is False
    assert handler.core.gateway.sent == []
