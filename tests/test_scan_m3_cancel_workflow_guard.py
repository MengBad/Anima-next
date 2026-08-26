# -*- coding: utf-8 -*-
"""M3 有界片段：cancel_workflow 对 core.intent_ledger 用 getattr 防护，
core 未挂 ledger 时也不在取消后抛 500(半完成状态)。"""
import asyncio
from types import SimpleNamespace

from anima.webui.server import WebUIServer


class _Wf:
    def __init__(self):
        self.ins = {}
    def cancel(self, wid):
        return True
    @property
    def items(self):
        return self.ins


def test_cancel_workflow_safe_without_intent_ledger():
    wf = _Wf(); wf.ins["wf1"] = SimpleNamespace(intent_id="i1", status="running")
    core = SimpleNamespace(workflows=wf)          # 故意不挂 intent_ledger
    srv = object.__new__(WebUIServer)
    srv.core = core
    srv._owner_id = lambda: 1
    srv._plain = lambda item: {"id": "wf1", "intent_id": "i1"}

    req = SimpleNamespace(match_info={"workflow_id": "wf1"})
    resp = asyncio.run(srv.cancel_workflow(req))
    assert resp.status == 200
