# -*- coding: utf-8 -*-
"""P1-B：send_file 对 caption 过内心独白过滤，整删不泄漏(空caption发文件)，并补出站审计。"""
import asyncio
from types import SimpleNamespace

import anima.core.gateway as gw_mod


class _Chan:
    def __init__(self):
        self.calls = 0; self.last = None
    async def send_file(self, **kw):
        self.calls += 1; self.last = kw
        return (42, {})


class _Audit:
    def __init__(self):
        self.events = []
    def emit(self, name, **kw):
        self.events.append((name, kw))


def _msg(caption="关注这个文件"):
    return SimpleNamespace(detail_type="private", target_id=7, message=caption,
                           extra={"file_path": "x.png", "name": "x.png"})


def _gw(chan, audit):
    g = object.__new__(gw_mod.Gateway)
    g.channel = chan
    g.core = SimpleNamespace(audit=audit)
    return g


def test_send_file_runs_caption_through_filter_and_audits(monkeypatch):
    recorded = []
    def _fake_filter(text):
        recorded.append(text)
        return text
    monkeypatch.setattr(gw_mod, "_filter_outbound_message", _fake_filter)
    chan, audit = _Chan(), _Audit()
    g = _gw(chan, audit)
    res = asyncio.run(g.send_file(_msg("关注这个文件")))
    assert recorded == ["关注这个文件"], "caption 必须过内心独白过滤"
    assert chan.last["caption"] == "关注这个文件"
    assert ("file_sent",) in [(e[0],) for e in audit.events]
    assert res == (42, {})


def test_send_file_fully_filtered_caption_does_not_leak(monkeypatch):
    monkeypatch.setattr(gw_mod, "_filter_outbound_message", lambda text: "")
    chan, audit = _Chan(), _Audit()
    g = _gw(chan, audit)
    res = asyncio.run(g.send_file(_msg("本喵对自己的内心独白")))
    assert chan.last["caption"] == "", "内部独白不得外泄"
    file_sent = [e for e in audit.events if e[0] == "file_sent"]
    assert file_sent and file_sent[0][1]["ok"] is True
    assert res == (42, {})
