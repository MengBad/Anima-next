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


# ── 8. 承诺质量闸（2026-10-21 首次自主触发的生产实证）──────────────────


def test_followable_debt_rejects_filler():
    """🔴 生产实证：首次自主触发的第一个任务就是废的 ——
    报备「跟进这个承诺：本大爷这就帮你去瞅瞅……」，
    Agent 检索不到可跟进事项，重规划后只能回「无法执行」。
    根因是 debts.json 里 `{due_ts:None, fuzzy:True, content:"本大爷这就帮你去瞅瞅……"}`
    被 check_due 按「创建满 1 天」放行。
    """
    from anima.agent.auto_trigger import is_followable_debt_text as F

    assert F("本大爷这就帮你去瞅瞅……") is False, "语气词不得触发自动任务"
    assert F("这就去看看") is False
    assert F("好的喵") is False
    assert F("嗯") is False
    assert F("") is False
    assert F("……") is False


def test_followable_debt_accepts_real_promises():
    from anima.agent.auto_trigger import is_followable_debt_text as F

    assert F("明天帮你查一下那个番剧的更新时间") is True
    assert F("下周三提醒大家交作业") is True
    assert F("帮你去群里问问有没有人有MC服务器") is True


def test_debt_trigger_filters_filler_end_to_end(monkeypatch):
    """端到端：即便 check_due 返回语气词债，也不得产出触发决策。"""
    import types as _t
    import anima.agent.auto_trigger as at

    class _FakeMgr:
        def check_due(self, now=None):
            alpha = _t.SimpleNamespace(content="本大爷这就帮你去瞅瞅……")
            beta = _t.SimpleNamespace(content="明天帮你查一下那个番剧的更新时间")
            return [alpha, beta]

    monkeypatch.setattr(
        "anima.cognitive.debt.get_debt_manager",
        lambda data_dir: _FakeMgr(),
    )
    core = _t.SimpleNamespace(
        config=_t.SimpleNamespace(
            persistence=_t.SimpleNamespace(data_dir="data"),
        )
    )
    got = at.collect_debt_triggers(core)
    assert len(got) == 1, f"只应保留真承诺，实得 {[g.request for g in got]}"
    assert "番剧" in got[0].request


# ── 2026-10-29：报备文案不能复述 bot 自己的话 ────────────────────────

def test_debt_request_is_imperative_not_echo():
    """承诺到期触发的 `request` 必须是**任务指令**，不是复述原话。

    生产实证（10-29 00:00:45，owner 原话「为什么这么多消息」）：

        本喵去干个活喵：跟进这个承诺并给出结果：
            等下，本喵这就给你找张最搞笑的……喏，看这个喵~
        为什么：你之前答应过这件事，现在到时间了：
            等下，本喵这就给你找张最搞笑的……喏，看这个喵~

    两行**一模一样** —— `content` 是 bot 自己说过的话（含语气词、
    未完成半句），被同时拼进 request 和 reason，planner 只能复述它。
    """
    from anima.agent.auto_trigger import collect_debt_triggers
    from anima.cognitive import debt as debt_mod

    SENTENCE = "等下，本喵这就给你找张最搞笑的……喏，看这个喵~"

    class _Debt:
        id = "com_test"
        content = SENTENCE
        due_at = 0.0

    class _Mgr:
        def check_due(self, now=None):
            return [_Debt()]

    class _Cfg:
        class persistence:
            data_dir = "data"

    class _Core:
        config = _Cfg()

    orig = debt_mod.get_debt_manager
    debt_mod.get_debt_manager = lambda *a, **k: _Mgr()
    try:
        out = collect_debt_triggers(_Core())
    finally:
        debt_mod.get_debt_manager = orig

    assert out, "到期承诺应产生一条触发决策"
    req, why = out[0].request, out[0].reason
    assert req != why, "request 与 reason 不能是同一段文本（这正是复述的形态）"
    assert "履行" in req or "做到" in req, "request 应是祈使式任务指令"
    assert "不要只是重复" in req, "应明确禁止复述原话"
    # 引用时省略号会被收敛（`……` → `…`），所以按「主干」断言
    assert "本喵这就给你找张最搞笑的" in req, "原话主干应被引用（保留上下文）"
    assert SENTENCE not in why, "reason 不该再重复整段原话"


def test_clean_quote_strips_ellipsis_pile():
    """省略号堆叠收敛成一个，避免引用里出现 `……` 后又跟 `…`。"""
    from anima.agent.auto_trigger import _clean_quote
    got = _clean_quote("等下，本喵这就给你找张最搞笑的……喏，看这个喵~")
    assert "……" not in got
    assert len(got) <= 80


def test_clean_quote_truncates_at_sentence_boundary():
    """超长时优先在完整句末截断，而不是硬切。"""
    from anima.agent.auto_trigger import _clean_quote
    got = _clean_quote("第一句很短。第二句特别特别长" + "拖" * 200)
    assert got.startswith("第一句很短")


# ── 2026-10-29：步骤推送闸门不能用 created_at ───────────────────────

def test_step_notify_gate_ignores_created_at():
    """`created_at` 含**排队等待**时间，不能用来判「跑得够久」。

    生产实证（10-29 00:00~00:02，同一分钟内三条报备）：

        id=5ed918804c created=00:00:45  完成=00:01:40  steps=2 replan=1
        id=d1cd236057 created=00:01:17  完成=00:03:18  steps=3 replan=2
        id=36063750df created=00:01:48  完成=00:02:24  steps=3 replan=1

    三个任务**全部 2~3 步**（远低于 5 步门槛），却都推了 `[1/2]` `[2/3]`
    `[重规划 #1]` —— 因为 `created_at` 距执行已 >60s，判据②被误触发。
    """
    import time
    from anima.agent.executor import AgentExecutor

    class _Task:
        steps = [1, 2, 3]          # 3 步 < 5
        id = "t"
        created_at = time.time() - 300   # 创建于 5 分钟前（排队）

    ex = AgentExecutor.__new__(AgentExecutor)
    assert ex._should_notify_steps(_Task()) is False, (
        "未记录执行起点时必须不推（fail-closed）；"
        "用 created_at 会让所有「到期才启动」的短任务刷屏"
    )
    # 记录执行起点后，真正跑够 60s 才推
    ex._exec_started_at = time.time() - 90
    assert ex._should_notify_steps(_Task()) is True
    ex._exec_started_at = time.time() - 5
    assert ex._should_notify_steps(_Task()) is False


def test_step_notify_gate_long_task_always_notifies():
    """步数够多的任务无条件推送（用户在等）。"""
    import time
    from anima.agent.executor import AgentExecutor

    class _Task:
        steps = list(range(6))     # 6 步 >= 5
        id = "t"
        created_at = time.time()

    ex = AgentExecutor.__new__(AgentExecutor)
    assert ex._should_notify_steps(_Task()) is True


# ── 2026-10-29：最小间隔闸门（防心跳连拍打光配额） ───────────────────

def test_gate_blocks_within_min_interval():
    """两次自主启动之间必须间隔 `min_interval_sec`。

    生产实证（10-29 00:00~00:02）：22 秒内连发 3 个 debt_due 任务，
    owner 直接问「为什么这么多消息」。根因：
      · 闸门只有「每日上限」+「睡眠/免打扰」，**没有最小间隔**
      · 心跳 30 秒一拍，每拍都启动一个
      · 10 条到期承诺 + daily_max=3 ⇒ 一分半钟全部打光
    """
    from anima.agent.auto_trigger import AutoTriggerGate, AutoTriggerState

    class _P:
        def read_json(self, *a, **k): return {}
        def write_json(self, *a, **k): pass

    g = AutoTriggerGate(daily_max=20, min_interval_sec=1800)
    st = AutoTriggerState(_P())
    now = 1_790_000_000.0

    # 从未启动过 ⇒ 放行
    st.last_started_at = 0.0
    assert g.blocked_reason(hour=12, state=st, now=now) == ""

    # 刚启动过 ⇒ 拦
    st.last_started_at = now - 22          # 22 秒前（正是生产实测的间隔）
    reason = g.blocked_reason(hour=12, state=st, now=now)
    assert reason.startswith("距上次自主启动不足"), reason

    # 过了 30 分钟 ⇒ 放行
    st.last_started_at = now - 1801
    assert g.blocked_reason(hour=12, state=st, now=now) == ""


def test_min_interval_zero_disables():
    """`min_interval_sec=0` 表示关闭该闸（保持旧行为，便于运维）。"""
    from anima.agent.auto_trigger import AutoTriggerGate, AutoTriggerState

    class _P:
        def read_json(self, *a, **k): return {}
        def write_json(self, *a, **k): pass

    g = AutoTriggerGate(daily_max=20, min_interval_sec=0)
    st = AutoTriggerState(_P())
    now = 1_790_000_000.0
    st.last_started_at = now - 1
    assert g.blocked_reason(hour=12, state=st, now=now) == ""


def test_last_started_at_survives_roll_day():
    """跨天**不重置** `last_started_at`。

    否则 23:59 启动一次、00:00 又能立刻启动 —— 跨天绕过间隔。
    """
    from anima.agent.auto_trigger import AutoTriggerState

    class _P:
        def read_json(self, *a, **k): return {}
        def write_json(self, *a, **k): pass

    st = AutoTriggerState(_P())
    st.day = "2026-10-28"
    st.last_started_at = 12345.0
    st.roll_day("2026-10-29")
    assert st.last_started_at == 12345.0, "跨天不该重置间隔基准"


def test_last_started_at_persists():
    """必须落盘，否则重启后间隔闸失效（可立刻再启动一个）。"""
    from anima.agent.auto_trigger import AutoTriggerState

    saved = {}

    class _P:
        def read_json(self, *a, **k): return {}
        def write_json(self, name, data, *a, **k): saved.update(data)

    st = AutoTriggerState(_P())
    st.last_started_at = 999.5
    st.save()
    assert saved.get("last_started_at") == 999.5


# ── 2026-10-29：承诺质量闸 —— 用生产真实数据做回归 ──────────────────

#: 生产 `commitments.json` 里 10 条「到期未用」的原文（2026-10-29 09:15 实测）。
#: 全部来自 bot 自己说过的话，**没有一条是真承诺**。
_PRODUCTION_JUNK = [
    "嗯，记下了，这就去热喵",
    "“嘿....嘿嘿....我...我会一直默默看着你...不要...不要觉得...觉得可以逃掉...我..我会一直陪..陪着你....永远..永远...嘿...嘿嘿嘿”",
    "要我来帮你○吗",
    "给萌新推荐强力的指挥官:\n1，阿塔尼斯 v0 达拉姆大主教 全模式最顶级的肉度与容错",
    "切，说得好像本喵稀罕你喜欢似的……少来这套肉麻的话，哼！不过……真、真的假的啊？（尾巴不自在地晃了两下）好啦好啦，不跟你计较这个",
    "你还没绑定过雨云账号呢喵。要想绑定的话，把你的雨云登录名和密码告诉我，我帮你记下来，以后就能天天帮你自动签到领积分啦~",
    "哼，还挺会使唤人的……不过既然是华初拜托的，本喵就勉为其难帮你在后台记下了。以后每天自动签到，积分跑不掉的喵~",
    "急什么急，这就帮你去戳雨云的服务器了……不过刚才网速有点卡，本喵再试一下喵~",
    "哼，这还要问呀？\n让我瞅瞅……你今天早就已经签过到啦，积分安安稳稳地躺在账户里呢喵~ 笨蛋主人，这种小事还要本喵帮你盯着",
    "哼，这还要本喵提醒呀？\n让我瞅瞅……你今天早就已经签过到啦，积分安安稳稳地躺在账户里呢喵~ 笨蛋主人，这种小事还要本喵帮你盯着",
]

_PRODUCTION_REAL = [
    "等下，本喵这就给你找张最搞笑的……喏，看这个喵~",
    "等下……本喵这就给你捏一张带着可爱猫耳发箍的，看你还满意不喵~",
    "我这就去整理一份这周的群聊总结报告给你",
    "我这就帮你查一下那个雨云账号的情况",
]


@pytest.mark.parametrize("text", _PRODUCTION_JUNK)
def test_production_junk_is_not_followable(text):
    """生产实测的 10 条「到期承诺」必须**全部**判定为不可跟进。

    这是「为什么这么多消息」的根因：`debts.json` 存的是 bot 上一句话，
    没有质量闸时整段闲聊都会被当待办，于是 22 秒连发 3 个必失败的任务。
    """
    from anima.agent.auto_trigger import is_followable_debt_text
    assert is_followable_debt_text(text) is False, (
        f"这条不是承诺却通过了闸门：{text[:50]!r}"
    )


@pytest.mark.parametrize("text", _PRODUCTION_REAL)
def test_production_real_commitment_passes(text):
    """真承诺不能被误伤（否则自主跟进功能等于被关掉）。"""
    from anima.agent.auto_trigger import is_followable_debt_text
    assert is_followable_debt_text(text) is True, (
        f"真承诺被误拦了（漏跟进）：{text[:50]!r}"
    )


def test_completed_dialogue_rejected():
    """「已经做完」的对话不是承诺。"""
    from anima.agent.auto_trigger import is_followable_debt_text
    assert not is_followable_debt_text(
        "让我瞅瞅……你今天早就已经签过到啦，积分安安稳稳地躺在账户里呢喵~")
    assert not is_followable_debt_text("我帮你把雨云签到搞定啦，积分已经到账喵~")


def test_vague_promise_without_object_rejected():
    """「这就给你翻出来喵」有动作词但**没说翻什么** ⇒ 不可跟进。"""
    from anima.agent.auto_trigger import is_followable_debt_text
    assert not is_followable_debt_text("急什么急，这就给你翻出来喵~")


def test_ball_in_user_court_rejected():
    """球在用户脚下的（等你把密码给我）不是承诺。"""
    from anima.agent.auto_trigger import is_followable_debt_text
    assert not is_followable_debt_text(
        "你还没绑定过雨云账号呢喵。要想绑定的话，把你的登录名和密码告诉我")
