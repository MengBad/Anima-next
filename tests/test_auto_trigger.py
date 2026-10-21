"""P1 自主触发契约（2026-10-21）—— 先报备，再执行。

## owner 拍板

计划书 P1 原设计「提议制」（只建待办等批）被 owner **明确否掉**，要求直接执行。
但直接执行 = 它会自己启动任务、花 token、可能改文件/联网/发消息。
折中实现为 **报备制**：

    判断该干活 → 立刻私聊报备（做什么/为什么/几步）→ 马上开跑（不等批准）
                                                       → 可 cancel → 完成后推结果

## 本测试锁死的边界

1. **默认关**：`auto_trigger_enabled=False` ⇒ 永远不启动（与 danger.* 同哲学）；
2. 开启后：有触发源 ⇒ **先报备再提交任务**，且报备只发私聊；
3. **每日上限**：达上限不再启动；
4. **主题去重**：当日已触发过的同主题不再启动；
5. **静默时段**：睡眠/免打扰窗口内不启动；
6. **熔断**：连续无产出达阈值 ⇒ 暂停一段时间；
7. fail-closed：owners 为空 / 无 agent / 报备发送失败 ⇒ 不崩、不伪造；
8. 审计 `auto_trigger_started`（可事后归因「它为什么自己动了」）。
"""
import types

import pytest

from anima.agent.auto_trigger import (
    AutoTriggerDecision, AutoTriggerGate, AutoTriggerState,
    COOLDOWN_AFTER_TRIP_SEC, DEFAULT_DAILY_MAX, FAIL_STREAK_TRIP,
    dedupe_against, topic_key, topic_similarity,
)
from anima.core.autonomy import Heartbeat

OWNER = 42


class _Mem:
    def __init__(self):
        self.data = {}

    def read_json(self, name, default=None):
        return self.data.get(name, default)

    def write_json(self, name, value):
        self.data[name] = value


class _GW:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    async def send(self, msg):
        if self.fail:
            raise RuntimeError("QQ 断了")
        self.sent.append(msg)


class _Auditor:
    def __init__(self):
        self.events = []

    def emit(self, name, **kw):
        self.events.append((name, kw))


class _Agent:
    def __init__(self):
        self.submitted = []

    async def submit(self, request, creator, *, auto_run=True, session=None):
        t = types.SimpleNamespace(id="auto001", request=request, creator=creator)
        self.submitted.append(t)
        return t


def _core(*, enabled=True, owners=(OWNER,), gw=None, agent=None, auditor=None,
          daily_max=3, mem=None, quiet=False):
    auto_cfg = types.SimpleNamespace(
        auto_trigger_enabled=enabled,
        auto_trigger_daily_max=daily_max,
        sleep_enabled=False,
        quiet_hours_enabled=quiet,
        quiet_hours_start_hour=23,
        quiet_hours_end_hour=7,
    )
    return types.SimpleNamespace(
        config=types.SimpleNamespace(
            persistence=types.SimpleNamespace(data_dir="data"),
            permissions=types.SimpleNamespace(owners=list(owners)),
            autonomy=auto_cfg,
        ),
        persistence=mem or _Mem(),
        gateway=gw,
        agent=agent,
        audit=auditor,
    )


def _hb(core):
    hb = Heartbeat.__new__(Heartbeat)
    hb.core = core
    hb._bj_date_str = lambda ts: "2026-10-21"
    return hb


# ── 1. 纯函数：闸门 ───────────────────────────────────────────────────


def test_default_off_by_config():
    from anima.core.config import AnimaConfig
    c = AnimaConfig()
    assert c.autonomy.auto_trigger_enabled is False, (
        "自主触发默认必须关闭（与 danger.* 同哲学：默认关、显式开）"
    )
    assert c.autonomy.auto_trigger_daily_max == 3


def _blank_state(**kw):
    st = AutoTriggerState.__new__(AutoTriggerState)
    st.day = "2026-10-21"
    st.count_today = kw.get("count", 0)
    st.fail_streak = kw.get("streak", 0)
    st.cooldown_until = kw.get("cd", 0.0)
    st.topics_today = kw.get("topics", [])
    return st


def test_gate_daily_max():
    g = AutoTriggerGate(daily_max=3)
    assert g.blocked_reason(hour=14, state=_blank_state(count=2)) == ""
    assert "上限" in g.blocked_reason(hour=14, state=_blank_state(count=3))


def test_gate_quiet_and_sleep_windows():
    g = AutoTriggerGate(quiet_hours=(23, 7))
    assert "免打扰" in g.blocked_reason(hour=2, state=_blank_state())
    assert "免打扰" in g.blocked_reason(hour=23, state=_blank_state())
    assert g.blocked_reason(hour=12, state=_blank_state()) == ""

    s = AutoTriggerGate(sleep_hours=(1, 5))
    assert "睡眠" in s.blocked_reason(hour=3, state=_blank_state())
    assert s.blocked_reason(hour=10, state=_blank_state()) == ""


def test_gate_cooldown():
    g = AutoTriggerGate()
    st = _blank_state(cd=2_000_000_000.0)
    assert "熔断" in g.blocked_reason(hour=14, state=st, now=1_000_000_000.0)


def test_topic_dedup():
    topics = ["跟进这个承诺：明天交报告"]
    assert dedupe_against(topics, "跟进这个承诺：明天交报告") is True
    assert dedupe_against(topics, "排查运行时异常") is False


# ── 2. 默认关 ⇒ 绝不启动 ──────────────────────────────────────────────


async def test_disabled_never_submits():
    agent = _Agent()
    core = _core(enabled=False, agent=agent, gw=_GW())
    hb = _hb(core)

    # 即便触发源有货也不能启动
    import anima.agent.auto_trigger as at
    orig = at.collect_triggers
    at.collect_triggers = lambda core, now=None: [
        AutoTriggerDecision("debt_due", "做个事", "到点了")
    ]
    try:
        await hb._auto_trigger_tick(now=1000.0)
    finally:
        at.collect_triggers = orig

    assert agent.submitted == [], "总开关关闭时绝不允许自主启动"


# ── 3. 开启后：先报备，再执行 ─────────────────────────────────────────


async def test_enabled_submits_and_notifies_privately():
    agent = _Agent()
    gw = _GW()
    auditor = _Auditor()
    core = _core(enabled=True, agent=agent, gw=gw, auditor=auditor)
    hb = _hb(core)

    import anima.agent.auto_trigger as at
    orig = at.collect_triggers
    at.collect_triggers = lambda core, now=None: [
        AutoTriggerDecision("debt_due", "跟进交报告这件事", "你之前答应过")
    ]
    try:
        await hb._auto_trigger_tick(now=1000.0)
    finally:
        at.collect_triggers = orig

    assert len(agent.submitted) == 1, "应启动 1 个任务"
    assert len(gw.sent) == 1, "应发 1 条报备"
    msg = gw.sent[0]
    assert msg.detail_type == "private", "报备必须走私聊，不得发群"
    assert "auto001" in msg.message, "报备必须含 task_id（便于叫停）"
    assert "为什么" in msg.message, "报备必须说明原因"
    assert "cancel" in msg.message, "必须给出叫停方式"
    names = [e[0] for e in auditor.events]
    assert "auto_trigger_started" in names, f"应写审计，实得 {names}"


async def test_notify_failure_still_submits():
    """报备发送失败不阻断执行 —— 「没报备」比「不干活」危害小。"""
    agent = _Agent()
    core = _core(enabled=True, agent=agent, gw=_GW(fail=True))
    hb = _hb(core)

    import anima.agent.auto_trigger as at
    orig = at.collect_triggers
    at.collect_triggers = lambda core, now=None: [
        AutoTriggerDecision("debt_due", "做个事", "到点了")
    ]
    try:
        await hb._auto_trigger_tick(now=1000.0)
    finally:
        at.collect_triggers = orig

    assert len(agent.submitted) == 1, "报备失败也应继续执行"


# ── 4. 每日上限 ───────────────────────────────────────────────────────


async def test_daily_max_blocks_further_starts():
    agent = _Agent()
    mem = _Mem()
    mem.data["auto_trigger.json"] = {
        "day": "2026-10-21", "count_today": 3, "topics_today": [],
        "fail_streak": 0, "cooldown_until": 0.0,
    }
    core = _core(enabled=True, agent=agent, gw=_GW(), daily_max=3, mem=mem)
    hb = _hb(core)

    import anima.agent.auto_trigger as at
    orig = at.collect_triggers
    at.collect_triggers = lambda core, now=None: [
        AutoTriggerDecision("debt_due", "另一个事", "到点了")
    ]
    try:
        await hb._auto_trigger_tick(now=1000.0)
    finally:
        at.collect_triggers = orig

    assert agent.submitted == [], "已达每日上限不得再启动"


# ── 5. 主题去重 ───────────────────────────────────────────────────────


async def test_same_topic_not_started_twice():
    agent = _Agent()
    mem = _Mem()
    mem.data["auto_trigger.json"] = {
        "day": "2026-10-21", "count_today": 1,
        "topics_today": ["跟进交报告这件事"],
        "fail_streak": 0, "cooldown_until": 0.0,
    }
    core = _core(enabled=True, agent=agent, gw=_GW(), mem=mem)
    hb = _hb(core)

    import anima.agent.auto_trigger as at
    orig = at.collect_triggers
    at.collect_triggers = lambda core, now=None: [
        AutoTriggerDecision("debt_due", "跟进交报告这件事", "到点了")
    ]
    try:
        await hb._auto_trigger_tick(now=1000.0)
    finally:
        at.collect_triggers = orig

    assert agent.submitted == [], "同主题当日不得重复启动"


# ── 6. 熔断 ───────────────────────────────────────────────────────────


async def test_fail_streak_trips_cooldown():
    agent = _Agent()
    mem = _Mem()
    mem.data["auto_trigger.json"] = {
        "day": "2026-10-21", "count_today": 1, "topics_today": [],
        "fail_streak": FAIL_STREAK_TRIP, "cooldown_until": 0.0,
    }
    core = _core(enabled=True, agent=agent, gw=_GW(), mem=mem)
    hb = _hb(core)

    import anima.agent.auto_trigger as at
    orig = at.collect_triggers
    at.collect_triggers = lambda core, now=None: [
        AutoTriggerDecision("debt_due", "做个事", "到点了")
    ]
    try:
        await hb._auto_trigger_tick(now=1000.0)
    finally:
        at.collect_triggers = orig

    assert agent.submitted == [], "连续无产出应熔断，不得启动"
    st = mem.data["auto_trigger.json"]
    assert st["cooldown_until"] > 0, "熔断应写入冷却时间"
    assert st["fail_streak"] == 0, "熔断后应重置计数"


# ── 7. fail-closed ───────────────────────────────────────────────────


async def test_no_owners_means_no_submit():
    agent = _Agent()
    core = _core(enabled=True, owners=(), agent=agent, gw=_GW())
    hb = _hb(core)

    import anima.agent.auto_trigger as at
    orig = at.collect_triggers
    at.collect_triggers = lambda core, now=None: [
        AutoTriggerDecision("debt_due", "做个事", "到点了")
    ]
    try:
        await hb._auto_trigger_tick(now=1000.0)
    finally:
        at.collect_triggers = orig

    assert agent.submitted == [], "owners 为空不得启动（不伪造目标）"


async def test_no_trigger_means_no_action():
    agent = _Agent()
    core = _core(enabled=True, agent=agent, gw=_GW())
    hb = _hb(core)

    import anima.agent.auto_trigger as at
    orig = at.collect_triggers
    at.collect_triggers = lambda core, now=None: []
    try:
        await hb._auto_trigger_tick(now=1000.0)
    finally:
        at.collect_triggers = orig

    assert agent.submitted == [], "无触发源时不得启动"
