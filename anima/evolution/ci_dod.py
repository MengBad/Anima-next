"""CI DoD —— 把「CI 怎么才算通过」编码为 fail-closed 校验器(代谢闭环 Stage 4)。

Criteria (对应用 .github/workflows/anima-ci.yml):
  backend:  pytest tests/ 全 0 fail
  security: anima_guard.py 产物; 触核心/敏感路径 -> 须 Owner 显式核可
  evidence: 候选自带 ImprovementEvidence 包(CI DoD 含 evidence)

纪律: 任一必需项未满足 -> overall pass = False(fail-closed);
     CI 绝不自动改码/部署/批准; Owner 是唯一最终闸。
"""
from __future__ import annotations

from typing import Any

#: WebUI 已整体移除（design/private_group_dual_mode_plan.md §5.5），
#: 前端构建不再是 DoD 的一环。
CI_DOD_SECTIONS = ("backend", "security", "evidence")


def check_ci_dod(*,
                 test_passed: bool | None = None,
                 evidence_ok: bool | None = None,
                 security_requires_owner_review: bool = False,
                 owner_acknowledged: bool = False) -> dict:
    """校验一次候选的 CI DoD。返回 {'pass', reasons, sections}。

    fail-closed: 任一 True|False 需要的项若为 None/False 即不过;
    触核心且未 Owner 核可 -> 不过(不完全自动放行)。
    """
    reasons: list = []
    ok = True
    if test_passed is not True:
        ok = False; reasons.append("backend: pytest 未全绿")
    if evidence_ok is not True:
        ok = False; reasons.append("evidence: 候选无 ImprovementEvidence 包(CI DoD 必需)")
    if security_requires_owner_review and not owner_acknowledged:
        ok = False; reasons.append("security: 触核心/敏感路径, 须 Owner 显式核可")
    return {
        "pass": ok,
        "reasons": reasons,
        "sections": {
            "backend": bool(test_passed),
            "evidence": bool(evidence_ok),
            "security_requires_owner_review": bool(security_requires_owner_review),
        },
    }


def candidate_reviewable(result: dict) -> bool:
    """CI DoD 通过才可上审核(仍须 Owner 最终闸)。"""
    return bool(result.get("pass"))
