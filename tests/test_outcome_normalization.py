"""F8.11 —— Execution Outcome Normalization：raw success → NormalizedExecutionOutcome。

把「handler 正常返回」与「执行真实成功」解耦（Q1 裁定：ProviderResult.success 降级为
transport/execution surface 字段，canonical governance input = NormalizedExecutionOutcome）：

    ProviderResult（data/success/error/metadata）
            ↓
    OutcomeNormalizer（纯投影，非 policy engine，Q3）
            ↓
    NormalizedExecutionOutcome（SUCCESS / FAILURE / UNKNOWN）
            ↓
    HealthEvidence.metadata（normalized_outcome + error_class，Q5）

Q2：governance 分类 adapter 只处理**已知安全 failure pattern**（小注册表），未知 →
UNKNOWN——**不复制** tools 层 marker 系统（避免双注册表长期漂移），也**不 import**
tools（R1 + import 纪律：health 包零 runtime/tools 依赖）。

红线（本文件测试钉死）：
- R1：不改 tools.normalize_tool_result / _TOOL_FAILURE_PREFIXES / _TOOL_INTERNAL_MARKERS
  （模型面行为冻结）；
- R2：normalize 是纯函数——输入 ProviderResult 不被变异，分类结果绝不反向影响
  execution（方向只能 execution → classification → evidence）；
- R3：UNKNOWN 保留——不为了 Recovery 方便把未知强行归失败。

RED 首测（用户谓词）：

    ProviderResult(success=True)
    + failure-like payload（"搜索时出错了：TimeoutError"）
    → NormalizedOutcome.FAILURE

证明当前 gap：raw success != normalized success（F8.10.1 边界诚实下 success 恒 True，
只有归一化能暴露 search.web 的失败形态）。
"""
from __future__ import annotations

import subprocess
import sys

import pytest

from anima.capability.governance.audit import InMemoryAuditSink
from anima.runtime.providers.base import ProviderResult
from anima.tools import (
    LEVEL_USER,
    create_default_registry,
)

FS_READ = "filesystem.read"
ALICE = "user:alice"


class _CaptureSink:
    def __init__(self) -> None:
        self.evidences = []

    def append(self, evidence) -> None:
        self.evidences.append(evidence)


def _governed_registry(monkeypatch, tmp_path, audit=None, health_sink=None, web_stub=None):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    if web_stub is not None:
        monkeypatch.setattr("anima.tools.builtin.tool_web_search", web_stub)
    return create_default_registry(
        None,
        capability_mode=True,
        pilot_capability_allowlist=[ALICE],
        capability_gateway_mode=True,
        governance_audit_sink=audit,
        health_evidence_sink=health_sink,
    )


def _web_failure_stub(query: str, num_results: int = 5) -> str:
    """镜像真实 handler 吞异常语义（web.py:202-204）。"""
    try:
        raise TimeoutError("network blip")
    except Exception as exc:
        return f"搜索时出错了：{type(exc).__name__}"


# ---------------------------------------------------------------------------
# RED 首测：raw success != normalized success（用户谓词）
# ---------------------------------------------------------------------------
def test_red_raw_success_vs_normalized_failure():
    """ProviderResult(success=True) + failure-like payload → FAILURE + error_class。
    证明当前 gap：边界成功 ≠ 执行成功（归一化前的 success 恒 True 会漏掉该失败）。"""
    from anima.capability.health.normalizer import (
        NormalizedExecutionOutcome,
        OutcomeNormalizer,
    )

    pr = ProviderResult(data="搜索时出错了：TimeoutError", success=True)
    outcome, error_class = OutcomeNormalizer.normalize(pr)
    assert outcome is NormalizedExecutionOutcome.FAILURE
    assert error_class == "TimeoutError"
    # gap 实证：raw success=True 但 normalized=FAILURE
    assert pr.success is True
    assert outcome is not NormalizedExecutionOutcome.SUCCESS


# ---------------------------------------------------------------------------
# SUCCESS 用例
# ---------------------------------------------------------------------------
def test_normalizer_success_cases():
    from anima.capability.health.normalizer import (
        NormalizedExecutionOutcome,
        OutcomeNormalizer,
    )

    # 1. filesystem.read 正常内容
    pr = ProviderResult(data="hello world", success=True)
    assert OutcomeNormalizer.normalize(pr)[0] is NormalizedExecutionOutcome.SUCCESS
    # 3. 普通文本
    assert OutcomeNormalizer.normalize_text("这是普通搜索结果文本")[0] is NormalizedExecutionOutcome.SUCCESS
    # 成功路径 error_class 为 None
    assert OutcomeNormalizer.normalize(pr)[1] is None


# ---------------------------------------------------------------------------
# FAILURE 用例（Q2：已知安全 failure pattern）
# ---------------------------------------------------------------------------
def test_normalizer_failure_cases():
    from anima.capability.health.normalizer import (
        NormalizedExecutionOutcome,
        OutcomeNormalizer,
    )

    # 2. search.web timeout 吞异常串
    out, err = OutcomeNormalizer.normalize(
        ProviderResult(data="搜索时出错了：TimeoutError", success=True)
    )
    assert out is NormalizedExecutionOutcome.FAILURE and err == "TimeoutError"
    # _SAFE_TOOL_FAILURE 形态（read/write 失败被归一为安全失败串）
    assert OutcomeNormalizer.normalize_text("工具暂时不可用，未获得可验证结果。")[0] is NormalizedExecutionOutcome.FAILURE
    # "错误:" 前缀
    assert OutcomeNormalizer.normalize_text("错误: 权限不足")[0] is NormalizedExecutionOutcome.FAILURE
    # provider 显式 error（未来路径：ProviderResult.error 非空 → 权威 FAILURE）
    assert OutcomeNormalizer.normalize(ProviderResult(data="x", success=False, error="TimeoutError"))[0] \
        is NormalizedExecutionOutcome.FAILURE


# ---------------------------------------------------------------------------
# UNKNOWN 保留（R3）
# ---------------------------------------------------------------------------
def test_unknown_preserved_not_forced_to_failure():
    from anima.capability.health.normalizer import (
        NormalizedExecutionOutcome,
        OutcomeNormalizer,
    )

    # 4. 未知异常文本（Traceback 形态）→ UNKNOWN，绝不强行归 FAILURE
    out, err = OutcomeNormalizer.normalize_text(
        "Traceback (most recent call last):\n  File \"x.py\", line 1"
    )
    assert out is NormalizedExecutionOutcome.UNKNOWN
    assert err is None  # 未知不伪造 error_class


# ---------------------------------------------------------------------------
# R2：纯投影 —— 输入不被变异、分类不反向影响执行
# ---------------------------------------------------------------------------
def test_r2_pure_projection_no_feedback_loop():
    from anima.capability.health.normalizer import OutcomeNormalizer

    pr = ProviderResult(data="搜索时出错了：TimeoutError", success=True, error="", metadata={"k": "v"})
    snapshot = (pr.data, pr.success, pr.error, dict(pr.metadata))
    OutcomeNormalizer.normalize(pr)
    assert (pr.data, pr.success, pr.error) == (snapshot[0], snapshot[1], snapshot[2])
    assert pr.metadata == snapshot[3]  # 不被注入任何字段（R2：不反向影响）


# ---------------------------------------------------------------------------
# 集成：emission 把 normalized_outcome 投影进 evidence.metadata（Q5）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_emission_writes_normalized_outcome_to_metadata(monkeypatch, tmp_path):
    """真实 governed 调用：成功路径 evidence.metadata["normalized_outcome"]=="success"；
    search.web 吞异常路径 ==failure"（归一化在发射点完成，结果进 metadata，不存
    result_fragment）。"""
    from anima.capability.health.normalizer import NormalizedExecutionOutcome

    # 成功：filesystem.read 正常读（先建 data 目录——write_text 不建父目录）
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "hello.txt").write_text("hello world", encoding="utf-8")
    cap = _CaptureSink()
    reg = _governed_registry(monkeypatch, tmp_path, health_sink=cap)
    await reg.call_capability(FS_READ, {"filename": "hello.txt"},
                              permission_level=LEVEL_USER, caller=ALICE)
    ev = cap.evidences[0]
    assert ev.metadata.get("normalized_outcome") == NormalizedExecutionOutcome.SUCCESS.value
    assert "result_fragment" not in ev.metadata  # Q5：不存原始结果文本
    assert "error_class" not in ev.metadata  # 成功无 error_class

    # 失败：search.web 吞异常（stub）→ FAILURE + error_class
    cap2 = _CaptureSink()
    reg2 = _governed_registry(monkeypatch, tmp_path, health_sink=cap2, web_stub=_web_failure_stub)
    await reg2.call_capability("search.web", {"query": "x"},
                               permission_level=LEVEL_USER, caller=ALICE)
    ev2 = cap2.evidences[0]
    assert ev2.metadata.get("normalized_outcome") == NormalizedExecutionOutcome.FAILURE.value
    assert ev2.metadata.get("error_class") == "TimeoutError"
    # 边界事实保留：success 仍为 presult.success（transport 语义），归一化在 metadata
    assert ev2.success is True


# ---------------------------------------------------------------------------
# import 纪律：normalizer 自包含，health 包不拉 runtime/tools（R1 依赖面）
# ---------------------------------------------------------------------------
def test_import_discipline_no_runtime_tools_leak():
    """import anima.capability.health 后 anima.runtime / anima.tools 不入 sys.modules
    （normalizer 用鸭子类型 getattr，零 runtime/tools import——R1 依赖面冻结）。"""
    code = (
        "import sys\n"
        "import anima.capability.health\n"
        "leaked = [m for m in ('anima.runtime', 'anima.tools') if m in sys.modules]\n"
        "if leaked:\n"
        "    print('LEAKED:', leaked)\n"
        "    sys.exit(1)\n"
        "print('OK')\n"
    )
    import pathlib
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True,
        cwd=str(pathlib.Path(__file__).resolve().parents[1]),
    )
    assert result.returncode == 0, f"import 泄漏 runtime/tools: {result.stdout} {result.stderr}"
