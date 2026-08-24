"""B5-merge compat shim: implementation moved into anima.capability.activation_proof.

Single source = anima/capability/activation_proof.py.
"""
from anima.capability.activation_proof import *  # noqa: F401,F403


def __getattr__(name: str):
    return getattr(__import__("anima.capability.activation_proof", fromlist=[name]), name)
