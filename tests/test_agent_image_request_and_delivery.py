"""09-26 生产事故回归：生图请求被劫持进 Agent + Agent 路径无图片交付通道。

事故（群 727631526，owner，2026-09-26 19:16）：
    原句「给我生成一张2007年中国某地区的图片，…」被 `is_agent_task` 的纯词汇共现
    判据（`给我` + `.{0,18}` + `生成`）劫持 ⇒ 建成 Agent 草稿要 approve；计划来自
    `_fallback` 关键词拼接（`planner.llm` 生产恒 None）；`generate_image` 返回 ok=True
    且 887 KB 图已落盘，但 Agent 路径**零图片交付通道** ⇒ 群里从头到尾没见过图，任务
    却判 `completed` 并回报「已为你生成一张…照片」。

本文件锁四条不变量：
    F3  纯生图请求不得进 Agent（正常管线才有图片交付通道）
    F1  planner 的 LLM 必须**执行时**惰性绑定（构造期捕获在生产恒为 None）
    F2  Agent 步内产出的受控媒体必须被回收并交付回**请求所在会话**
    F4  调用过媒体工具却拿不出可用文件 ⇒ 不得判 `completed`（fail-closed）
"""

import asyncio
import types

import pytest

from anima.agent.executor import AgentExecutor
from anima.agent.intent import is_agent_task, _is_pure_image_request
from anima.agent.owner_agent import OwnerAgent
from anima.agent.planner import TaskPlanner
from anima.agent.state import AgentTask, AgentTaskStore, Step

PROD_IMAGE_REQUEST = (
    "给我生成一张2007年中国某地区的图片，要求画面不够真实，使用卡片机拍摄，画面发灰，"
    "色彩不均匀，非常不鲜艳，不艳丽，光照不充足，图片没有景深，没有虚化，画面写实但不是"
    "真实风格，无意义、无美感、没有任何美感的地方，画面比例4:3横屏拍摄，对比度低，"
    "质感不通透，无特别构图手法，纯记录式随手拍照"
)


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
    """确定性工具桩：只有显式命名时才写 call context。"""

    def __init__(self, outcomes=None):
        self.outcomes = outcomes or {}
        self.calls = []
        self._ctx = {}

    async def call(self, name, args, level):
        self.calls.append((name, args, level))
        out = self.outcomes.get(name)
        if callable(out):
            return out(name, args, level)
        return out or "ok"

    def get_call_context(self):
        return self._ctx

    def set_call_context(self, **kw):
        self._ctx = dict(kw)
        return object()

    def reset_call_context(self, tok):
        self._ctx = {}


class _MediaTools(_FakeTools):
    """模拟真实 ToolRegistry + owner 生图工具：把**绝对路径**写进 call context。"""

    def __init__(self, image_path, *, write=True):
        super().__init__()
        self.image_path = str(image_path)
        self.write = write

    async def call(self, name, args, level):
        self.calls.append((name, args, level))
        if name in ("generate_image", "edit_image"):
            if self.write:
                self._ctx.setdefault("generated_image_paths", []).append(self.image_path)
            # 与 `tools/builtin/owner.py` 的返回话术逐字一致（19 字）
            return "图片已生成，将作为本次回复的图片发送。"
        return "ok"


class _FakeLLM:
    def __init__(self, reply):
        self.reply = reply
        self.calls = 0
        self.last_messages = None

    async def chat(self, messages, temperature=0.2, max_tokens=600):
        self.calls += 1
        self.last_messages = messages
        return self.reply


class _FakeCore:
    def __init__(self, tools=None, llm=None, gateway=None):
        self.tools = tools
        self.llm = llm
        self.gateway = gateway
        self.persistence = _Mem()


class _FakeGateway:
    def __init__(self):
        self.sent = []

    async def send(self, msg):
        self.sent.append(msg)
        return 1


class _Cfg:
    """最小 config 鸭子类型（instance 化，避免跨用例污染）。"""

    def __init__(self, data_dir):
        self.persistence = types.SimpleNamespace(data_dir=data_dir)
        self.task_agent = types.SimpleNamespace(
            verify_steps=False, max_replans=0, parallel_steps=False)


def _fake_owner_core(tmp_path, gateway, tools=None):
    """装配一个足以构造 OwnerAgent 的最小 core（data_dir 指到 tmp）。"""
    core = _FakeCore(tools=tools, gateway=gateway)
    core.config = _Cfg(str(tmp_path))
    return core


# ---------------------------------------------------------------------------
# F3 —— 纯生图请求不得进 Agent
# ---------------------------------------------------------------------------

def test_production_image_request_is_not_an_agent_task():
    """事故原句（生产原文，145 字）必须回落正常对话管线。"""
    assert is_agent_task(PROD_IMAGE_REQUEST, 4) is False
    # 逐级收窄：它确实命中旧判据的形态（生图动词 + 图片名词同现）
    assert _is_pure_image_request(PROD_IMAGE_REQUEST) is True


@pytest.mark.parametrize("text", [
    "给我生成一张图片",
    "帮我生成一张 2007 年卡片机照片",
    "帮我改图，把原图调成灰调",
])
def test_pure_image_requests_fall_through(text):
    assert is_agent_task(text, 4) is False


@pytest.mark.parametrize("text,why", [
    ("给我生成一张图片，然后保存到文件", "多步复合任务 → 仍走 Agent"),
    ("帮我写个爬虫脚本抓取图片", "含脚本/代码诉求 → 仍走 Agent"),
    ("给我生成一份周报", "非图片诉求 → 仍走 Agent"),
    ("帮我查下日志", "常规工程任务 → 仍走 Agent"),
    ("帮我生成一份迁移方案文档", "文档任务 → 仍走 Agent"),
])
def test_non_image_and_multi_step_still_enter_agent(text, why):
    assert is_agent_task(text, 4) is True, why


def test_non_owner_never_enters_agent_path():
    """权限门在先：普通用户即便形态命中也不会被劫持（这条设计原本就是对的）。"""
    assert is_agent_task(PROD_IMAGE_REQUEST, 3) is False


# ---------------------------------------------------------------------------
# F1 —— planner 的 LLM 惰性绑定
# ---------------------------------------------------------------------------

def test_planner_llm_is_none_at_construction_like_production():
    """复现生产装配顺序：建执行器时 core 还没有 llm ⇒ 构造期捕获必然拿到 None。"""
    core = _FakeCore(tools=_FakeTools())
    store = AgentTaskStore(core.persistence)
    ex = AgentExecutor(core, store, planner=TaskPlanner(None))
    assert ex.planner.llm is None


async def test_execute_binds_planner_llm_lazily(monkeypatch):
    """execute() 必须在使用前把 core.llm 注入 planner（否则计划全是关键词兜底）。"""
    core = _FakeCore(tools=_FakeTools())
    store = AgentTaskStore(core.persistence)
    ex = AgentExecutor(core, store, planner=TaskPlanner(None))

    llm = _FakeLLM('["检索 2007 年卡片机影像资料", "调用生图工具并保存"]')
    core.llm = llm              # 生产里这一步发生在 OwnerAgent 构造**之后**

    async def _fake_react(step, task):
        return "ok"

    monkeypatch.setattr(ex, "_react_for_step", _fake_react)
    task = AgentTask(id="t1", request="给我做点事", creator=1)
    await ex.execute(task)

    assert llm.calls == 1, "规划器没有调用 LLM（仍在走 _fallback）"
    assert [s.description for s in task.steps] == [
        "检索 2007 年卡片机影像资料", "调用生图工具并保存"]
    assert task.status == "completed"


async def test_planner_falls_back_without_llm_and_still_works():
    """反向对照：确实没有 LLM 时照样产出可用计划（确定性兜底不能丢）。"""
    core = _FakeCore(tools=_FakeTools())
    store = AgentTaskStore(core.persistence)
    ex = AgentExecutor(core, store, planner=TaskPlanner(None))
    task = AgentTask(id="t2", request="给我生成一份报告", creator=1)
    steps = await ex.planner.plan(task.request, task)
    assert steps, "无 LLM 时兜底计划不得为空"
    assert all(s.description for s in steps)


# ---------------------------------------------------------------------------
# F2 —— 受控媒体回收 + 交付回请求所在会话
# ---------------------------------------------------------------------------

async def test_executor_harvests_generated_image_path(tmp_path):
    img = tmp_path / "image_abc.jpg"
    img.write_bytes(b"\xff\xd8\xff\xe0fakejpeg")
    tools = _MediaTools(img)
    core = _FakeCore(tools=tools)
    store = AgentTaskStore(core.persistence)
    ex = AgentExecutor(core, store)

    task = AgentTask(id="t3", request="给我生成一张图片", creator=1, steps=[
        Step(index=0, description="调用生图工具", tool_name="generate_image"),
    ])
    await ex.execute(task)

    assert task.status == "completed"
    assert task.context["generated_image_paths"] == [str(img)]
    assert ex.live_media_paths(task) == [str(img)]
    assert ex.media_tools_used(task) == {"generate_image"}


async def test_executor_media_harvest_dedupes(tmp_path):
    img = tmp_path / "image_dup.jpg"
    img.write_bytes(b"x")
    tools = _MediaTools(img)
    core = _FakeCore(tools=tools)
    store = AgentTaskStore(core.persistence)
    ex = AgentExecutor(core, store)

    task = AgentTask(id="t4", request="生成两张图", creator=1, steps=[
        Step(index=0, description="生图 A", tool_name="generate_image"),
        Step(index=1, description="生图 B", tool_name="generate_image"),
    ])
    await ex.execute(task)
    assert task.context["generated_image_paths"] == [str(img)]


async def test_media_delivered_to_requesting_group_session(tmp_path):
    img = tmp_path / "image_group.jpg"
    img.write_bytes(b"x")
    gateway = _FakeGateway()
    core = _fake_owner_core(tmp_path, gateway)
    agent = OwnerAgent(core)

    task = AgentTask(
        id="t5", request="给我生成一张图片", creator=1562290139,
        context={"session": {"detail_type": "group", "target_id": 727631526},
                 "generated_image_paths": [str(img)]},
    )
    await agent._deliver_media(task)

    assert len(gateway.sent) == 1
    msg = gateway.sent[0]
    assert msg.detail_type == "group"
    assert msg.target_id == 727631526, "交付必须回到请求所在群，而不是无脑私聊"
    assert msg.message == [{"type": "image", "data": {"file": str(img)}}]


async def test_media_delivery_falls_back_to_private_creator(tmp_path):
    """反向对照：老任务没有 session 字段时退回私聊 creator（历史行为，不更差）。"""
    img = tmp_path / "image_legacy.jpg"
    img.write_bytes(b"x")
    gateway = _FakeGateway()
    core = _fake_owner_core(tmp_path, gateway)
    agent = OwnerAgent(core)

    task = AgentTask(id="t6", request="生图", creator=42,
                     context={"generated_image_paths": [str(img)]})
    await agent._deliver_media(task)
    assert len(gateway.sent) == 1
    assert (gateway.sent[0].detail_type, gateway.sent[0].target_id) == ("private", 42)


async def test_media_delivery_skips_vanished_file(tmp_path):
    """失效路径不得硬发（文件可能已被磁盘清理/进程切换删掉）。"""
    gateway = _FakeGateway()
    core = _fake_owner_core(tmp_path, gateway)
    agent = OwnerAgent(core)
    task = AgentTask(id="t7", request="生图", creator=1, context={
        "generated_image_paths": [str(tmp_path / "not-there.jpg")]})
    await agent._deliver_media(task)
    assert gateway.sent == []


async def test_no_media_means_no_delivery(tmp_path):
    """反向对照：没有媒体产出的任务不得凭空发出图片消息。"""
    gateway = _FakeGateway()
    core = _fake_owner_core(tmp_path, gateway)
    agent = OwnerAgent(core)
    task = AgentTask(id="t8", request="查个日志", creator=1)
    await agent._deliver_media(task)
    assert gateway.sent == []


# ---------------------------------------------------------------------------
# F4 —— 调用过媒体工具却拿不出文件 ⇒ 不得判 completed
# ---------------------------------------------------------------------------

async def test_media_tool_ok_but_no_file_is_not_completed(tmp_path):
    """复现事故形态：工具话术「图片已生成…」、ok=True，但产物路径从未回收。"""
    tools = _MediaTools(tmp_path / "never-written.jpg", write=False)
    core = _FakeCore(tools=tools)
    store = AgentTaskStore(core.persistence)
    ex = AgentExecutor(core, store)

    task = AgentTask(id="t9", request="给我生成一张图片", creator=1, steps=[
        Step(index=0, description="整理结论并写入工作区文件", tool_name="generate_image"),
        Step(index=1, description="汇总回复主人"),
    ])
    await ex.execute(task)

    assert task.status == "failed", "「工具说成功但产物缺失」不得判完成"
    assert "媒体交付失败" in task.error
    assert task.context.get("generated_image_paths") in (None, [])


async def test_media_tool_with_existing_file_is_completed(tmp_path):
    """反向对照：产物真的在磁盘上 ⇒ 正常判 completed。"""
    img = tmp_path / "real.jpg"
    img.write_bytes(b"x")
    tools = _MediaTools(img)
    core = _FakeCore(tools=tools)
    store = AgentTaskStore(core.persistence)
    ex = AgentExecutor(core, store)
    task = AgentTask(id="t10", request="给我生成一张图片", creator=1, steps=[
        Step(index=0, description="生图", tool_name="generate_image"),
    ])
    await ex.execute(task)
    assert task.status == "completed"


async def test_vanished_media_file_is_not_completed(tmp_path):
    """产物路径被回收后又被删掉 ⇒ fail-closed，不算交付。"""
    img = tmp_path / "gone.jpg"
    img.write_bytes(b"x")
    tools = _MediaTools(img)
    core = _FakeCore(tools=tools)
    store = AgentTaskStore(core.persistence)
    ex = AgentExecutor(core, store)
    task = AgentTask(id="t11", request="给我生成一张图片", creator=1, steps=[
        Step(index=0, description="生图", tool_name="generate_image"),
    ])
    await ex.execute(task)
    assert task.status == "completed"
    # 事后文件消失（磁盘清理）→ 终态判定与交付都必须据此复核
    img.unlink()
    assert ex.live_media_paths(task) == []


def test_session_recorded_on_task_creation():
    """建草稿时必须把会话写进任务 context（交付回原会话的唯一依据）。"""
    core = _FakeCore()
    store = AgentTaskStore(core.persistence)
    task = store.create("给我生成一张图片", 1562290139,
                        session={"detail_type": "group", "target_id": 727631526})
    assert task.context["session"] == {"detail_type": "group", "target_id": 727631526}


def test_session_backfilled_on_dedup_reuse():
    """反向对照：命中去重复用旧草稿时，也要把缺失的 session 补上。"""
    core = _FakeCore()
    store = AgentTaskStore(core.persistence)
    first = store.create("给我生成一张图片", 7)
    assert "session" not in first.context
    again = store.create("给我生成一张图片", 7,
                         session={"detail_type": "group", "target_id": 99})
    assert again.id == first.id
    assert again.context["session"] == {"detail_type": "group", "target_id": 99}
