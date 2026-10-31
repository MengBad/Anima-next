"""P2 · 后台任务的观察与控制（对齐 Memoh 的 background 工具组）。

## 现状（2026-10-31 调研）

后台执行**已经存在**：`OwnerAgent.submit(auto_run=True)` 就是
`asyncio.create_task(self._run(task))`，任务真的在后台跑，
完成后经 `_deliver_text` / `_deliver_media` 交付。

缺的是**观察与控制**：用户看不到进度，也没法在任务跑歪时叫停。
本模块补这一层，不重建引擎。

## 与 Memoh 的对应

| Memoh | 本模块 |
|---|---|
| list_background | `list_tasks` |
| wait_until(task_id) | `wait_task` |
| get_background_status | `get_task_status` |
| kill_background | `kill_task` |

## 关键设计（抄 Memoh 的洞察）

`wait_task` 的返回值区分四种终止原因，**不要傻等 completed**：
  · completed / failed —— 任务真的结束了
  · idle —— 长时间无进展（可能卡住或在等外部）
  · timeout —— 本次等待超时，任务仍在跑，可以再等

私有执行边界：工具一律 `private_only=True` + `min_level=OWNER`。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

logger = logging.getLogger("anima.agent.background")

#: 单次 wait 的上限（秒）。任务不会因此被取消，只是本次等待返回。
MAX_WAIT_SEC = 300.0
#: 判定「idle」的阈值：多久没有 updated_at 变化算无进展。
DEFAULT_IDLE_SEC = 60.0

#: 终态集合
_TERMINAL = frozenset({"completed", "failed", "cancelled", "interrupted"})


def _store(core: Any):
    return getattr(getattr(core, "agent", None), "store", None)


def _snapshot(task: Any) -> dict:
    steps = getattr(task, "steps", None) or []
    done = sum(1 for s in steps if getattr(s, "status", "") == "done")
    return {
        "id": getattr(task, "id", ""),
        "status": getattr(task, "status", ""),
        "request": str(getattr(task, "request", ""))[:80],
        "steps_done": done,
        "steps_total": len(steps),
        "current_step": getattr(task, "current_step", 0),
        "replan_count": getattr(task, "replan_count", 0),
        "waiting_user": getattr(task, "status", "") == "waiting_user",
        "updated_at": getattr(task, "updated_at", 0.0),
        "error": str(getattr(task, "error", ""))[:200],
    }


def list_tasks(core: Any, limit: int = 10) -> str:
    """列出最近的 Agent 任务（含状态与进度）。"""
    store = _store(core)
    if store is None:
        return "错误: 任务存储不可用。"
    items = list((getattr(store, "items", None) or {}).values())
    if not items:
        return "当前没有任何 Agent 任务。"
    items.sort(key=lambda t: getattr(t, "updated_at", 0) or 0, reverse=True)
    rows = []
    for t in items[:max(1, min(int(limit or 10), 30))]:
        s = _snapshot(t)
        prog = f"{s['steps_done']}/{s['steps_total']}" if s["steps_total"] else "-"
        rows.append(f"· {s['id']} [{s['status']}] 进度 {prog}  {s['request'][:40]}")
    return "最近的任务：\n" + "\n".join(rows)


def get_task_status(core: Any, task_id: str) -> str:
    """读单个任务的详细状态。"""
    store = _store(core)
    if store is None:
        return "错误: 任务存储不可用。"
    tid = str(task_id or "").strip()
    if not tid:
        return "错误: 需要 task_id。"
    task = store.get(tid)
    if task is None:
        return f"错误: 找不到任务 {tid}。"
    s = _snapshot(task)
    lines = [
        f"任务 {s['id']}",
        f"  状态: {s['status']}",
        f"  请求: {s['request']}",
        f"  进度: {s['steps_done']}/{s['steps_total']}（当前第 {s['current_step']} 步）",
        f"  重规划: {s['replan_count']} 次",
    ]
    if s["waiting_user"]:
        lines.append("  ⏸ 正在等你回答（ask_user）")
    if s["error"]:
        lines.append(f"  错误: {s['error']}")
    return "\n".join(lines)


async def wait_task(core: Any, task_id: str, timeout_sec: float = 120.0) -> str:
    """等任务结束或「看起来不动了」。任务本身不会因此被取消。

    返回的 reason 四种：completed / failed / idle / timeout。
    """
    store = _store(core)
    if store is None:
        return "错误: 任务存储不可用。"
    tid = str(task_id or "").strip()
    task = store.get(tid) if tid else None
    if task is None:
        return f"错误: 找不到任务 {tid}。"

    budget = max(1.0, min(float(timeout_sec or 120.0), MAX_WAIT_SEC))
    deadline = time.time() + budget
    last_change = time.time()
    last_updated = getattr(task, "updated_at", 0.0)

    while time.time() < deadline:
        status = getattr(task, "status", "")
        if status in _TERMINAL:
            return (f"任务 {tid} 结束：{status}\n" + get_task_status(core, tid))
        cur = getattr(task, "updated_at", 0.0)
        if cur != last_updated:
            last_updated, last_change = cur, time.time()
        elif time.time() - last_change >= DEFAULT_IDLE_SEC:
            return (f"任务 {tid} 已 {int(DEFAULT_IDLE_SEC)}s 无进展（idle），"
                    "它可能卡住或在等外部响应。要我停掉就说「停」。\n"
                    + get_task_status(core, tid))
        await asyncio.sleep(2.0)

    return (f"任务 {tid} 仍在跑（等待 {int(budget)}s 超时），可以再等一次。\n"
            + get_task_status(core, tid))


def kill_task(core: Any, task_id: str) -> str:
    """请求取消一个正在跑的任务。"""
    store = _store(core)
    if store is None:
        return "错误: 任务存储不可用。"
    tid = str(task_id or "").strip()
    task = store.get(tid) if tid else None
    if task is None:
        return f"错误: 找不到任务 {tid}。"
    if getattr(task, "status", "") in _TERMINAL:
        return f"任务 {tid} 已经结束了（{task.status}），不用停。"

    agent = getattr(core, "agent", None)
    executor = getattr(agent, "executor", None) if agent is not None else None
    if executor is None or not hasattr(executor, "request_cancel"):
        return "错误: 执行器不支持取消。"
    try:
        executor.request_cancel(tid)
    except Exception:
        logger.exception("取消任务失败 id=%s", tid)
        return "取消失败了（内部错误）。"
    logger.info("[background] 请求取消任务 %s", tid)
    return f"好，正在停任务 {tid}。"
