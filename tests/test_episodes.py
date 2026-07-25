from anima.core.persistence import PersistenceProvider
from anima.memory.episodes import EpisodicMemory


def test_episodes_and_commitments_are_scope_isolated(tmp_path):
    memory = EpisodicMemory(PersistenceProvider(str(tmp_path)))
    memory.observe("group", 1, 10, "user", "我会在明天把测试结果发出来")
    memory.observe("group", 2, 10, "user", "这是另一个群的长问题，应该不会串过去。")
    assert "测试结果" in memory.context("group", 1, 10)
    assert "另一个群" not in memory.context("group", 1, 10)
    assert memory.summary()["commitments_open"] == 1


def test_short_casual_message_is_not_an_episode(tmp_path):
    memory = EpisodicMemory(PersistenceProvider(str(tmp_path)))
    assert memory.observe("private", None, 10, "user", "在吗") is None


def test_group_commitment_can_schedule_followup_agenda(tmp_path):
    created = []

    def agenda_factory(group_id, text, **kwargs):
        created.append((group_id, text, kwargs))
        return type("Agenda", (), {"id": "ag_1"})()

    memory = EpisodicMemory(PersistenceProvider(str(tmp_path)))
    memory.observe(
        "group", 42, 7, "user", "提醒我今晚检查部署日志",
        agenda_factory=agenda_factory,
    )
    assert created and created[0][0] == 42
    assert "提醒我今晚检查部署日志" in created[0][1]
    open_items = memory.open_commitments()
    assert open_items and open_items[0].agenda_id == "ag_1"


def test_bot_promise_creates_commitment_and_agenda(tmp_path):
    created = []

    def agenda_factory(group_id, text, **kwargs):
        created.append((group_id, text, kwargs))
        return type("Agenda", (), {"id": "ag_bot"})()

    memory = EpisodicMemory(PersistenceProvider(str(tmp_path)))
    memory.observe(
        "group", 42, 7, "assistant", "行，本喵会帮你盯着部署结果，到时候告诉你",
        agenda_factory=agenda_factory,
    )
    assert created and "本喵会帮你盯着部署结果" in created[0][1]
    assert memory.summary()["commitments_open"] == 1
