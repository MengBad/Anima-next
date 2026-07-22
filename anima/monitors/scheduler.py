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

    async def start(self) -> None:
        self.store.restore(); self._running = True
        self._task = asyncio.create_task(self._loop())

    async def close(self) -> None:
        self._running = False
        if self._task: self._task.cancel(); await asyncio.gather(self._task, return_exceptions=True)
        # SubscriptionStore 的增删与状态迁移均已立即原子落盘；关闭时再次把
        # 旧内存整体写回会覆盖运维期间刚写入的订阅文件。

    async def _loop(self) -> None:
        while self._running:
            await self.tick()
            await asyncio.sleep(self.interval_sec)

    async def tick(self) -> None:
        now = time.time()
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
            except Exception as exc:
                logger.warning("订阅 %s 查询失败: %s", sub.id, exc)
                audit = getattr(self.core, "audit", None)
                if audit: audit.emit("subscription_error", subscription_id=sub.id, detail=str(exc)[:200], ok=False)
                self._next[sub.id] = now + max(300, sub.poll_interval_sec * 2)
