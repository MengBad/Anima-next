"""B5-merge compat shim: implementation moved into anima.capability.runtime_execution_ledger_reader.

Single source = anima/capability/runtime_execution_ledger_reader.py.
"""
from anima.capability.runtime_execution_ledger_reader import *  # noqa: F401,F403


def __getattr__(name: str):
    return getattr(__import__("anima.capability.runtime_execution_ledger_reader", fromlist=[name]), name)
