"""P6 —— Personal Skill Evolution（个人能力生态：提案 → 沙箱 → Benchmark → 采纳）。

把「个人 Skill 能力」也纳入自治演进闭环，但**严格守纪律：采纳需 Owner 授权**。

生命周期：Skill Proposal（生成草案）→ Sandbox（静态护栏 + 无害预览）→ Benchmark（量化评估）
→ Adopt（仅当沙箱通过且 Benchmark 过阈值，才把草案标记为「可采纳」，等待 Owner 显式批准；
**绝不自我采纳**）。

设计纪律（延续 R6–R15 / P0–P5，尤其 No Hidden Authority / Audit Everything / Production Is Sacred）：
- 复用 R 系 Skill 基础设施：``SkillDraftManager`` 的 validate（禁止代码/外部执行、参数一致、
  min_level 受限）与 approve（需 Owner，`http_get` 必须先预览）。
- **沙箱即护栏**：Benchmark 前必须 Sandbox 通过（无禁止指令、参数闭环、权限受限）。
- **Benchmark 可注入**：默认提供确定性启发式（清单完整度 / 禁止模式惩罚 / 参数一致性），
  真实部署可注入更丰富的评测（如对照已有技能、离线 dry-run）。
- **采纳门 fail-closed**：沙箱未过 / Benchmark 未达阈值 → 不可采纳；即便可采纳也只是
  ``pending_owner``，真正的安装复用 Owner 的 ``!anima draft approve``。
- **不自我授权**：``SkillEvolutionAgent`` 只产出评估结论，不调用 approve、不写 skill.yaml。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import re

# 提案生成器签名：request -> dict（manifest）。可注入以便离线/单测。
ProposeFn = Callable[[str], Awaitable[dict[str, Any]] | dict[str, Any]]
# Benchmark 函数签名：manifest -> SkillBenchmark。可注入。
BenchmarkFn = Callable[[dict[str, Any]], "SkillBenchmark"]


@dataclass
class SkillBenchmark:
    """对一条 Skill 草案的量化评估。"""

    score: float               # 0..1
    verdict: str               # pass | fail
    rationale: str
    metrics: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"score": round(self.score, 4), "verdict": self.verdict,
                "rationale": self.rationale, "metrics": dict(self.metrics)}


@dataclass
class SkillEvaluation:
    """一条 Skill 草案的完整生命周期评估（提案→沙箱→Benchmark→采纳判定）。"""

    draft_id: str
    name: str
    request: str
    sandbox_ok: bool
    sandbox_errors: list[str]
    benchmark: SkillBenchmark
    adoptable: bool            # 仅当沙箱通过且 Benchmark 过阈值（仍须 Owner 显式批准）
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "draft_id": self.draft_id,
            "name": self.name,
            "request": self.request,
            "sandbox_ok": self.sandbox_ok,
            "sandbox_errors": list(self.sandbox_errors),
            "benchmark": self.benchmark.to_dict(),
            "adoptable": self.adoptable,
            "note": self.note,
        }


# ---------------------------------------------------------------------------
# 默认确定性 Benchmark（启发式；真实部署可注入更丰富评测）
# ---------------------------------------------------------------------------
_FORBIDDEN = re.compile(
    r"\b(import|exec|eval|subprocess|os\.system|pip\s+install|curl\s+|wget\s+)\b", re.I
)
_PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]*)\}")


def _default_benchmark(manifest: dict[str, Any]) -> SkillBenchmark:
    """确定性 Benchmark：清单完整度 + 禁止模式惩罚 + 参数一致性。"""
    metrics: dict[str, float] = {}
    penalties = 0.0

    # 1) manifest 完整性（id/name/description/parameters/execution）
    required = ["id", "name", "description", "parameters", "execution"]
    present = sum(1 for k in required if manifest.get(k) not in (None, "", {}, []))
    completeness = present / len(required)
    metrics["completeness"] = round(completeness, 4)

    # 2) 禁止模式惩罚（模板含代码/外部执行）
    tmpl = str((manifest.get("execution") or {}).get("template", ""))
    if _FORBIDDEN.search(tmpl):
        penalties += 0.5
        metrics["forbidden_pattern"] = 0.0
    else:
        metrics["forbidden_pattern"] = 1.0

    # 3) 参数一致性（模板占位符必须全部在声明参数内）
    declared = set((manifest.get("parameters") or {}).keys())
    used = set(_PLACEHOLDER.findall(tmpl))
    if used and used <= declared:
        metrics["param_consistency"] = 1.0
    elif not used:
        metrics["param_consistency"] = 1.0
    else:
        metrics["param_consistency"] = 0.0
        penalties += 0.3

    # 4) 权限受限（自动生成的 Skill 不应申请高于 user）
    lvl = manifest.get("min_level")
    if lvl in {"readonly", "user"}:
        metrics["level_safe"] = 1.0
    else:
        metrics["level_safe"] = 0.0
        penalties += 0.3

    score = max(0.0, min(1.0, completeness - penalties))
    verdict = "pass" if score >= 0.6 and metrics["forbidden_pattern"] == 1.0 else "fail"
    rationale = (
        f"完整度={completeness:.2f}，禁止模式={'命中(-0.5)' if metrics['forbidden_pattern']==0.0 else '无'}"
        f"，参数一致={'是' if metrics['param_consistency']==1.0 else '否(-0.3)'}"
        f"，权限安全={'是' if metrics['level_safe']==1.0 else '否(-0.3)'}；"
        f"综合分={score:.2f}（阈值0.6）→ {verdict}。"
    )
    return SkillBenchmark(score=score, verdict=verdict, rationale=rationale, metrics=metrics)


# ---------------------------------------------------------------------------
# 个人能力演进代理
# ---------------------------------------------------------------------------
class SkillEvolutionAgent:
    """个人 Skill 演进代理：提案→沙箱→Benchmark→采纳判定（采纳仍须 Owner）。

    不调用 approve、不安装任何 Skill；只产出 ``SkillEvaluation`` 供 Owner 决策。
    """

    def __init__(self, draft_manager: Any = None, *, propose_fn: ProposeFn | None = None,
                 benchmark_fn: BenchmarkFn | None = None,
                 benchmark_threshold: float = 0.6) -> None:
        self._dm = draft_manager
        self._propose_fn = propose_fn
        self._benchmark_fn = benchmark_fn
        self._threshold = benchmark_threshold

    async def propose_skill(self, request: str) -> dict[str, Any]:
        """生成一条 Skill 草案 manifest（fail-closed：无生成器→报错，不猜测）。"""
        if self._propose_fn is not None:
            out = self._propose_fn(request)
            if hasattr(out, "__await__"):
                out = await out
            return out
        if self._dm is not None and hasattr(self._dm, "create_from_request"):
            draft = await self._dm.create_from_request(request)
            return dict(draft.manifest)
        raise RuntimeError("未配置提案生成器（propose_fn 或 draft_manager），无法生成 Skill 草案")

    def sandbox_validate(self, manifest: dict[str, Any]) -> list[str]:
        """沙箱静态护栏：复用 SkillDraftManager.validate（若存在），否则用默认校验。"""
        if self._dm is not None and hasattr(self._dm, "validate"):
            try:
                return list(self._dm.validate(manifest))
            except Exception:  # noqa: BLE001 - 校验异常按不通过处理
                pass
        # 默认校验（与 drafts.validate 对齐的最小集）
        errors: list[str] = []
        if _FORBIDDEN.search(str((manifest.get("execution") or {}).get("template", ""))):
            errors.append("模板包含禁止的代码或外部执行指令")
        declared = set((manifest.get("parameters") or {}).keys())
        used = set(_PLACEHOLDER.findall(str((manifest.get("execution") or {}).get("template", ""))))
        if used - declared:
            errors.append("模板引用了未声明参数: " + ", ".join(sorted(used - declared)))
        lvl = manifest.get("min_level")
        if lvl not in {"readonly", "user"}:
            errors.append("自动生成的 Skill 只能申请 readonly 或 user 权限")
        return errors

    def benchmark(self, manifest: dict[str, Any]) -> SkillBenchmark:
        if self._benchmark_fn is not None:
            return self._benchmark_fn(manifest)
        return _default_benchmark(manifest)

    def evaluate(self, draft_id: str, request: str, manifest: dict[str, Any]) -> SkillEvaluation:
        """对一条草案执行完整生命周期评估（沙箱 + Benchmark + 采纳判定）。

        采纳判定 fail-closed：沙箱未过 / Benchmark 未达阈值 → adoptable=False。
        即便 adoptable=True，也仅表示「可提交 Owner 批准」，本代理不执行批准。
        """
        errors = self.sandbox_validate(manifest)
        sandbox_ok = not errors
        bm = self.benchmark(manifest)
        adoptable = sandbox_ok and bm.verdict == "pass" and bm.score >= self._threshold
        note = (
            "可通过 Sandbox 与 Benchmark，建议提交 Owner 批准采纳。"
            if adoptable else
            ("沙箱未通过，禁止采纳。" if not sandbox_ok else
             f"Benchmark 未达阈值（{bm.score:.2f} < {self._threshold}），暂缓采纳。")
        )
        return SkillEvaluation(
            draft_id=draft_id, name=str(manifest.get("name", "")),
            request=request, sandbox_ok=sandbox_ok, sandbox_errors=errors,
            benchmark=bm, adoptable=adoptable, note=note,
        )

    async def run_lifecycle(self, request: str, *, draft_id: str = "pending") -> SkillEvaluation:
        """便捷端到端：提案 → 评估（不采纳、不安装）。"""
        manifest = await self.propose_skill(request)
        return self.evaluate(draft_id, request, manifest)
