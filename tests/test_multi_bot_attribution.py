"""P2 回归：多 bot 环境识别 + 群头衔变更事件溯源写 self_notes。

- is_bot_member：role==bot 或命中 known_bot_ids 判定为 bot；
- build_title_change_note：群专属头衔变更且目标是自己时生成 self_notes 文本；
- 集成：群消息发信人标记 sender_is_bot；group_title_update 事件写 self_notes。
"""
import pytest

from anima.channels.onebot11 import (
    OneBot11Adapter,
    is_bot_member,
    build_title_change_note,
)


# ---------------------------------------------------------------------------
# 纯函数：bot 判定
# ---------------------------------------------------------------------------
def test_is_bot_member_by_role():
    assert is_bot_member({"user_id": 1, "role": "bot"}) is True
    assert is_bot_member({"user_id": 1, "role": "member"}) is False


def test_is_bot_member_by_known_ids():
    assert is_bot_member({"user_id": 999, "role": "member"}, known_bot_ids={999}) is True
    assert is_bot_member({"user_id": 1, "role": "member"}, known_bot_ids={999}) is False


def test_is_bot_member_invalid_input():
    assert is_bot_member(None) is False
    assert is_bot_member({}) is False


# ---------------------------------------------------------------------------
# 纯函数：头衔变更 note
# ---------------------------------------------------------------------------
def test_title_change_note_target_self():
    ev = {"notice_type": "group_title_update", "user_id": 123, "operator_id": 456, "title": "群宠"}
    note = build_title_change_note(ev, self_id=123)
    assert note == "头衔被QQ 456改为【群宠】"


def test_title_change_note_target_other():
    ev = {"notice_type": "group_title_update", "user_id": 999, "operator_id": 456, "title": "群宠"}
    assert build_title_change_note(ev, self_id=123) is None


def test_title_change_note_cleared():
    ev = {"notice_type": "group_title_update", "user_id": 123, "title": ""}
    note = build_title_change_note(ev, self_id=123)
    assert note == "头衔被有人清空了"


# ---------------------------------------------------------------------------
# 集成：notice 事件写 self_notes
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_title_change_writes_self_notes(monkeypatch, tmp_path):
    captured = []

    class _FakeLearning:
        def add_note(self, content, delta=0.05, category=None):
            captured.append(content)
            return True

    monkeypatch.setattr(
        "anima.cognitive.learning.get_learning", lambda *_a, **_k: _FakeLearning()
    )

    adapter = OneBot11Adapter()

    class _EventBus:
        async def publish(self, topic, event):
            return None

    class _Gateway:
        async def on_event(self, event):
            return None

    class _Cfg:
        class qq:
            known_bot_ids = []

        persistence = type("P", (), {"data_dir": str(tmp_path)})()

    class _Core:
        config = _Cfg()
        event_bus = _EventBus()
        gateway = _Gateway()

    adapter.core = _Core()
    adapter.self_id = 123

    raw = {
        "post_type": "notice",
        "notice_type": "group_title_update",
        "group_id": 727631526,
        "user_id": 123,
        "operator_id": 456,
        "title": "群宠",
        "self_id": 123,
        "time": 1,
    }
    await adapter._dispatch_event_inner(raw)
    assert any("头衔被QQ 456改为【群宠】" in c for c in captured)


# ---------------------------------------------------------------------------
# 集成：群消息发信人标记为 bot
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_sender_bot_marked_in_group_message(monkeypatch):
    adapter = OneBot11Adapter()

    class _EventBus:
        async def publish(self, topic, event):
            return None

    class _Gateway:
        async def on_event(self, event):
            return None

    class _Cfg:
        class qq:
            known_bot_ids = [888]

    class _Core:
        config = _Cfg()
        event_bus = _EventBus()
        gateway = _Gateway()

    adapter.core = _Core()
    adapter.self_id = 123

    captured = {}

    async def _capture(ev):
        captured.update(ev)

    adapter.core.gateway.on_event = _capture

    raw = {
        "post_type": "message",
        "message_type": "group",
        "self_id": 123,
        "user_id": 888,
        "group_id": 727631526,
        "message_id": 10,
        "message": [{"type": "text", "data": {"text": "大家好"}}],
        "raw_message": "大家好",
        "sender": {"user_id": 888, "role": "member"},
        "time": 1,
    }
    await adapter._dispatch_event_inner(raw)
    assert captured.get("sender_is_bot") is True
