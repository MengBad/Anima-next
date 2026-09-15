"""B5-merge 兼容 shim: 实现已移入 anima.capability.intent_fact。

单一源 = anima/capability/intent_fact.py; 此文件仅 re-export 以保持对旧导入
``from anima.capability_intent_fact import ...`` 的兼容。新增能力代码不得再摆顶layer。
"""
from anima.capability.intent_fact import *  # noqa: F401,F403


def __getattr__(name: str):
    return getattr(__import__("anima.capability.intent_fact", fromlist=[name]), name)


def __dir__():
    return sorted(set(globals()) | set(dir(__import__("anima.capability.intent_fact", fromlist=[""]))))
