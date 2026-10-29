"""私聊自然语言审批（design/private_group_dual_mode_plan.md §3 / §3.6）。"""
from __future__ import annotations

import pytest

from anima.agent import approval_dialog as ad


# ── 决策解析 ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text", ["行", "可以", "同意", "去吧", "ok", "好的", "嗯"])
def test_parse_approve(text):
    assert ad.parse_decision(text) == ad.APPROVE


@pytest.mark.parametrize("text", ["不行", "不要", "拒绝", "算了", "停", "no"])
def test_parse_reject(text):
    assert ad.parse_decision(text) == ad.REJECT


def test_reject_wins_over_approve():
    # '不行' 同时含 '行'，否定必须优先
    assert ad.parse_decision("不行") == ad.REJECT
    assert ad.parse_decision("不可以") == ad.REJECT


def test_long_text_is_not_a_decision():
    # 长句里夹带"可以"多半在讨论别的事
    assert ad.parse_decision("我觉得这个方案可以再想想细节") is None


def test_empty_and_garbage():
    assert ad.parse_decision("") is None
    assert ad.parse_decision("今天天气不错") is None


# ── 授权指令解析 ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("text,qq", [
    ("授权 1562290139", 1562290139),
    ("授权给123456789", 123456789),
    ("给 123456789 管理员", 123456789),
    ("123456789 为管理员", 123456789),
])
def test_parse_grant(text, qq):
    action, parsed = ad.parse_admin_action(text)
    assert action == "grant"
    assert parsed == qq


@pytest.mark.parametrize("text,qq", [
    ("撤销授权 1562290139", 1562290139),
    ("取消授权123456789", 123456789),
    ("123456789 的权限撤销", 123456789),
])
def test_parse_revoke(text, qq):
    action, parsed = ad.parse_admin_action(text)
    assert action == "revoke"
    assert parsed == qq


def test_plain_chat_is_not_admin_action():
    action, qq = ad.parse_admin_action("今天群里好热闹")
    assert action is None and qq is None


# ── 授权权限边界 ───────────────────────────────────────────────────────────
class _Store:
    def __init__(self):
        self.admins = [1]

    def add_admin(self, qq):
        self.admins = sorted({*self.admins, int(qq)})
        return self.admins

    def remove_admin(self, qq):
        self.admins = [a for a in self.admins if a != int(qq)]
        return self.admins


class _Perms:
    owners = [1]
    admins = [1, 3]


class _Cfg:
    permissions = _Perms()


class _Core:
    def __init__(self):
        self.config = _Cfg()
        self.permission_store = _Store()


def test_owner_can_grant():
    core = _Core()
    ok, msg = ad.grant_admin(core, 1, 999)
    assert ok is True
    assert 999 in core.permission_store.admins


def test_admin_cannot_grant():
    """admin 再授权 = 转让 root，必须拒绝。"""
    core = _Core()
    ok, msg = ad.grant_admin(core, 3, 999)
    assert ok is False
    assert 999 not in core.permission_store.admins


def test_stranger_cannot_grant():
    core = _Core()
    ok, _ = ad.grant_admin(core, 12345, 999)
    assert ok is False


def test_owner_can_revoke():
    core = _Core()
    ad.grant_admin(core, 1, 999)
    ok, _ = ad.revoke_admin(core, 1, 999)
    assert ok is True
    assert 999 not in core.permission_store.admins


def test_admin_cannot_revoke():
    core = _Core()
    ok, _ = ad.revoke_admin(core, 3, 1)
    assert ok is False


# ── 授权队列 TTL ───────────────────────────────────────────────────────────
def test_grant_queue_expires():
    q = ad.GrantQueue(ttl_sec=100)
    q.register(555, "想用工具", now=1000.0)
    assert len(q.pending(now=1050.0)) == 1
    assert len(q.pending(now=1200.0)) == 0


def test_grant_queue_pop():
    q = ad.GrantQueue()
    q.register(555)
    assert q.pop(555) is not None
    assert q.pop(555) is None


# ── 非 owner 私聊不被消费 ──────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_non_owner_private_not_consumed():
    core = _Core()
    assert await ad.handle_owner_private(core, "行", 999) is None


@pytest.mark.asyncio
async def test_owner_grant_via_dialog():
    core = _Core()
    # QQ 号 5~12 位；太短的号码不当作授权指令（避免「授权 3 个」这类误判）
    out = await ad.handle_owner_private(core, "授权 1562290139", 1)
    assert out is not None
    assert 1562290139 in core.permission_store.admins


def test_short_number_is_not_a_grant():
    action, qq = ad.parse_admin_action("授权 3 个")
    assert action is None and qq is None
