"""Owner 工程任务队列：计划、候选产物、审批与执行记录分离。"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from anima.core.persistence import PersistenceProvider


@dataclass
class Task:
    request: str
    creator: int
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    # planned|running|awaiting_approval|completed|failed|cancelled|rejected
    status: str = "planned"
    kind: str = "diagnostic"
    plan: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    # 仅保存脱敏工程摘要、候选文件名、测试结论与风险；绝不写入 Token/聊天正文。
    proposal: dict[str, Any] = field(default_factory=dict)
    reviewed_at: float | None = None
    reviewer: int | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


class TaskStore:
    def __init__(self, persistence: PersistenceProvider):
        self.persistence = persistence
        self.items: dict[str, Task] = {}
        for raw in persistence.read_json("owner_tasks.json", []) or []:
            try:
                self.items[raw["id"]] = Task(**raw)
            except (TypeError, ValueError, KeyError):
                continue

    def _save(self) -> None:
        self.persistence.write_json("owner_tasks.json", [asdict(item) for item in self.items.values()])

    def create(self, request: str, creator: int, plan: list[str], *,
               kind: str = "diagnostic") -> Task:
        item = Task(
            request=request[:2000], creator=int(creator), plan=plan[:8], kind=kind[:64],
        )
        self.items[item.id] = item
        self._save()
        return item

    def get(self, task_id: str) -> Task | None:
        return self.items.get(task_id)

    def list(self) -> list[Task]:
        return sorted(self.items.values(), key=lambda item: item.updated_at, reverse=True)

    def set_status(self, task_id: str, status: str, evidence: str = "") -> Task:
        item = self.get(task_id)
        if item is None:
            raise ValueError("未找到任务")
        item.status = status
        item.updated_at = time.time()
        if evidence:
            item.evidence.append(evidence[:2000])
        self._save()
        return item

    def set_proposal(self, task_id: str, proposal: dict[str, Any], *,
                     evidence: str = "") -> Task:
        item = self.get(task_id)
        if item is None:
            raise ValueError("未找到任务")
        item.proposal = proposal
        item.status = "awaiting_approval"
        item.updated_at = time.time()
        if evidence:
            item.evidence.append(evidence[:2000])
        self._save()
        return item

    def approve(self, task_id: str, reviewer: int) -> Task:
        item = self.get(task_id)
        if item is None:
            raise ValueError("未找到任务")
        if item.status != "awaiting_approval":
            raise ValueError(f"任务当前状态为 {item.status}，不能审批")
        item.reviewer = int(reviewer)
        item.reviewed_at = time.time()
        item.updated_at = item.reviewed_at
        self._save()
        return item

    def reject(self, task_id: str, reviewer: int, reason: str = "") -> Task:
        item = self.get(task_id)
        if item is None:
            raise ValueError("未找到任务")
        if item.status != "awaiting_approval":
            raise ValueError(f"任务当前状态为 {item.status}，不能驳回")
        item.status = "rejected"
        item.reviewer = int(reviewer)
        item.reviewed_at = time.time()
        item.updated_at = item.reviewed_at
        if reason:
            item.evidence.append(f"Owner 驳回：{reason[:1000]}")
        self._save()
        return item

    def claim(self, task_id: str) -> Task:
        item = self.get(task_id)
        if item is None:
            raise ValueError("未找到任务")
        if item.status != "planned":
            raise ValueError(f"任务当前状态为 {item.status}，不可重复执行")
        item.status = "running"
        item.updated_at = time.time()
        self._save()
        return item


class OwnerTaskAgent:
    def __init__(self, core: Any, pilot: Any = None):
        self.core = core
        self._locks: dict[str, asyncio.Lock] = {}
        # P7.18 pilot wiring: defaults to None (legacy path); a wired PilotEntry
        # admits pilot traffic through the gate, everything else stays legacy.
        self._pilot = pilot

    async def execute(self, task: Task) -> str:
        lock = self._locks.setdefault(task.id, asyncio.Lock())
        if lock.locked():
            raise ValueError("任务正在执行")
        async with lock:
            return await self._execute_locked(task)

    async def _execute_locked(self, task: Task) -> str:
        from anima.agent.react import ReActLoop, is_placeholder_reply

        task = self.core.tasks.claim(task.id)
        messages = [{"role": "system", "content": (
            "你是 Anima 的 Owner 工程任务代理。只处理当前任务，不闲聊。\n"
            "先用只读工具收集证据，再给出明确结论；不要虚构已修改或已部署。\n"
            "任何写入、重启、部署或外部状态变更都必须停在等待 Owner 审批。"
        )}, {"role": "user", "content": f"任务目标：{task.request}"}]
        # Phase 1：主人分层沙箱——把权限等级注入工具调用上下文，
        # 使 run_python 在任务内获得网络/放宽超时（与 ReActLoop 的 permission_level=4 一致）。
        # 防御式访问：测试桩 core 可能没有 tools 属性或旧版 registry 无上下文接口。
        _tools = getattr(self.core, "tools", None)
        _ctx_token = None
        if _tools is not None and hasattr(_tools, "set_call_context"):
            _base_ctx = _tools.get_call_context() if hasattr(_tools, "get_call_context") else {}
            _ctx_token = _tools.set_call_context(**{**_base_ctx, "permission_level": 4})
        try:
            loop = ReActLoop(self.core.llm, self.core.tools, max_rounds=8, permission_level=4)
            result = await loop.run(messages, temperature=0.2, max_tokens=800)
            result_text = str(result).strip()
            # 🔴 空/占位结果**不是完成**（09-18）。这里原本对 ReAct 返回值没有任何占位守卫：
            # 上游全空时占位串会进 `task.evidence`，同时任务被判 `completed` —— 假完成，
            # 且污染后续「这任务做过了」的判断。判定与 ReAct 共用同一函数，避免各自腐烂。
            if is_placeholder_reply(result_text):
                task.status = "failed"
                task.evidence.append("上游返回空结果（空串/占位），未取得任何结论")
                task.updated_at = time.time()
                self.core.tasks._save()
                return "任务未取得结论：上游返回空结果，请稍后重试或换个更具体的问法。"
            trace = getattr(loop, "tool_trace", [])
            pending = any("等待主人审批" in str(item.get("result", "")) for item in trace)
            pending = pending or any(word in result_text for word in ("需要审批", "等待审批", "需要确认后"))
            task.status = "awaiting_approval" if pending else "completed"
            task.evidence.append(result_text[:2000])
            task.updated_at = time.time()
            self.core.tasks._save()
            return result_text
        except Exception as exc:
            task.status = "failed"
            task.evidence.append(type(exc).__name__)
            task.updated_at = time.time()
            self.core.tasks._save()
            return f"任务执行失败：{type(exc).__name__}: {str(exc)[:160]}"
        finally:
            if _tools is not None and _ctx_token is not None:
                _tools.reset_call_context(_ctx_token)
