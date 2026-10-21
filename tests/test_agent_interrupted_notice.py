"""重启打断任务的告知契约（2026-10-21）。

## 背景

改造前 `AgentTaskStore.__init__` 把重启时在跑的任务直接标 `failed` + 静默 ——
用户只知道「我没收到结果」，不知道「它被重启打断了」。

改造后：状态标 `interrupted`（进度保留、可重跑）+ 登记 `interrupted_on_load`
+ 启动后 `OwnerAgent.notify_interrupted_on_startup()` 私聊告知 owner。

## 本测试锁死的边界

1. 有 interrupted 任务 → 私聊发通知，**且只发私聊**；
2. 通知内容含 task_id（owner 能据此重跑）、含进度（已 n/N 步）；
3. 无 interrupted → 不发（不刷屏）；
4. gateway 未就绪 → 不抛异常、返回 0（fail-soft，绝不拖垮启动）；
5. 发送失败 → 已隔离，不影响其他任务的通知；
6. 通知后写审计（可追溯）。
"""
import types

from anima.agent.owner_agent import OwnerAgent
from anima.agent.state import AgentTask, AgentTaskStore, Step

OWNER = 42


class _Mem:
    """内存版 PersistenceProvider。"""

    def __init__(self, data=None):
        self.data = data or {}

    def read_json(self, name, default=None):
        return self.data.get(name, default)

    def write_json(self, name, value):
        self.data[name] = value


class _GW:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    async def send(self, msg):
        if self.fail:
            raise RuntimeError("QQ 断了")
        self.sent.append(msg)


class _Auditor:
    def __init__(self):
        self.events = []

    def emit(self, name, **kw):
        self.events.append((name, kw))


def _make_agent(store, gateway, auditor=None):
    """构造一个只带必要属性的 OwnerAgent（绕过 __init__ 的完整装配）。"""
    agent = OwnerAgent.__new__(OwnerAgent)
    agent.core = types.SimpleNamespace(gateway=gateway)
    agent.store = store
    agent.auditor = auditor or _Auditor()
    return agent


def _seed_interrupted(store, *, steps_done=1, total=3, request="分析一下日志"):
    t = store.create(request, OWNER)
    t.status = "executing"
    t.steps = [Step(i, f"步骤{i}") for i in range(total)]
    for i in range(steps_done):
        t.steps[i].status = "done"
        t.steps[i].result = f"产物{i}"
    t.current_step = steps_done
    store.save(t)
    return t


# ── 1. 正常通知 ───────────────────────────────────────────────────────


async def test_notifies_owner_privately():
    p = _Mem()
    store = AgentTaskStore(p)
    task = _seed_interrupted(store)
    store = AgentTaskStore(p)      # 模拟重启
    gw = _GW()
    agent = _make_agent(store, gw)

    n = await agent.notify_interrupted_on_startup()

    assert n == 1, f"应有 1 条通知，实得 {n}"
    assert len(gw.sent) == 1
    msg = gw.sent[0]
    assert msg.detail_type == "private", "重启打断通知必须走私聊，不得发群"
    assert task.id in msg.message, "必须含 task_id，否则 owner 无法重跑"
    assert "1/3" in msg.message, f"必须含进度（已 1/3 步）：{msg.message!r}"
    assert "approve" in msg.message, "必须给出重跑方式"


async def test_notification_never_goes_to_group():
    p = _Mem()
    store = AgentTaskStore(p)
    _seed_interrupted(store)
    store = AgentTaskStore(p)
    gw = _GW()
    agent = _make_agent(store, gw)

    await agent.notify_interrupted_on_startup()

    assert all(m.detail_type == "private" for m in gw.sent), "出现群出站即违规"


# ── 2. 无 interrupted 不发（不刷屏）───────────────────────────────────


async def test_no_interrupted_means_no_send():
    store = AgentTaskStore(_Mem())
    gw = _GW()
    agent = _make_agent(store, gw)

    n = await agent.notify_interrupted_on_startup()

    assert n == 0
    assert gw.sent == [], "没有被打断的任务时不得发送任何消息"


# ── 3. fail-soft ─────────────────────────────────────────────────────


async def test_no_gateway_does_not_raise():
    p = _Mem()
    store = AgentTaskStore(p)
    _seed_interrupted(store)
    store = AgentTaskStore(p)
    agent = _make_agent(store, None)   # gateway 未就绪

    n = await agent.notify_interrupted_on_startup()   # 不应抛出
    assert n == 0


async def test_send_failure_is_isolated():
    p = _Mem()
    store = AgentTaskStore(p)
    _seed_interrupted(store)
    store = AgentTaskStore(p)
    gw = _GW(fail=True)
    agent = _make_agent(store, gw)

    n = await agent.notify_interrupted_on_startup()   # 不应抛出
    assert n == 0


# ── 4. 审计留痕 ───────────────────────────────────────────────────────


async def test_audit_emitted_after_notify():
    p = _Mem()
    store = AgentTaskStore(p)
    task = _seed_interrupted(store)
    store = AgentTaskStore(p)
    gw = _GW()
    auditor = _Auditor()
    agent = _make_agent(store, gw, auditor)

    await agent.notify_interrupted_on_startup()

    names = [e[0] for e in auditor.events]
    assert "agent_tasks_interrupted" in names, f"应写审计事件，实得 {names}"
    ev = dict(auditor.events)["agent_tasks_interrupted"]
    assert ev.get("count") == 1
    assert task.id in (ev.get("task_ids") or [])
