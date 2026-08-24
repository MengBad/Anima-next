"""B5-merge compat shim: implementation moved into anima.capability.authorization_fact.

Single source = anima/capability/authorization_fact.py.
"""
from anima.capability.authorization_fact import *  # noqa: F401,F403


def __getattr__(name: str):
    return getattr(__import__("anima.capability.authorization_fact", fromlist=[name]), name)
