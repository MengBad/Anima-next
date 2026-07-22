"""systemd 看门狗测试：验证 sd_notify 协议与心跳调度（纯 mock，跨平台）。"""
from __future__ import annotations

import asyncio
import os
import socket
from unittest import mock

import pytest

from anima import watchdog


def test_notify_sends_datagram_payload(monkeypatch):
    # Windows 无 socket.AF_UNIX 常量，stub 掉以便测试协议逻辑（Linux 上本就存在）
    monkeypatch.setattr(socket, "AF_UNIX", 1, raising=False)
    monkeypatch.setattr(socket, "SOCK_DGRAM", 2, raising=False)
    fake_sock = mock.MagicMock()
    monkeypatch.setenv("NOTIFY_SOCKET", "/run/systemd/notify")
    with mock.patch("anima.watchdog.socket.socket", return_value=fake_sock):
        ok = watchdog._notify("WATCHDOG=1")
    assert ok is True
    fake_sock.connect.assert_called_once()
    fake_sock.sendall.assert_called_once_with(b"WATCHDOG=1")


def test_notify_abstract_socket_strips_at(monkeypatch):
    monkeypatch.setattr(socket, "AF_UNIX", 1, raising=False)
    monkeypatch.setattr(socket, "SOCK_DGRAM", 2, raising=False)
    captured = {}
    fake_sock = mock.MagicMock()

    def fake_connect(addr):
        captured["addr"] = addr

    fake_sock.connect.side_effect = fake_connect
    monkeypatch.setenv("NOTIFY_SOCKET", "@/run/systemd/notify")
    with mock.patch("anima.watchdog.socket.socket", return_value=fake_sock):
        watchdog._notify("WATCHDOG=1")
    assert captured["addr"] == b"\x00/run/systemd/notify"


def test_notify_no_env_returns_false(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    assert watchdog._notify("WATCHDOG=1") is False


@pytest.mark.asyncio
async def test_start_watchdog_pings_periodically(monkeypatch):
    states = []

    def fake_notify(state: str):
        states.append(state)
        return True

    monkeypatch.setattr(watchdog, "_notify", fake_notify)
    monkeypatch.setattr(watchdog, "_arm_traceback_dump", lambda delay: True)
    monkeypatch.setattr(watchdog, "_cancel_traceback_dump", lambda: None)
    task = watchdog.start_watchdog(interval=0.2, dump_after=1.0)
    assert task is not None
    try:
        # 首发（立即）+ 周期 0.2s，等 0.7s 应至少 2 次
        await asyncio.sleep(0.7)
        assert states.count("WATCHDOG=1") >= 2, f"states={states}"
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_start_watchdog_not_in_loop_returns_none(monkeypatch):
    # 模拟不在事件循环上下文：get_running_loop 抛 RuntimeError
    monkeypatch.setattr(
        asyncio, "get_running_loop",
        mock.MagicMock(side_effect=RuntimeError("no loop")),
    )
    assert watchdog.start_watchdog() is None


def test_watchdog_health_records_event_loop_lag():
    before = watchdog.watchdog_health()
    watchdog._record_loop_lag(3.25)
    health = watchdog.watchdog_health()
    assert health["last_lag_sec"] == 3.25
    assert health["max_lag_sec"] >= 3.25
    assert health["late_ticks"] == before["late_ticks"] + 1
    assert isinstance(health["last_tick_at"], float)


def test_traceback_dump_is_armed_for_all_threads(monkeypatch):
    calls = []
    monkeypatch.setattr(watchdog.faulthandler, "is_enabled", lambda: False)
    monkeypatch.setattr(
        watchdog.faulthandler, "enable",
        lambda **kwargs: calls.append(("enable", kwargs)),
    )
    monkeypatch.setattr(
        watchdog.faulthandler, "cancel_dump_traceback_later",
        lambda: calls.append(("cancel", {})),
    )
    monkeypatch.setattr(
        watchdog.faulthandler, "dump_traceback_later",
        lambda delay, **kwargs: calls.append(("dump", {"delay": delay, **kwargs})),
    )
    assert watchdog._arm_traceback_dump(75.0) is True
    assert any(name == "enable" and args["all_threads"] is True for name, args in calls)
    assert any(name == "dump" and args["delay"] == 75.0 for name, args in calls)
