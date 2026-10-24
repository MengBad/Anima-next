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
    # P2-1：验收标准（自然语言）。空表示跳过独立验收，只看工具是否报错。
    verify: str = ""
    # P2-1：同名字段的连续步骤在「都有显式 tool_name」时可并行执行；空=串行。
    parallel_group: str = ""
    verified: bool = False
    attempts: int = 0
    origin: str = "plan"           # plan | replan（便于审计区分重规划产出）
    #: 🔴 2026-10-25：本步实际调用过的工具名（ReAct / 显式工具分支回填）。
    #: 用途：识别「**声称做了、其实什么都没做**」的编造步骤。
    #: 生产实证（10-24 14:06）：某步声称「本喵已经帮你抽好今日运势和猪猪啦」，
    #: 但该步**没有任何工具调用**，而前一步刚说「未发现明确的抽卡任务」。
    tools_called: list[str] = field(default_factory=list)


@dataclass
class AgentTask:
    id: str
    request: str
    creator: int
    status: str = "planning"       # planning|executing|completed|failed|cancelled|interrupted
    #: `interrupted`（2026-10-21 新增）：进程重启打断了正在执行的任务。
    #: 与 `failed` 的区别 ——**它不是任务本身的失败**，进度与已完成步骤都保留，
    #: 可经 approve 重跑。此前这类一律判 `failed` 且静默无通知。
    steps: list[Step] = field(default_factory=list)
    current_step: int = 0
    context: dict = field(default_factory=dict)
    error: str = ""
    # P2-1：已发生的重规划次数（上限由 executor 把关，防无界重试）
    replan_count: int = 0
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
        verify=str(raw.get("verify", "")),
        parallel_group=str(raw.get("parallel_group", "")),
        verified=bool(raw.get("verified", False)),
        attempts=int(raw.get("attempts", 0)),
        origin=str(raw.get("origin", "plan")),
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
        replan_count=int(raw.get("replan_count", 0)),
        created_at=float(raw.get("created_at", time.time())),
        updated_at=float(raw.get("updated_at", time.time())),
    )


class AgentTaskStore:
    """Agent 多步任务持久化（data/agent_tasks.json）。"""

    def __init__(self, persistence, max_kept: int = 100):
        self.persistence = persistence
        self.max_kept = max_kept
        self.items: dict[str, AgentTask] = {}
        #: 本次进程启动时被判定「重启打断」的任务 id（供上层推送/审计用）。
        self.interrupted_on_load: list[str] = []
        for raw in (persistence.read_json("agent_tasks.json", []) or []):
            try:
                task = _task_from(raw)
                # 🔴 2026-10-21 改造（Dots 分界线）：重启时不再把在跑任务判成 failed。
                #
                #   原实现 `task.status = "failed"` + `error="进程重启，执行中断"`：
                #   一个跑了 3 小时的任务，重启即判死，且**静默无通知** ——
                #   用户只知道"我没收到结果"，不知道"它被重启打断了"。
                #   这也与 executor.py:12 的设计意图相悖（那里注释写的是
                #   「逐步持久化，进程重启可从 current_step 续跑」）。
                #
                #   现在：标 `interrupted`（新状态），**保留 current_step 与已完成的
                #   step.result**，并在 `interrupted_on_load` 里登记，供上层私聊告知
                #   owner「有任务被打断了，要不要重跑」。
                #
                #   ⚠️ 刻意**不自动重跑**：重投需要幂等键与副作用分类（发消息类操作
                #   重跑 = 重复发消息；run_python 改过的文件重跑不会撤销）。
                #   那属独立 slice，须先出设计稿。本步只消除"静默判死"这个信息黑洞。
                if task.status == "executing":
                    task.status = "interrupted"
                    task.error = task.error or "进程重启，执行中断（进度已保留，可重跑）"
                    task.updated_at = time.time()
                    self.interrupted_on_load.append(task.id)
                self.items[task.id] = task
            except (TypeError, ValueError, KeyError):
                continue
        if self.interrupted_on_load:
            self._save()

    def _save(self) -> None:
        rows = [asdict(t) for t in self.items.values()]
        self.persistence.write_json("agent_tasks.json", rows)

    def create(self, request: str, creator: int,
               steps: list[Step] | None = None,
               *, session: dict | None = None) -> AgentTask:
        # 去重：5 分钟内同一创建者、同一请求文本且仍处于 planning 的任务
        # 直接复用（QQ 消息重发/连点会触发重复入站，避免刷出多个草稿）。
        now = time.time()
        for t in self.items.values():
            if (t.creator == int(creator) and t.request == request[:2000]
                    and t.status == "planning" and now - t.created_at < 300):
                # 09-26：复用旧草稿时补齐会话信息（老草稿可能没有），交付才不会退回私聊。
                if session and not t.context.get("session"):
                    t.context["session"] = dict(session)
                    self._save()
                return t
        task = AgentTask(
            id=uuid.uuid4().hex[:10],
            request=request[:2000],
            creator=int(creator),
            steps=steps or [],
            status="planning" if steps else "planning",
            # 09-26：记录「请求来自哪个会话」，交付产物（生图）时发回这里，
            # 而不是无脑发私聊。持久化在 agent_tasks.json 的 context 里。
            context={"session": dict(session)} if session else {},
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
