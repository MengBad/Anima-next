"""P1-4 主动优先级队列。

验收口径：
- 优先级有单一事实来源，且「要事」> 「闲聊主动」
- 队首被挡时不再堵死整轮（逐个尝试）
- 订阅连续失败 → 进主动队列（此前只写审计，QQ 上无感）
"""
from __future__ import annotations

import time

import pytest

from anima.core.proactive import (
    URGENT_THRESHOLD,
    family_of,
    is_urgent,
    priority_for,
    rank_agenda,
)
from anima.cognitive.agenda import AgendaItem


# ---------------------------------------------------------------------------
# 优先级注册表
# ---------------------------------------------------------------------------
def test_useful_sources_rank_above_chitchat():
    for src in ("debt_due", "health_alert", "subscription_anomaly", "calendar"):
        assert priority_for(src) > priority_for("heartbeat"), src
        assert priority_for(src) > priority_for("bot_social"), src


def test_family_strips_subscription_id():
    assert family_of("subscription:abc123") == "subscription"
    assert family_of("debt_due") == "debt_due"
    assert priority_for("subscription:abc123") == priority_for("subscription")


def test_unknown_source_falls_back():
    assert priority_for("totally_new_source") == 0.5


def test_config_override_wins():
    cfg = type("C", (), {"source_priority": {"heartbeat": 0.05}})()
    assert priority_for("heartbeat", cfg) == 0.05
    # 未覆盖的来源仍走注册表
    assert priority_for("calendar", cfg) == priority_for("calendar")


def test_is_urgent():
    assert is_urgent("debt_due")
    assert not is_urgent("heartbeat")
    assert is_urgent(URGENT_THRESHOLD)


# ---------------------------------------------------------------------------
# 排序 / 队首阻塞
# ---------------------------------------------------------------------------
def _item(gid, text, priority, not_before=0.0, status="pending"):
    return AgendaItem(group_id=gid, text_intent=text, priority=priority,
                      not_before_at=not_before, status=status)


def test_rank_agenda_sorts_by_priority_desc():
    items = [_item(1, "闲聊", 0.25), _item(1, "欠你的答复", 0.8), _item(1, "日程", 0.9)]
    ranked = rank_agenda(items, time.time())
    assert [i.text_intent for i in ranked] == ["日程", "欠你的答复", "闲聊"]


def test_rank_agenda_skips_not_ready_and_done():
    now = time.time()
    items = [
        _item(1, "未来的", 0.95, not_before=now + 3600),
        _item(1, "已消费", 0.9, status="consumed"),
        _item(1, "现在能说", 0.5),
    ]
    ranked = rank_agenda(items, now)
    assert [i.text_intent for i in ranked] == ["现在能说"]


def test_rank_agenda_filters_disallowed_group():
    """队首所在群被禁 → 不应堵死整轮，后面的要事仍应被选中。"""
    items = [_item(111, "被禁群的日程", 0.9), _item(222, "要紧事", 0.8)]
    ranked = rank_agenda(items, time.time(), group_allowed=lambda g: g != 111)
    assert [i.text_intent for i in ranked] == ["要紧事"]


def test_rank_agenda_respects_limit():
    items = [_item(1, f"t{i}", 0.9 - i / 100) for i in range(10)]
    assert len(rank_agenda(items, time.time(), limit=3)) == 3


def test_agenda_queue_ready_items():
    from anima.cognitive.agenda import AgendaQueue

    class _P:
        def __init__(self):
            self.data = []

        def read_json(self, path, default=None):
            return self.data

        def write_json(self, path, obj):
            self.data = obj

    q = AgendaQueue(_P())
    now = time.time()
    q.push(_item(1, "a", 0.9))
    q.push(_item(1, "b", 0.2))
    q.push(_item(1, "future", 0.99, not_before=now + 600))
    ready = q.ready_items(now=now)
    assert {i.text_intent for i in ready} == {"a", "b"}
    # peek_ready 仍是最高优先级那一条（既有契约不变）
    assert q.peek_ready(now=now).text_intent == "a"


# ---------------------------------------------------------------------------
# 订阅异常进主动队列
# ---------------------------------------------------------------------------
class _Sub:
    def __init__(self, sid="s1", groups=(777,)):
        self.id = sid
        self.subject_id = "某UP主"
        self.target_groups = list(groups)


class _Cfg:
    def __init__(self, enabled=True, threshold=3, cooldown=21600):
        self.proactive = type("P", (), {
            "enabled": enabled,
            "subscription_failure_threshold": threshold,
            "subscription_anomaly_cooldown_sec": cooldown,
            "urgent_threshold": 0.7,
            "source_priority": {},
        })()


class _Core:
    def __init__(self, cfg=None):
        self.config = cfg or _Cfg()
        self.agenda = []
        self.audit = None

    def propose_agenda(self, gid, text, *, priority=0.5, expires_at=0.0, source=""):
        self.agenda.append({"gid": gid, "text": text, "priority": priority, "source": source})
        return type("A", (), {"intent_id": "int_x"})()


def _sched(core):
    from anima.monitors.scheduler import MonitorScheduler
    s = MonitorScheduler.__new__(MonitorScheduler)  # 不启动 asyncio loop
    s.core = core
    s._fail_streak = {}
    s._anomaly_notified_at = {}
    return s


def test_subscription_anomaly_notified_after_threshold():
    core, s = _Core(), None
    s = _sched(core)
    now = time.time()
    for _ in range(2):
        s._maybe_notify_anomaly(_Sub(), "timeout", now)
    assert core.agenda == []  # 未达阈值不打扰
    s._maybe_notify_anomaly(_Sub(), "timeout", now)
    assert len(core.agenda) == 1
    assert "连续 3 次" in core.agenda[0]["text"]
    assert core.agenda[0]["priority"] == priority_for("subscription_anomaly")
    assert core.agenda[0]["source"].startswith("subscription_anomaly:")


def test_subscription_anomaly_cooldown():
    core, s = _Core(), None
    s = _sched(core)
    now = time.time()
    for _ in range(5):
        s._maybe_notify_anomaly(_Sub(), "err", now)
    assert len(core.agenda) == 1  # 冷却期内不重复


def test_subscription_anomaly_resumes_after_cooldown():
    core = _Core(_Cfg(cooldown=60))
    s = _sched(core)
    now = time.time()
    for _ in range(3):
        s._maybe_notify_anomaly(_Sub(), "err", now)
    assert len(core.agenda) == 1
    for _ in range(3):
        s._maybe_notify_anomaly(_Sub(), "err", now + 120)
    assert len(core.agenda) == 2


def test_subscription_anomaly_disabled():
    core = _Core(_Cfg(enabled=False))
    s = _sched(core)
    for _ in range(9):
        s._maybe_notify_anomaly(_Sub(), "err", time.time())
    assert core.agenda == []


def test_subscription_anomaly_no_target_group():
    core = _Core()
    s = _sched(core)
    for _ in range(5):
        s._maybe_notify_anomaly(_Sub(groups=()), "err", time.time())
    assert core.agenda == []


def test_success_resets_fail_streak():
    """成功一次应清零（在 tick 里调用），否则偶发失败会累积到阈值。"""
    core = _Core()
    s = _sched(core)
    s._fail_streak["s1"] = 2
    s._fail_streak.pop("s1", None)  # tick 成功路径的同样动作
    s._maybe_notify_anomaly(_Sub(), "err", time.time())
    assert core.agenda == []  # 只失败 1 次，不提醒
