"""Phase 3/4 整合层：OwnerAgent 串起规划器 / 执行器 / 存储 / 审计 / 通知。

供 handler 的 `!anima agent` 命令与 is_agent_task 自动路由调用。所有入口都要求
permission_level >= 4（真正的主人），普通用户无法触达。
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
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
        # 让 agent_tools._audit 能直接找到 AgentAuditor（否则工具审计会落到 core.audit）
        core.agent_auditor = self.auditor
        self.store = AgentTaskStore(persistence)
        _cfg = getattr(core.config, "task_agent", None)
        self.executor = AgentExecutor(
            core, self.store,
            # 🔴 09-26：**不能**在构造期捕获 `core.llm`。生产装配顺序是
            # `anima_core.py` 先 `self.agent = OwnerAgent(self)`、**之后**才
            # `self.llm = None` / `cli.py` 里 `core.llm = llm`，此处
            # `getattr(core, "llm", None)` 必然拿到 None 且全仓无回填 ⇒ 规划器的
            # LLM 分支变成死代码（计划全部退化为关键词拼接）。真正的绑定改到
            # `AgentExecutor._bind_planner_llm()`（执行时惰性解析）。
            planner=TaskPlanner(None),
            auditor=self.auditor,
            notify=self._notify,
            verify_steps=bool(getattr(_cfg, "verify_steps", True)),
            max_replans=getattr(_cfg, "max_replans", None),
            parallel_steps=bool(getattr(_cfg, "parallel_steps", True)),
            max_parallel=getattr(_cfg, "max_parallel", None),
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

    async def _deliver_media(self, task: AgentTask) -> None:
        """把 Agent 任务产出的受控媒体（生图/改图）发到**请求所在会话**。

        09-26 生产事故：Agent 路径**没有任何图片交付通道** —— `generate_image` 只把
        路径写进 task-local call context，而该 key 的读者只有对话 pipeline 的
        generation 阶段与 `handler.build_reply_segments`；Agent 出站只走 `_notify`
        （纯文本 + 固定私聊）⇒ 887 KB 的图生成成功、落盘、从未发出，用户在群里什么
        都没收到。此处复用 `executor` 回收的路径显式交付。

        会话选择：建草稿时把 ``session``（detail_type/target_id）写进任务 context，
        交付回原会话；老任务没有该字段时退回私聊 creator（历史行为，不更差）。

        fail-open：单张失败只记日志，不改变任务状态（状态已由 executor 判定）。
        """
        paths = self.executor.live_media_paths(task)
        if not paths:
            return
        gateway = getattr(self.core, "gateway", None)
        if gateway is None:
            logger.warning("Agent 媒体交付跳过：gateway 未就绪（task=%s, %d 张）",
                           task.id, len(paths))
            return
        try:
            from anima.core.gateway import OutboundMsg
        except Exception:
            logger.exception("Agent 媒体交付失败：无法导入 OutboundMsg")
            return
        sess = task.context.get("session") or {}
        detail_type = str(sess.get("detail_type") or "private")
        try:
            target = int(sess.get("target_id") or task.creator)
        except (TypeError, ValueError):
            target = int(task.creator)
        delivered, missing = 0, 0
        for p in paths:
            try:
                await gateway.send(OutboundMsg(
                    detail_type, target, [{"type": "image", "data": {"file": p}}]))
                delivered += 1
            except Exception:
                missing += 1
                logger.exception("Agent 媒体单张交付失败（已隔离）: %s", p)
        if self.auditor:
            self.auditor.emit("agent_media_delivered", task_id=task.id,
                              delivered=delivered, failed=missing,
                              detail_type=detail_type, target=target)
        logger.info("Agent 媒体交付: task=%s 成功 %d / 失败 %d -> %s:%s",
                    task.id, delivered, missing, detail_type, target)

    async def submit(self, request: str, creator: int, *, auto_run: bool = True,
                     session: dict | None = None) -> AgentTask:
        """创建 Agent 任务；auto_run=True 时后台异步执行并返回任务句柄。"""
        task = self.store.create(request, creator, session=session)
        if auto_run:
            asyncio.create_task(self._run(task))
        return task

    async def _run(self, task: AgentTask) -> None:
        try:
            await self.executor.execute(task)
        except Exception:
            logger.exception("Agent 任务执行异常")
            try:
                task.status = "failed"
                task.error = "执行代理发生未捕获异常"
                task.updated_at = time.time()
                self.store.save(task)
                await self._notify(task, "[失败] 执行代理发生未捕获异常")
            except Exception:
                pass
        finally:
            # 09-26 F2：无论成败，只要真的产出了媒体就交付（异常路径下也可能已有图）。
            try:
                await self._deliver_media(task)
            except Exception:
                logger.exception("Agent 媒体交付异常（已隔离）")

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
