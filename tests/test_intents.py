from anima.cognitive.agenda import AgendaItem
from anima.core.intents import IntentLedger, SubjectOrchestrator
from anima.core.persistence import PersistenceProvider


def test_intent_lifecycle_is_deduplicated_and_persisted(tmp_path):
    ledger = IntentLedger(PersistenceProvider(str(tmp_path)))
    first = ledger.create(kind="expression", source="owner", target_kind="group", target_id=7,
                          content="提醒大家喝水", priority=.8)
    duplicate = ledger.create(kind="expression", source="owner", target_kind="group", target_id=7,
                              content="提醒大家喝水", priority=.8)
    assert duplicate.id == first.id
    assert ledger.claim(first.id) is not None
    assert ledger.complete(first.id, "sent") is True
    restored = IntentLedger(PersistenceProvider(str(tmp_path)))
    assert restored.get(first.id).status == "completed"


def test_legacy_agenda_is_imported_then_released(tmp_path):
    ledger = IntentLedger(PersistenceProvider(str(tmp_path)))
    subject = SubjectOrchestrator(ledger)
    agenda = AgendaItem(9, "稍后提醒", priority=.7)
    intent = subject.claim_agenda(agenda, now=10)
    assert intent is not None and agenda.intent_id == intent.id
    assert ledger.mark_dispatched(intent.id) is True
    assert ledger.get(intent.id).status == "dispatched"
    assert ledger.release(intent.id, "cooldown") is True
    assert ledger.get(intent.id).status == "queued"


def test_expired_intent_notifies_callback(tmp_path):
    seen = []
    ledger = IntentLedger(PersistenceProvider(str(tmp_path)))
    ledger.on_expired = seen.append
    item = ledger.create(
        kind="expression", source="test", target_kind="group", target_id=1,
        content="过期测试", expires_at=1.0,
    )
    assert ledger.expire(now=10) == 1
    assert item.id in seen
    assert ledger.get(item.id).status == "expired"
