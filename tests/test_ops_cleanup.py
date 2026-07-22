"""运维清理：僵尸 selfcode、粘滞笔记/世界观、玩具欲望。"""
from __future__ import annotations

from types import SimpleNamespace

from anima.cognitive.ops_cleanup import (
    cancel_stale_selfcode_tasks,
    filter_world_entries,
    is_sticky_text,
    purge_sticky_note_lines,
    run_ops_cleanup,
    should_resolve_toy_desire,
)
from anima.core.tasks import Task, TaskStore
from anima.core.persistence import PersistenceProvider


def test_sticky_detection():
    assert is_sticky_text("吴语萌欠本喵一百个小鱼干")
    assert is_sticky_text("本喵叫哈基米，不叫沙琪玛")
    assert not is_sticky_text("今天天气不错想吃罐头")


def test_purge_sticky_note_lines():
    raw = "\n".join([
        "[2026-07-18] 今天群里挺热闹",
        "[2026-07-18] 小鱼干债务必须讨回来",
        "[2026-07-18] 本喵叫哈基米不叫沙琪玛",
        "[2026-07-18] 想睡觉",
    ])
    new, dropped = purge_sticky_note_lines(raw)
    assert dropped == 2
    assert "小鱼干" not in new
    assert "沙琪玛" not in new
    assert "挺热闹" in new
    assert "想睡觉" in new


def test_cancel_stale_selfcode():
    now = 1_000_000.0
    tasks = [
        SimpleNamespace(kind="selfcode", status="running", updated_at=now - 10, evidence=[], id="a"),
        SimpleNamespace(kind="selfcode", status="failed", updated_at=now - 10, evidence=[], id="b"),
        SimpleNamespace(kind="fault_ticket", status="running", updated_at=now - 10, evidence=[], id="c"),
    ]
    changed = cancel_stale_selfcode_tasks(tasks, now=now, cancel_all_running=True)
    assert len(changed) == 1
    assert tasks[0].status == "cancelled"


def test_filter_world_entries():
    entries = [
        SimpleNamespace(text="群里大家挺有趣"),
        SimpleNamespace(text="朗姆酒地狱笑话真多"),
        SimpleNamespace(text="喜欢晒太阳"),
    ]
    kept, dropped = filter_world_entries(entries)
    assert dropped == 1
    assert len(kept) == 2


def test_toy_desire_resolve():
    d = SimpleNamespace(kind="self_improvement", resolved=False, acted=False, intensity=0.2, goal="喵语翻译")
    assert should_resolve_toy_desire(d)
    d2 = SimpleNamespace(kind="self_improvement", resolved=False, acted=False, intensity=0.9, goal="修 B 站空转")
    assert not should_resolve_toy_desire(d2)


def test_run_ops_cleanup_end_to_end(tmp_path):
    data = str(tmp_path)
    # notes
    (tmp_path / "self_notes.md").write_text(
        "普通笔记\n欠本喵小鱼干一百个\n又一条普通\n", encoding="utf-8",
    )
    # world
    import json
    (tmp_path / "world_model.json").write_text(json.dumps({
        "entries": [
            {"text": "喜欢晒太阳", "category": "preference", "weight": 0.5},
            {"text": "朗姆酒地狱笑话", "category": "belief", "weight": 0.4},
        ]
    }, ensure_ascii=False), encoding="utf-8")
    # tasks
    store = TaskStore(PersistenceProvider(data))
    t = store.create("make toy", 1, ["a"], kind="selfcode")
    store.set_status(t.id, "running", "stuck")
    # desires
    (tmp_path / "desires.json").write_text(json.dumps([
        {"content": "toy", "intensity": 0.2, "kind": "self_improvement", "goal": "toy",
         "acted": False, "resolved": False},
        {"content": "chat", "intensity": 0.5, "kind": "chat", "acted": False, "resolved": False},
    ], ensure_ascii=False), encoding="utf-8")

    rep = run_ops_cleanup(data_dir=data, cancel_all_running_selfcode=True)
    assert rep.cancelled_selfcode == 1
    assert rep.purged_note_lines >= 1
    assert rep.purged_world_entries == 1
    assert rep.resolved_desires == 1
    notes = (tmp_path / "self_notes.md").read_text(encoding="utf-8")
    assert "小鱼干" not in notes
    store2 = TaskStore(PersistenceProvider(data))
    assert store2.get(t.id).status == "cancelled"
