"""B5-merge compat shim: implementation moved into anima.capability.health_evidence_linker.

Single source = anima/capability/health_evidence_linker.py.
"""
from anima.capability.health_evidence_linker import *  # noqa: F401,F403


def __getattr__(name: str):
    return getattr(__import__("anima.capability.health_evidence_linker", fromlist=[name]), name)
