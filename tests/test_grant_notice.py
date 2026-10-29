"""非 owner 授权申请通知（design §3.6）。

这条链路此前只有 GrantQueue 定义、无调用点 ⇒ owner 永远收不到"谁想用"的通知，
"授权"在体验上等于不存在。本文件守住这条链路。
"""
from __future__ import annotations

import pytest

from anima.agent.approval_dialog import GrantQueue


class _Perms:
    owners = [1562290139]
    admins = [1562290139]


class _Cfg:
    permissions = _Perms()


class _Gateway:
    def __init__(self):
        self.sent = []

    async def send(self, msg):
        self.sent.append(msg)


class _Core:
    def __init__(self):
        self.config = _Cfg()
        self.gateway = _Gateway()


class _Handler:
    """最小宿主：只借 handler 的 _request_grant_notice。"""

    def __init__(self, core):
        self.core = core

    def _is_owner(self, uid):
        return int(uid) in (self.core.config.permissions.owners or ())


async def _call(handler, uid, name, text, now=None):
    # 直接复用真实实现，但把时间戳钉死以避开频控
    import anima.llm.handler as H

    return await H.LLMHandler._request_grant_notice(handler, uid, name, text)


@pytest.mark.asyncio
async def test_stranger_request_notifies_owner():
    core = _Core()
    h = _Handler(core)
    await _call(h, 999888777, "小明", "帮我跑个脚本")

    assert core.gateway.sent, "owner 必须收到授权申请通知"
    body = core.gateway.sent[0].message
    assert "999888777" in body
    assert "小明" in body
    assert "授权 999888777" in body, "通知里必须给出可直接复制的授权指令"


@pytest.mark.asyncio
async def test_grant_queue_gets_registered():
    core = _Core()
    h = _Handler(core)
    await _call(h, 111222333, "小红", "我也想用")
    assert core.grant_queue is not None
    pending = core.grant_queue.pending()
    assert [p.user_id for p in pending] == [111222333]


@pytest.mark.asyncio
async def test_notice_is_rate_limited():
    core = _Core()
    h = _Handler(core)
    await _call(h, 555666777, "刷屏者", "第一次")
    await _call(h, 555666777, "刷屏者", "第二次")
    await _call(h, 555666777, "刷屏者", "第三次")
    assert len(core.gateway.sent) == 1, "同一申请人 10 分钟内只通知一次"


@pytest.mark.asyncio
async def test_no_owners_does_not_crash():
    class _NoOwnerPerms:
        owners = []
        admins = []

    class _NoOwnerCfg:
        permissions = _NoOwnerPerms()

    core = _Core()
    core.config = _NoOwnerCfg()
    h = _Handler(core)
    await _call(h, 123123123, "x", "y")
    assert core.gateway.sent == []
