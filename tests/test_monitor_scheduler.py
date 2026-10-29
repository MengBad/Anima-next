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


# ── 2026-10-29：订阅删除后的残留计数清理 ─────────────────────────────

def test_removed_subscription_leaves_no_residue():
    """订阅删除后，三个以 `sub.id` 为键的 dict 必须清空。

    `_fail_streak` / `_anomaly_notified_at` / `_next` 都只增不减：
    唯一的 pop 在「查询成功」分支（那是清零，不是删键）。
    长期运行 + 订阅增删 ⇒ `_anomaly_notified_at` 永久膨胀。
    """
    import asyncio, time
    from anima.monitors.scheduler import MonitorScheduler

    class _Core:
        class persistence: pass
        class config: pass

    s = MonitorScheduler(_Core())
    # 手工塞入一个「已不存在于 store」的残留键
    s._fail_streak["ghost-sub"] = 3
    s._anomaly_notified_at["ghost-sub"] = time.time()
    s._next["ghost-sub"] = time.time() + 999
    asyncio.run(s.tick())
    assert "ghost-sub" not in s._fail_streak, "_fail_streak 残留未清"
    assert "ghost-sub" not in s._anomaly_notified_at, "_anomaly_notified_at 残留未清"
    assert "ghost-sub" not in s._next, "_next 残留未清"
