# -*- coding: utf-8 -*-
"""B5 — 底座冻结核 (anti-entropy): 唯一源在场, 遗留重复层冻结, 防漂移。

新增顶层 anima/capability_*.py 或向 anima/capability_contracts 落文件即 CI 红(fail-closed),
承接「CI 永不自动改码/部署/绕过失败」。
"""
import glob
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FREEZE_TOP_CAPABILITY = frozenset({
    "capability_activation_contract.py", "capability_activation_proof.py",
    "capability_authorization_fact.py", "capability_evolution_contract.py",
    "capability_execution_dispatch.py", "capability_health_evidence_linker.py",
    "capability_intent_fact.py", "capability_runtime_consumption.py",
    "capability_runtime_evidence_integration.py", "capability_runtime_evidence_reconciliation.py",
    "capability_runtime_execution_ledger_reader.py", "capability_runtime_observation.py",
    "capability_runtime_observation_store.py", "capability_runtime_workflow_snapshot.py",
    "capability_staged_activation.py", "capability_suspend_revocation.py",
})


def _files(root: Path, pattern: str) -> set[str]:
    return {Path(p).name for p in glob.glob(str(root / pattern))}


def test_top_level_capability_layer_is_frozen():
    actual = _files(ROOT, "anima/capability_*.py")
    assert actual == set(FREEZE_TOP_CAPABILITY), ("顶层 capability_* 集合漂移! 新能力代码必须进 anima/capability/。",)


def test_capability_contracts_freeze_store_empty():
    store = ROOT / "anima" / "capability_contracts"
    if store.exists():
        assert (not any(store.rglob("*")) or all(
        p.suffix == ".pyc" or "__pycache__" in str(p) for p in store.rglob("*"))),\
        "anima/capability_contracts 是冻结空存储, 不得落文件。"


def test_canonical_capability_layer_present():
    core = ROOT / "anima" / "capability"
    for name in ("rbac.py", "scopes.py", "contract.py", "schema.py", "resolver.py",
                 "evidence.py", "isolation.py", "index.py"):
        assert (core / name).exists(), f"规范能力层缺 {name}"


def test_rfc0001_source_of_truth_present():
    assert (ROOT / "docs" / "rfc" / "RFC-0001-Capability.md").exists(), "RFC-0001 唯一源缺失"
