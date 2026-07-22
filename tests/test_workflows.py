from anima.core.persistence import PersistenceProvider
from anima.core.workflows import WorkflowStore


def test_workflow_tracks_intent_until_delivery(tmp_path):
    store = WorkflowStore(PersistenceProvider(str(tmp_path)))
    run = store.trigger("subscription", "sub-1", 9, "开播通知", event_key="live-1")
    assert store.trigger("subscription", "sub-1", 9, "开播通知", event_key="live-1").id == run.id
    assert store.bind_intent(run.id, "int_1")
    assert store.complete_by_intent("int_1") == 1
    assert store.items[run.id].status == "completed"


def test_workflow_fails_when_intent_expires(tmp_path):
    store = WorkflowStore(PersistenceProvider(str(tmp_path)))
    run = store.trigger("subscription", "sub-2", 9, "变更通知", event_key="chg-1")
    assert store.bind_intent(run.id, "int_expired")
    assert store.fail_by_intent("int_expired", "关联意图已过期") == 1
    assert store.items[run.id].status == "failed"
