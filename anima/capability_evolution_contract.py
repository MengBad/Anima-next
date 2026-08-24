"""B5-merge compat shim: implementation moved into anima.capability.evolution_contract.

Single source = anima/capability/evolution_contract.py.
"""
from anima.capability.evolution_contract import *  # noqa: F401,F403


def __getattr__(name: str):
    return getattr(__import__("anima.capability.evolution_contract", fromlist=[name]), name)
