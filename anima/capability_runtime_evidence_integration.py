"""B5-merge compat shim: implementation moved into anima.capability.runtime_evidence_integration.

Single source = anima/capability/runtime_evidence_integration.py.
"""
from anima.capability.runtime_evidence_integration import *  # noqa: F401,F403


def __getattr__(name: str):
    return getattr(__import__("anima.capability.runtime_evidence_integration", fromlist=[name]), name)
