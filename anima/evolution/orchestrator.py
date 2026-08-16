"""R10 工程化自治闭环收口 —— Observation → Evolution → Evaluation 单一可观测闭环。

把已落地的三块能力串成一条**默认不部署、fail-closed**的闭环：

    Observation（R10-01 现实观测）
        → 发现现实问题（高频 DENY / 运行时降级 / 用户反馈）
        → 桥接 R8 DevIssue（to_dev_issues，仅归集不决策）
        → EvolutionLoop（R9 受控自我进化：提案 → 围栏 → Owner 审批门）
        → Evaluation（R10-02 部署前后 Before/After 改进证据）
        → 产出 ObservableCycleReport（可审计、不自我授权）

设计纪律（与 R6–R10 一致）：
- 闭环**只产出提案与证据**，绝不自我授权生产更新；真正落地需 Owner 显式审批 + 授权部署。
- 观测层只归集不决策；评估层只量化不自愈（自愈属 R13）。
- 任何一步失败都 fail-open 记录，不阻断其余信号（Audit Everything）。
"""
from __future__ import annotations

import time
from typing import Any

from .evaluator import Benchmark
from ..agent.self_evolution import EvolutionLoop
from ..observation import collect_all, to_dev_issues


class ObservationOptimizationLoop:
    """把「观测 → 演化 → 评估」串成单一可观测闭环。

    ``dry_run``（默认 True）= 只观测 + 提案 + 量化评估，**不部署**；
    ``dry_run=False`` 仍需 ``owner_authorized=True`` 才会真正部署（受控门禁不变）。
    """

    def __init__(self, dev_loop: Any, *, selfcode_manager: Any = None,
                 gate: Any = None, core: Any = None, dry_run: bool = True) -> None:
        self._dev = dev_loop
        self._core = core
        self._dry_run = dry_run
        # 复用 R9 受控自我进化闭环（含单例审批门）。dry_run 时强制不部署。
        self._loop = EvolutionLoop(
            dev_loop, selfcode_manager=selfcode_manager, gate=gate, core=core,
        )
        # 观测聚合参数
        self.window_sec = 86400
        self.min_denials = 5

    def observe(self, *, audit: Any = None,
                audit_denials: list[dict[str, Any]] | None = None,
                feedback_queue: list[dict[str, Any]] | None = None) -> list[Any]:
        """观测现实信号，桥接为 R8 DevIssue 列表（仅归集）。"""
        events = collect_all(
            self._core, audit=audit, audit_denials=audit_denials,
            feedback_queue=feedback_queue,
            window_sec=self.window_sec, min_denials=self.min_denials,
        )
        self._last_events = events
        issues = to_dev_issues(events)
        return issues

    async def run_cycle(self, *, audit: Any = None,
                        audit_denials: list[dict[str, Any]] | None = None,
                        feedback_queue: list[dict[str, Any]] | None = None,
                        owner_authorized: bool = False) -> dict[str, Any]:
        """跑一次可观测闭环：观测 → 演化提案 →（受控）评估。

        返回 ObservableCycleReport：含观测事件、生成的提案、改进证据、整体结论。
        默认 dry_run（不部署）；dry_run=False 且 owner_authorized 才部署。
        """
        started_at = time.time()
        issues = self.observe(
            audit=audit, audit_denials=audit_denials, feedback_queue=feedback_queue,
        )

        proposals: list[dict[str, Any]] = []
        evidence_log: list[dict[str, Any]] = []
        for issue in issues:
            goal = f"{issue.title}"
            target_files = tuple(getattr(issue, "target_files", ()) or ())
            # 经 R9 受控门禁：提案 → 围栏 → Owner 审批（fail-closed，默认不部署）
            res = await self._loop.evolve(
                goal, target_files=target_files,
                auto_deploy=(not self._dry_run),
                owner_authorized=(owner_authorized and not self._dry_run),
            )
            proposals.append(res)
            ie = res.get("improvement_evidence")
            if ie is not None:
                evidence_log.append({"proposal_id": res.get("id"), "evidence": ie})

        # 聚合本次闭环的改进证据结论
        overall = _aggregate_evidence(evidence_log)

        report = {
            "ok": True,
            "mode": "dry_run" if self._dry_run else "deployed",
            "observed_events": [getattr(e, "to_dict", lambda: {})() for e in getattr(self, "_last_events", [])],
            "issues": [getattr(i, "to_dict", lambda: {})() for i in issues],
            "proposals": proposals,
            "evidence": evidence_log,
            "evidence_summary": overall,
            "started_at": started_at,
            "finished_at": time.time(),
            "note": "闭环仅产出提案与证据，绝不自我授权生产更新；落地需 Owner 显式审批 + 授权部署。",
        }
        return report


def _aggregate_evidence(evidence_log: list[dict[str, Any]]) -> dict[str, Any]:
    """把多份 improvement_evidence 的 summary 聚合为整体结论。"""
    total_effective = total_harmful = total_ineffective = total_metrics = 0
    for entry in evidence_log:
        rep = entry.get("evidence") or {}
        summ = rep.get("summary") or {}
        total_effective += int(summ.get("effective", 0))
        total_harmful += int(summ.get("harmful", 0))
        total_ineffective += int(summ.get("ineffective", 0))
        total_metrics += int(summ.get("total", 0))
    if total_harmful > 0:
        overall = "harmful"
    elif total_effective > 0:
        overall = "effective"
    elif total_metrics > 0:
        overall = "ineffective"
    else:
        overall = "no_evidence"
    return {
        "overall": overall,
        "effective": total_effective,
        "harmful": total_harmful,
        "ineffective": total_ineffective,
        "total_metrics": total_metrics,
    }
