"""P1-1 承诺追踪（Debt）回归测试 —— DESIGN_DEBT_TRACKING.md。

覆盖：时间解析 / 记账 / 状态机 / 完成取消信号 / 到期检查 / 落盘 / 空账不编造。
"""
from __future__ import annotations

import time

import pytest

from anima.cognitive.debt import (
    Debt,
    DebtManager,
    get_debt_manager,
    parse_due_hint,
)


@pytest.fixture
def mgr(tmp_path):
    return DebtManager(tmp_path)


# ---------------------------------------------------------------------------
# 1. 时间线索解析
# ---------------------------------------------------------------------------
def test_parse_tomorrow():
    now = time.time()
    ts, hint, fuzzy = parse_due_hint("我明天帮你查", now=now)
    assert ts is not None and not fuzzy
    assert abs(ts - (now + 86400)) < 5


def test_parse_day_after_tomorrow():
    now = time.time()
    ts, _, fuzzy = parse_due_hint("后天给你", now=now)
    assert ts is not None and not fuzzy
    assert abs(ts - (now + 2 * 86400)) < 5


def test_parse_hours():
    now = time.time()
    ts, _, fuzzy = parse_due_hint("3小时后提醒我", now=now)
    assert ts is not None and not fuzzy
    assert abs(ts - (now + 3 * 3600)) < 5


def test_parse_fuzzy_no_hard_guess():
    """解析不出来就标模糊，绝不硬算时间戳。"""
    ts, hint, fuzzy = parse_due_hint("下次帮你看看", now=time.time())
    assert ts is None and fuzzy is True and hint == "下次帮你看看"


def test_parse_empty():
    ts, hint, fuzzy = parse_due_hint("", now=time.time())
    assert ts is None and hint == "" and fuzzy is True


# ---------------------------------------------------------------------------
# 2. 记账与查询
# ---------------------------------------------------------------------------
def test_add_and_pending(mgr):
    d = mgr.add("帮阿布查 B 站 up 主开播时间", source_group="727631526",
                source_user="1562290139", beneficiary="阿布",
                source_text="我明天帮你查")
    assert d.status == "pending"
    assert mgr.pending("727631526") == [d]
    assert mgr.pending("999") == []


def test_add_persists(tmp_path):
    DebtManager(tmp_path).add("记账持久化验证", source_group="1")
    again = DebtManager(tmp_path)
    assert len(again.pending("1")) == 1
    assert again.pending("1")[0].content == "记账持久化验证"


# ---------------------------------------------------------------------------
# 3. 状态机
# ---------------------------------------------------------------------------
def test_complete(mgr):
    d = mgr.add("完成态验证")
    assert mgr.complete(d.id) is True
    assert d.status == "completed" and d.completed_at > 0
    assert mgr.pending() == []


def test_cancel(mgr):
    d = mgr.add("取消态验证")
    assert mgr.cancel(d.id) is True
    assert d.status == "cancelled"


def test_terminal_state_is_sticky(mgr):
    """入终态后不能再改（避免重复结掉同一条债）。"""
    d = mgr.add("终态粘滞")
    mgr.complete(d.id)
    assert mgr.complete(d.id) is False
    assert mgr.cancel(d.id) is False


def test_complete_by_keyword_done(mgr):
    mgr.add("帮你查开播时间", source_group="7")
    assert mgr.complete_by_keyword("搞定了，谢谢", "7") == 1
    assert mgr.pending("7") == []


def test_complete_by_keyword_cancel(mgr):
    mgr.add("帮你查开播时间", source_group="7")
    assert mgr.complete_by_keyword("不用了，我自己看", "7") == 1
    assert mgr.all()[0].status == "cancelled"


def test_complete_by_keyword_only_one(mgr):
    """一句「好了」只结一条，不清空整本账。"""
    mgr.add("债A", source_group="7")
    mgr.add("债B", source_group="7")
    assert mgr.complete_by_keyword("搞定了", "7") == 1
    assert len(mgr.pending("7")) == 1


def test_complete_by_keyword_no_match(mgr):
    mgr.add("债A", source_group="7")
    assert mgr.complete_by_keyword("今天天气不错", "7") == 0
    assert len(mgr.pending("7")) == 1


# ---------------------------------------------------------------------------
# 4. 到期检查
# ---------------------------------------------------------------------------
def test_check_due_when_due(mgr):
    now = time.time()
    mgr.add("到期提醒验证", source_group="7", due_hint="明天", now=now - 90000)
    due = mgr.check_due(now=now)
    assert len(due) == 1


def test_check_due_respects_reminded_max(mgr):
    now = time.time()
    d = mgr.add("提醒上限验证", source_group="7", due_hint="明天", now=now - 90000)
    for _ in range(3):
        mgr.mark_reminded(d.id)
    assert mgr.check_due(now=now, reminded_max=3) == []


def test_check_due_respects_interval(mgr):
    now = time.time()
    d = mgr.add("提醒间隔验证", source_group="7", due_hint="明天", now=now - 90000)
    mgr.mark_reminded(d.id)
    assert mgr.check_due(now=now + 3600, remind_interval_sec=86400) == []


def test_check_due_expires_after_grace(mgr):
    now = time.time()
    d = mgr.add("宽限过期验证", source_group="7", due_hint="明天", now=now - 10 * 86400)
    assert mgr.check_due(now=now, grace_hours=48.0) == []
    assert d.status == "expired"


def test_check_due_fuzzy_after_one_day(mgr):
    """模糊承诺：不硬算时间，创建满 1 天才提醒一次。"""
    now = time.time()
    mgr.add("下次帮你看看", source_group="7", due_hint="下次", now=now - 2 * 86400)
    assert len(mgr.check_due(now=now)) == 1


def test_check_due_empty_book(mgr):
    """空账不编造。"""
    assert mgr.check_due(now=time.time()) == []


# ---------------------------------------------------------------------------
# 5. 单例
# ---------------------------------------------------------------------------
def test_get_debt_manager_is_singleton(tmp_path):
    a = get_debt_manager(str(tmp_path))
    b = get_debt_manager(str(tmp_path))
    assert a is b
