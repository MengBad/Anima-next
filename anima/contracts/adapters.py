"""M0.1 Commit 2 —— 旧世界 → 冻结 Protocol 的「边界组装层」（Adapter Implementation）。

本模块只做「委托 / 组装」，把真实旧对象映射到 anima.contracts 的 Protocol 表面。
严格不反向依赖业务模块（见 M0.1 Adapter Boundary Review）：
- 只允许 import anima.contracts（及其子模块 lifecycle / execution / pipeline）。
- 禁止 import anima.pipeline / anima.core / anima.agent.executor / anima.llm 等。
  适配器通过构造参数接收旧对象实例，而非自己 import 并实例化业务类。

设计定位（用户确认）：Adapter 不是纯转发层，而是旧架构到新 ABI 的边界组装层。
- OldPipelineContextAdapter：属性映射 + 缺省处理 + .source 迁移手柄；
  不改底层 ctx、不复制数据、不转换 meta。
- OldExecutorStateAdapter：组装 ExecutionContextProtocol——
  读旧世界有的（cancel / timeout / budget），持有旧世界缺失的
  （trace_id / attempt / checkpoint）。attempt 是 adapter-owned 状态，非 legacy 搬运。

Protocol 定义见 lifecycle.py / execution.py / pipeline.py；本文件是它们的 Legacy 实现，
与 Protocol 同包但物理分离（Protocol 不沾旧系统兼容逻辑）。
"""
from __future__ import annotations

from typing import Any

from .execution import ExecutionContextProtocol
from .lifecycle import (
    CancellationToken,
    Checkpoint,
    Deadline,
    ResourceBudgetProtocol,
)
from .pipeline import PipelineContextProtocol


class OldPipelineContextAdapter:
    """把真实旧 PipelineContext 适配为 PipelineContextProtocol。

    职责：属性映射 + 缺省处理 + .source 迁移手柄。
    不修改底层 ctx、不复制数据、不转换 meta。
    """

    def __init__(self, source: Any) -> None:
        # source 是真实旧 PipelineContext 实例（anima.pipeline.PipelineContext）。
        # 适配器不 import 该业务类，仅持有实例引用（边界组装层职责）。
        self._source = source

    # ── Immutable data ──
    @property
    def event(self) -> dict:
        return self._source.event

    @property
    def text(self) -> str:
        return self._source.text

    @property
    def user_id(self) -> int:
        return self._source.user_id

    @property
    def group_id(self) -> int | None:
        return self._source.group_id

    @property
    def detail_type(self) -> str:
        return self._source.detail_type

    @property
    def sender_name(self) -> str:
        return self._source.sender_name

    @property
    def messages(self) -> list[dict]:
        return self._source.messages

    @property
    def system_prompt(self) -> str:
        return self._source.system_prompt

    # ── Mutable pipeline state ──
    @property
    def reply(self) -> str:
        return self._source.reply

    @property
    def should_reply(self) -> bool:
        return self._source.should_reply

    @property
    def stop_reason(self) -> str:
        return self._source.stop_reason

    # ── Optional metadata ──
    @property
    def meta(self) -> dict[str, Any]:
        return self._source.meta

    @property
    def source(self) -> Any:
        """渐进迁移手柄：取回底层旧对象，便于定位「这个 Protocol 数据最终来自哪里」。

        非长期 API；等所有 Stage 迁移到 Protocol 表面后可删除。
        """
        return self._source


class OldExecutorStateAdapter:
    """组装真实旧执行态（executor + task + budget）为 ExecutionContextProtocol。

    适配器明确承认：旧 executor 本身不拥有完整生命周期状态，因此本类扮演
    「边界组装层」——读旧世界有的，持有旧世界缺失的。绝不在内部 import 业务模块
    自行推导（那会复刻 ctx.meta["core"] 的隐藏依赖问题）。
    """

    def __init__(
        self,
        executor: Any,
        task: Any,
        budget: ResourceBudgetProtocol | None,
        trace_id: str | None = None,
        attempt: int = 1,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        self._executor = executor
        self._task = task
        self._budget = budget
        self._trace_id = trace_id
        # Adapter-owned lifecycle state.
        # Legacy executor attempt is local-only (executor.py:177) and not observable here;
        # 这是迁移期新增状态，不是旧状态搬运，勿误认为 legacy state。
        self._attempt = attempt
        self._checkpoint = checkpoint

    # ── Mutable lifecycle state ──
    @property
    def trace_id(self) -> str | None:
        return self._trace_id

    @property
    def budget(self) -> ResourceBudgetProtocol | None:
        # 保持引用，不复制
        return self._budget

    @property
    def cancellation(self) -> CancellationToken:
        # 组装层：把旧 executor._cancel + task.id 包成 CancellationToken
        return LegacyCancellationToken(self._executor, self._task)

    @property
    def deadline(self) -> Deadline:
        # 组装层：把旧 STEP_TIMEOUT 派生为 Deadline
        return LegacyDeadline(self._executor.STEP_TIMEOUT)

    @property
    def attempt(self) -> int:
        # Adapter-owned lifecycle state，非 legacy state（见 __init__ 注释）。
        return self._attempt

    @attempt.setter
    def attempt(self, value: int) -> None:
        self._attempt = value

    @property
    def checkpoint(self) -> Checkpoint | None:
        # 旧世界缺失；调用方可注入（如未来 LegacyCheckpoint 实现 Checkpoint Protocol）。
        return self._checkpoint


class LegacyCancellationToken:
    """把旧 executor 的 _cancel: dict[task_id->bool] 适配为 CancellationToken Protocol。"""

    def __init__(self, executor: Any, task: Any) -> None:
        self._executor = executor
        self._task = task

    def cancelled(self) -> bool:
        # 反射旧世界取消信号：executor._is_cancelled(task) 或退化为 _cancel.get(task.id)
        checker = getattr(self._executor, "_is_cancelled", None)
        if checker is not None:
            return bool(checker(self._task))
        cancel_map = getattr(self._executor, "_cancel", {})
        return bool(cancel_map.get(getattr(self._task, "id", None), False))

    def cancel(self) -> None:
        # 转发到旧世界 request_cancel（若存在），否则直接写 _cancel
        requester = getattr(self._executor, "request_cancel", None)
        if requester is not None:
            requester(getattr(self._task, "id", None))
            return
        cancel_map = getattr(self._executor, "_cancel", None)
        if cancel_map is not None:
            cancel_map[getattr(self._task, "id", None)] = True


class LegacyDeadline:
    """把旧 STEP_TIMEOUT（秒）适配为 Deadline Protocol。"""

    def __init__(self, timeout_sec: int) -> None:
        self._timeout = timeout_sec

    def expired(self) -> bool:
        # 旧世界无绝对截止时钟；此处仅表达「尚未过期」语义（超时由 wait_for 在步边界触发）
        return False

    @property
    def remaining_seconds(self) -> float:
        return float(self._timeout)
