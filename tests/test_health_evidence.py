"""F8.10 —— Health Evidence：runtime evidence → governance adapter → persistent store。

单向投影链（用户 Q1-C 裁定 + Implementation Authorization）：

    runtime evidence (ExecutionObservation / ProviderResult / resolution metadata)
            ↓
    governance evidence adapter (EvidenceAdapter)
            ↓
    HealthEvidenceSink → HealthEvidenceStore (JSONL)
            ↓
    (未来) HealthObserver → Lifecycle

本 slice 只建 **health evidence 事实层**：model + adapter + store + sink seam。
**不接线生产发射点**（tools/__init__.py governed route 保持不动——发射接线是独立
slice）、**不实现 policy**（health_score / quarantine_decision / recovery_decision
不入模型，Q3）、**不改 runtime**（ExecutionObservation / execution_ledger / resolver /
provider contract / audit 全部冻结）。

Q4 核心语义（钉死）：**selected ≠ success**——
- ``selected``：resolution 层选了一个 provider（来自 resolution metadata）；
- ``executed``：执行被尝试（产生了 observation）；
- ``success``：**从 observation.success 读取**，**绝不**从 ``ResolutionOutcome.SELECTED``
  推导。search.web 失败形态（handler 吞异常）投影为
  ``selected=True / executed=True / success=False``，而非 ``selected=True, error=None``。

RED 首测（用户谓词）：

    fake provider execution → produce ExecutionObservation → adapter 投影
    → reopen store → query 命中

当前 ``anima.capability.health`` 不存在 → ``ModuleNotFoundError``。

GREEN 后证明：Execution fact → HealthEvidence → Persistent store 闭环 + fail-open。
"""
from __future__ import annotations

import subprocess
import sys
import uuid

import pytest

from anima.runtime.executor import ExecutionObservation


def _mk_observation(
    *,
    success: bool,
    error: str = "",
    task_id: str = "task-1",
    metadata: dict | None = None,
) -> ExecutionObservation:
    return ExecutionObservation(
        task_id=task_id,
        success=success,
        result={} if success else {},
        error=error,
        metadata=metadata or {},
    )


def _mk_store(tmp_path, name: str = "health_evidence.jsonl"):
    from anima.capability.health.store import HealthEvidenceStore

    return HealthEvidenceStore(str(tmp_path / name))


# ---------------------------------------------------------------------------
# RED 首测：闭环 —— Execution fact → HealthEvidence → Persistent store → query
# ---------------------------------------------------------------------------
def test_evidence_chain_persists_and_queries(tmp_path):
    """闭环：fake provider execution 产出的 ExecutionObservation 经 adapter 投影为
    HealthEvidence，写入 store；**重新打开**（新实例同路径）后 query 命中。"""
    from anima.capability.health.adapter import EvidenceAdapter
    from anima.capability.health.store import HealthEvidenceStore

    store = _mk_store(tmp_path)
    adapter = EvidenceAdapter(sink=store)

    # 成功执行事实（fake provider → ExecutionObservation）
    obs = _mk_observation(success=True, task_id="t1")
    ev = adapter.record(
        obs,
        capability_id="search.web",
        provider_id="web_search",
        execution_key=("run-1", "t1", 0),
        selected=True,
    )
    assert ev.success is True
    assert ev.selected is True
    assert ev.executed is True
    assert ev.error_type == ""
    assert ev.capability_id == "search.web"

    # 失败执行事实（search.web 形态：selected 但 execution 失败）
    obs_fail = _mk_observation(
        success=False, error="搜索时出错了：TimeoutError", task_id="t2"
    )
    ev_fail = adapter.record(
        obs_fail,
        capability_id="search.web",
        provider_id="web_search",
        execution_key=("run-1", "t2", 0),
        selected=True,
    )
    assert ev_fail.success is False
    assert ev_fail.selected is True
    assert ev_fail.executed is True

    # reopen（新实例，同一路径）→ query 命中
    store2 = HealthEvidenceStore(str(tmp_path / "health_evidence.jsonl"))
    hits = store2.query(capability_id="search.web")
    assert len(hits) == 2
    assert all(e.capability_id == "search.web" for e in hits)
    ok = store2.query(capability_id="search.web", success=True)
    bad = store2.query(capability_id="search.web", success=False)
    assert len(ok) == 1 and ok[0].execution_id == str(("run-1", "t1", 0))
    assert len(bad) == 1 and bad[0].execution_id == str(("run-1", "t2", 0))


# ---------------------------------------------------------------------------
# Q4：selected ≠ success —— 成败必须来自 observation，绝不从 SELECTED 推导
# ---------------------------------------------------------------------------
def test_q4_selected_is_not_success(tmp_path):
    """Q4 钉死：adapter 的 success 来自 observation.success；selected=True 的调用
    失败时投影为 success=False（search.web 吞异常形态），而不是错误地被当成成功。"""
    from anima.capability.health.adapter import EvidenceAdapter

    store = _mk_store(tmp_path)
    adapter = EvidenceAdapter(sink=store)

    # 形态 A：selected + executed + 失败（search.web 失败：handler 吞异常）
    ev_a = adapter.record(
        _mk_observation(success=False, error="搜索时出错了：TimeoutError", task_id="sw"),
        capability_id="search.web", provider_id="web_search",
        execution_key=("r", "sw", 0), selected=True,
    )
    assert (ev_a.selected, ev_a.executed, ev_a.success) == (True, True, False)
    assert "TimeoutError" in ev_a.error_type

    # 形态 B：selected + executed + 成功
    ev_b = adapter.record(
        _mk_observation(success=True, task_id="ok"),
        capability_id="search.web", provider_id="web_search",
        execution_key=("r", "ok", 0), selected=True,
    )
    assert (ev_b.selected, ev_b.executed, ev_b.success) == (True, True, True)

    # 形态 C：resolution 未选中（NO_PROVIDER / UNAVAILABLE）→ selected=False，无执行
    ev_c = adapter.record(
        None,  # 无 observation —— 执行未尝试
        capability_id="filesystem.write", provider_id=None,
        execution_key=("r", "np", 0), selected=False,
    )
    assert (ev_c.selected, ev_c.executed, ev_c.success) == (False, False, False)
    assert ev_c.error_type == ""
    assert ev_c.provider_id is None


# ---------------------------------------------------------------------------
# Q3：字段契约 —— 十一字段，无 policy 字段
# ---------------------------------------------------------------------------
def test_q3_evidence_field_contract():
    """Q3：HealthEvidence 恰好含 evidence_id/capability_id/provider_id/execution_id/
    observed_at/selected/executed/success/error_type/duration_ms/metadata；
    **不得**含 health_score / quarantine_decision / recovery_decision（属 policy）。"""
    import dataclasses

    from anima.capability.health.model import HealthEvidence

    fields = {f.name for f in dataclasses.fields(HealthEvidence)}
    assert fields == {
        "evidence_id", "capability_id", "provider_id", "execution_id",
        "observed_at", "selected", "executed", "success",
        "error_type", "duration_ms", "metadata",
    }, f"HealthEvidence 字段集不符合 Q3 契约: {fields}"
    assert "health_score" not in fields
    assert "quarantine_decision" not in fields
    assert "recovery_decision" not in fields


# ---------------------------------------------------------------------------
# fail-open：sink 抛错不阻断，计数
# ---------------------------------------------------------------------------
def test_fail_open_sink_failure(tmp_path):
    """fail-open（F8.8 红线延续）：sink.append 抛 IO 错误时，adapter.record 不抛、
    返回证据、failures 计数 +1（持久化失败 ≠ 事实不存在）。"""
    from anima.capability.health.adapter import EvidenceAdapter

    class _BrokenSink:
        failures_called = 0

        def append(self, evidence) -> None:
            self.failures_called += 1
            raise OSError("disk full")

    sink = _BrokenSink()
    adapter = EvidenceAdapter(sink=sink)
    ev = adapter.record(
        _mk_observation(success=True, task_id="x"),
        capability_id="code.execute", provider_id="code_exec",
        execution_key=("r", "x", 0), selected=True,
    )
    assert ev is not None
    assert ev.success is True
    assert adapter.failures == 1
    assert sink.failures_called == 1


# ---------------------------------------------------------------------------
# query 过滤 + reopen roundtrip
# ---------------------------------------------------------------------------
def test_query_filters_and_roundtrip(tmp_path):
    from anima.capability.health.adapter import EvidenceAdapter
    from anima.capability.health.store import HealthEvidenceStore

    store = _mk_store(tmp_path)
    adapter = EvidenceAdapter(sink=store)
    for i in range(3):
        adapter.record(
            _mk_observation(success=(i % 2 == 0), task_id=f"t{i}"),
            capability_id="search.web", provider_id="web_search",
            execution_key=("r", f"t{i}", 0), selected=True,
        )
    adapter.record(
        _mk_observation(success=True, task_id="other"),
        capability_id="filesystem.read", provider_id="read_file",
        execution_key=("r", "other", 0), selected=True,
    )

    store2 = HealthEvidenceStore(str(tmp_path / "health_evidence.jsonl"))
    # capability_id 过滤
    assert len(store2.query(capability_id="search.web")) == 3
    assert len(store2.query(capability_id="filesystem.read")) == 1
    # provider_id 过滤
    assert len(store2.query(provider_id="web_search")) == 3
    # success 过滤（t0/t2/other 成功 = 3；t1 失败 = 1）
    assert len(store2.query(success=True)) == 3
    assert len(store2.query(success=False)) == 1
    # 时间窗过滤（observed_at 单调）
    all_ev = store2.query()
    t_mid = all_ev[1].observed_at
    assert len(store2.query(start=t_mid)) >= 3
    assert len(store2.query(end=t_mid)) >= 2
    # evidence_id 唯一
    ids = [e.evidence_id for e in all_ev]
    assert len(set(ids)) == len(ids)


# ---------------------------------------------------------------------------
# import 纪律：health 包不拉 runtime/tools（鸭子类型 observation，惰性 import）
# ---------------------------------------------------------------------------
def test_import_discipline_no_runtime_leak():
    """import anima.capability.health 后，anima.runtime / anima.tools 不得进入
    sys.modules（子进程隔离验证——模型/adapter/store 不 import runtime/tools）。"""
    code = (
        "import sys\n"
        "import anima.capability.health\n"
        "leaked = [m for m in ('anima.runtime', 'anima.tools', 'anima.capability.audit', 'anima.capability.ops') if m in sys.modules]\n"
        "if leaked:\n"
        "    print('LEAKED:', leaked)\n"
        "    sys.exit(1)\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, cwd=str(__import__("pathlib").Path(__file__).resolve().parents[1]),
    )
    assert result.returncode == 0, f"import 泄漏 runtime/tools: {result.stdout} {result.stderr}"
