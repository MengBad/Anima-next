"""Phase 4 Agent 执行循环 —— 多步 Plan→Execute→Verify→Replan。

设计（与审计 B1 一致，复用现有 ReAct 与 ToolRegistry，不重复实现工具执行）：
- 每步若已指定 tool_name → 直接以主人权限调用 core.tools.call（带重试）。
- 每步若未指定 tool_name → 由 ReActLoop（permission_level=4）自行决定工具。
- P2-1 独立验收：步骤执行成功后，若该步有 ``verify`` 判据，由 LLM 判定成立与否；
  不成立按失败处理（而不是只看工具有没有抛错）。
- P2-1 重规划：步骤失败或验收不通过时，让 planner 产出替代步骤替换剩余计划，
  上限 MAX_REPLANS（默认 2）防止无界重试。
- P2-1 并行：planner 标记了同一 ``parallel_group`` 且都有显式 tool_name 的连续步骤
  可并发执行（同一权限域内，仍受统一资源预算约束）。
- 逐步持久化（AgentTaskStore.save），进程重启可从 current_step 续跑。
- 资源预算（磁盘/网络/时长/输出）由 guardrails.ResourceBudget 把关。
- 进度通过 notify 回调推送（QQ 私聊给主人 / WebUI）。
- 熔断：连续 3 个任务失败 → 暂停 Agent 能力 10 分钟。
- 紧急停止：request_cancel 置位后，循环在步边界安全退出。

设计取舍（fail-open 处均写审计，避免「验收器自己坏掉」变成任务杀手）：
- 验收与重规划都依赖 LLM；无 LLM 时二者自动跳过（确定性兜底的「换方式重做」
  步骤没有 LLM 也无法执行，只会把一次失败拖成多次失败）。
- 验收回答无法判定（既非 PASS 也非 FAIL）或验收超时 → 视为通过并写
  ``agent_verify_inconclusive`` 审计，不因判据模糊而毁掉一个已成功的任务。
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
    MAX_REPLANS = 2
    MAX_PARALLEL = 4
    VERIFY_TIMEOUT = 60
    VERIFY_OUTPUT_CHARS = 1200

    def __init__(self, core, store, planner: TaskPlanner | None = None,
                 auditor: AgentAuditor | None = None,
                 notify: Optional[Callable[[AgentTask, str], Any]] = None,
                 *, verify_steps: bool = True, max_replans: int | None = None,
                 parallel_steps: bool = True, max_parallel: int | None = None):
        self.core = core
        self.store = store
        self.planner = planner or TaskPlanner(getattr(core, "llm", None))
        self.auditor = auditor
        self.notify = notify
        self.verify_steps = bool(verify_steps)
        self.max_replans = self.MAX_REPLANS if max_replans is None else max(0, int(max_replans))
        self.parallel_steps = bool(parallel_steps)
        self.max_parallel = self.MAX_PARALLEL if max_parallel is None else max(1, int(max_parallel))
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
        result_parts: list[str] = list(task.context.get("result_parts") or [])
        failed = False
        executed = 0
        idx = task.current_step

        while idx < len(task.steps) and executed < self.MAX_STEPS:
            if self._is_cancelled(task):
                task.status = "cancelled"
                self.store.save(task)
                await self._notify(task, "[已取消] 主人终止了任务")
                return "任务已取消。"

            batch = self._collect_batch(task, idx)
            total = len(task.steps)
            for step in batch:
                await self._notify(task, f"[{step.index + 1}/{total}] {step.description}…")
                step.status = "running"
            self.store.save(task)

            try:
                budget.check_duration()
                results = await self._run_batch(batch, task, budget)
            except asyncio.TimeoutError:
                step = batch[0]
                step.status = "failed"
                step.error = "步骤超时"
                task.current_step = step.index
                if await self._replan_or_give_up(task, step, "步骤超时", "step_failed"):
                    executed += 1
                    idx = task.current_step
                    continue
                task.status = "failed"
                task.error = f"步骤 {step.index + 1} 超时"
                failed = True
                self.store.save(task)
                break
            except ResourceExceeded as e:
                step = batch[0]
                step.status = "failed"
                step.error = str(e)
                task.current_step = step.index
                task.status = "failed"
                task.error = str(e)
                failed = True
                self.store.save(task)
                await self._notify(task, f"[资源超限] {e}")
                break
            except StepFailed as e:
                step = batch[0]
                step.status = "failed"
                step.error = str(e)
                task.current_step = step.index
                if await self._replan_or_give_up(task, step, str(e), "step_failed"):
                    executed += 1
                    idx = task.current_step
                    continue
                task.status = "failed"
                task.error = f"步骤 {step.index + 1} 失败: {e}"
                failed = True
                self.store.save(task)
                break

            # ---- 成功路径 ----
            if len(batch) == 1:
                step = batch[0]
                step.status = "done"
                step.result = results[0][:4000]
                task.context[f"step_{step.index}_result"] = results[0][:2000]
                ok, vreason = True, ""
                if step.verify:
                    checked, ok, vreason = await self._verify_step(step, task)
                    step.verified = checked and ok
                if not ok:
                    step.verified = False
                    step.status = "failed"
                    step.error = f"验收未通过：{vreason}"
                    task.current_step = step.index
                    self.store.save(task)
                    if await self._replan_or_give_up(
                            task, step, step.error, "verify_failed"):
                        executed += 1
                        idx = task.current_step
                        continue
                    task.status = "failed"
                    task.error = f"步骤 {step.index + 1} 验收未通过：{vreason}"
                    failed = True
                    self.store.save(task)
                    break
                task.current_step = step.index + 1
                self.store.save(task)
                result_parts.append(f"步骤{step.index + 1}: {step.description} ✓")
                executed += 1
                idx = task.current_step
            else:
                # 并行批次不逐项验收（都是确定性显式工具调用，验收交给后续步骤）
                for step, result in zip(batch, results):
                    step.status = "done"
                    step.result = result[:4000]
                    task.context[f"step_{step.index}_result"] = result[:2000]
                    result_parts.append(f"步骤{step.index + 1}: {step.description} ✓（并行）")
                task.current_step = batch[-1].index + 1
                self.store.save(task)
                executed += len(batch)
                idx = task.current_step

        task.context["result_parts"] = result_parts

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
        if task.status == "cancelled":
            return "任务已取消。"
        await self._notify(task, f"[失败] {task.error or '未知错误'}")
        return f"任务失败：{task.error or '未知错误'}"

    # ------------------------------------------------------------------
    # P2-1 验收
    # ------------------------------------------------------------------

    def _verify_available(self) -> bool:
        if not self.verify_steps:
            return False
        return getattr(self.core, "llm", None) is not None

    async def _verify_step(self, step: Step, task: AgentTask) -> tuple[bool, bool, str]:
        """独立验收：返回 ``(是否真的验过, 是否通过, 原因)``。

        - ``checked=False``：没有判据 / 无 LLM / 验收器故障 → 未真正验收，
          调用方据此不要把 ``verified`` 标成 True（「没验」和「验过通过」是两回事）。
        - ``checked=True, ok=False``：判据明确不成立。
        """
        criterion = (step.verify or "").strip()
        if not criterion or not self._verify_available():
            return False, True, ""
        llm = getattr(self.core, "llm", None)
        prompt = (
            f"原始请求：{task.request}\n"
            f"刚完成的步骤：{step.description}\n"
            f"该步骤的验收标准：{criterion}\n"
            f"步骤实际输出（截断）：\n{(step.result or '（空）')[:self.VERIFY_OUTPUT_CHARS]}\n\n"
            "请判断「步骤实际输出」是否满足验收标准。只回答一行："
            "PASS 或 FAIL，后跟一句理由。"
        )
        try:
            raw = await asyncio.wait_for(
                llm.chat([{"role": "user", "content": prompt}],
                         temperature=0.0, max_tokens=120),
                timeout=self.VERIFY_TIMEOUT,
            )
        except Exception as e:  # noqa: BLE001 - 验收器故障不阻塞主流程
            if self.auditor:
                self.auditor.emit("agent_verify_inconclusive", task_id=task.id,
                                  step=step.index, reason=f"verify_error:{type(e).__name__}")
            return False, True, ""

        text = (raw if isinstance(raw, str) else str(raw)).strip()
        head = text.upper().lstrip()
        if head.startswith("FAIL") or "FAIL" in head[:12]:
            return True, False, text[:200] or "验收判定不通过"
        if head.startswith("PASS") or "PASS" in head[:12]:
            if self.auditor:
                self.auditor.emit("agent_verify_passed", task_id=task.id,
                                  step=step.index, criterion=criterion[:120])
            return True, True, ""
        # 判定模糊 → 放行但留痕（避免判据含糊毁掉已成功的步骤）
        if self.auditor:
            self.auditor.emit("agent_verify_inconclusive", task_id=task.id,
                              step=step.index, reason=f"ambiguous:{text[:80]}")
        return True, True, ""

    # ------------------------------------------------------------------
    # P2-1 重规划
    # ------------------------------------------------------------------

    def _replan_available(self, task: AgentTask) -> bool:
        if task.replan_count >= self.max_replans:
            return False
        # 重规划需要 LLM 才能真正换思路；无 LLM 只会把一次失败拖成多次失败
        return getattr(self.planner, "llm", None) is not None

    async def _replan_or_give_up(self, task: AgentTask, failed_step: Step,
                                 error: str, reason: str) -> bool:
        """尝试重规划；返回 True 表示已替换剩余计划、外层可继续循环。"""
        if not self._replan_available(task):
            return False
        done = [s.description for s in task.steps
                if s.status == "done" and s.index < failed_step.index]
        remaining = [s.description for s in task.steps[failed_step.index + 1:]
                     if s.status == "pending"]
        try:
            new_steps = await self.planner.replan(
                request=task.request, done=done,
                failed_step_desc=failed_step.description,
                error=error, remaining=remaining, reason=reason,
            )
        except Exception:
            logger.exception("重规划失败（已隔离，按原失败处理）")
            return False
        if not new_steps:
            return False

        kept = task.steps[:failed_step.index]
        for offset, s in enumerate(new_steps):
            s.index = len(kept) + offset
        task.steps = kept + new_steps
        task.current_step = len(kept)
        task.replan_count += 1
        task.context[f"replan_{task.replan_count}"] = {
            "failed_step": failed_step.description,
            "reason": reason,
            "error": error[:300],
            "new_steps": [s.description for s in new_steps],
        }
        task.error = ""
        self.store.save(task)
        if self.auditor:
            self.auditor.emit("agent_replan", task_id=task.id,
                              replan_count=task.replan_count, reason=reason,
                              failed_step=failed_step.description[:200],
                              error=error[:200],
                              new_steps=[s.description[:120] for s in new_steps])
        await self._notify(
            task,
            f"[重规划 #{task.replan_count}] 「{failed_step.description[:40]}」未达成"
            f"（{reason}），改为 {len(new_steps)} 步替代方案",
        )
        return True

    # ------------------------------------------------------------------
    # P2-1 并行批次
    # ------------------------------------------------------------------

    def _collect_batch(self, task: AgentTask, idx: int) -> list[Step]:
        """收集可并行的连续步骤。默认返回单元素列表（串行）。"""
        steps = task.steps
        first = steps[idx]
        if not self.parallel_steps or not first.parallel_group:
            return [first]
        group = first.parallel_group
        batch: list[Step] = []
        j = idx
        while j < len(steps) and len(batch) < self.max_parallel:
            s = steps[j]
            # 只并发「显式指定工具」的步骤：ReAct 步骤会互相干扰上下文，不可并发
            if s.parallel_group != group or not s.tool_name:
                break
            batch.append(s)
            j += 1
        return batch if len(batch) > 1 else [first]

    async def _run_batch(self, batch: list[Step], task: AgentTask,
                         budget: ResourceBudget) -> list[str]:
        if len(batch) == 1:
            return [await asyncio.wait_for(
                self._run_step(batch[0], task, budget), timeout=self.STEP_TIMEOUT)]

        # 并发批次共享同一个 ResourceBudget。不额外加锁：ResourceBudget 的方法
        # 内部没有 await，单线程 asyncio 下不存在真正的竞态；加锁反而会把批次串行化
        # （这正是本方法存在的意义）。
        return list(await asyncio.wait_for(
            asyncio.gather(*(self._run_step(s, task, budget) for s in batch)),
            timeout=self.STEP_TIMEOUT))

    # ------------------------------------------------------------------
    # 单步执行（带重试）
    # ------------------------------------------------------------------

    async def _run_step(self, step: Step, task: AgentTask, budget: ResourceBudget) -> str:
        last_err: str | None = None
        for attempt in range(1, self.MAX_TOOL_RETRIES + 1):
            step.attempts += 1
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
