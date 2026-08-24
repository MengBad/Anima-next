"""B5-merge compat shim: implementation moved into anima.capability.runtime_workflow_snapshot.

Single source = anima/capability/runtime_workflow_snapshot.py.
"""
from anima.capability.runtime_workflow_snapshot import *  # noqa: F401,F403


def __getattr__(name: str):
    return getattr(__import__("anima.capability.runtime_workflow_snapshot", fromlist=[name]), name)
