# -*- coding: utf-8 -*-
"""M3 有界：webui cancel_agenda/agenda/agenda_pending 对可选子系统(agenda/intent_ledger)加 getattr 防护，
core 未挂时不得在操作后半程抛 500。"""
import asyncio
from types import SimpleNamespace

from anima.webui.server import WebUIServer


class _Agenda:
    def __init__(self):
        self.items = [SimpleNamespace(id="a1", intent_id="i1", status="pending")]
    def cancel(self, aid):
        return any(x.id == aid for x in self.items)
    def pending(self):
        return [x for x in self.items if x.status == "pending"]


def test_cancel_agenda_works_without_intent_ledger():
    srv = object.__new__(WebUIServer)
    srv.core = SimpleNamespace(agenda=_Agenda())   # 故意不挂 intent_ledger
    srv._owner_id = lambda: 1
    srv._plain = lambda item: {"id": item.id, "intent_id": item.intent_id}
    req = SimpleNamespace(match_info={"agenda_id": "a1"})
    resp = asyncio.run(srv.cancel_agenda(req))
    assert resp.status == 200
