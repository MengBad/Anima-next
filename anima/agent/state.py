"""Phase 3 任务状态与持久化 —— AgentTask / Step 数据结构 + AgentTaskStore。

设计：
- AgentTask 与 Step 为纯 dataclass，可 JSON 序列化。
- AgentTaskStore 复用 PersistenceProvider（与 SelfCode 的 owner_tasks.json 并列）。
- 重启恢复：status=executing 的任务在加载时标记为 failed（需人工重跑），
  避免进程崩溃后“幽灵执行”。current_step 决定从哪一步继续。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Step:
    index: int
    description: str
    tool_name: str = ""           # 空表示由 ReAct 在该步自行决定工具
    tool_args: dict = field(default_factory=dict)
    status: str = "pending"        # pending|running|done|failed|skipped
    result: str = ""
    error: str = ""


@dataclass
class AgentTask:
    id: str
    request: str
    creator: int
    status: str = "planning"       # planning|executing|completed|failed|cancelled
    steps: list[Step] = field(default_factory=list)
    current_step: int = 0
    context: dict = field(default_factory=dict)
    error: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


def _step_from(raw: dict) -> Step:
    return Step(
        index=int(raw.get("index", 0)),
        description=str(raw.get("description", "")),
        tool_name=str(raw.get("tool_name", "")),
        tool_args=dict(raw.get("tool_args", {}) or {}),
        status=str(raw.get("status", "pending")),
        result=str(raw.get("result", "")),
        error=str(raw.get("error", "")),
    )


def _task_from(raw: dict) -> AgentTask:
    return AgentTask(
        id=str(raw["id"]),
        request=str(raw.get("request", "")),
        creator=int(raw.get("creator", 0)),
        status=str(raw.get("status", "planning")),
        steps=[_step_from(s) for s in (raw.get("steps", []) or [])],
        current_step=int(raw.get("current_step", 0)),
        context=dict(raw.get("context", {}) or {}),
        error=str(raw.get("error", "")),
        created_at=float(raw.get("created_at", time.time())),
        updated_at=float(raw.get("updated_at", time.time())),
    )


class AgentTaskStore:
    """Agent 多步任务持久化（data/agent_tasks.json）。"""

    def __init__(self, persistence, max_kept: int = 100):
        self.persistence = persistence
        self.max_kept = max_kept
        self.items: dict[str, AgentTask] = {}
        for raw in (persistence.read_json("agent_tasks.json", []) or []):
            try:
                task = _task_from(raw)
                # 重启恢复：崩溃时正在执行的任务标记为 failed，等待人工重跑
                if task.status == "executing":
                    task.status = "failed"
                    task.error = task.error or "进程重启，执行中断"
                    task.updated_at = time.time()
                self.items[task.id] = task
            except (TypeError, ValueError, KeyError):
                continue
        if any(t.status == "failed" and "进程重启" in (t.error or "") for t in self.items.values()):
            self._save()

    def _save(self) -> None:
        rows = [asdict(t) for t in self.items.values()]
        self.persistence.write_json("agent_tasks.json", rows)

    def create(self, request: str, creator: int,
               steps: list[Step] | None = None) -> AgentTask:
        # 去重：5 分钟内同一创建者、同一请求文本且仍处于 planning 的任务
        # 直接复用（QQ 消息重发/连点会触发重复入站，避免刷出多个草稿）。
        now = time.time()
        for t in self.items.values():
            if (t.creator == int(creator) and t.request == request[:2000]
                    and t.status == "planning" and now - t.created_at < 300):
                return t
        task = AgentTask(
            id=uuid.uuid4().hex[:10],
            request=request[:2000],
            creator=int(creator),
            steps=steps or [],
            status="planning" if steps else "planning",
        )
        self.items[task.id] = task
        self._save()
        return task

    def get(self, task_id: str) -> AgentTask | None:
        return self.items.get(task_id)

    def list(self) -> list[AgentTask]:
        return sorted(self.items.values(), key=lambda t: t.updated_at, reverse=True)

    def save(self, task: AgentTask) -> None:
        task.updated_at = time.time()
        self.items[task.id] = task
        self._save()
        self._prune()

    def set_status(self, task_id: str, status: str, error: str = "") -> AgentTask:
        task = self.get(task_id)
        if task is None:
            raise ValueError("未找到 Agent 任务")
        task.status = status
        if error:
            task.error = error[:2000]
        self.save(task)
        return task

    def _prune(self) -> None:
        """保留最近 max_kept 条，避免无限增长。"""
        if len(self.items) <= self.max_kept:
            return
        ordered = sorted(self.items.values(), key=lambda t: t.updated_at)
        for old in ordered[: len(self.items) - self.max_kept]:
            self.items.pop(old.id, None)
        self._save()
