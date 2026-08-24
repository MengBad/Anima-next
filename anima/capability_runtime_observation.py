"""B5-merge compat shim: implementation moved into anima.capability.runtime_observation.

Single source = anima/capability/runtime_observation.py.
"""
from anima.capability.runtime_observation import *  # noqa: F401,F403


def __getattr__(name: str):
    return getattr(__import__("anima.capability.runtime_observation", fromlist=[name]), name)
