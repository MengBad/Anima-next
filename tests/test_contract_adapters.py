"""M0.1 Commit 2 —— 旧世界接入新协议的契约测试（TEST-FIRST / RED）。

本文件在适配器实现之前编写，用于定义 OldPipelineContextAdapter /
OldExecutorStateAdapter 必须实现的接口。在 anima.contracts.adapters 存在之前，
每个 test_* 会以 ImportError 失败（红色），这是 TDD 预期的信号。

纪律（来自用户 M0.1 审批）：
- 不改 Stage、不改 Pipeline.run、不移除 meta、不引入 Resolver。
- 适配器只做「委托 / 组装」，把真实旧对象映射到冻结的 Protocol 表面。

关键发现（来自 Protocol_Inventory + 真实代码读取）：
- 真实 PipelineContext 是 @dataclass，12 个 Protocol 字段全在 → 适配器直通委托。
- 真实 executor 的 attempt 是 _run_step 局部变量（executor.py:177，不落盘）；
  trace_id / checkpoint 在消息层缺失；cancellation 散在 _cancel: dict[task_id->bool]。
  → 适配器必须「组装」出 ExecutionContextProtocol：读旧世界有的，持有旧世界缺失的。
"""
from __future__ import annotations

from anima.pipeline import PipelineContext
from anima.contracts import CancellationToken, Deadline


# ── fixtures ──
def _make_real_ctx() -> PipelineContext:
    """构造一个真实的旧世界 PipelineContext（@dataclass），字段全部填充。"""
    return PipelineContext(
        event={"post_type": "message", "message_id": 1},
        text="你好哈基米",
        user_id=12345,
        group_id=999,
        detail_type="group",
        sender_name="阿布",
        reply="",
        should_reply=True,
        stop_reason="",
        messages=[{"role": "user", "content": "你好哈基米"}],
        system_prompt="你是哈基米",
        meta={"permission_level": 2, "chat_id": 999},
    )


class _FakeBudget:
    """最小 ResourceBudget 替身，仅验证适配器「持有引用」这一职责。"""

    def consume_output(self, mb: float) -> None:  # pragma: no cover
        pass


class _FakeTask:
    def __init__(self, tid: str, current_step: int, steps: list) -> None:
        self.id = tid
        self.current_step = current_step
        self.steps = steps


class _FakeExecutor:
    """复刻真实 AgentExecutor 与适配器相关的公有表面（executor.py）。"""

    STEP_TIMEOUT = 180

    def __init__(self) -> None:
        self._cancel: dict[str, bool] = {}

    def request_cancel(self, task_id: str) -> None:
        self._cancel[task_id] = True

    def _is_cancelled(self, task) -> bool:
        return bool(self._cancel.get(task.id))


# ── PipelineContext 适配器契约 ──
def test_pipeline_adapter_delegates_all_protocol_fields():
    from anima.contracts.adapters import OldPipelineContextAdapter

    ctx = _make_real_ctx()
    adapter = OldPipelineContextAdapter(ctx)

    # Immutable data
    assert adapter.event is ctx.event
    assert adapter.text == ctx.text
    assert adapter.user_id == ctx.user_id
    assert adapter.group_id == ctx.group_id
    assert adapter.detail_type == ctx.detail_type
    assert adapter.sender_name == ctx.sender_name
    assert adapter.messages is ctx.messages
    assert adapter.system_prompt == ctx.system_prompt
    # Mutable pipeline state
    assert adapter.reply == ctx.reply
    assert adapter.should_reply == ctx.should_reply
    assert adapter.stop_reason == ctx.stop_reason
    # Optional metadata
    assert adapter.meta is ctx.meta


def test_pipeline_adapter_exposes_source_for_gradual_migration():
    from anima.contracts.adapters import OldPipelineContextAdapter

    ctx = _make_real_ctx()
    adapter = OldPipelineContextAdapter(ctx)
    # 渐进迁移手柄：调用方必要时可取回底层旧对象
    assert adapter.source is ctx


def test_pipeline_adapter_satisfies_protocol_shape():
    from anima.contracts.adapters import OldPipelineContextAdapter

    ctx = _make_real_ctx()
    adapter = OldPipelineContextAdapter(ctx)
    # 数据 Protocol 不强制 runtime_checkable；用 duck-typing 校验协议字段存在且可读
    for field in (
        "event", "text", "user_id", "group_id", "detail_type",
        "sender_name", "messages", "system_prompt",
        "reply", "should_reply", "stop_reason", "meta",
    ):
        assert hasattr(adapter, field), f"adapter 缺少 Protocol 字段 {field}"


# ── ExecutionContext 适配器契约 ──
def test_exec_adapter_assembles_lifecycle_from_old_world():
    from anima.contracts.adapters import OldExecutorStateAdapter

    executor = _FakeExecutor()
    task = _FakeTask(tid="t1", current_step=0, steps=[{"name": "s1"}])
    budget = _FakeBudget()
    trace_id = "abc123"

    adapter = OldExecutorStateAdapter(
        executor, task, budget, trace_id=trace_id, attempt=1, checkpoint=None
    )

    # trace_id：旧世界缺失 → 适配器持有新状态
    assert adapter.trace_id == trace_id
    # budget：直通持有真实 ResourceBudget 引用
    assert adapter.budget is budget
    # attempt：新状态，1-indexed，可写（重试行为属执行态，非任务数据）
    assert adapter.attempt == 1
    adapter.attempt = 2
    assert adapter.attempt == 2
    # checkpoint：旧世界缺失 → 允许 None（协议允许 Checkpoint | None）
    assert adapter.checkpoint is None


def test_exec_adapter_cancellation_reflects_old_world():
    from anima.contracts.adapters import OldExecutorStateAdapter

    executor = _FakeExecutor()
    task = _FakeTask(tid="t1", current_step=0, steps=[{"name": "s1"}])
    adapter = OldExecutorStateAdapter(executor, task, _FakeBudget(), trace_id="x")

    assert isinstance(adapter.cancellation, CancellationToken)
    assert adapter.cancellation.cancelled() is False

    # 取消信号来自真实旧世界的 request_cancel（executor.py:54）
    executor.request_cancel(task.id)
    assert adapter.cancellation.cancelled() is True


def test_exec_adapter_deadline_derived_from_step_timeout():
    from anima.contracts.adapters import OldExecutorStateAdapter

    executor = _FakeExecutor()
    task = _FakeTask(tid="t1", current_step=0, steps=[{"name": "s1"}])
    adapter = OldExecutorStateAdapter(executor, task, _FakeBudget(), trace_id="x")

    assert isinstance(adapter.deadline, Deadline)
    assert adapter.deadline.expired() is False
    # deadline 至少反映 STEP_TIMEOUT 量级（executor.py:36）
    assert adapter.deadline.remaining_seconds >= executor.STEP_TIMEOUT - 1


# ── 架构验收：Adapter 不反向依赖业务模块（用户 M0.1 验收点 #5）──
def test_adapter_has_no_reverse_business_dependency():
    """Adapter 必须只依赖 anima.contracts，禁止 import 业务模块（pipeline/core/agent…）。

    否则 adapter 会变成新的耦合点，复刻 ctx.meta["core"] 的隐藏依赖问题。
    """
    import ast
    import inspect

    import anima.contracts.adapters as adapters_mod

    src = inspect.getfile(adapters_mod)
    with open(src, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read())

    # 允许：anima.contracts（含子模块）与相对导入（level>0，如 from .lifecycle import）
    # 禁止：anima.pipeline / anima.core / anima.agent / anima.llm / anima.handler … 等业务模块
    forbidden_prefixes = (
        "anima.pipeline",
        "anima.core",
        "anima.agent",
        "anima.llm",
        "anima.handler",
        "anima.channels",
        "anima.cognitive",
        "anima.tools",
        "anima.memory",
        "anima.cognition",
        "anima.guardrails",
    )
    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith("anima.") and not node.module.startswith("anima.contracts"):
                if any(node.module.startswith(p) for p in forbidden_prefixes):
                    violations.append(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("anima.") and not alias.name.startswith("anima.contracts"):
                    if any(alias.name.startswith(p) for p in forbidden_prefixes):
                        violations.append(alias.name)

    assert not violations, f"adapters.py 反向依赖了业务模块: {violations}"
