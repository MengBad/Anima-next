"""Phase F4.1 — Runtime Persistence primitive tests (anima/runtime/persistence.py).

Validates the F3 Persistence Boundary as implemented:
1. WorkflowRun round-trip — every field survives save -> load intact.
2. TaskState snapshot fidelity — per-Task status/attempts/result/error preserved, and
   `result` is deep-copied (loaded run never aliases the serialized dict).
3. Enum + nested PlanRef restoration — RunStatus / TaskRunStatus values and the frozen
   PlanRef handle round-trip correctly.
4. Persistence is NOT a second state machine — the store has no lifecycle mutation API:
   no status write, no _transition, no update_status/complete/transition (AST guard).
5. load of a missing snapshot raises KeyError; exists() probes without loading.
6. save actually writes a durable file (via PersistenceProvider, atomic write).
"""
from __future__ import annotations

import ast
import pathlib
import tempfile

from anima.constitution.state import (
    PlanRef,
    RunStatus,
    TaskRunStatus,
    TaskState,
    WorkflowRun,
)
from anima.core.persistence import PersistenceProvider
from anima.runtime.persistence import WorkflowRunStore

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _sample_run() -> WorkflowRun:
    return WorkflowRun(
        id="run#f4-1",
        contract_version="1.0.0",
        plan_ref=PlanRef(plan_id="plan#understand-X", contract_version="1.0.0"),
        goal_ref="goal#understand-X",
        status=RunStatus.SUSPENDED,  # non-trivial status: exercises enum round-trip
        task_states=[
            TaskState(
                task_id="t1",
                status=TaskRunStatus.DONE,
                attempts=2,
                result={"read": "X", "nested": {"k": [1, 2]}},
                error="",
            ),
            TaskState(
                task_id="t2",
                status=TaskRunStatus.PENDING,
                attempts=0,
                result={},
                error="",
            ),
        ],
        current_task_id="t2",
        checkpoint_ref="t1",
        retry_policy={"max_retries": 3},
        schedule_ref="sched#1",
        budget_ref="budget#1",
        context_ref="ctx#understand-X",
        created_at="2026-08-08T00:00:00+00:00",
        completed_at="",
    )


def _fresh_store(tmp_dir: str) -> WorkflowRunStore:
    return WorkflowRunStore(PersistenceProvider(data_dir=tmp_dir))


def test_workflow_run_round_trip_preserves_all_fields():
    """F4.1-1: save -> load restores every field of a WorkflowRun."""
    with tempfile.TemporaryDirectory() as td:
        store = _fresh_store(td)
        run = _sample_run()
        store.save(run)
        restored = store.load(run.id)

    assert restored.id == run.id
    assert restored.contract_version == run.contract_version
    assert restored.plan_ref.plan_id == run.plan_ref.plan_id
    assert restored.plan_ref.contract_version == run.plan_ref.contract_version
    assert restored.goal_ref == run.goal_ref
    assert restored.status is RunStatus.SUSPENDED
    assert restored.current_task_id == run.current_task_id
    assert restored.checkpoint_ref == run.checkpoint_ref
    assert restored.retry_policy == run.retry_policy
    assert restored.schedule_ref == run.schedule_ref
    assert restored.budget_ref == run.budget_ref
    assert restored.context_ref == run.context_ref
    assert restored.created_at == run.created_at
    assert restored.completed_at == run.completed_at


def test_task_state_snapshot_fidelity():
    """F4.1-2: per-Task status/attempts/result/error survive; result is deep-copied."""
    with tempfile.TemporaryDirectory() as td:
        store = _fresh_store(td)
        run = _sample_run()
        store.save(run)
        restored = store.load(run.id)

    t1 = restored.task_states[0]
    assert t1.task_id == "t1"
    assert t1.status is TaskRunStatus.DONE
    assert t1.attempts == 2
    assert t1.result == {"read": "X", "nested": {"k": [1, 2]}}
    assert t1.error == ""
    assert restored.task_states[1].status is TaskRunStatus.PENDING

    # deep copy: mutating the restored run's payload must not affect a second load
    with tempfile.TemporaryDirectory() as td2:
        store2 = _fresh_store(td2)
        store2.save(run)
        first = store2.load(run.id)
        first.task_states[0].result["read"] = "MUTATED"
        second = store2.load(run.id)
        assert second.task_states[0].result["read"] == "X"


def test_status_enums_and_plan_ref_restored():
    """F4.1-3: RunStatus / TaskRunStatus enums and the frozen PlanRef handle restore."""
    with tempfile.TemporaryDirectory() as td:
        store = _fresh_store(td)
        run = _sample_run()
        store.save(run)
        restored = store.load(run.id)

    assert isinstance(restored.status, RunStatus)
    assert isinstance(restored.task_states[0].status, TaskRunStatus)
    assert isinstance(restored.plan_ref, PlanRef)


def test_persistence_has_no_lifecycle_mutation_api():
    """F4.1-3: the store is a snapshot primitive, NOT a second state machine.

    Mechanical guard over the executable code (docstrings stripped): no `.status =`
    assignment, no `_transition`, no update_status/complete/transition methods, no
    import of the WorkflowInterface (it must not reach lifecycle internals).
    """
    src = (REPO_ROOT / "anima" / "runtime" / "persistence.py").read_text(encoding="utf-8")

    # the class surface offers only snapshot ops — no lifecycle-mutating verbs
    import re

    public = {
        name
        for name in re.findall(r"def (\w+)\(", src)
        if not name.startswith("_")
    }
    assert public <= {"save", "load", "exists"}, f"unexpected public API: {public}"
    for forbidden in ("update_status", "complete", "transition"):
        assert forbidden not in public

    # executable code (docstrings stripped) has no status write and no lifecycle call
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
            if ast.get_docstring(node) is not None:
                node.body = node.body[1:]
    executable = ast.unparse(tree)
    assert ".status = " not in executable
    assert "_transition" not in executable
    assert "run.status = " not in executable
    # no reference to the WorkflowInterface type or its registry internals
    # (the file path "workflow_runs/..." legitimately contains the substring, so the
    # guard targets the type name, not the word)
    assert "WorkflowInterface" not in executable
    assert "_registry" not in executable


def test_load_missing_snapshot_raises_keyerror():
    """F4.1-4: loading a never-persisted run raises KeyError; exists() probes cleanly."""
    with tempfile.TemporaryDirectory() as td:
        store = _fresh_store(td)
        assert store.exists("run#ghost") is False
        try:
            store.load("run#ghost")
        except KeyError as exc:
            assert "run#ghost" in str(exc)
        else:  # pragma: no cover - failure path guard
            raise AssertionError("load of a missing snapshot must raise KeyError")


def test_save_writes_a_durable_file():
    """F4.1-5: save() persists a real JSON file under the store's directory."""
    with tempfile.TemporaryDirectory() as td:
        store = _fresh_store(td)
        store.save(_sample_run())

        target = pathlib.Path(td) / "workflow_runs" / "run#f4-1.json"
        assert target.exists()
        import json

        raw = json.loads(target.read_text(encoding="utf-8"))
        assert raw["id"] == "run#f4-1"
        assert raw["status"] == "suspended"
        assert raw["task_states"][0]["task_id"] == "t1"
