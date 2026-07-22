"""零依赖的 systemd 看门狗集成。

通过 ``$NOTIFY_SOCKET`` 的 Unix 数据报套接字发送 ``WATCHDOG=1`` 通知，
使 systemd 的 ``WatchdogSec=`` 能检测到事件循环死锁（CPU 自旋或同步阻塞）
并自动重启服务。纯标准库实现，无需 systemd-python 包。

看门狗任务运行在**主事件循环**上：一旦 loop 卡死（无论自旋还是阻塞），
ping 就无法调度 → systemd 收不到心跳 → 自动 SIGKILL 重启。这是与具体根因
无关的兜底，专门应对「进程活着但不干活」的假死。
"""
from __future__ import annotations

import asyncio
import faulthandler
import logging
import os
import socket
import sys
import time
from threading import Lock
from typing import Optional

logger = logging.getLogger("anima.watchdog")

# 两次心跳之间的间隔（秒）。需远小于 unit 里的 WatchdogSec，留出余量。
_WATCHDOG_INTERVAL = 30.0
_TRACEBACK_DUMP_AFTER = 75.0
_LATE_TICK_THRESHOLD = 2.0

_health_lock = Lock()
_health = {
    "running": False,
    "last_tick_at": None,
    "last_lag_sec": 0.0,
    "max_lag_sec": 0.0,
    "late_ticks": 0,
    "dump_after_sec": _TRACEBACK_DUMP_AFTER,
}


def watchdog_health() -> dict:
    """返回不含敏感数据的事件循环看门狗运行指标。"""
    with _health_lock:
        return dict(_health)


def _set_health(**values: object) -> None:
    with _health_lock:
        _health.update(values)


def _record_loop_lag(lag_sec: float) -> None:
    lag_sec = max(0.0, float(lag_sec))
    with _health_lock:
        _health["last_tick_at"] = time.time()
        _health["last_lag_sec"] = round(lag_sec, 3)
        _health["max_lag_sec"] = round(max(float(_health["max_lag_sec"]), lag_sec), 3)
        if lag_sec >= _LATE_TICK_THRESHOLD:
            _health["late_ticks"] = int(_health["late_ticks"]) + 1


def _arm_traceback_dump(delay: float) -> bool:
    """由 CPython 的独立看门线程定时转储所有线程，不依赖 asyncio 调度。"""
    try:
        if not faulthandler.is_enabled():
            faulthandler.enable(file=sys.stderr, all_threads=True)
        faulthandler.cancel_dump_traceback_later()
        faulthandler.dump_traceback_later(
            max(1.0, float(delay)), repeat=False, file=sys.stderr, exit=False,
        )
        return True
    except (OSError, RuntimeError, ValueError):
        logger.debug("无法启用假死前全线程栈转储", exc_info=True)
        return False


def _cancel_traceback_dump() -> None:
    try:
        faulthandler.cancel_dump_traceback_later()
    except (OSError, RuntimeError, ValueError):
        pass


def _notify(state: str) -> bool:
    """发送一条 sd_notify 数据报。成功返回 True。

    抽象套接字（路径以 ``@`` 开头）按 systemd 约定替换为 ``\\0`` 前缀。
    ``$NOTIFY_SOCKET`` 不存在（非 systemd 环境，如本地测试）时静默返回 False。
    """
    sock_path = os.environ.get("NOTIFY_SOCKET")
    if not sock_path or not state:
        return False
    if sock_path.startswith("@"):
        addr: bytes = b"\x00" + sock_path[1:].encode("utf-8")
    else:
        addr = sock_path.encode("utf-8")
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            sock.connect(addr)
            sock.sendall(state.encode("utf-8"))
        finally:
            sock.close()
    except Exception as exc:  # 尽力而为，绝不要影响主流程
        logger.debug("sd_notify(%r) 失败: %s", state, exc)
        return False
    return True


def notify_ready() -> bool:
    """发送 READY=1（即便 unit 是 Type=simple 也无害）。"""
    return _notify("READY=1")


async def _watchdog_loop(
    interval: float = _WATCHDOG_INTERVAL,
    dump_after: float = _TRACEBACK_DUMP_AFTER,
) -> None:
    """周期性发送 WATCHDOG=1。运行在主事件循环上。"""
    _set_health(running=True, dump_after_sec=float(dump_after))
    last_tick = time.monotonic()
    try:
        # 立即先发一次，尽快让 systemd 武装看门狗。
        _notify("WATCHDOG=1")
        _record_loop_lag(0.0)
        while True:
            _arm_traceback_dump(dump_after)
            await asyncio.sleep(interval)
            now = time.monotonic()
            lag = max(0.0, now - last_tick - interval)
            last_tick = now
            _cancel_traceback_dump()
            _record_loop_lag(lag)
            if lag >= _LATE_TICK_THRESHOLD:
                logger.warning("事件循环延迟 %.3fs（阈值 %.1fs）", lag, _LATE_TICK_THRESHOLD)
            _notify("WATCHDOG=1")
    finally:
        _cancel_traceback_dump()
        _set_health(running=False)


def start_watchdog(
    loop: Optional[asyncio.AbstractEventLoop] = None,
    interval: float = _WATCHDOG_INTERVAL,
    dump_after: float = _TRACEBACK_DUMP_AFTER,
) -> Optional[asyncio.Task]:
    """在运行中的事件循环上启动看门狗任务。

    返回创建的 Task；若不在事件循环上下文中（如同步导入）则返回 None。
    """
    try:
        loop = loop or asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("看门狗未启动：当前不在事件循环中")
        return None

    task = loop.create_task(_watchdog_loop(interval, dump_after))

    def _reschedule(fut: asyncio.Future) -> None:
        if fut.cancelled():
            return
        try:
            fut.result()
        except Exception as exc:
            logger.warning("看门狗任务异常退出，尝试重启: %s", exc)
        try:
            loop.call_later(1.0, lambda: start_watchdog(loop, interval, dump_after))
        except RuntimeError:
            pass

    task.add_done_callback(_reschedule)
    logger.info(
        "systemd 看门狗已启动（间隔 %.0fs，假死栈转储 %.0fs）",
        interval, dump_after,
    )
    return task
