"""低频订阅调度器；每项异常隔离，不影响 QQ 主循环。"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from anima.core.gateway import OutboundMsg
from anima.monitors.bilibili import BilibiliLiveProvider
from anima.monitors.http_page import HttpPageProvider
from anima.monitors.subscriptions import SubscriptionStore

logger = logging.getLogger("anima.monitors")


class MonitorScheduler:
    def __init__(self, core: Any, interval_sec: float = 15.0):
        self.core, self.interval_sec = core, interval_sec
        self.store = SubscriptionStore(core.persistence)
        self._next: dict[str, float] = {}
        self._task: asyncio.Task | None = None
        self._running = False
        self._providers = {"bilibili_live": BilibiliLiveProvider(), "http_page": HttpPageProvider()}
        # P1-4：连续失败计数 + 上次告警时间（用于把「订阅挂了」变成可见的主动提醒）
        self._fail_streak: dict[str, int] = {}
        self._anomaly_notified_at: dict[str, float] = {}

    async def start(self) -> None:
        self.store.restore(); self._running = True
        self._task = asyncio.create_task(self._loop())

    async def close(self) -> None:
        self._running = False
        if self._task: self._task.cancel(); await asyncio.gather(self._task, return_exceptions=True)
        # SubscriptionStore 的增删与状态迁移均已立即原子落盘；关闭时再次把
        # 旧内存整体写回会覆盖运维期间刚写入的订阅文件。

    def _maybe_notify_anomaly(self, sub: Any, detail: str, now: float) -> None:
        """P1-4：订阅连续失败 N 次 → 生成一条主动议程，让人知道「订阅挂了」。

        此前失败只写审计 ``subscription_error``，QQ 上完全无感——订阅静默死亡。
        冷却期内不重复提醒；目标群为空则跳过（没有可说的地方）。
        """
        cfg = getattr(getattr(self.core, "config", None), "proactive", None)
        if cfg is not None and not getattr(cfg, "enabled", True):
            return
        threshold = int(getattr(cfg, "subscription_failure_threshold", 3) or 3)
        cooldown = float(getattr(cfg, "subscription_anomaly_cooldown_sec", 21600) or 21600)
        streak = self._fail_streak.get(sub.id, 0) + 1
        self._fail_streak[sub.id] = streak
        if streak < threshold:
            return
        if now - float(self._anomaly_notified_at.get(sub.id, 0.0)) < cooldown:
            return
        groups = [int(g) for g in (getattr(sub, "target_groups", None) or []) if str(g).isdigit()]
        if not groups:
            return
        try:
            from anima.core.proactive import priority_for
            priority = priority_for("subscription_anomaly", cfg)
        except Exception:
            priority = 0.85
        name = str(getattr(sub, "subject_id", "") or sub.id)[:40]
        text = (f"订阅「{name}」连续 {streak} 次查不到（{detail}），"
                f"可能已失效，要修的话跟我说一声。")
        for gid in groups:
            try:
                self.core.propose_agenda(
                    gid, text, priority=priority,
                    expires_at=now + 12 * 3600,
                    source=f"subscription_anomaly:{sub.id}",
                )
            except Exception:
                logger.debug("订阅异常议程入队失败 sub=%s group=%s", sub.id, gid, exc_info=True)
        self._anomaly_notified_at[sub.id] = now
        audit = getattr(self.core, "audit", None)
        if audit:
            audit.emit("subscription_anomaly_notified", subscription_id=sub.id,
                       streak=streak, groups=groups, ok=True)

    async def _loop(self) -> None:
        while self._running:
            await self.tick()
            await asyncio.sleep(self.interval_sec)

    async def tick(self) -> None:
        now = time.time()
        # 🔴 2026-10-29：**清理已删除订阅的残留计数**。
        #
        # `_fail_streak` / `_anomaly_notified_at` / `_next` 三个 dict
        # 以 `sub.id` 为键，但订阅被删除时**没有任何清理点** ——
        # 只写了 `pop` 在「查询成功」分支（那是清零，不是删键）。
        # 后果：每建一个订阅就永久留一条记录，长期运行后
        # `_anomaly_notified_at` 只增不减（订阅增删频繁时尤其明显）。
        #
        # 每 tick 用活跃 id 集合做一次差集，顺手清掉。
        _live_ids = set(self.store.items.keys())
        for _d in (self._fail_streak, self._anomaly_notified_at, self._next):
            for _k in [k for k in _d if k not in _live_ids]:
                _d.pop(_k, None)
        for sub in list(self.store.items.values()):
            if not sub.enabled or now < self._next.get(sub.id, 0): continue
            self._next[sub.id] = now + max(30, sub.poll_interval_sec)
            try:
                provider = self._providers.get(sub.platform)
                if provider is None: raise ValueError(f"未知平台: {sub.platform}")
                status = await provider.query(sub.subject_id)
                changed = (self.store.transition_on_change(sub.id, status.state, status.event_key)
                           if sub.platform == "http_page"
                           else self.store.transition(sub.id, status.state, status.event_key))
                if changed:
                    text = sub.template.format(name=status.name, title=status.title, url=status.url)
                    for gid in sub.target_groups:
                        workflows = getattr(self.core, "workflows", None)
                        run = workflows.trigger("subscription", sub.id, gid, text, event_key=status.event_key) if workflows else None
                        agenda = self.core.propose_agenda(
                            gid, text, priority=.95, expires_at=now + 6 * 3600,
                            source=f"subscription:{sub.id}",
                        )
                        if run:
                            workflows.bind_intent(run.id, getattr(agenda, "intent_id", ""))
                    audit = getattr(self.core, "audit", None)
                    if audit: audit.emit("subscription_notify", subscription_id=sub.id, groups=sub.target_groups, ok=True)
                self._fail_streak.pop(sub.id, None)  # 成功即清零
            except Exception as exc:
                logger.warning("订阅 %s 查询失败: %s", sub.id, exc)
                audit = getattr(self.core, "audit", None)
                if audit: audit.emit("subscription_error", subscription_id=sub.id, detail=str(exc)[:200], ok=False)
                self._next[sub.id] = now + max(300, sub.poll_interval_sec * 2)
                self._maybe_notify_anomaly(sub, str(exc)[:120], now)
