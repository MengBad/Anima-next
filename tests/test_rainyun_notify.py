"""雨云签到结果通知（2026-10-30 owner 要求）。

历史问题：成功是静默的，只有失败才通知本人。
生产实证 10-30：2 个账号全部签到成功（成功 2 / 失败 0 / 跳过 0），
owner 和绑定者**都**没收到任何通知 —— 只能靠查日志才知道签成了。

现在：成功、失败都各自通知绑定者本人；不再发 owner 汇总（避免重复）。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest


class _Gateway:
    def __init__(self):
        self.sent = []

    async def send(self, msg):
        self.sent.append((msg.target_id, msg.message))


class _Perms:
    owners = [1562290139]


class _Cfg:
    permissions = _Perms()


class _Host:
    """最小宿主：只借真正要测的那两个方法。"""

    def __init__(self):
        self.core = SimpleNamespace(config=_Cfg(), gateway=_Gateway())


def _call(host, name, *args):
    from anima.core.autonomy import Heartbeat
    return getattr(Heartbeat, name)(host, *args)


def test_success_notifies_bound_user():
    host = _Host()
    asyncio.run(_call(host, "_notify_rainyun_success", "999888777", {"ok": True}))
    assert len(host.core.gateway.sent) == 1
    uid, body = host.core.gateway.sent[0]
    assert uid == 999888777
    assert "签到成功" in body


def test_success_includes_points_when_present():
    host = _Host()
    asyncio.run(_call(host, "_notify_rainyun_success",
                      "999888777", {"ok": True, "points": 500}))
    _uid, body = host.core.gateway.sent[0]
    assert "500" in body


def test_success_multi_account_mentions_count():
    host = _Host()
    asyncio.run(_call(host, "_notify_rainyun_success",
                      "999888777", {"ok": True, "items": [{"a": 1}, {"b": 2}]}))
    _uid, body = host.core.gateway.sent[0]
    assert "2" in body


def test_success_does_not_leak_email():
    """账号标识不该出现在 QQ 里。"""
    host = _Host()
    asyncio.run(_call(host, "_notify_rainyun_success", "999888777",
                      {"ok": True, "items": [{"email": "secret@example.com"}]}))
    _uid, body = host.core.gateway.sent[0]
    assert "secret@example.com" not in body


def test_failure_still_notifies():
    host = _Host()
    asyncio.run(_call(host, "_notify_rainyun_failure",
                      "999888777", {"ok": False, "reason": "配额用尽"}))
    assert len(host.core.gateway.sent) == 1
    _uid, body = host.core.gateway.sent[0]
    assert "没成功" in body


def test_no_owner_summary_method():
    """owner 汇总已移除：绑定者各收各的，避免同一事实报两遍。"""
    from anima.core.autonomy import Heartbeat
    assert not hasattr(Heartbeat, "_notify_rainyun_signin_summary")