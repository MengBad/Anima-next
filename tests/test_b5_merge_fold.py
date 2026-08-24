# -*- coding: utf-8 -*-
"""B5-merge 折叠批: 规范层与顶层 shim 同源可导入。"""
import importlib

FOLDED = ["intent_fact", "evolution_contract", "runtime_execution_ledger_reader",
          "runtime_observation_store", "runtime_workflow_snapshot",
          "runtime_consumption", "runtime_evidence_reconciliation"]

def test_each_folded_importable_via_both_paths():
    for n in FOLDED:
        canonical = importlib.import_module("anima.capability." + n)
        shim = importlib.import_module("anima.capability_" + n)
        assert shim.__doc__, (n, "shim missing doc")
        assert canonical.__name__ == "anima.capability." + n

def test_shim_exposes_canonical_public_names():
    src = importlib.import_module("anima.capability.runtime_consumption")
    shim = importlib.import_module("anima.capability_runtime_consumption")
    pub = [k for k in dir(src) if not k.startswith("_")]
    assert pub, "no public names in runtime_consumption"
    for k in pub[:3]:
        assert getattr(shim, k) is getattr(src, k), k
