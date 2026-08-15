"""R7 —— Multi-Agent Architecture：多 Agent 安全协作编排层。

## 目标（R7）

多个 Agent 安全协作：Planner → Developer → Tester → Reviewer → Deploy。
每个 Agent 拥有：**独立身份、独立权限、独立能力范围**。

建立：Planner Agent → Developer Agent → Tester Agent → Reviewer Agent → Deploy Agent

## 设计（加性新增，不动共享核心）

本模块是**编排层**，组合既有单 Agent 执行循环（``agent/executor.py``）与
R6.3 RBAC（独立权限）和 R6.5 Evidence（行为证据）：

- 每个 Agent 以独立 ``AgentIdentity`` 注册（id + 角色 + 能力/scope 范围），
  其权限由 R6.3 RBAC 引擎按角色授权。
- 编排器把任务沿流水线路由到各 Agent，每个阶段都要求该 Agent 的权限覆盖
  请求的能力 + scope（fail-closed：权限不足 → 阶段 DENY，不执行）。
- 每个阶段执行后落 R6.5 Evidence trace（谁/什么/结果/何时），行为可事后还原。

## 模型

:class:`AgentIdentity` —— 独立身份：``agent_id`` + ``role`` + ``capabilities``（能力+scope）。
:class:`AgentRegistry` —— 注册/查询 Agent（可选集成 R6.3 RBAC 授权）。
:class:`AgentOrchestrator` —— 流水线编排：按阶段把任务路由到对应 Agent，逐段授权+执行+取证。
:class:`PipelineResult` —— 流水线输出（各阶段结果 + 全程证据链）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

# 流水线阶段（与最终架构对齐）。
STAGE_PLAN = "planner"
STAGE_DEVELOP = "developer"
STAGE_TEST = "tester"
STAGE_REVIEW = "reviewer"
STAGE_DEPLOY = "deploy"

# 标准流水线顺序。
DEFAULT_PIPELINE = (STAGE_PLAN, STAGE_DEVELOP, STAGE_TEST, STAGE_REVIEW, STAGE_DEPLOY)


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AgentIdentity:
    """一个 Agent 的独立身份与能力边界。

    - 独立身份：``agent_id`` 全局唯一（Principal）。
    - 独立权限：``role`` 决定其在 R6.3 RBAC 中的角色。
    - 独立能力范围：``capabilities`` 声明该 Agent 可用的能力 + 各自 scope。
    - 是否允许：经 RBAC 授权（granted ⊇ required），fail-closed。
    """

    agent_id: str
    role: str
    capabilities: dict[str, frozenset[str]] = field(default_factory=dict)  # {capability: {scope}}
    description: str = ""

    @property
    def granted_scopes(self) -> dict[str, tuple[str, ...]]:
        return {cap: tuple(sc) for cap, sc in self.capabilities.items()}

    def may(self, capability: str, required_scopes: frozenset[str]) -> bool:
        """该 Agent 是否被授权执行该能力+scope（fail-closed：未声明→False）。"""
        allowed = self.capabilities.get(capability)
        if allowed is None:
            return False
        return required_scopes.issubset(allowed)


class AgentRegistry:
    """注册/查询 Agent 身份（独立权限边界）。"""

    def __init__(self) -> None:
        self._agents: dict[str, AgentIdentity] = {}

    def register(self, identity: AgentIdentity) -> None:
        self._agents[identity.agent_id] = identity

    def get(self, agent_id: str) -> AgentIdentity | None:
        return self._agents.get(agent_id)

    def list(self) -> list[AgentIdentity]:
        return list(self._agents.values())


@dataclass
class PipelineStageResult:
    """流水线一个阶段的执行结果。"""

    stage: str
    agent_id: str
    outcome: str                       # allowed | denied | completed
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    result: str = ""
    error: str = ""
    evidence: dict = field(default_factory=dict)  # 该阶段行为证据（R6.5 投影）

    @property
    def latency(self) -> float:
        return max(self.finished_at - self.started_at, 0.0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "agent_id": self.agent_id,
            "outcome": self.outcome,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "latency": self.latency,
            "result": self.result,
            "error": self.error,
            "evidence": self.evidence,
        }


@dataclass
class PipelineResult:
    """整条流水线的输出。"""

    task_id: str
    stages: list[PipelineStageResult] = field(default_factory=list)
    status: str = "completed"          # completed | failed
    error: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0

    def evidence_trail(self) -> list[dict]:
        """全程证据链（R6.5 可 replay：任何 Agent 行为可事后还原）。"""
        return [s.to_dict() for s in self.stages]

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "stages": self.evidence_trail(),
        }


# ---------------------------------------------------------------------------
# 编排器
# ---------------------------------------------------------------------------
class AgentOrchestrator:
    """多 Agent 流水线编排。

    用法：注册各 Agent 身份 → 调用 ``run()`` 把任务沿流水线路由到对应 Agent。

    - 每个阶段先做 R6.3 风格授权（该 Agent 的 role/capability 是否覆盖请求能力+scope，
      fail-closed：不覆盖 → 该阶段 DENY，不执行）。
    - 授权通过后调该 Agent 的执行器（注入的 handler），并记录 R6.5 证据。
    - 阶段处理函数签名：``async def handler(task: AgentTask, stage, identity) -> str``。
    - 编排器本身**不做任何权限判定之外的事**：执行交给 handler，判定交给
      AgentIdentity.may / RBAC。
    """

    def __init__(
        self,
        registry: AgentRegistry,
        *,
        handlers: dict[str, Callable[..., Awaitable[str]]],
        evidence_sink: Any = None,  # 可选的 R6.5 EvidenceSink
        pipeline: tuple[str, ...] = DEFAULT_PIPELINE,
    ) -> None:
        self._registry = registry
        self._handlers = handlers
        self._evidence_sink = evidence_sink
        self._pipeline = pipeline

    def authorize_stage(self, agent_id: str, capability: str,
                        required_scopes: frozenset[str]) -> bool:
        """R6.3 授权：该 Agent 是否被授权执行此能力+scope。fail-closed。"""
        identity = self._registry.get(agent_id)
        if identity is None:
            return False
        return identity.may(capability, required_scopes)

    async def run(self, task_id: str, request: str, *,
                  required_capability: str,
                  required_scopes: frozenset[str] = frozenset()) -> PipelineResult:
        """沿流水线运行：每阶段按能力+scope 授权对应 Agent，授权通过才执行+取证。

        审计纪律（R6.5 "Audit Everything"）：**每个阶段无论 ALLOW/DENY 都记录证据**，
        拒绝是最高价值的审计事件，绝不因"该阶段未执行"就无证据可查。
        """
        result = PipelineResult(task_id=task_id)

        def _evidence(stage_res: PipelineStageResult, identity: AgentIdentity | None) -> dict:
            return {
                "task_id": task_id,
                "stage": stage_res.stage,
                "agent": stage_res.agent_id,
                "role": identity.role if identity is not None else None,
                "capability": required_capability,
                "scopes": tuple(required_scopes),
                "outcome": stage_res.outcome,
                "result": stage_res.result,
                "error": stage_res.error,
                "latency": stage_res.latency,
            }

        for stage in self._pipeline:
            agent_id = f"agent-{stage}"
            identity = self._registry.get(agent_id)

            # fail-closed 授权：Agent 不存在或无权限 → 阶段 DENY（仍记录证据）
            if identity is None or not self.authorize_stage(agent_id, required_capability, required_scopes):
                stage_res = PipelineStageResult(
                    stage=stage, agent_id=agent_id, outcome="denied",
                    error=f"agent {agent_id} 无权限执行 {required_capability}",
                )
                stage_res.evidence = _evidence(stage_res, identity)
                self._record_evidence(stage_res.evidence)
                result.stages.append(stage_res)
                continue

            handler = self._handlers.get(stage)
            if handler is None:
                stage_res = PipelineStageResult(
                    stage=stage, agent_id=agent_id, outcome="denied",
                    error=f"no handler for stage {stage}",
                )
                stage_res.evidence = _evidence(stage_res, identity)
                self._record_evidence(stage_res.evidence)
                result.stages.append(stage_res)
                continue

            stage_res = PipelineStageResult(stage=stage, agent_id=agent_id, outcome="allowed")
            try:
                out = await handler(task_id, request, identity)
                stage_res.finished_at = time.time()
                stage_res.result = str(out)[:2000]
                stage_res.outcome = "completed"
            except Exception as exc:  # noqa: BLE001 - 单阶段失败不阻断其余阶段
                stage_res.finished_at = time.time()
                stage_res.error = f"{stage} 阶段失败: {exc}"
                stage_res.outcome = "denied"
                result.status = "failed"
                result.error = result.error or stage_res.error

            # R6.5 证据：该阶段行为可事后还原（谁/什么/结果/何时）
            stage_res.evidence = _evidence(stage_res, identity)
            self._record_evidence(stage_res.evidence)
            result.stages.append(stage_res)

        result.finished_at = time.time()
        if result.status != "failed":
            result.status = "completed"
        return result

    def _record_evidence(self, evidence: dict) -> None:
        """把阶段证据投给 R6.5 EvidenceSink（fail-open：写失败只计数）。"""
        if self._evidence_sink is None:
            return
        try:
            self._evidence_sink(evidence)
        except Exception:  # noqa: BLE001 - 观测失灵不得阻断编排
            pass


def make_agent_registry(
    agents: dict[str, AgentIdentity],
) -> AgentRegistry:
    """便捷工厂：由 dict 构造注册表（agent_id → AgentIdentity）。"""
    reg = AgentRegistry()
    for agent_id, identity in agents.items():
        if identity.agent_id != agent_id:
            identity = AgentIdentity(
                agent_id=agent_id, role=identity.role,
                capabilities=identity.capabilities, description=identity.description)
        reg.register(identity)
    return reg