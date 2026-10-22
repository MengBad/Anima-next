"""P2 · 长期目标（Goal）契约（2026-10-23）。

设计稿：reports/2026-10-23_p2_goal_design.md
数据备份（改动前）：/opt/anima/backups/2026-10-23_p2_0509/

## 为什么有这个模块

P1 自主触发**机制已通但空转**：承诺 pending=0、健康异常仅 info 级
⇒ 它每天醒来发现没事可做。Goal 是自主触发的**第一个真实数据源**。

## 本测试锁死的边界

1. Goal 是上层容器（objective / constraints / success_criteria / cadence）；
2. 到期判定：cadence=0 永不自动派生；到期后 mark_run 即不再到期；
3. **派生时带上次进展**（否则每次推进都是失忆重来，那不叫持续推进）；
4. 意图识别：goal 必须抢在 do_task 之前（「帮我盯着」≠「帮我查一下」）；
5. owner-only：非 owner 不能建立目标；
6. fail-closed：文件损坏 / 缺 persistence ⇒ 不崩、返回空/诚实指引；
7. 不污染：独立 data/goals.json，不动 owner_tasks / debts。
"""
import types

import pytest

from anima.agent.goal import (
    GoalStore, format_goal_list, is_goal_request, parse_goal,
)
from anima.llm.request_executor import classify_request_intent


class _Mem:
    def __init__(self, data=None):
        self.data = data if data is not None else {}

    def read_json(self, name, default=None):
        v = self.data.get(name, default)
        return v

    def write_json(self, name, value):
        self.data[name] = value


def _store(data=None):
    return GoalStore(_Mem(data if data is not None else {}))


# ── 1. 意图识别：goal 必须在 do_task 之前 ──────────────────────────────


@pytest.mark.parametrize("text", [
    "帮我盯着 X 群的动态",
    "每周帮我整理一次 Y",
    "持续关注那个番的更新",
    "每天盯一下服务器状态",
    "长期跟进这个项目的进展",
])
def test_goal_intent_recognized(text):
    """🔴 「帮我盯着」= 长期目标，不是一次性任务。"""
    assert classify_request_intent(text) == "goal"


@pytest.mark.parametrize("text", [
    "帮我修下代码",
    "记住我不吃香菜",
    "别做了",
    "今天天气不错",
])
def test_other_intents_not_stolen_by_goal(text):
    """goal 词表不得抢走其他意图。"""
    assert classify_request_intent(text) != "goal"


@pytest.mark.parametrize("text", ["我的目标", "你在追什么", "目标列表"])
def test_goal_view_requests_recognized(text):
    """查看类也必须进 goal 分支（否则被判 chat，走不到）。"""
    assert classify_request_intent(text) == "goal"


def test_is_goal_request_boundary():
    """边界：盯=长期 vs 查=一次性。"""
    assert is_goal_request("帮我盯着 X") is True
    assert is_goal_request("帮我查一下 X") is False


# ── 2. 解析 ───────────────────────────────────────────────────────────


def test_parse_extracts_cadence():
    _, _, _, cad = parse_goal("每周帮我整理一次 Y")
    assert cad == 7 * 86400.0
    _, _, _, cad2 = parse_goal("每天盯一下服务器")
    assert cad2 == 86400.0


def test_parse_no_cadence_when_absent():
    """识别不到周期 ⇒ 0（不自动派生），由 owner 决定何时推动。"""
    _, _, _, cad = parse_goal("帮我盯着 X 群")
    assert cad == 0.0


def test_parse_strips_trigger_prefix():
    obj, _, _, _ = parse_goal("帮我盯着 X 群的动态")
    assert obj == "X 群的动态", f"应剥掉触发词前缀，实得 {obj!r}"


def test_parse_does_not_invent_long_criteria():
    """🔴 保守：过长的分句不得被当成验收标准（实测踩过整句塞进去）。"""
    _, _, crit, _ = parse_goal("每天盯一下服务器状态，不要打扰别人，稳定就算达标")
    for c in crit:
        assert len(c) <= 30, f"标准应简短，实得 {c!r}"


# ── 3. 到期与推进 ─────────────────────────────────────────────────────


def test_cadence_zero_never_due():
    g = _store().create("盯着 X", 42, cadence_sec=0)
    assert g.due() is False, "cadence=0 ⇒ 永不自动派生"


def test_new_goal_is_due_immediately():
    """新建（last_run=0）且 cadence>0 ⇒ 应尽快推进第一次。"""
    import time
    g = _store().create("盯着 X", 42, cadence_sec=3600)
    assert g.due(now=time.time()) is True


def test_mark_run_clears_due():
    import time
    st = _store()
    g = st.create("盯着 X", 42, cadence_sec=3600)
    st.mark_run(g.id, "task1")
    assert st.get(g.id).due(now=time.time()) is False, "推进过就不该立即再推"


def test_add_note_bounded():
    st = _store()
    g = st.create("盯着 X", 42)
    for i in range(30):
        st.add_note(g.id, f"进展{i}")
    assert len(st.get(g.id).notes) <= 20, "进展必须有界"


def test_persistence_roundtrip():
    import time
    mem = _Mem()
    st = GoalStore(mem)
    g = st.create("盯着 X", 42, cadence_sec=3600)
    st.add_note(g.id, "第一次进展")
    st2 = GoalStore(mem)
    assert g.id in st2.items, "应能重新读回"
    assert st2.get(g.id).notes == ["第一次进展"]


def test_corrupted_data_is_skipped():
    """fail-closed：坏数据不得让整个 store 崩。"""
    st = _store({"goals.json": [{"id": "ok1", "objective": "正常", "owner": 1}, "garbage", 123]})
    assert "ok1" in st.items
    assert len(st.items) == 1, "坏条目应被跳过，好条保留"


# ── 4. 列表展示 ───────────────────────────────────────────────────────


def test_format_empty_list_is_honest():
    assert "没有" in format_goal_list(_store())


def test_format_list_contains_id_and_status():
    st = _store()
    g = st.create("盯着 X", 42)
    out = format_goal_list(st)
    assert g.id in out
    assert "active" in out


# ── 5. 心跳派生：必须带上次进展 ───────────────────────────────────────


def test_goal_decision_carries_last_progress():
    """🔴 否则每次推进都是失忆的重来 —— 那不叫持续推进。"""
    import time
    from anima.core.autonomy import Heartbeat

    st = _store()
    g = st.create("整理群动态", 42, cadence_sec=3600)
    st.add_note(g.id, "上次整理到 10-22")

    hb = Heartbeat.__new__(Heartbeat)
    hb.core = types.SimpleNamespace(persistence=st.persistence)
    dec = hb._goal_decisions(time.time())
    assert len(dec) == 1
    assert "上次整理到 10-22" in dec[0].request, f"应带上次进展：{dec[0].request[:80]}"
    assert dec[0].source == f"goal:{g.id}"


def test_goal_decision_absent_when_not_due():
    import time
    from anima.core.autonomy import Heartbeat

    st = _store()
    g = st.create("整理群动态", 42, cadence_sec=3600)
    st.mark_run(g.id, "t1")
    hb = Heartbeat.__new__(Heartbeat)
    hb.core = types.SimpleNamespace(persistence=st.persistence)
    assert hb._goal_decisions(time.time()) == []


def test_goal_decision_missing_persistence_is_safe():
    import time
    from anima.core.autonomy import Heartbeat
    hb = Heartbeat.__new__(Heartbeat)
    hb.core = types.SimpleNamespace(persistence=None)
    assert hb._goal_decisions(time.time()) == []


# ── 6. 两处词表必须一致（防判据分叉）───────────────────────────────────


def test_goal_trigger_tables_stay_in_sync():
    """🔴 `request_executor._TRIGGER["goal"]` 与 `goal.GOAL_TRIGGERS`
    必须一致 —— 否则会出现「分类说 goal、解析说不是」的撕裂状态。

    实测踩过：「每天盯一下服务器状态」含「盯一下」，
    而当时两处词表都只收「盯着」 ⇒ 判成 chat，goal 分支走不到。
    """
    from anima.agent.goal import GOAL_TRIGGERS
    from anima.llm.request_executor import _TRIGGER

    a = set(_TRIGGER["goal"])
    b = set(GOAL_TRIGGERS)
    assert a == b, (
        f"两处 goal 词表不一致（会导致分类与解析撕裂）：\n"
        f"  仅 executor 有: {sorted(a - b)}\n"
        f"  仅 goal.py 有: {sorted(b - a)}"
    )


@pytest.mark.parametrize("text", [
    "每天盯一下服务器状态",
    "盯着那个群的动静",
    "定期看看项目进展",
])
def test_common_phrasings_hit_goal(text):
    """常见说法都要能命中（避免「盯着」vs「盯一下」这类漏判）。"""
    assert classify_request_intent(text) == "goal", f"{text!r} 应命中 goal"


# ── 7. 任务启动后必须立即标记 Goal 已推进（防重复派生）─────────────────


def test_goal_decision_carries_goal_id():
    """决策必须带 goal_id —— 否则启动任务后无法回写，会重复派生。

    生产实测（2026-10-23）：任务 b96b79c59e 四步全部 completed，
    但 `goal.last_run_at` 仍为 0 ⇒ 下一个心跳会立刻重复派生同一目标。
    根因：`mark_run()` 在心跳里**从未被调用**（设计稿写了，实现漏了）。
    """
    import time
    from anima.core.autonomy import Heartbeat

    st = _store()
    g = st.create("整理群动态", 42, cadence_sec=3600)

    hb = Heartbeat.__new__(Heartbeat)
    hb.core = types.SimpleNamespace(persistence=st.persistence)
    dec = hb._goal_decisions(time.time())

    assert len(dec) == 1
    assert dec[0].goal_id == g.id, "决策必须携带 goal_id"


def test_mark_run_prevents_immediate_rederive():
    """🔴 端到端语义：标记推进后，同一 cadence 周期内不得再派生。"""
    import time
    from anima.core.autonomy import Heartbeat

    st = _store()
    g = st.create("整理群动态", 42, cadence_sec=3600)
    hb = Heartbeat.__new__(Heartbeat)
    hb.core = types.SimpleNamespace(persistence=st.persistence)

    assert len(hb._goal_decisions(time.time())) == 1, "前置：应立即到期"
    st.mark_run(g.id, "task_x")
    assert hb._goal_decisions(time.time()) == [], "标记推进后不得立即重复派生"


def test_auto_trigger_marks_goal_after_submit_source_level():
    """源码级契约：`_auto_trigger_tick` 必须在 submit 后调用 mark_run。

    为什么用源码级：完整链路需要 agent/gateway 全套桩，成本高且易漏；
    本次 bug 的性质是「漏了一次调用」，顺序/存在性检查即可锁定。
    """
    import inspect
    from anima.core.autonomy import Heartbeat

    src = inspect.getsource(Heartbeat._auto_trigger_tick)
    i_submit = src.find("await agent.submit")
    i_mark = src.find("mark_run")
    assert i_submit > 0, "应有 submit 调用"
    assert i_mark > 0, "提交后必须标记 Goal 推进（否则重复派生）"
    assert i_submit < i_mark, "mark_run 必须在 submit 之后"
