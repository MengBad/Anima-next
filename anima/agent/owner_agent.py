"""Phase 3/4 整合层：OwnerAgent 串起规划器 / 执行器 / 存储 / 审计 / 通知。

供 handler 的 `!anima agent` 命令与 is_agent_task 自动路由调用。所有入口都要求
permission_level >= 4（真正的主人），普通用户无法触达。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from anima.agent.audit import AgentAuditor
from anima.agent.executor import AgentExecutor
from anima.agent.planner import TaskPlanner
from anima.agent.state import AgentTask, AgentTaskStore

logger = logging.getLogger("anima.agent.owner_agent")


class OwnerAgent:
    def __init__(self, core):
        self.core = core
        persistence = core.persistence
        data_dir = getattr(getattr(core.config, "persistence", None), "data_dir", "data")
        self.auditor = AgentAuditor(str(data_dir))
        self.store = AgentTaskStore(persistence)
        self.executor = AgentExecutor(
            core, self.store,
            planner=TaskPlanner(getattr(core, "llm", None)),
            auditor=self.auditor,
            notify=self._notify,
        )

    async def _notify(self, task: AgentTask, message: str) -> None:
        gateway = getattr(self.core, "gateway", None)
        if not gateway:
            return
        try:
            from anima.llm.handler import OutboundMsg
        except Exception:
            return
        try:
            await gateway.send(OutboundMsg("private", int(task.creator), message[:1500]))
        except Exception:
            logger.exception("Agent 进度推送失败（已隔离）")

    async def submit(self, request: str, creator: int, *, auto_run: bool = True) -> AgentTask:
        """创建 Agent 任务；auto_run=True 时后台异步执行并返回任务句柄。"""
        task = self.store.create(request, creator)
        if auto_run:
            asyncio.create_task(self._run(task))
        return task

    async def _run(self, task: AgentTask) -> None:
        try:
            await self.executor.execute(task)
        except Exception:
            logger.exception("Agent 任务执行异常")

    async def approve(self, task_id: str) -> str:
        task = self.store.get(task_id)
        if task is None:
            return "未找到 Agent 任务"
        if task.status == "executing":
            return f"任务 {task_id} 正在执行中"
        # 清除可能的历史取消标志/状态，允许重新执行
        self.executor._cancel.pop(task_id, None)
        try:
            self.store.set_status(task_id, "planning")
        except ValueError:
            pass
        asyncio.create_task(self._run(task))
        return f"已启动 Agent 任务 {task_id}"

    def cancel(self, task_id: str) -> str:
        self.executor.request_cancel(task_id)
        try:
            self.store.set_status(task_id, "cancelled", "主人取消")
        except ValueError:
            pass
        return f"已请求取消 Agent 任务 {task_id}"

    def status_text(self, limit: int = 10) -> str:
        items = self.store.list()[:limit]
        if not items:
            return "暂无 Agent 任务"
        return "\n".join(f"{t.id} [{t.status}] {t.request[:60]}" for t in items)
