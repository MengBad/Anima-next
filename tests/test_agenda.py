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


def test_terminal_agenda_rows_are_capped_pending_kept(tmp_path):
    """agenda.json 不得单调增长：**终态**条目有上限，`pending` 全留。

    修复前 `save()` 全量写 `self.items`，而 `_prune_expired()` 只把 `pending`
    转成 `expired`、**从不删除** ⇒ 文件只增不减（对比 `episodes[-1000:]` /
    `commitments[-500:]` 都有上限）。终态条目没有行为作用，只占盘。
    """
    persistence = PersistenceProvider(str(tmp_path))
    queue = AgendaQueue(persistence)
    pending_ids = {
        queue.push(AgendaItem(7, f"待发 {i}", not_before_at=9e9)).id for i in range(5)
    }
    for i in range(AgendaQueue._TERMINAL_KEEP + 20):
        queue.consume(queue.push(AgendaItem(7, f"已完成 {i}")).id)

    raw = persistence.read_json("agenda.json", [])
    assert len(raw) == 5 + AgendaQueue._TERMINAL_KEEP, f"终态未裁剪: {len(raw)} 行"

    restored = AgendaQueue(persistence)
    kept_pending = {it.id for it in restored.items if it.status == "pending"}
    assert kept_pending == pending_ids, "pending 条目被误删"
    # 保留的是**最近**的终态，不是最早的
    newest = f"已完成 {AgendaQueue._TERMINAL_KEEP + 19}"
    assert any(it.text_intent == newest for it in restored.items), "最新的终态被裁掉了"


def test_agenda_persists_source_label(tmp_path):
    """🔴 `source` 必须真的落盘 —— 否则清账时只能靠文本形状猜来源。

    修复前 `propose_agenda(source=…)` 只把 source 传给了 subject/audit，
    `AgendaItem` 没有这个字段 ⇒ 生产 agenda.json 33 条全是 `source="?"`，
    无法区分「承诺跟进」「自主思考」「晚安问候」。
    """
    persistence = PersistenceProvider(str(tmp_path))
    queue = AgendaQueue(persistence)
    queue.push(AgendaItem(1, "跟进一下", source="promise_followup"))
    restored = AgendaQueue(persistence)
    assert restored.items[0].source == "promise_followup"


def test_old_agenda_items_without_source_still_load(tmp_path):
    """向后兼容：旧文件没有 `source` 键时，反序列化不得抛（默认 "system"）。"""
    persistence = PersistenceProvider(str(tmp_path))
    persistence.write_json("agenda.json", [
        {"group_id": 1, "text_intent": "旧条目", "priority": 0.5,
         "not_before_at": 0.0, "expires_at": 0.0, "id": "old1",
         "status": "pending", "intent_id": ""},
    ])
    queue = AgendaQueue(persistence)
    assert [i.id for i in queue.items] == ["old1"]
    assert queue.items[0].source == "system"


def test_propose_agenda_passes_source_into_item(tmp_path):
    """源码级守卫：`propose_agenda` 的 source 必须进 AgendaItem。"""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "anima" / "core" / "anima_core.py").read_text(
        encoding="utf-8-sig")
    assert "intent_id=intent.id, source=source," in src


@pytest.mark.asyncio
async def test_heartbeat_consumes_agenda_only_after_success(tmp_path, monkeypatch):
    # mock 时间为白天，避免凌晨静默门控拦截议程消费
    from datetime import datetime as _real_dt
    class _DayDT(_real_dt):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 27, 12, 0, 0, tzinfo=tz)
    import anima.core.autonomy as _auto_mod
    monkeypatch.setattr(_auto_mod, "datetime", _DayDT)

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
