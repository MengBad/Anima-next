from anima.core.persistence import PersistenceProvider
from anima.monitors import Subscription, SubscriptionStore


def test_live_transition_notifies_once_and_survives_restore(tmp_path):
    store = SubscriptionStore(PersistenceProvider(str(tmp_path)))
    sub = store.add(Subscription(platform="bilibili_live", subject_id="42", target_groups=[727631526]))
    assert not store.transition(sub.id, "offline", "")
    assert store.transition(sub.id, "live", "room-1")
    assert not store.transition(sub.id, "live", "room-1")
    restored = SubscriptionStore(PersistenceProvider(str(tmp_path))); restored.restore()
    assert not restored.transition(sub.id, "live", "room-1")
    assert restored.transition(sub.id, "live", "room-2")


def test_subscription_rejects_invalid_group(tmp_path):
    store = SubscriptionStore(PersistenceProvider(str(tmp_path)))
    try:
        store.add(Subscription(platform="x", subject_id="y", target_groups=[0]))
    except ValueError:
        return
    assert False
