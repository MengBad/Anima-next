"""P5 — Autonomous Maintenance 单元测试（确定性，检测器/CI 可注入）。"""
from __future__ import annotations

from pathlib import Path

import pytest

from anima.agent import autonomous_maintenance as am
from anima.agent.self_evolution import EvolutionGate, EvolutionProposer
from anima.memory import long_term as lt


# 用真实的 requirements.txt 检测器解析临时文件，避免联网
def _make_req(tmp_path: Path, text: str) -> str:
    p = tmp_path / "requirements.txt"
    p.write_text(text, encoding="utf-8")
    return str(p)


@pytest.fixture
def gate():
    return EvolutionGate()


@pytest.fixture
def proposer():
    # 真实部署必有 SelfCodeManager；测试用 fake 让 propose 产出非空 diff_summary（否则 R9 判 reject）
    class _FakeTask:
        id = "task-fake-1"
    class _FakeSC:
        async def create_engineering_task(self, goal, creator=0):
            return _FakeTask()
    return EvolutionProposer(selfcode_manager=_FakeSC())


# ---------------------------------------------------------------------------
# 1. 过期依赖 → 生成待审提案 + 注册进 R9 审批门（pending_owner）
# ---------------------------------------------------------------------------
async def test_stale_dep_produces_pending_proposal(tmp_path, gate, proposer):
    req = _make_req(tmp_path, "requests==2.31.0\n")
    detect = am.requirements_detector(req, stale_floor={"requests": "2.32.0"})
    agent = am.MaintenanceAgent(proposer=proposer, gate=gate, detect_fn=detect)
    scan = await agent.run_maintenance()
    assert scan.findings_total == 1
    assert scan.proposed == 1
    mp = scan.proposals[0]
    assert mp.finding.pkg == "requests"
    assert mp.risk_level in ("low", "medium", "high")
    assert mp.proposal is not None
    # 已注册进 R9 审批门，状态为 pending_owner（fail-closed：未批准）
    assert gate.get(mp.proposal.id) is not None
    assert gate.get(mp.proposal.id).status == "proposed"


# ---------------------------------------------------------------------------
# 2. 非过期（精确锁定未低于下限）→ 跳过，不制造噪声提案
# ---------------------------------------------------------------------------
async def test_non_stale_skipped(tmp_path, gate, proposer):
    req = _make_req(tmp_path, "requests==2.31.0\n")
    detect = am.requirements_detector(req, stale_floor={"requests": "2.30.0"})  # 当前已高于下限
    agent = am.MaintenanceAgent(proposer=proposer, gate=gate, detect_fn=detect)
    scan = await agent.run_maintenance()
    assert scan.findings_total == 1
    assert scan.proposed == 0
    assert scan.skipped == 1
    assert "无需升级" in scan.proposals[0].skip_reason


# ---------------------------------------------------------------------------
# 3. P4 记忆护栏：命中 why-described「禁止自动改」→ 跳过
# ---------------------------------------------------------------------------
async def test_memory_guard_skips_forbidden(tmp_path, gate, proposer):
    req = _make_req(tmp_path, "cryptography==41.0.0\n")
    detect = am.requirements_detector(req, stale_floor={"cryptography": "42.0.0"})
    mem = lt.LongTermMemory(str(tmp_path / "data"))
    mem.record_why("cryptography", "安全核心，禁止自动改，必须人工审查")
    agent = am.MaintenanceAgent(proposer=proposer, gate=gate, memory=mem, detect_fn=detect)
    scan = await agent.run_maintenance()
    assert scan.skipped == 1
    assert "P4 记忆护栏" in scan.proposals[0].skip_reason
    # 不应生成任何提案
    assert scan.proposed == 0


# ---------------------------------------------------------------------------
# 4. CI 预检：失败 → risk=high；通过 → risk=low
# ---------------------------------------------------------------------------
async def test_ci_verify_failure_raises_risk(tmp_path, gate, proposer):
    req = _make_req(tmp_path, "numpy==1.26.0\n")
    detect = am.requirements_detector(req, stale_floor={"numpy": "2.0.0"})

    def ci_fail(f: am.DependencyFinding):
        return False, "CI 失败：import 不兼容"

    agent = am.MaintenanceAgent(proposer=proposer, gate=gate, detect_fn=detect, ci_verify_fn=ci_fail)
    scan = await agent.run_maintenance()
    assert scan.proposed == 1
    mp = scan.proposals[0]
    assert mp.ci_ok is False
    assert mp.risk_level == "high"


async def test_ci_verify_pass_low_risk(tmp_path, gate, proposer):
    req = _make_req(tmp_path, "numpy==1.26.0\n")
    detect = am.requirements_detector(req, stale_floor={"numpy": "2.0.0"})

    def ci_ok(f: am.DependencyFinding):
        return True, "CI 通过：全部测试绿"

    agent = am.MaintenanceAgent(proposer=proposer, gate=gate, detect_fn=detect, ci_verify_fn=ci_ok)
    scan = await agent.run_maintenance()
    mp = scan.proposals[0]
    assert mp.ci_ok is True
    assert mp.risk_level == "low"


# ---------------------------------------------------------------------------
# 5. fail-closed：检测器抛异常 → 空结果 + 诚实说明，不崩溃
# ---------------------------------------------------------------------------
async def test_detector_error_fail_closed(gate, proposer):
    def boom() -> list:
        raise RuntimeError("无法读取依赖清单")
    agent = am.MaintenanceAgent(proposer=proposer, gate=gate, detect_fn=boom)
    scan = await agent.run_maintenance()
    assert scan.findings_total == 0
    assert scan.proposed == 0
    assert "检测失败" in scan.note


# ---------------------------------------------------------------------------
# 6. 未配置检测器 → 诚实跳过，不产生提案
# ---------------------------------------------------------------------------
async def test_no_detector_honest(gate, proposer):
    agent = am.MaintenanceAgent(proposer=proposer, gate=gate)
    scan = await agent.run_maintenance()
    assert scan.findings_total == 0
    assert "未配置依赖检测器" in scan.note


# ---------------------------------------------------------------------------
# 7. 核心文件保护：依赖属 CORE_PROTECTED → 跳过（需人工 PR）
# ---------------------------------------------------------------------------
async def test_core_protected_dep_skipped(tmp_path, gate, proposer):
    from anima.agent.self_evolution import CORE_PROTECTED
    protected = next(iter(CORE_PROTECTED))  # 取一个真实受保护核心文件
    detect_fake = lambda: [am.DependencyFinding(pkg=protected,  # 命中 CORE_PROTECTED
                                                current="1.0", latest="2.0", stale=True)]
    agent = am.MaintenanceAgent(proposer=proposer, gate=gate, detect_fn=detect_fake)
    scan = await agent.run_maintenance()
    assert scan.skipped == 1
    assert "受保护核心" in scan.proposals[0].skip_reason
