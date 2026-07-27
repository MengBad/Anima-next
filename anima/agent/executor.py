"""Phase 4 Agent 执行循环 —— 多步 Plan→Execute→Verify。

设计（与审计 B1 一致，复用现有 ReAct 与 ToolRegistry，不重复实现工具执行）：
- 每步若已指定 tool_name → 直接以主人权限调用 core.tools.call（带重试）。
- 每步若未指定 tool_name → 由 ReActLoop（permission_level=4）自行决定工具。
- 逐步持久化（AgentTaskStore.save），进程重启可从 current_step 续跑。
- 资源预算（磁盘/网络/时长/输出）由 guardrails.ResourceBudget 把关。
- 进度通过 notify 回调推送（QQ 私聊给主人 / WebUI）。
- 熔断：连续 3 个任务失败 → 暂停 Agent 能力 10 分钟。
- 紧急停止：request_cancel 置位后，循环在步边界安全退出。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Optional

from anima.agent.audit import AgentAuditor
from anima.agent.guardrails import ResourceBudget, ResourceExceeded
from anima.agent.planner import TaskPlanner
from anima.agent.state import AgentTask, Step
from anima.tools import LEVEL_OWNER, tool_result_ok

logger = logging.getLogger("anima.agent.executor")


class StepFailed(Exception):
    """单步失败（重试耗尽）。"""


class AgentExecutor:
    MAX_STEPS = 20
    MAX_TOOL_RETRIES = 3
    STEP_TIMEOUT = 180

    def __init__(self, core, store, planner: TaskPlanner | None = None,
                 auditor: AgentAuditor | None = None,
                 notify: Optional[Callable[[AgentTask, str], Any]] = None):
        self.core = core
        self.store = store
        self.planner = planner or TaskPlanner(getattr(core, "llm", None))
        self.auditor = auditor
        self.notify = notify
        self._cancel: dict[str, bool] = {}
        self._fail_streak = 0
        self._pause_until = 0.0

    # ------------------------------------------------------------------
    # 紧急停止
    # ------------------------------------------------------------------

    def request_cancel(self, task_id: str) -> None:
        self._cancel[task_id] = True

    def _is_cancelled(self, task: AgentTask) -> bool:
        if self._cancel.get(task.id):
            return True
        t = self.store.get(task.id)
        return t is not None and t.status == "cancelled"

    # ------------------------------------------------------------------
    # 进度通知
    # ------------------------------------------------------------------

    async def _notify(self, task: AgentTask, message: str) -> None:
        if self.auditor:
            self.auditor.emit("agent_progress", task_id=task.id,
                              step=task.current_step, message=message[:200])
        if self.notify:
            try:
                await self.notify(task, message)
            except Exception:
                logger.exception("Agent 进度通知失败（已隔离）")

    # ------------------------------------------------------------------
    # 主执行
    # ------------------------------------------------------------------

    async def execute(self, task: AgentTask) -> str:
        if time.time() < self._pause_until:
            msg = "Agent 能力已熔断暂停（连续失败过多），请 10 分钟后再试。"
            await self._notify(task, msg)
            return msg

        # 尚未规划则先规划
        if not task.steps:
            task.steps = await self.planner.plan(task.request, task)

        task.status = "executing"
        task.error = ""
        self.store.save(task)

        budget = ResourceBudget()
        total = len(task.steps)
        result_parts: list[str] = []
        steps = task.steps[task.current_step:][:self.MAX_STEPS]
        failed = False

        for step in steps:
            if self._is_cancelled(task):
                task.status = "cancelled"
                self.store.save(task)
                await self._notify(task, "[已取消] 主人终止了任务")
                return "任务已取消。"

            await self._notify(task, f"[{step.index + 1}/{total}] {step.description}…")
            step.status = "running"
            self.store.save(task)

            try:
                budget.check_duration()
                result = await asyncio.wait_for(
                    self._run_step(step, task, budget), timeout=self.STEP_TIMEOUT
                )
            except asyncio.TimeoutError:
                step.status = "failed"
                step.error = "步骤超时"
                task.status = "failed"
                task.error = f"步骤 {step.index + 1} 超时"
                failed = True
                self.store.save(task)
                break
            except ResourceExceeded as e:
                step.status = "failed"
                step.error = str(e)
                task.status = "failed"
                task.error = str(e)
                failed = True
                self.store.save(task)
                await self._notify(task, f"[资源超限] {e}")
                break
            except StepFailed as e:
                step.status = "failed"
                step.error = str(e)
                task.status = "failed"
                task.error = f"步骤 {step.index + 1} 失败: {e}"
                failed = True
                self.store.save(task)
                break

            step.result = result[:4000]
            step.status = "done"
            task.context[f"step_{step.index}_result"] = result[:2000]
            task.current_step = step.index + 1
            self.store.save(task)
            result_parts.append(f"步骤{step.index + 1}: {step.description} ✓")
            # 预留 replan 接口：复杂任务可在此让 LLM 调整后续步骤

        if not failed and task.status == "executing":
            task.status = "completed"
            task.error = ""
        self.store.save(task)

        # 熔断统计
        if task.status == "failed":
            self._fail_streak += 1
            if self._fail_streak >= 3:
                self._pause_until = time.time() + 600
                await self._notify(task, "⚠️ 连续 3 个 Agent 任务失败，能力暂停 10 分钟")
        else:
            self._fail_streak = 0

        if task.status == "completed":
            await self._notify(task, "[完成] 任务已全部执行")
            return "\n".join(result_parts) or "（无步骤产出）"
        await self._notify(task, f"[失败] {task.error or '未知错误'}")
        return f"任务失败：{task.error or '未知错误'}"

    # ------------------------------------------------------------------
    # 单步执行（带重试）
    # ------------------------------------------------------------------

    async def _run_step(self, step: Step, task: AgentTask, budget: ResourceBudget) -> str:
        last_err: str | None = None
        for attempt in range(1, self.MAX_TOOL_RETRIES + 1):
            try:
                if step.tool_name:
                    budget.consume_network_request()
                    result = await self.core.tools.call(
                        step.tool_name, step.tool_args or {}, LEVEL_OWNER
                    )
                else:
                    result = await self._react_for_step(step, task)
                budget.consume_output(len(str(result or "")) / 1024.0)
                if tool_result_ok(result):
                    return result
                last_err = (result or "")[:300]
            except ResourceExceeded:
                raise
            except Exception as e:  # noqa: BLE001
                last_err = f"{type(e).__name__}: {e}"[:300]

            if attempt < self.MAX_TOOL_RETRIES:
                await asyncio.sleep(min(2 ** attempt, 8))  # 指数退避

        step.error = last_err or "unknown"
        raise StepFailed(last_err)

    async def _react_for_step(self, step: Step, task: AgentTask) -> str:
        from anima.agent.react import ReActLoop

        llm = getattr(self.core, "llm", None)
        tools = getattr(self.core, "tools", None)
        if llm is None or tools is None:
            return "(无 LLM/工具，无法执行该步骤)"
        loop = ReActLoop(llm, tools, max_rounds=6, permission_level=LEVEL_OWNER)
        messages = [
            {"role": "system", "content": (
                "你是 Anima 的执行代理，只完成当前这一个小步骤，用工具取得结果即可，不要闲聊。"
            )},
            {"role": "user", "content": f"任务背景：{task.request}\n当前步骤：{step.description}"},
        ]
        # 主人分层沙箱：把权限等级注入调用上下文（防御式兼容测试桩）
        ctx_token = None
        if hasattr(tools, "get_call_context") and hasattr(tools, "set_call_context"):
            ctx = tools.get_call_context()
            ctx_token = tools.set_call_context(**{**ctx, "permission_level": LEVEL_OWNER})
        try:
            return await loop.run(messages, temperature=0.3, max_tokens=600)
        finally:
            if ctx_token is not None and hasattr(tools, "reset_call_context"):
                tools.reset_call_context(ctx_token)
