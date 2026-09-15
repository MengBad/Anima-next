"""B5-merge compat shim: implementation moved into anima.capability.activation_contract.

Single source = anima/capability/activation_contract.py.
"""
from anima.capability.activation_contract import *  # noqa: F401,F403


def __getattr__(name: str):
    return getattr(__import__("anima.capability.activation_contract", fromlist=[name]), name)
