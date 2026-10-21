"""整任务总预算契约（2026-10-21 P0-1）。

## 背景

`AgentExecutor` 此前**只有单步超时**（`STEP_TIMEOUT=180s`）和 `MAX_STEPS=20`
⇒ 理论最坏 **60 分钟无上限**。这与 2026-10-19 出站 LLM「无整轮预算、等 100 秒
才吐一句兜底」是同一类教训（详见 `reports/2026-10-19_direction.md`）。

**为什么现在必须做**：P1 自主触发一旦上线，它会**自己启动任务**。没有整任务
上限 = 失控放大器。先装刹车，再给油门。

生产实证：现有 6 次 Agent 任务实测 30–70s ⇒ 默认 300s 留约 4 倍余量。

## 本测试锁死的边界

1. 超预算 ⇒ 任务标 `failed`、error 写明「超出整任务预算」、**不继续下一步**；
2. **已完成步骤的产物必须保留**（不因超时丢掉已产出的东西）；
3. 未超预算 ⇒ 正常完成（不得误伤）；
4. `total_budget_sec = 0` ⇒ 不限制（排障用）；
5. 超预算写审计 `agent_task_budget_exceeded`（可事后归因）；
6. 私聊告知 owner（说清花了多久、完成几几步、产物留着）。
"""
import asyncio
import types

import pytest

from anima.agent.executor import AgentExecutor
from anima.agent.state import AgentTask, Step

OWNER = 42


class _Mem:
    def __init__(self):
        self.data = {}

    def read_json(self, name, default=None):
        return self.data.get(name, default)

    def write_json(self, name, value):
        self.data[name] = value


class _Store:
    """最小 AgentTaskStore 替身：只实现 executor 用到的 save/is_cancelled 语义。"""

    def __init__(self, task):
        self.task = task
        self.saved = 0

    def save(self, task):
        self.task = task
        self.saved += 1

    def get(self, tid):
        return self.task if self.task.id == tid else None


class _Notifier:
    def __init__(self):
        self.msgs = []

    async def __call__(self, task, message):
        self.msgs.append(message)


class _Auditor:
    def __init__(self):
        self.events = []

    def emit(self, name, **kw):
        self.events.append((name, kw))


def _make_task(n_steps=4, slow=False):
    steps = []
    for i in range(n_steps):
        s = Step(i, f"步骤{i}")
        s.tool_name = "slow_tool" if slow else ""
        steps.append(s)
    t = AgentTask(id="t1", request="测试任务", creator=OWNER, steps=steps)
    return t


def _make_executor(task, budget, *, step_delay=0.0, auditor=None, notify=None):
    store = _Store(task)
    ex = AgentExecutor(
        core=types.SimpleNamespace(config=types.SimpleNamespace()),
        store=store,
        planner=None, auditor=auditor, notify=notify,
        total_budget_sec=budget,
    )

    async def _fake_run_step(step, task_arg, budget_arg):
        if step_delay:
            await asyncio.sleep(step_delay)
        step.status = "done"
        step.result = f"{step.description} 的产物"
        return step.result

    ex._run_step = _fake_run_step
    ex._verify_step = lambda *a, **k: asyncio.sleep(0, result=(False, True, ""))
    ex._replan_or_give_up = lambda *a, **k: asyncio.sleep(0, result=False)
    return ex, store


# ── 1. 超预算必须中止 ─────────────────────────────────────────────────


async def test_over_budget_aborts_task():
    task = _make_task(6)
    notify = _Notifier()
    ex, store = _make_executor(task, budget=0.05, step_delay=0.03, notify=notify)

    result = await ex.execute(task)

    assert task.status == "failed", f"超预算应判 failed，实得 {task.status!r}"
    assert "超出整任务预算" in (task.error or ""), f"error 应说明原因：{task.error!r}"
    assert "已完成" in (task.error or ""), "error 应说明完成了几步"
    assert task.current_step < len(task.steps), "超预算后不得跑完全部步骤"


async def test_over_budget_preserves_completed_products():
    """🔴 超时不能把已经产出的东西丢掉。"""
    task = _make_task(8)
    ex, store = _make_executor(task, budget=0.05, step_delay=0.03)

    await ex.execute(task)

    done = [s for s in task.steps if s.status == "done"]
    assert done, "至少应完成 1 步（超时前已做的工作）"
    assert all(s.result for s in done), "已完成步骤的产物不得丢失"
    for s in done:
        assert s.result.endswith("的产物")


async def test_over_budget_notifies_owner():
    task = _make_task(6)
    notify = _Notifier()
    ex, _ = _make_executor(task, budget=0.05, step_delay=0.03, notify=notify)

    await ex.execute(task)

    joined = "\n".join(notify.msgs)
    assert "超时中止" in joined, f"应告知 owner 超时：{joined!r}"
    assert "上限" in joined
    assert "留着" in joined, "应说明产物保留"


async def test_over_budget_writes_audit():
    task = _make_task(6)
    auditor = _Auditor()
    ex, _ = _make_executor(task, budget=0.05, step_delay=0.03, auditor=auditor)

    await ex.execute(task)

    names = [e[0] for e in auditor.events]
    assert "agent_task_budget_exceeded" in names, f"应写审计，实得 {names}"
    ev = dict(auditor.events)["agent_task_budget_exceeded"]
    assert ev.get("budget_sec") == 0.05
    assert isinstance(ev.get("elapsed_sec"), float)


# ── 2. 未超预算不得误伤 ───────────────────────────────────────────────


async def test_within_budget_completes_normally():
    task = _make_task(3)
    ex, store = _make_executor(task, budget=60.0)

    await ex.execute(task)

    assert task.status == "completed", f"未超预算应正常完成，实得 {task.status!r}"
    assert all(s.status == "done" for s in task.steps), "所有步骤都应完成"


# ── 3. 0 = 不限制 ────────────────────────────────────────────────────


async def test_zero_budget_means_unlimited():
    task = _make_task(5)
    ex, _ = _make_executor(task, budget=0.0)

    await ex.execute(task)

    assert task.status == "completed", "budget=0 表示不限制，不得中止"


# ── 4. 默认值契约 ────────────────────────────────────────────────────


def test_default_budget_is_300s():
    """默认 300s。改动此值需说明依据（生产实证 30–70s）。"""
    assert AgentExecutor.DEFAULT_TOTAL_BUDGET_SEC == 300.0


def test_config_default_total_budget_and_parallel():
    from anima.core.config import TaskAgentConfig
    c = TaskAgentConfig()
    assert c.total_budget_sec == 300.0
    assert c.max_parallel == 2, (
        "2 核/3.9G 机上 max_parallel 必须为 2：owner 沙箱单实例 512MB，"
        "4 并发 ≈ 2G 叠加主进程会 OOM"
    )
