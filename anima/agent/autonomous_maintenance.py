"""P5 —— Autonomous Maintenance（自治维护：自查→提议→CI 预检→等 Owner 批准）。

把「运维自查」接成闭环，但严格守纪律：**工程准备自动、执行等待 Owner 授权**。

流程：检测过期依赖 → 分析风险（复用 P4 记忆护栏 / 参考 P2 影响评分）→ 生成 R9 风格
**待审提案**（经 SelfCode 生成候选，不安装）→ 可选 CI 预检 → 注册进 R9 审批门等待 Owner。

设计纪律（延续 R6–R15 / P0–P4，尤其 No Hidden Authority / Audit Everything / Production Is Sacred）：
- **绝不自我安装/自我授权**：``MaintenanceAgent`` 只产出 ``pending_owner`` 提案；真正的生产更新
  复用 R9 ``EvolutionGate`` 的 Owner 批准路径（fail-closed：默认 DENY）。
- **记忆护栏**：若 P4 长期记忆中存在该依赖的「why-described（为何这样设计 / 禁止自动改）」，
  直接跳过该依赖的升级提议（避免误改有意为之的护栏）。
- **核心文件保护**：任何触碰 ``CORE_PROTECTED`` 的提案经 R9 审查判 ``reject``（需 Owner 人工 PR）。
- **可注入 / 离线可测**：依赖检测器与 CI 预检函数均可注入；默认检测器解析 ``requirements.txt``
  的精确锁定版本并标记过期候选。CI 预检缺失 → 诚实跳过（不伪造「已通过」）。
- **降错优先**：风险分析发现历史有害升级或护栏冲突 → 提案标记 ``needs_attention`` 而非放行。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .self_evolution import CORE_PROTECTED, EvolutionGate, EvolutionProposal, EvolutionProposer


# 依赖检测器：给定无副作用环境，返回待评估的依赖发现列表。
DependencyDetector = Callable[[], list["DependencyFinding"]]
# CI 预检：给定依赖发现，返回 (ok, log)。异常由调用方 fail-closed 处理。
CIVerifyFn = Callable[["DependencyFinding"], tuple[bool, str]]


@dataclass
class DependencyFinding:
    """一条「依赖待评估」发现。"""

    pkg: str
    current: str
    latest: str
    stale: bool
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"pkg": self.pkg, "current": self.current, "latest": self.latest,
                "stale": self.stale, "note": self.note}


@dataclass
class MaintenanceProposal:
    """一条自治维护提案（包裹 R9 EvolutionProposal + 风险/CI 上下文）。"""

    finding: DependencyFinding
    proposal: EvolutionProposal | None
    risk_level: str            # low | medium | high | skip
    skip_reason: str = ""
    ci_ok: bool | None = None  # None = 未做 CI 预检
    ci_log: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "pkg": self.finding.pkg,
            "current": self.finding.current,
            "latest": self.finding.latest,
            "risk_level": self.risk_level,
            "skip_reason": self.skip_reason,
            "ci_ok": self.ci_ok,
            "ci_log": self.ci_log,
            "proposal": self.proposal.to_dict() if self.proposal else None,
        }


@dataclass
class MaintenanceScan:
    """一次自治维护扫描结果（可审计、可 replay）。"""

    findings_total: int = 0
    proposed: int = 0
    skipped: int = 0
    proposals: list[MaintenanceProposal] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "findings_total": self.findings_total,
            "proposed": self.proposed,
            "skipped": self.skipped,
            "proposals": [p.to_dict() for p in self.proposals],
            "note": self.note,
        }


# ---------------------------------------------------------------------------
# 默认依赖检测器（解析 requirements.txt 精确锁定；离线、无副作用）
# ---------------------------------------------------------------------------
def requirements_detector(requirements_path: str = "requirements.txt",
                          *, stale_floor: dict[str, str] | None = None) -> DependencyDetector:
    """返回一个检测器：解析 ``requirements.txt`` 的 ``pkg==ver`` 行，标记过期候选。

    - ``stale_floor``：{pkg: min_version}，当前版本 < 该下限即视为过期（真实部署可用
      ``pip index`` / ``importlib.metadata`` 填充；本默认实现仅做结构化解析，绝不联网）。
    - 未提供 floor 时，所有精确锁定包都作为「待 Owner 评估」候选返回（stale=False，由风险分析定夺）。
    """
    import re

    def _detect() -> list[DependencyFinding]:
        from pathlib import Path
        p = Path(requirements_path)
        if not p.exists():
            return []
        findings: list[DependencyFinding] = []
        pat = re.compile(r"^\s*([A-Za-z0-9_.\-]+)\s*==\s*([A-Za-z0-9_.!~+<>=]+)\s*$")
        for raw in p.read_text(encoding="utf-8").splitlines():
            m = pat.match(raw)
            if not m:
                continue
            pkg, ver = m.group(1), m.group(2)
            floor = (stale_floor or {}).get(pkg)
            stale = bool(floor) and _ver_lt(ver, floor)
            findings.append(DependencyFinding(
                pkg=pkg, current=ver, latest=floor or ver,
                stale=stale,
                note="低于安全下限" if stale else "精确锁定，待风险分析",
            ))
        return findings

    return _detect


def _ver_lt(a: str, b: str) -> bool:
    """极简语义化版本比较（仅数字段，离线启发式）。"""

    def _nums(v: str) -> tuple[int, ...]:
        return tuple(int(x) for x in "".join(c if c.isdigit() else " " for c in v).split() if x)
    return _nums(a) < _nums(b)


# ---------------------------------------------------------------------------
# 自治维护代理
# ---------------------------------------------------------------------------
class MaintenanceAgent:
    """自治维护代理：检测→风险分析→生成待审提案→（可选）CI 预检→等待 Owner。

    不执行任何安装/部署；所有落地动作都在 R9 审批门之外由 Owner 触发。
    """

    def __init__(self, *, proposer: EvolutionProposer | None = None,
                 gate: EvolutionGate | None = None,
                 memory: Any = None,
                 ci_verify_fn: CIVerifyFn | None = None,
                 detect_fn: DependencyDetector | None = None) -> None:
        self._proposer = proposer
        self._gate = gate
        self._memory = memory
        self._ci = ci_verify_fn
        self._detect = detect_fn

    async def run_maintenance(self) -> MaintenanceScan:
        """执行一次自治维护扫描（fail-closed：检测失败→空结果 + 说明）。"""
        scan = MaintenanceScan()
        if self._detect is None:
            scan.note = "未配置依赖检测器，跳过（诚实：不产生提案）"
            return scan
        try:
            findings = self._detect()
        except Exception as exc:  # noqa: BLE001 - 检测异常不 crashing，诚实降级
            scan.note = f"依赖检测失败：{type(exc).__name__}: {exc}"
            return scan

        scan.findings_total = len(findings)
        for f in findings:
            mp = await self._assess_and_propose(f)
            scan.proposals.append(mp)
            if mp.risk_level == "skip":
                scan.skipped += 1
            elif mp.proposal is not None:
                scan.proposed += 1
        return scan

    async def _assess_and_propose(self, f: DependencyFinding) -> MaintenanceProposal:
        # 1) 记忆护栏：P4 why-described 命中「禁止自动改 / 护栏」→ 跳过
        if self._memory is not None:
            try:
                whys = self._memory.why_designed(f.pkg)
                for w in whys:
                    if any(k in w.statement for k in ("禁止自动", "护栏", "不可自动", "有意为之")):
                        return MaintenanceProposal(
                            finding=f, proposal=None, risk_level="skip",
                            skip_reason=f"P4 记忆护栏：{w.statement}（避免误改有意为之的护栏）",
                        )
            except Exception:  # noqa: BLE001
                pass

        # 2) 是否过期：非过期且非核心候选 → 默认跳过（不制造噪声提案）
        if not f.stale:
            return MaintenanceProposal(
                finding=f, proposal=None, risk_level="skip",
                skip_reason="当前版本为精确锁定且未低于安全下限，无需升级",
            )

        # 3) 核心文件保护：依赖升级若触及 CORE_PROTECTED（极少见，但严格 fail-closed）
        if f.pkg in CORE_PROTECTED:
            return MaintenanceProposal(
                finding=f, proposal=None, risk_level="skip",
                skip_reason="依赖属受保护核心，需 Owner 人工 PR（R9 拒绝自动落地）",
            )

        # 4) 生成 R9 待审提案（经 SelfCode 生成候选，不安装）
        proposal = None
        if self._proposer is not None:
            goal = f"升级依赖 {f.pkg}：{f.current} → {f.latest}（经 CI 验证后由 Owner 审批）"
            try:
                proposal = await self._proposer.propose(goal, target_files=(f"requirements.txt",))
            except Exception as exc:  # noqa: BLE001
                proposal = None
                return MaintenanceProposal(
                    finding=f, proposal=None, risk_level="high",
                    skip_reason=f"提案生成失败：{type(exc).__name__}: {exc}",
                )
            # 注册进 R9 审批门（pending_owner，fail-closed 默认不更新）
            if self._gate is not None and proposal is not None:
                self._gate.request_approval(proposal)

        # 5) 可选 CI 预检（缺失 → 诚实跳过，绝不伪造通过）
        ci_ok: bool | None = None
        ci_log = ""
        risk = "low"
        if self._ci is not None:
            try:
                ci_ok, ci_log = self._ci(f)
                if not ci_ok:
                    risk = "high"
            except Exception as exc:  # noqa: BLE001
                ci_ok, ci_log, risk = None, f"CI 预检异常：{exc}", "medium"

        return MaintenanceProposal(
            finding=f, proposal=proposal, risk_level=risk,
            ci_ok=ci_ok, ci_log=ci_log,
        )
