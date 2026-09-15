"""B5-merge compat shim: implementation moved into anima.capability.staged_activation.

Single source = anima/capability/staged_activation.py.
"""
from anima.capability.staged_activation import *  # noqa: F401,F403


def __getattr__(name: str):
    return getattr(__import__("anima.capability.staged_activation", fromlist=[name]), name)
