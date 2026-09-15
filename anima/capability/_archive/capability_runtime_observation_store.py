"""B5-merge compat shim: implementation moved into anima.capability.runtime_observation_store.

Single source = anima/capability/runtime_observation_store.py.
"""
from anima.capability.runtime_observation_store import *  # noqa: F401,F403


def __getattr__(name: str):
    return getattr(__import__("anima.capability.runtime_observation_store", fromlist=[name]), name)
