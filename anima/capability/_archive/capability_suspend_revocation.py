"""B5-merge compat shim: implementation moved into anima.capability.suspend_revocation.

Single source = anima/capability/suspend_revocation.py.
"""
from anima.capability.suspend_revocation import *  # noqa: F401,F403


def __getattr__(name: str):
    return getattr(__import__("anima.capability.suspend_revocation", fromlist=[name]), name)
