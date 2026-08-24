# -*- coding: utf-8 -*-
"""B5-merge — 首折叠 intent_fact: 规范层与顶层 shim 同一单源。"""
import importlib
import inspect

def test_both_import_paths_expose_same_intent_fact():
    canonical = importlib.import_module("anima.capability.intent_fact")
    shim = importlib.import_module("anima.capability_intent_fact")
    assert canonical.IntentFact is shim.IntentFact
    assert shim.__doc__

def test_legacy_from_import_works():
    from anima.capability_intent_fact import IntentFact
    assert inspect.isclass(IntentFact)
