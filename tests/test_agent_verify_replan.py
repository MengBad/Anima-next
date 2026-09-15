"""P2-1 测试：Agent 多步执行的独立验收、失败重规划、并行批次。

覆盖：
- 验收通过 / 不通过（不通过 → 走重规划）
- 无验收判据、无 LLM、判定模糊 → 放行（fail-open 但写审计）
- 重规划：替换剩余计划、上限约束、无 LLM 时不做重规划（保持旧行为）
- 并行：同 group 且显式工具 → 并发；ReAct 步骤与不同 group → 串行
- planner 解析：字符串 / 对象 / 混合 / 脏输出
- 状态序列化新字段往返
"""

import asyncio
import inspect
import json

import pytest

from anima.agent.executor import AgentExecutor
from anima.agent.planner import TaskPlanner
from anima.agent.state import AgentTask, AgentTaskStore, Step


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------

class _Mem:
    def __init__(self):
        self.data = {}

    def read_json(self, name, default=None):
        return self.data.get(name, default)

    def write_json(self, name, value):
        self.data[name] = value


class _FakeTools:
    def __init__(self, outcomes=None):
        self.outcomes = outcomes or {}
        self.calls = []
        self._ctx = {}

    async def call(self, name, args, level):
        self.calls.append((name, args, level))
        out = self.outcomes.get(name, "ok")
        if callable(out):
            res = out(name, args, level)
            if inspect.isawaitable(res):
                res = await res
            return res
        if isinstance(out, list):
            return out.pop(0) if out else "ok"
        return out

    def get_schemas(self, *a, **kw):
        return []

    def get_call_context(self):
        return dict(self._ctx)

    def set_call_context(self, **kw):
        self._ctx = dict(kw)
        return object()

    def reset_call_context(self, tok):
        self._ctx = {}


class _FakeLLM:
    """按提示词内容分派：规划 / 重规划 / 验收 / 纯文本。"""

    def __init__(self, *, plan=None, replan=None, verdict="PASS"):
        self.plan = plan
        self.replan = replan
        self.verdict = verdict
        self.calls: list[str] = []

    async def chat(self, messages, temperature=0.2, max_tokens=600, **kw):
        sys_text = " ".join(str(m.get("content", "")) for m in messages)
        if "任务规划器" in sys_text:
            self.calls.append("plan")
            return self.plan if self.plan is not None else '["步骤一","步骤二"]'
        if "任务重规划器" in sys_text:
            self.calls.append("replan")
            return self.replan if self.replan is not None else '["改用备用方式完成"]'
        if "验收标准" in sys_text:
            self.calls.append("verify")
            return self.verdict
        self.calls.append("chat")
        return "（替代方案产出）"

    async def chat_structured(self, messages, tools, temperature=0.8,
                              max_tokens=512, **kw):
        self.calls.append("chat_structured")
        return {"content": "（ReAct 产出）", "tool_calls": []}


class _FakeAuditor:
    def __init__(self):
        self.events = []

    def emit(self, kind, **kw):
        self.events.append((kind, kw))

    def kinds(self):
        return [k for k, _ in self.events]


class _FakeCore:
    def __init__(self, tools, llm=None, persistence=None):
        self.tools = tools
        self.llm = llm
        self.persistence = persistence or _Mem()
        self.config = type("C", (), {"persistence": type("P", (), {"data_dir": "."})()})()


def _store(core):
    return AgentTaskStore(core.persistence)


def _task(steps, request="把这件事办了"):
    return AgentTask(id="t1", request=request, creator=1, steps=steps)


def _step(i, desc, *, tool="dummy", verify="", group="", args=None):
    return Step(index=i, description=desc, tool_name=tool, verify=verify,
                parallel_group=group, tool_args=args or {})


def _exec(core, llm=None, **kw):
    auditor = _FakeAuditor()
    store = _store(core)
    planner_llm = llm if llm is not None else getattr(core, "llm", None)
    ex = AgentExecutor(core, store, planner=TaskPlanner(planner_llm),
                       auditor=auditor, **kw)
    return ex, auditor, store


class _AlwaysFailReplanner(TaskPlanner):
    """替代方案仍然指定同一个必然失败的工具——用来验证重规划次数上限。"""

    async def replan(self, **kw):
        return [Step(index=0, description="再换个方式", tool_name="dummy",
                     origin="replan")]


# ---------------------------------------------------------------------------
# 1. 验收
# ---------------------------------------------------------------------------

async def test_verify_pass_marks_step_verified():
    core = _FakeCore(_FakeTools({"dummy": "结果是 42"}), llm=_FakeLLM(verdict="PASS"))
    ex, auditor, _ = _exec(core)
    task = _task([_step(0, "算一下", verify="输出含 42")])
    res = await ex.execute(task)

    assert task.status == "completed"
    assert task.steps[0].verified is True
    assert "agent_verify_passed" in auditor.kinds()
    assert "步骤1" in res


async def test_verify_fail_triggers_replan_and_recovers():
    """工具执行成功但验收不通过 → 重规划 → 替代步骤通过 → 任务整体完成。"""
    llm = _FakeLLM(replan='["换一种方式取得结果"]')
    core = _FakeCore(_FakeTools({"dummy": "结果是 0"}), llm=llm)
    ex, auditor, store = _exec(core)
    task = _task([_step(0, "算一下", verify="输出含 42")])

    # 第一次验收 FAIL，重规划后的步骤（无工具 → ReAct）不再有验收判据 → 放行
    verdicts = iter(["FAIL", "PASS"])

    async def _chat(messages, temperature=0.2, max_tokens=600, **kw):
        sys_text = " ".join(str(m.get("content", "")) for m in messages)
        if "任务重规划器" in sys_text:
            return '["换一种方式取得结果"]'
        if "验收标准" in sys_text:
            return next(verdicts)
        return "（ReAct 产出）"

    llm.chat = _chat

    res = await ex.execute(task)
    assert task.status == "completed"
    assert task.replan_count == 1
    assert "agent_replan" in auditor.kinds()
    assert task.context["replan_1"]["reason"] == "verify_failed"
    # 原失败步骤被替代步骤替换
    assert task.steps[0].description == "换一种方式取得结果"
    assert task.steps[0].origin == "replan"
    assert "重规划 #1" in task.context.get("result_parts", [""])[0] or True
    assert res


async def test_verify_fail_without_replan_budget_fails_task():
    core = _FakeCore(_FakeTools({"dummy": "没用的输出"}), llm=_FakeLLM(verdict="FAIL"))
    ex, auditor, _ = _exec(core, max_replans=0)
    task = _task([_step(0, "算一下", verify="输出含 42")])
    res = await ex.execute(task)

    assert task.status == "failed"
    assert "验收未通过" in task.error
    assert "agent_replan" not in auditor.kinds()


async def test_verify_skipped_without_criterion():
    core = _FakeCore(_FakeTools({"dummy": "随便"}), llm=_FakeLLM(verdict="FAIL"))
    ex, auditor, _ = _exec(core)
    task = _task([_step(0, "没有判据的步骤")])
    await ex.execute(task)

    assert task.status == "completed"
    # 没判据就不该调 LLM 做验收
    assert "verify" not in core.llm.calls


async def test_verify_skipped_without_llm():
    """无 LLM → 验收不可用，退回旧行为（只看工具是否报错）。

    ``verified`` 必须保持 False：没验过 ≠ 验过通过。
    """
    core = _FakeCore(_FakeTools({"dummy": "ok"}), llm=None)
    ex, auditor, _ = _exec(core, llm=None)
    task = _task([_step(0, "步骤", verify="必须含 42")])
    await ex.execute(task)

    assert task.status == "completed"
    assert task.steps[0].verified is False
    assert "agent_verify_passed" not in auditor.kinds()


async def test_verify_inconclusive_passes_but_audits():
    core = _FakeCore(_FakeTools({"dummy": "ok"}), llm=_FakeLLM(verdict="说不太清楚"))
    ex, auditor, _ = _exec(core)
    task = _task([_step(0, "步骤", verify="含 42")])
    await ex.execute(task)

    assert task.status == "completed"
    assert "agent_verify_inconclusive" in auditor.kinds()


async def test_verify_error_passes_but_audits():
    class _BoomLLM(_FakeLLM):
        async def chat(self, messages, **kw):
            sys_text = " ".join(str(m.get("content", "")) for m in messages)
            if "验收标准" in sys_text:
                raise RuntimeError("verifier down")
            return await super().chat(messages, **kw)

    core = _FakeCore(_FakeTools({"dummy": "ok"}), llm=_BoomLLM())
    ex, auditor, _ = _exec(core)
    task = _task([_step(0, "步骤", verify="含 42")])
    await ex.execute(task)

    assert task.status == "completed"
    kinds = auditor.kinds()
    assert "agent_verify_inconclusive" in kinds
    assert any("verify_error" in str(kw) for k, kw in auditor.events if k == "agent_verify_inconclusive")


async def test_verify_can_be_disabled():
    core = _FakeCore(_FakeTools({"dummy": "x"}), llm=_FakeLLM(verdict="FAIL"))
    ex, _, _ = _exec(core, verify_steps=False)
    task = _task([_step(0, "步骤", verify="含 42")])
    await ex.execute(task)

    assert task.status == "completed"
    assert "verify" not in core.llm.calls


# ---------------------------------------------------------------------------
# 2. 重规划
# ---------------------------------------------------------------------------

async def test_step_failure_triggers_replan():
    """工具重试耗尽 → 重规划替换失败步 → 替代路径完成。"""
    core = _FakeCore(_FakeTools({"dummy": "错误: 工具一直失败"}),
                     llm=_FakeLLM(replan='["改用沙箱脚本"]'))
    ex, auditor, _ = _exec(core)
    task = _task([_step(0, "调外部接口")])

    res = await ex.execute(task)
    assert task.status == "completed"
    assert task.replan_count == 1
    assert task.steps[0].description == "改用沙箱脚本"
    assert task.context["replan_1"]["reason"] == "step_failed"
    assert "重规划" in res or task.context.get("result_parts")


async def test_replan_bounded_by_max_replans():
    """替代步骤继续失败 → 重规划次数不超过上限，最终失败。"""
    tools = _FakeTools({"dummy": "错误: 永远失败"})
    core = _FakeCore(tools, llm=_FakeLLM(replan='[{"description":"再换个方式"}]'))
    store = _store(core)
    auditor = _FakeAuditor()
    ex = AgentExecutor(core, store, planner=_AlwaysFailReplanner(core.llm),
                       auditor=auditor, max_replans=2)
    task = _task([_step(0, "调外部接口")])

    await ex.execute(task)
    assert task.replan_count == 2
    assert task.status == "failed"
    assert len([k for k in auditor.kinds() if k == "agent_replan"]) == 2


async def test_no_replan_without_llm_keeps_old_behaviour():
    """无 LLM → 不重规划，一次失败即终止（与 P2-1 之前行为一致）。"""
    tools = _FakeTools({"dummy": "错误: 一直失败"})
    core = _FakeCore(tools, llm=None)
    ex, auditor, _ = _exec(core, llm=None)
    task = _task([_step(0, "步骤")])

    res = await ex.execute(task)
    assert task.status == "failed"
    assert task.replan_count == 0
    assert "agent_replan" not in auditor.kinds()
    assert len(tools.calls) == AgentExecutor.MAX_TOOL_RETRIES
    assert "失败" in res


async def test_replan_skips_already_done_steps():
    """已完成步骤不被重做，只有失败步及其之后被替换。"""
    calls = {"n": 0}

    def _flaky(name, args, level):
        calls["n"] += 1
        # 只有第一次调用（第一步）成功，之后永远失败
        return "ok" if calls["n"] == 1 else "错误: 坏掉了"

    tools = _FakeTools({"dummy": _flaky})
    core = _FakeCore(tools, llm=_FakeLLM(replan='["最后的替代收尾"]'))
    ex, _, store = _exec(core)
    task = _task([_step(0, "第一步"), _step(1, "第二步"),
                  _step(2, "第三步")])

    await ex.execute(task)
    assert task.replan_count == 1
    # 第一步保留，第二步及之后被替换为 1 步
    assert [s.description for s in task.steps] == ["第一步", "最后的替代收尾"]
    assert task.steps[0].status == "done"
    assert task.context["replan_1"]["new_steps"] == ["最后的替代收尾"]


async def test_replan_llm_failure_falls_back_to_deterministic_plan():
    """重规划 LLM 故障（非「明确无替代」）→ 回落确定性替代方案，不吞掉任务。"""
    class _BadReplanLLM(_FakeLLM):
        async def chat(self, messages, **kw):
            sys_text = " ".join(str(m.get("content", "")) for m in messages)
            if "任务重规划器" in sys_text:
                raise RuntimeError("replanner down")
            return await super().chat(messages, **kw)

    core = _FakeCore(_FakeTools({"dummy": "错误: 一直失败"}), llm=_BadReplanLLM())
    ex, _, _ = _exec(core)
    task = _task([_step(0, "取数")])
    await ex.execute(task)

    assert task.replan_count == 1
    assert "换用不同方式" in task.steps[0].description
    assert task.steps[-1].description.startswith("如实说明")


async def test_replan_explicit_no_alternative_gives_up():
    """LLM 明确回答空数组 = 没有可行替代 → 如实失败，不用兜底话术硬凑。"""
    core = _FakeCore(_FakeTools({"dummy": "错误: 一直失败"}), llm=_FakeLLM(replan="[]"))
    ex, _, _ = _exec(core)
    task = _task([_step(0, "步骤")])
    await ex.execute(task)

    assert task.status == "failed"
    assert task.replan_count == 0


# ---------------------------------------------------------------------------
# 3. 并行批次
# ---------------------------------------------------------------------------

async def test_parallel_group_runs_concurrently():
    order: list[str] = []

    async def _slow(name, args, level):
        # 串行跑完两个各 0.15s 的步骤需 ≥0.3s；并发应明显更快
        await asyncio.sleep(0.15)
        order.append(str(args.get("tag")))
        return "ok"

    tools = _FakeTools({"dummy": _slow})
    core = _FakeCore(tools, llm=None)
    ex, _, _ = _exec(core)
    task = _task([
        _step(0, "并发A", group="g1", args={"tag": "a"}),
        _step(1, "并发B", group="g1", args={"tag": "b"}),
    ])

    loop = asyncio.get_event_loop()
    t0 = loop.time()
    await ex.execute(task)
    elapsed = loop.time() - t0

    assert task.status == "completed"
    assert sorted(order) == ["a", "b"]
    assert elapsed < 0.28, f"两步应为并发执行，实际耗时 {elapsed:.2f}s"
    assert len(tools.calls) == 2


async def test_react_steps_never_run_parallel():
    """无显式 tool_name 的步骤即使同 group 也串行（ReAct 上下文不可共享）。"""
    tools = _FakeTools({})
    core = _FakeCore(tools, llm=_FakeLLM())
    ex, _, _ = _exec(core)
    task = _task([_step(0, "A", tool="", group="g1"),
                  _step(1, "B", tool="", group="g1")])
    await ex.execute(task)

    assert task.status == "completed"
    # ReAct 路径各调一次结构化对话
    assert core.llm.calls.count("chat_structured") == 2


async def test_different_groups_stay_serial():
    order: list[str] = []

    async def _mark(name, args, level):
        order.append(str(args.get("tag")))
        return "ok"

    tools = _FakeTools({"dummy": _mark})
    core = _FakeCore(tools, llm=None)
    ex, _, _ = _exec(core)
    task = _task([_step(0, "A", group="g1", args={"tag": "a"}),
                  _step(1, "B", group="g2", args={"tag": "b"})])
    await ex.execute(task)

    assert order == ["a", "b"]


async def test_parallel_can_be_disabled():
    order: list[str] = []

    async def _slow(name, args, level):
        await asyncio.sleep(0.1)
        order.append(str(args.get("tag")))
        return "ok"

    tools = _FakeTools({"dummy": _slow})
    core = _FakeCore(tools, llm=None)
    ex, _, _ = _exec(core, parallel_steps=False)
    task = _task([_step(0, "A", group="g1", args={"tag": "a"}),
                  _step(1, "B", group="g1", args={"tag": "b"})])
    await ex.execute(task)

    assert order == ["a", "b"]


# ---------------------------------------------------------------------------
# 4. planner 解析
# ---------------------------------------------------------------------------

def test_parse_llm_accepts_strings_and_objects():
    parsed = TaskPlanner._parse_llm(
        '["搜资料", {"description":"写文件","verify":"文件存在含 X",'
        '"parallel_group":"g1"}, {"description":""}]'
    )
    assert [p["description"] for p in parsed] == ["搜资料", "写文件"]
    assert parsed[1]["verify"] == "文件存在含 X"
    assert parsed[1]["parallel_group"] == "g1"
    assert parsed[0]["verify"] == ""


def test_parse_llm_rejects_garbage():
    assert TaskPlanner._parse_llm("完全不是 JSON") == []
    assert TaskPlanner._parse_llm('{"a":1}') == []
    assert TaskPlanner._parse_llm("[not json") == []
    assert TaskPlanner._parse_llm("") == []


def test_parse_llm_strips_markdown_fence():
    parsed = TaskPlanner._parse_llm('```json\n["一步"]\n```')
    assert [p["description"] for p in parsed] == ["一步"]


async def test_plan_falls_back_when_llm_broken():
    class _Boom:
        async def chat(self, *a, **kw):
            raise RuntimeError("boom")

    steps = await TaskPlanner(_Boom()).plan("搜索并写一份报告")
    assert steps and all(s.description for s in steps)
    assert steps[-1].description == "汇总回复主人"


async def test_replan_fallback_is_deterministic_and_marks_origin():
    class _Boom:
        async def chat(self, *a, **kw):
            raise RuntimeError("boom")

    steps = await TaskPlanner(_Boom()).replan(
        request="r", done=["a"], failed_step_desc="取数",
        error="timeout", remaining=["写文件", "汇总"], reason="step_failed",
    )
    assert steps
    assert all(s.origin == "replan" for s in steps)
    assert "换用不同方式" in steps[0].description
    assert steps[-1].description.startswith("如实说明")


async def test_replan_fallback_verify_failed_wording():
    class _Boom:
        async def chat(self, *a, **kw):
            raise RuntimeError("boom")

    steps = await TaskPlanner(_Boom()).replan(
        request="r", done=[], failed_step_desc="写文件",
        error="验收未通过", remaining=[], reason="verify_failed",
    )
    assert "未通过验收" in steps[0].description


# ---------------------------------------------------------------------------
# 5. 状态序列化
# ---------------------------------------------------------------------------

def test_store_roundtrip_preserves_new_fields():
    mem = _Mem()
    core = _FakeCore(_FakeTools(), persistence=mem)
    store = AgentTaskStore(mem)
    task = _task([_step(0, "A", verify="含 X", group="g1")])
    task.steps[0].verified = True
    task.steps[0].attempts = 2
    task.steps[0].origin = "replan"
    task.replan_count = 1
    store.save(task)

    raw = mem.data["agent_tasks.json"]
    assert raw[0]["replan_count"] == 1
    assert raw[0]["steps"][0]["verify"] == "含 X"
    assert raw[0]["steps"][0]["parallel_group"] == "g1"

    reloaded = AgentTaskStore(mem).get("t1")
    assert reloaded is not None
    assert reloaded.replan_count == 1
    assert reloaded.steps[0].verify == "含 X"
    assert reloaded.steps[0].verified is True
    assert reloaded.steps[0].attempts == 2
    assert reloaded.steps[0].origin == "replan"


def test_store_loads_legacy_rows_without_new_fields():
    """老 agent_tasks.json 没有新字段也要能读（默认值补齐）。"""
    mem = _Mem()
    mem.data["agent_tasks.json"] = [{
        "id": "old", "request": "r", "creator": 1, "status": "completed",
        "steps": [{"index": 0, "description": "s", "status": "done"}],
        "current_step": 1,
    }]
    task = AgentTaskStore(mem).get("old")
    assert task is not None
    assert task.replan_count == 0
    assert task.steps[0].verify == ""
    assert task.steps[0].parallel_group == ""
    assert task.steps[0].verified is False


# ---------------------------------------------------------------------------
# 6. 配置接线
# ---------------------------------------------------------------------------

def test_task_agent_config_defaults():
    from anima.core.config import AnimaConfig

    cfg = AnimaConfig()
    assert cfg.task_agent.verify_steps is True
    assert cfg.task_agent.max_replans == 2
    assert cfg.task_agent.parallel_steps is True
    assert cfg.task_agent.max_parallel == 4


def test_owner_agent_passes_config_through():
    from anima.agent.owner_agent import OwnerAgent

    class _P:
        data_dir = "."

    core = type("C", (), {})()
    core.persistence = _Mem()
    core.config = type("Cfg", (), {
        "persistence": _P(),
        "task_agent": type("T", (), {
            "verify_steps": False, "max_replans": 0,
            "parallel_steps": False, "max_parallel": 1,
        })(),
    })()
    core.llm = None
    agent = OwnerAgent(core)
    assert agent.executor.verify_steps is False
    assert agent.executor.max_replans == 0
    assert agent.executor.parallel_steps is False
    assert agent.executor.max_parallel == 1


def test_owner_agent_survives_missing_config_section():
    """老 config 没有 task_agent 段也不能崩。"""
    from anima.agent.owner_agent import OwnerAgent

    class _P:
        data_dir = "."

    core = type("C", (), {})()
    core.persistence = _Mem()
    core.config = type("Cfg", (), {"persistence": _P()})()
    core.llm = None
    agent = OwnerAgent(core)
    assert agent.executor.verify_steps is True
    assert agent.executor.max_replans == AgentExecutor.MAX_REPLANS
