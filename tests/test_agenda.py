from anima.core.persistence import PersistenceProvider
import pytest

from anima.cognitive.agenda import AgendaItem, AgendaQueue
from anima.core.anima_core import AnimaCore
from anima.core.config import AnimaConfig
from anima.core.autonomy import Heartbeat
from anima.core.persistence import PersistenceProvider


def test_agenda_persist_and_consume(tmp_path):
    queue = AgendaQueue(PersistenceProvider(str(tmp_path)))
    item = queue.push(AgendaItem(1, "hi"))
    assert queue.peek_ready(1).id == item.id
    assert queue.consume(item.id)
    assert AgendaQueue(PersistenceProvider(str(tmp_path))).peek_ready(1) is None


def test_agenda_expiry_and_cancel(tmp_path):
    queue = AgendaQueue(PersistenceProvider(str(tmp_path)))
    expired = queue.push(AgendaItem(1, "expired", expires_at=1))
    active = queue.push(AgendaItem(2, "active"))
    assert queue.peek_ready(now=2) is active
    assert expired.status == "expired"
    assert queue.cancel(active.id)
    assert not queue.pending()


def test_agenda_waits_until_scheduled_time_and_persists(tmp_path):
    persistence = PersistenceProvider(str(tmp_path))
    queue = AgendaQueue(persistence)
    item = queue.push(AgendaItem(7, "稍后提醒", not_before_at=200))
    assert queue.peek_ready(now=199) is None
    restored = AgendaQueue(persistence)
    assert restored.peek_ready(now=200).id == item.id


@pytest.mark.asyncio
async def test_heartbeat_consumes_agenda_only_after_success(tmp_path, monkeypatch):
    cfg = AnimaConfig.model_validate({"persistence": {"data_dir": str(tmp_path)}})
    core = AnimaCore(cfg)
    heartbeat = Heartbeat(core, data_dir=str(tmp_path), active_global_cooldown_sec=0)
    item = core.propose_agenda(727631526, "提醒大家喝水")
    calls = []

    async def speak(group_id, now, intent=""):
        calls.append((group_id, intent))
        return True

    monkeypatch.setattr(heartbeat, "_speak", speak)
    monkeypatch.setattr(heartbeat, "_life_checkpoints", lambda: __import__("asyncio").sleep(0))
    await heartbeat._tick()

    assert calls == [(727631526, "提醒大家喝水")]
    assert core.agenda.peek_ready() is None
    assert core.intent_ledger.get(item.intent_id).status == "completed"
