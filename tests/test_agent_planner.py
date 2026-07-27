"""Phase 3 测试：意图识别、任务状态持久化、规划器。"""

import time

from anima.agent.intent import is_agent_task
from anima.agent.planner import TaskPlanner
from anima.agent.state import AgentTask, AgentTaskStore, Step
from anima.tools import LEVEL_OWNER, LEVEL_USER


# ---------------------------------------------------------------------------
# 意图识别
# ---------------------------------------------------------------------------

def test_is_agent_task_owner_match():
    assert is_agent_task("帮我搜索一下2026奥运会举办地", LEVEL_OWNER) is True
    assert is_agent_task("请帮我写个脚本统计文件", LEVEL_OWNER) is True


def test_is_agent_task_rejects_normal_user():
    # 普通用户即使文本命中触发词，也不进入 Agent 路径
    assert is_agent_task("帮我搜索一下2026奥运会举办地", LEVEL_USER) is False
    assert is_agent_task("帮我搜索一下2026奥运会举办地", 3) is False


def test_is_agent_task_rejects_casual():
    assert is_agent_task("今天天气真好", LEVEL_OWNER) is False


# ---------------------------------------------------------------------------
# 状态持久化与重启恢复
# ---------------------------------------------------------------------------

class _Mem:
    def __init__(self): self.data = {}
    def read_json(self, name, default=None): return self.data.get(name, default)
    def write_json(self, name, value): self.data[name] = value


def test_store_create_and_persist():
    p = _Mem()
    s = AgentTaskStore(p)
    t = s.create("帮我做X", 12345)
    assert t.id and t.status == "planning"
    # 重新加载应读回
    s2 = AgentTaskStore(p)
    assert s2.get(t.id) is not None
    assert s2.get(t.id).request == "帮我做X"


def test_store_restart_recovery_marks_executing_failed():
    p = _Mem()
    s = AgentTaskStore(p)
    t = s.create("做X", 1)
    t.status = "executing"
    t.steps = [Step(0, "a"), Step(1, "b")]
    s.save(t)
    # 模拟重启：新加载
    s2 = AgentTaskStore(p)
    loaded = s2.get(t.id)
    assert loaded.status == "failed"  # 崩溃中任务标记为 failed，避免幽灵执行


def test_store_prune_keeps_recent():
    p = _Mem()
    s = AgentTaskStore(p, max_kept=3)
    for i in range(5):
        t = s.create(f"req{i}", 1)
        t.updated_at = time.time() + i
        s.save(t)
    assert len(s.items) <= 3


# ---------------------------------------------------------------------------
# 规划器
# ---------------------------------------------------------------------------

class _FakeLLM:
    def __init__(self, returns): self.returns = returns
    async def chat(self, messages, temperature=0.2, max_tokens=600):
        return self.returns


async def test_planner_uses_llm_when_available():
    llm = _FakeLLM('["步骤一：搜索","步骤二：写入文件"]')
    planner = TaskPlanner(llm)
    steps = await planner.plan("帮我搜索并写入", None)
    assert [s.description for s in steps] == ["步骤一：搜索", "步骤二：写入文件"]


async def test_planner_fallback_on_bad_llm():
    llm = _FakeLLM("这不是json")
    planner = TaskPlanner(llm)
    steps = await planner.plan("帮我搜索资料", None)
    assert len(steps) >= 2
    assert any("搜索" in s.description or "资料" in s.description for s in steps)


async def test_planner_fallback_keywords():
    planner = TaskPlanner(None)
    steps = await planner.plan("请帮我下载并生成报告", None)
    descs = [s.description for s in steps]
    assert any("下载" in d or "检索" in d for d in descs)
    assert any("报告" in d or "文件" in d for d in descs)
