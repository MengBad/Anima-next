"""B5-merge compat shim: implementation moved into anima.capability.runtime_consumption.

Single source = anima/capability/runtime_consumption.py.
"""
from anima.capability.runtime_consumption import *  # noqa: F401,F403


def __getattr__(name: str):
    return getattr(__import__("anima.capability.runtime_consumption", fromlist=[name]), name)
