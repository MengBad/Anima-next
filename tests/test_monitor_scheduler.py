import types
import pytest
from anima.core.persistence import PersistenceProvider
from anima.monitors.scheduler import MonitorScheduler
from anima.monitors.subscriptions import Subscription
from anima.core.workflows import WorkflowStore

@pytest.mark.asyncio
async def test_scheduler_enqueues_once_on_live_transition(tmp_path):
    sent=[]
    async def send(msg):
        sent.append(msg)
    persistence=PersistenceProvider(str(tmp_path))
    proposed=[]
    def propose(group_id, text, **kwargs):
        proposed.append((group_id,text,kwargs))
        return types.SimpleNamespace(intent_id="int_notify")
    core=types.SimpleNamespace(persistence=persistence, gateway=types.SimpleNamespace(send=send),
                               workflows=WorkflowStore(persistence), propose_agenda=propose)
    scheduler=MonitorScheduler(core)
    class Provider:
        async def query(self, room): return types.SimpleNamespace(state="live", event_key="x", name="UP", title="标题", url="u")
    scheduler._providers["bilibili_live"]=Provider()
    sub=scheduler.store.add(Subscription(platform="bilibili_live",subject_id="1",target_groups=[2],poll_interval_sec=30))
    await scheduler.tick(); assert sent == []
    assert len(proposed)==1
    assert core.workflows.list()[0].status == "awaiting_delivery"
    scheduler._next[sub.id]=0; await scheduler.tick(); assert len(proposed)==1
