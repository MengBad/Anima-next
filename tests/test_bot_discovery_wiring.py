# -*- coding: utf-8 -*-
"""C 方案：把 bot 自动发现接进生产调用链（RED 先行）。

钉死四条不可协商的行为：
  1. 配置开关存在且默认关（fail-closed）
  2. 只有平台 role=='bot' 才自动写入权威集
  3. 昵称命中【绝不】写入权威集（设计红线）—— 只记建议日志
  4. 无 channel / 异常时 fail-soft，不抛、不影响心跳
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from anima.cognitive import bot_social


# ---------------------------------------------------------------------------
# 假 channel
# ---------------------------------------------------------------------------
class FakeChannel:
    def __init__(self, groups: dict[int, list[dict]]):
        self._groups = groups
        self.calls: list[str] = []

    async def get_group_list(self):
        self.calls.append("get_group_list")
        return [{"group_id": gid} for gid in self._groups]

    async def get_group_member_list(self, group_id: int):
        self.calls.append("get_group_member_list:%s" % group_id)
        return self._groups.get(int(group_id))


class BoomChannel:
    async def get_group_list(self):
        raise RuntimeError("WS 炸了")

    async def get_group_member_list(self, group_id):  # noqa: ARG002
        raise RuntimeError("WS 炸了")


def _core(channel, data_dir: str) -> SimpleNamespace:
    return SimpleNamespace(
        gateway=SimpleNamespace(channel=channel),
        config=SimpleNamespace(persistence=SimpleNamespace(data_dir=data_dir)),
    )


# ---------------------------------------------------------------------------
# 1. 配置开关
# ---------------------------------------------------------------------------
def test_config_switch_exists_and_defaults_off():
    """开关必须有 pydantic 字段且默认关（fail-closed）。

    🔴 2026-10-14 踩坑：开关**本来就在 SocialConfig**，我一度在 QQConfig 又加了
    一个同名字段 ⇒ 两处同名开关、语义分裂。这里钉死「只有一处，且在 social 段」。
    """
    from anima.core.config import QQConfig, SocialConfig

    cfg = SocialConfig()
    assert hasattr(cfg, "bot_discovery_enabled"), (
        "SocialConfig 缺 bot_discovery_enabled 字段（只写 yaml 会被 extra=ignore 静默丢弃）"
    )
    assert cfg.bot_discovery_enabled is False
    # 反例守卫：qq 段绝不能再有同名开关
    assert not hasattr(QQConfig(), "bot_discovery_enabled"), (
        "QQConfig 出现重复的 bot_discovery_enabled —— 两处同名开关会导致语义分裂"
    )


# ---------------------------------------------------------------------------
# 2. channel 具备 get_group_list
# ---------------------------------------------------------------------------
def test_channel_exposes_get_group_list():
    from anima.channels.onebot11 import OneBot11Adapter

    fn = getattr(OneBot11Adapter, "get_group_list", None)
    assert fn is not None, "channel 缺 get_group_list，扫描无法枚举群"
    assert asyncio.iscoroutinefunction(fn)


# ---------------------------------------------------------------------------
# 3/4. 扫描行为
# ---------------------------------------------------------------------------
def test_role_bot_is_auto_confirmed(tmp_path, caplog):
    """平台真相（role=='bot'）才允许自动写入权威集。"""
    members = [
        {"user_id": 111, "nickname": "真人甲", "role": "member"},
        {"user_id": 222, "nickname": "平台标注的Bot", "role": "bot"},
    ]
    ch = FakeChannel({1001: members})
    core = _core(ch, str(tmp_path))

    with caplog.at_level("INFO", logger="anima.cognitive.bot_social"):
        res = asyncio.run(bot_social.scan_group_bots(core))

    assert res.get("confirmed"), "role=='bot' 未被自动确认"
    assert 222 in res["confirmed"]
    store = bot_social.KnownBotsStore(str(tmp_path))
    assert store.is_known(222)
    assert 222 in store.uids()
    assert not store.is_known(111)


def test_nickname_hit_never_auto_confirmed(tmp_path, caplog):
    """🔴 红线：昵称命中只记建议，绝不写入权威集。"""
    members = [
        {"user_id": 333, "nickname": "可怜Bot", "role": "member"},
        # 444 必须真的命中 _BOT_NICKNAME_HINTS（含「机器人」）
        {"user_id": 444, "nickname": "机器人管家", "role": "member"},
    ]
    ch = FakeChannel({1002: members})
    core = _core(ch, str(tmp_path))

    with caplog.at_level("INFO", logger="anima.cognitive.bot_social"):
        res = asyncio.run(bot_social.scan_group_bots(core))

    store = bot_social.KnownBotsStore(str(tmp_path))
    assert not store.is_known(333), "昵称命中被自动确认 —— 踩红线"
    assert not store.is_known(444), "昵称命中被自动确认 —— 踩红线"
    assert set(store.uids()) == set()
    # 但必须有建议留痕，供 owner 人工确认
    assert set(res.get("suggested") or []) == {333, 444}
    joined = caplog.text
    assert "333" in joined and "444" in joined, "建议未落日志，owner 看不到候选"


def test_existing_confirmed_uid_preserved(tmp_path):
    """已确认的 UID 不因扫描被清除/降级。"""
    store = bot_social.KnownBotsStore(str(tmp_path))
    store.add(486159181, name="Aki", confirmed=True)
    ch = FakeChannel({1003: [{"user_id": 486159181, "nickname": "Rezon",
                              "card": "Aki", "role": "member"}]})
    asyncio.run(bot_social.scan_group_bots(_core(ch, str(tmp_path))))
    after = bot_social.KnownBotsStore(str(tmp_path))
    assert after.is_known(486159181)
    assert after._bots["486159181"]["bot_name"] == "Aki"


def test_no_channel_is_failsoft(tmp_path):
    """无 channel 时不抛异常（心跳不能被拖垮）。"""
    core = SimpleNamespace(
        gateway=SimpleNamespace(channel=None),
        config=SimpleNamespace(persistence=SimpleNamespace(data_dir=str(tmp_path))),
    )
    res = asyncio.run(bot_social.scan_group_bots(core))
    assert res.get("skipped")


def test_channel_blowup_is_failsoft(tmp_path):
    core = _core(BoomChannel(), str(tmp_path))
    res = asyncio.run(bot_social.scan_group_bots(core))
    assert res.get("skipped") or res.get("error"), "channel 异常必须被隔离"


def test_empty_group_list_is_failsoft(tmp_path):
    ch = FakeChannel({})
    res = asyncio.run(bot_social.scan_group_bots(_core(ch, str(tmp_path))))
    assert res.get("groups") == 0


# ---------------------------------------------------------------------------
# 5. 接线：心跳真的会调它
# ---------------------------------------------------------------------------
def test_autonomy_heartbeat_wires_scan():
    """autonomy._tick 必须有调度点，且受开关保护（源码守卫）。"""
    src = open("anima/core/autonomy.py", encoding="utf-8").read()
    assert "scan_group_bots" in src, "心跳未接线 scan_group_bots"
    assert "bot_discovery_enabled" in src, "扫描未受配置开关保护"
    # 必须每日门控，不能每个 tick 都扫
    assert "_last_bot_discovery_date" in src


def test_scan_once_per_day_guard(monkeypatch):
    """同一天只调度一次（测 Heartbeat 上的**真实**方法，不是桩）。"""
    from anima.core import autonomy
    from anima.core.autonomy import Heartbeat

    scheduled: list[str] = []

    async def _fake_scan(core):  # noqa: ARG001
        scheduled.append("scan")
        return {"groups": 0, "confirmed": [], "suggested": []}

    monkeypatch.setattr(autonomy, "_launch_bg", lambda coro, name: (
        coro.close(), scheduled.append(name)) and None)
    # 用真实模块里的 bot_social.scan_group_bots 也可以，这里只验证门控次数
    monkeypatch.setattr("anima.cognitive.bot_social.scan_group_bots", _fake_scan)

    hb = object.__new__(Heartbeat)
    hb.core = SimpleNamespace(
        config=SimpleNamespace(social=SimpleNamespace(bot_discovery_enabled=True))
    )
    hb._last_bot_discovery_date = ""
    hb.persist = lambda: None

    # 开关开 + 当天未扫 ⇒ 调度
    assert asyncio.run(hb._bot_discovery_tick(0.0)) is True
    # 同一天再调 ⇒ 不再调度
    assert asyncio.run(hb._bot_discovery_tick(0.0)) is False
    assert scheduled == ["bot_discovery"]

    # 开关关 ⇒ 永不调度
    hb2 = object.__new__(Heartbeat)
    hb2.core = SimpleNamespace(
        config=SimpleNamespace(social=SimpleNamespace(bot_discovery_enabled=False))
    )
    hb2._last_bot_discovery_date = ""
    hb2.persist = lambda: None
    scheduled.clear()
    assert asyncio.run(hb2._bot_discovery_tick(0.0)) is False
    assert scheduled == []
