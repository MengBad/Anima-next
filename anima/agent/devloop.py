"""R8 —— Autonomous Development Loop：自主软件工程闭环。

把 R7 多 Agent 编排（Planner → Developer → Tester → Reviewer → Deploy）串成
**端到端自主开发闭环**：

    发现问题(Issue) → 任务规划(Planner) → 代码修改(Developer)
    → 测试(Tester) → Review(Reviewer) → 部署(Deploy) → 验证

## 受控原则（R9 前置，self-improvement ≠ self-authorization）

闭环**只生成候选 + 跑测试 + 收集证据**，**绝不直接改写生产核心**：
- Developer 阶段复用 R6 的 SelfCode 双 Agent 生成「受限运行时工具候选」，
  但按 SelfCode 既有纪律**不自动安装**（需 Owner 审批）。
- Deploy 阶段默认 **DENY**（fail-closed）：只有 ``owner_authorized=True`` 且注入了
  部署函数时，才调用部署；否则产出待审批部署提案，由 Owner 在 WebUI/运维侧执行。

## 与 R7 的关系

本模块是 R7 编排器的**高层封装**：R7 负责「多 Agent 安全协作（身份/权限/取证）」，
R8 负责「把一次 Issue 驱动成一条完整开发流水线，并在受控门禁下收口」。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .orchestrator import (
    DEFAULT_PIPELINE,
    STAGE_DEPLOY,
    STAGE_DEVELOP,
    STAGE_PLAN,
    STAGE_REVIEW,
    STAGE_TEST,
    AgentIdentity,
    AgentOrchestrator,
    AgentRegistry,
    PipelineResult,
)

# 闭环阶段内部标识（与 R7 流水线阶段对齐）。
ISSUE_SOURCE_OWNER = "owner"
ISSUE_SOURCE_SELF_MONITOR = "self_monitor"
ISSUE_SOURCE_AUDIT = "audit"


@dataclass
class DevIssue:
    """一个待处理的开发问题。

    source：问题来源（owner 指令 / 自监控指标 / 审计异常）。
    title：一句话描述。
    detail：补充上下文（用于驱动 Developer 生成候选）。
    capability：处理该 issue 所需的 R6.3 能力（决定哪类 Agent 被授权）。
    required_scopes：处理该 issue 所需的 scope 集合。
    """

    title: str
    detail: str = ""
    source: str = ISSUE_SOURCE_OWNER
    capability: str = "code.develop"
    required_scopes: frozenset[str] = frozenset({"write"})
    severity: str = "normal"  # low | normal | high

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "detail": self.detail,
            "source": self.source,
            "capability": self.capability,
            "required_scopes": tuple(self.required_scopes),
            "severity": self.severity,
        }


@dataclass
class DevLoopResult:
    """一次闭环的执行结果（受控收口：是否需 Owner 审批）。"""

    task_id: str
    issue: DevIssue
    pipeline: PipelineResult | None = None
    needs_owner_approval: bool = False       # 部署门：True 表示等待 Owner 授权
    deploy_attempted: bool = False
    deploy_ok: bool = False
    deploy_error: str = ""
    finished_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "issue": self.issue.to_dict(),
            "pipeline": self.pipeline.to_dict() if self.pipeline else None,
            "needs_owner_approval": self.needs_owner_approval,
            "deploy_attempted": self.deploy_attempted,
            "deploy_ok": self.deploy_ok,
            "deploy_error": self.deploy_error,
        }


# 部署函数签名：``async def deploy_fn(issue: DevIssue, pipeline: PipelineResult) -> str``
DeployFn = Callable[[DevIssue, PipelineResult], Awaitable[str]]


class DevLoopOrchestrator:
    """R8 自主开发闭环编排器。

    包装 R7 ``AgentOrchestrator``，把一次 ``DevIssue`` 跑成完整开发流水线，并在
    Deploy 阶段施加**受控门禁**（默认 DENY，需 Owner 显式授权才部署）。

    阶段 handler 在构造时注入（便于测试 mock；生产环境由 ``make_r8_handlers`` 装配
    SelfCodeManager / pytest / 部署脚本）。
    """

    def __init__(
        self,
        registry: AgentRegistry,
        handlers: dict[str, Callable[..., Awaitable[str]]],
        *,
        evidence_sink: Any = None,
        deploy_fn: DeployFn | None = None,
        pipeline: tuple[str, ...] = DEFAULT_PIPELINE,
    ) -> None:
        self._deploy_fn = deploy_fn
        self._orchestrator = AgentOrchestrator(
            registry, handlers=handlers, evidence_sink=evidence_sink, pipeline=pipeline
        )

    @property
    def orchestrator(self) -> AgentOrchestrator:
        return self._orchestrator

    async def run_issue(self, issue: DevIssue, *, owner_authorized: bool = False) -> DevLoopResult:
        """跑一次自主开发闭环。

        owner_authorized：是否允许 Deploy 阶段真正部署（fail-closed：默认 False → 不部署）。
        返回 ``DevLoopResult``，含是否需 Owner 审批与部署实证。

        各阶段按各自能力授权（per-stage capability）：planner/developer/tester/reviewer
        各管一种能力，只有 deploy 阶段需要 issue.capability（通常是 deploy.release）。
        这样 Developer 不会被要求拥有 deploy 权限，符合「独立权限边界」。
        """
        task_id = f"devloop-{int(time.time()*1000)}"
        request = f"[R8] {issue.title}\n{issue.detail}".strip()

        # 各阶段独立能力授权（R7 的 stage_capabilities 加性特性）。
        stage_capabilities = {
            STAGE_PLAN: ("task.plan", frozenset({"read"})),
            STAGE_DEVELOP: ("code.develop", frozenset({"write"})),
            STAGE_TEST: ("test.run", frozenset({"read"})),
            STAGE_REVIEW: ("review.approve", frozenset({"read"})),
            # deploy 阶段需要 issue 声明的能力（如 deploy.release）+ write scope。
            STAGE_DEPLOY: (issue.capability, issue.required_scopes),
        }

        pipeline = await self._orchestrator.run(
            task_id, request,
            required_capability=issue.capability,
            required_scopes=issue.required_scopes,
            stage_capabilities=stage_capabilities,
        )

        result = DevLoopResult(task_id=task_id, issue=issue, pipeline=pipeline)

        # 受控门禁：流水线整体成功，且 Deploy Agent 被授权，才进入「是否真部署」判定。
        deploy_stage = next((s for s in pipeline.stages if s.stage == STAGE_DEPLOY), None)
        if deploy_stage is None or deploy_stage.outcome == "denied":
            # Deploy Agent 未被授权（如 issue 能力不匹配 deploy.release）→ 受控收口。
            result.needs_owner_approval = True
            return result

        if not owner_authorized or self._deploy_fn is None:
            # fail-closed：未授权或未配部署函数 → 产出待审批部署提案，等 Owner 审批。
            result.needs_owner_approval = True
            return result

        # Owner 已授权 → 执行部署并实证。
        result.deploy_attempted = True
        try:
            msg = await self._deploy_fn(issue, pipeline)
            result.deploy_ok = True
            result.deploy_error = str(msg)[:500]
        except Exception as exc:  # noqa: BLE001 - 部署失败不影响闭环证据留存
            result.deploy_ok = False
            result.deploy_error = f"{type(exc).__name__}: {exc}"
        return result


def make_r8_identity_registry() -> AgentRegistry:
    """构造 R8 闭环使用的标准 5-Agent 身份注册表（每 Agent 独立权限边界）。

    与 R7 标准流水线对齐；Deploy Agent 仅声明 deploy.release 能力，
    所以只有带该能力的 issue 才会被授权进入 Deploy 阶段（fail-closed）。
    """
    reg = AgentRegistry()
    reg.register(AgentIdentity(
        agent_id="agent-planner", role="planner",
        capabilities={"task.plan": frozenset({"read"})},
        description="把 Issue 拆解为开发计划",
    ))
    reg.register(AgentIdentity(
        agent_id="agent-developer", role="developer",
        capabilities={"code.develop": frozenset({"write"})},
        description="生成受限运行时工具候选（SelfCode 双 Agent）",
    ))
    reg.register(AgentIdentity(
        agent_id="agent-tester", role="tester",
        capabilities={"test.run": frozenset({"read"})},
        description="运行测试套件验证候选",
    ))
    reg.register(AgentIdentity(
        agent_id="agent-reviewer", role="reviewer",
        capabilities={"review.approve": frozenset({"read"})},
        description="审查候选并收集待审批提案",
    ))
    reg.register(AgentIdentity(
        agent_id="agent-deploy", role="deploy",
        capabilities={"deploy.release": frozenset({"write"})},
        description="Owner 授权后部署到生产",
    ))
    return reg
