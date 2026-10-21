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


def test_store_restart_recovery_marks_interrupted_not_failed():
    """🔴 2026-10-21 语义变更：重启打断 → `interrupted`，不再判 `failed`。

    旧行为（`failed`）的两个问题：
    1. **它不是任务失败** —— 任务本身没问题，是被重启打断的；
    2. **静默无通知** —— 用户只看到「没收到结果」，不知道是被打断。

    新行为保留旧测试关心的那件事（**避免幽灵执行**）：
    `interrupted` 不是 `executing`，没有任何执行器会认领它；
    同时保留 progress（current_step / 已完成的 step.result）以便重跑。
    """
    p = _Mem()
    s = AgentTaskStore(p)
    t = s.create("做X", 1)
    t.status = "executing"
    t.steps = [Step(0, "a"), Step(1, "b")]
    t.steps[0].status = "done"
    t.steps[0].result = "第一步的产物"
    t.current_step = 1
    s.save(t)

    s2 = AgentTaskStore(p)          # 模拟重启
    loaded = s2.get(t.id)

    assert loaded.status == "interrupted", "重启打断应标 interrupted 而非 failed"
    assert loaded.status != "failed", "不是任务本身失败，不该判 failed"
    assert loaded.status != "executing", "绝不能留在 executing（否则幽灵执行）"

    # 进度必须保留，否则「可重跑」是空话
    assert loaded.current_step == 1, "current_step 必须保留"
    assert loaded.steps[0].status == "done"
    assert loaded.steps[0].result == "第一步的产物", "已完成步骤的产物不得丢失"

    # 必须登记，供启动后私聊告知 owner
    assert t.id in s2.interrupted_on_load, "应登记到 interrupted_on_load 供通知"


def test_interrupted_tasks_are_listed_for_notification_only_once():
    """再次加载（未再被打断）不应重复登记 —— 防每次重启都重报同一批。"""
    p = _Mem()
    s = AgentTaskStore(p)
    t = s.create("做Y", 1)
    t.status = "executing"
    s.save(t)

    s2 = AgentTaskStore(p)
    assert t.id in s2.interrupted_on_load
    assert s2.get(t.id).status == "interrupted"

    s3 = AgentTaskStore(p)          # 第二次加载：已是 interrupted，不该再登记
    assert s3.interrupted_on_load == [], "已标记过的任务不应重复通知"


def test_store_create_dedups_recent_planning():
    """5 分钟内同人同文本的 planning 草稿应复用，不重复创建（QQ 重发防抖）。"""
    p = _Mem()
    s = AgentTaskStore(p)
    t1 = s.create("现在就去 搭建mc服务器", 1562290139)
    t2 = s.create("现在就去 搭建mc服务器", 1562290139)
    assert t1.id == t2.id
    # 不同创建者不去重
    t3 = s.create("现在就去 搭建mc服务器", 42)
    assert t3.id != t1.id
    # 已离开 planning 的不去重
    t1.status = "completed"
    s.save(t1)
    t4 = s.create("现在就去 搭建mc服务器", 1562290139)
    assert t4.id != t1.id


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
