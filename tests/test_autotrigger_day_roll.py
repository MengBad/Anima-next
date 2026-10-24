"""自主触发日切必须落盘（2026-10-24）。

## 生产实证的 bug

10-24 早上查 `auto_trigger.json`：

```json
{ "day": "2026-10-23", "count_today": 1, ... }
```

**今天已经是 10-24，文件里却还是昨天。**

## 根因

`AutoTriggerState.roll_day()` 只改**内存**，`save()` 只在
「熔断」或「实际触发」时调用。

⇒ 跨天后若当天**一直没触发**（例：目标 11:42 才到期、或整夜无触发源），
内存里已重置为今天、文件里仍是昨天。

⇒ **一旦重启，就从文件读回昨天的 day 与 count_today**，
**跨天重置失效，当天配额凭空少一次**（1/3 而不是 0/3）。

## 修法

`roll_day` 返回「是否真的跨天」；调用方在 True 时 `save()` 一次
（每天仅一次，开销可接受），并打一条 INFO 日志。

## 本测试锁死

1. 跨天返回 True，且**落盘后** day/count/topics 全部重置；
2. 同日再 roll 返回 False（不重复写盘，也不误重置）；
3. 重启后（新实例从文件读）必须是重置后的状态 —— 这是 bug 的核心症状。
"""
import pytest

from anima.agent.auto_trigger import AutoTriggerState
from anima.core.persistence import PersistenceProvider


def _state(tmp_path, day="2026-10-23", count=1, topics=None):
    P = PersistenceProvider(str(tmp_path))
    st = AutoTriggerState(P)
    st.day = day
    st.count_today = count
    st.topics_today = topics if topics is not None else ["旧主题"]
    st.save()
    return st, P


# ── 1. 跨天返回 True 并重置 ───────────────────────────────────────────


def test_roll_day_returns_true_on_new_day(tmp_path):
    st, _ = _state(tmp_path)
    assert st.roll_day("2026-10-24") is True
    assert st.day == "2026-10-24"
    assert st.count_today == 0
    assert st.topics_today == []


def test_roll_day_returns_false_same_day(tmp_path):
    st, _ = _state(tmp_path, day="2026-10-24", count=2)
    assert st.roll_day("2026-10-24") is False
    assert st.count_today == 2, "同日不得误重置"


# ── 2. 落盘（核心：修的就是这个）──────────────────────────────────────


def test_rollback_persisted_after_save(tmp_path):
    """跨天 + save 后，文件里必须是新的一天。"""
    st, P = _state(tmp_path)
    st.roll_day("2026-10-24")
    st.save()
    raw = P.read_json("auto_trigger.json", {}) or {}
    assert raw.get("day") == "2026-10-24", f"文件应记今天，实得 {raw.get('day')}"
    assert raw.get("count_today") == 0
    assert raw.get("topics_today") == []


def test_reset_survives_restart(tmp_path):
    """🔴 bug 的核心症状：重启后必须是重置后的状态。

    修之前：内存重置了但文件没写 ⇒ 新实例读回昨天 ⇒ 当天配额少一次。
    """
    st, P = _state(tmp_path)
    # 模拟 _auto_trigger_tick：跨天则 save
    if st.roll_day("2026-10-24"):
        st.save()

    # 重启：全新实例从文件读
    st2 = AutoTriggerState(PersistenceProvider(str(tmp_path)))
    assert st2.day == "2026-10-24", (
        f"重启后应为今天，实得 {st2.day}（跨天重置失效 = 当天配额变少）"
    )
    assert st2.count_today == 0, f"重启后配额应为 0，实得 {st2.count_today}"
    assert st2.topics_today == []


def test_without_save_day_stays_stale(tmp_path):
    """反证：若不 save，文件确实是旧的（说明 save 这一步是必需的）。"""
    st, P = _state(tmp_path)
    st.roll_day("2026-10-24")     # 只改内存，不 save
    raw = P.read_json("auto_trigger.json", {}) or {}
    assert raw.get("day") == "2026-10-23", "不落盘就还是昨天（正是生产看到的现象）"


# ── 3. 接线契约 ───────────────────────────────────────────────────────


def test_tick_saves_when_day_rolls():
    """源码契约：`_auto_trigger_tick` 必须在 roll_day 返回 True 时 save。"""
    import inspect
    from anima.core.autonomy import Heartbeat

    src = inspect.getsource(Heartbeat._auto_trigger_tick)
    assert "if state.roll_day(" in src, "必须用 roll_day 的返回值判断"
    assert "state.save()" in src, "跨天必须落盘"
    # save 必须在 roll_day 的 if 分支内
    i_roll = src.find("if state.roll_day(")
    i_save = src.find("state.save()", i_roll)
    assert i_save > i_roll, "save 必须在跨天分支内"
