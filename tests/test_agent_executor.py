"""Phase 4 测试：AgentExecutor 多步执行、重试、失败、取消、熔断、资源限额。"""

import asyncio

from anima.agent.executor import AgentExecutor
from anima.agent.guardrails import ResourceExceeded
from anima.agent.state import AgentTask, AgentTaskStore, Step


class _Mem:
    def __init__(self): self.data = {}
    def read_json(self, name, default=None): return self.data.get(name, default)
    def write_json(self, name, value): self.data[name] = value


class _FakeTools:
    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.calls = []
        self._ctx = {}
    async def call(self, name, args, level):
        self.calls.append((name, args, level))
        out = self.outcomes.get(name)
        if callable(out):
            return out(name, args, level)
        if isinstance(out, list):
            return out.pop(0) if out else "ok"
        return out or "ok"
    def get_call_context(self): return self._ctx
    def set_call_context(self, **kw):
        self._ctx = dict(kw); return object()
    def reset_call_context(self, tok): self._ctx = {}


class _FakeCore:
    def __init__(self, tools): self.tools = tools; self.persistence = _Mem()


def _task_with_steps(n, tool_name="dummy"):
    steps = [Step(index=i, description=f"step{i}", tool_name=tool_name,
                  tool_args={"x": i}) for i in range(n)]
    return AgentTask(id="t1", request="do things", creator=1, steps=steps)


async def test_executor_runs_all_steps():
    tools = _FakeTools({"dummy": "step-done"})
    core = _FakeCore(tools)
    store = AgentTaskStore(core.persistence)
    ex = AgentExecutor(core, store)
    task = _task_with_steps(3)
    res = await ex.execute(task)
    assert "步骤1" in res and "步骤3" in res
    assert task.status == "completed"
    assert task.current_step == 3
    assert len(tools.calls) == 3


async def test_executor_retries_then_succeeds():
    # 前两次失败，第三次成功
    seq = ["错误: 失败", "错误: 又失败", "success"]
    tools = _FakeTools({"dummy": list(seq)})
    core = _FakeCore(tools)
    store = AgentTaskStore(core.persistence)
    ex = AgentExecutor(core, store)
    task = _task_with_steps(1)
    res = await ex.execute(task)
    assert task.status == "completed"
    assert len(tools.calls) == 3  # 重试两次


async def test_executor_fails_after_retries():
    tools = _FakeTools({"dummy": "错误: 一直失败"})
    core = _FakeCore(tools)
    store = AgentTaskStore(core.persistence)
    ex = AgentExecutor(core, store)
    task = _task_with_steps(1)
    res = await ex.execute(task)
    assert task.status == "failed"
    assert "失败" in res


async def test_executor_cancel_mid_run():
    # 第一步就把 cancel 置位，应在步边界退出
    def _flaky(name, args, level):
        return "ok"
    tools = _FakeTools({"dummy": _flaky})
    core = _FakeCore(tools)
    store = AgentTaskStore(core.persistence)
    ex = AgentExecutor(core, store)
    task = _task_with_steps(3)
    ex.request_cancel(task.id)
    res = await ex.execute(task)
    assert task.status == "cancelled"
    assert "取消" in res
    assert len(tools.calls) == 0  # 未执行任何步骤


async def test_executor_circuit_breaker_after_3_failures():
    tools = _FakeTools({"dummy": "错误: 失败"})
    core = _FakeCore(tools)
    store = AgentTaskStore(core.persistence)
    ex = AgentExecutor(core, store)
    for _ in range(3):
        t = _task_with_steps(1)
        await ex.execute(t)
    assert ex._fail_streak >= 3
    assert ex._pause_until > asyncio.get_event_loop().time()
    # 熔断期间新任务被拒
    t4 = _task_with_steps(1)
    res = await ex.execute(t4)
    # 🔴 2026-10-29：用户消息里不再出现「熔断/暂停 10 分钟」这类工程词。
    # 改成一句人话（「刚才连着没弄成，先缓十分钟喵」）。
    # 断言改为「表达了『现在做不了』这个意思」，而不是某个内部术语。
    assert "缓" in res or "十分钟" in res or "没弄成" in res, (
        f"应告知用户当前不可用，实得：{res!r}"
    )


async def test_executor_resource_limit():
    # 单步输出超限 -> 任务失败并标记
    big = "x" * (600 * 1024)  # 600KB > 512KB 上限
    tools = _FakeTools({"dummy": big})
    core = _FakeCore(tools)
    store = AgentTaskStore(core.persistence)
    # 放宽其它配额，仅卡输出
    ex = AgentExecutor(core, store)
    task = _task_with_steps(1)
    res = await ex.execute(task)
    assert task.status == "failed"
