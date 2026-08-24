# -*- coding: utf-8 -*-
"""B5-merge 折叠全量: 16 个顶层 capability_* 均已折叠进 anima/capability/, 顶层 re-export shim 同源。"""
import importlib

FOLDED = [
    "activation_contract", "activation_proof", "authorization_fact",
    "evolution_contract", "execution_dispatch", "health_evidence_linker",
    "intent_fact", "runtime_consumption", "runtime_evidence_integration",
    "runtime_evidence_reconciliation", "runtime_execution_ledger_reader",
    "runtime_observation", "runtime_observation_store",
    "runtime_workflow_snapshot", "staged_activation", "suspend_revocation",
]

def test_each_folded_importable_via_both_paths():
    for n in FOLDED:
        canonical = importlib.import_module("anima.capability." + n)
        shim = importlib.import_module("anima.capability_" + n)
        assert shim.__doc__, (n, "shim missing doc")
        assert canonical.__name__ == "anima.capability." + n

def test_shim_is_reexport_of_canonical():
    """顶层 shim 是同源 re-export: 指向 canonical 的实现, 非自含副本。"""
    import pathlib
    for n in FOLDED:
        src = pathlib.Path("anima/capability_" + n + ".py").read_text(encoding="utf-8")
        assert "from anima.capability." + n + " import *" in src, (n, "not a re-export shim")

def test_all_16_top_level_still_present():
    """冻结核: 顶层 16 个 shim 仍在(集合不被削减/新增)。"""
    import pathlib
    present = {p.stem for p in pathlib.Path("anima").glob("capability_*.py")}
    assert present == {"capability_" + n for n in FOLDED}
