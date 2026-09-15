"""B5-merge compat shim: implementation moved into anima.capability.execution_dispatch.

Single source = anima/capability/execution_dispatch.py.
"""
from anima.capability.execution_dispatch import *  # noqa: F401,F403


def __getattr__(name: str):
    return getattr(__import__("anima.capability.execution_dispatch", fromlist=[name]), name)
