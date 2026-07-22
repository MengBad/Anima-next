from anima.cognitive.group_presence import GroupPresenceManager


def test_group_presence_transitions_and_persists(tmp_path):
    manager = GroupPresenceManager(str(tmp_path))
    assert manager.observe(100).state == "OBSERVATION"
    for _ in range(4):
        manager.observe(100)
    assert manager.get(100).state == "GETTING_FAMILIAR"
    assert manager.observe(100, summoned=True).state == "SUMMONED"

    restored = GroupPresenceManager(str(tmp_path))
    assert restored.get(100).state == "SUMMONED"
    assert "被呼唤" in restored.fragment(100)
