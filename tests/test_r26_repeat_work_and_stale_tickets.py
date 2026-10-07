# -*- coding: utf-8 -*-
"""r26：两处「重复劳动 / 僵尸状态」的收口判据。

## ① SelfCode 能力边界：沙箱无网络出口，出网类目标必须在**受理期**拒绝

生产实证（2026-10-06 诊断）：`owner_tasks.json` 里 **17/17** 条 selfcode 任务
全是「失效链接 → 快照正文」这一类；**15 条失败的全部原因**都是静态校验的
`禁止导入：urllib / httpx / aiohttp`。该目标在 08-31→09-20 的 20 天里被
**重复受理 15 次**，每次白烧一轮 Coder + Critic 双 Agent 算力。

🔴 契约事实（不是语义猜测）：`selfcode._FORBIDDEN_IMPORTS` 禁掉
requests / aiohttp / httpx / urllib **全部** HTTP 客户端
⇒ 需要发起网络请求的工具在该沙箱内**结构上无法实现**。

⇒ 判据的**正样本直接用生产真实语料**（那 17 条原文），负样本钉本地需求不被误伤。

## ② 故障工单自愈关闭：不给工单加过期机制，僵尸工单会永久占 Owner 队列

生产实证：两张 `awaiting_approval` 工单挂了 9~15 天
（`react_max_rounds` 开于 09-21、`outbound_failures` 开于 09-27），
而这两个信号在诊断当日实测均为 0 —— 故障早已不存在，单子还在。
"""
from __future__ import annotations

import pytest

from anima.cognitive.controlled_iteration import ControlledIteration, _FAULT_RULES
from anima.cognitive.selfcode import network_egress_boundary
from anima.core.persistence import PersistenceProvider
from anima.core.tasks import TaskStore


# ======================================================================
# ① 能力边界探测器
# ======================================================================

#: 生产 `owner_tasks.json` 里 selfcode 任务的**原文**（17 条，全部同一意图）。
REAL_NETWORK_GOALS = [
    "做一个输入失效URL自动查询网页时光机历史快照并提取正文的工具。",
    "做一个输入失效或拦截的网页链接，自动抓取并提取 Wayback Machine 历史快照文本的工具。",
    "做一个传入失效网页链接，自动从网页时光机（Wayback Machine）抓取历史快照正文的工具。",
    "做一个输入打不开或失效的网页链接，自动从时光机与快照源提取正文内容的工具。",
    "做一个输入打不开或失效的网页链接，自动从快照归档服务抓取历史网页内容的工具。",
    "做一个输入失效网址自动检索Web Archive历史快照并提取正文内容的容灾查询工具",
    "做一个输入失效网址自动检索Wayback等快照存档并提取正文的工具。",
    "做一个输入失效或404链接自动检索历史快照并提取正文的网页归档查询工具。",
    "做一个输入失效URL后自动检索网页时光机快照并提取纯文本摘要的排障工具。",
    "做一个输入失效或打不开的URL，自动从网页时光机检索并提取历史快照文本的工具。",
    "做一个输入失效或受限链接，自动查询Wayback Machine快照并提取纯文本正文的工具",
    "做一个传入失效或受阻网页链接，自动检索网页历史快照并提取正文的备用阅读工具。",
    "做一个在链接打不开或404时自动抓取网页时光机与搜索引擎快照正文的工具。",
    "做一个当链接失效打不开时自动从网络快照和归档站检索并提取正文的工具。",
    "做一个输入失效链接自动查询网络历史快照并提取正文内容的工具。",
    "做一个输入失效或受限网页链接，自动从网络时光机检索历史快照并提取正文的工具。",
    "做一个输入无法访问的网页链接，自动检索历史快照或镜像站并提取正文内容的工具。",
]

#: 合法**本地**工具需求：一条都不许被误拒。
LOCAL_TOOL_GOALS = [
    "做一个统计群成员发言次数的工具",
    "做一个把 JSON 格式化输出的工具",
    "做一个记账工具，记录每天开销",
    "做一个把长文总结成要点的工具",
    "做一个读取本地文件并统计词频的工具",
    "做一个查询本地数据库的工具",
    "做一个把 markdown 转成图片的工具",
    "做一个计算两个日期相差天数的工具",
    "做一个把网页收藏整理成清单的工具",       # 有「网页」但无取回动作
    "做一个分析网站日志的工具",               # 有「网站」但无取回动作
    "做一个读取网站访问日志统计 PV 的工具",    # 有「读取」但有本地语境词
    "做一个 demo 工具",
    "做一个可重做 demo 工具",
]


@pytest.mark.parametrize("goal", REAL_NETWORK_GOALS)
def test_real_network_goals_are_rejected_at_intake(goal):
    """生产 17 条真实出网目标必须**全部**被判为需要网络出口。"""
    reason = network_egress_boundary(goal)
    assert reason, f"漏判出网目标（会白烧一轮 Coder/Critic）：{goal}"
    assert "网络出口" in reason


@pytest.mark.parametrize("goal", LOCAL_TOOL_GOALS)
def test_local_tool_goals_are_not_rejected(goal):
    """本地工具需求不得误伤 —— 误拒会**永久堵死**一个合法工具。"""
    assert network_egress_boundary(goal) == "", f"误伤本地工具需求：{goal}"


def test_empty_goal_is_safe():
    assert network_egress_boundary("") == ""
    assert network_egress_boundary("   ") == ""


def test_rejection_reason_names_existing_alternatives():
    """拒绝理由必须**可执行**：指出已有的联网能力，否则 Owner 只知道「不行」。"""
    reason = network_egress_boundary(REAL_NETWORK_GOALS[0])
    for tool in ("web_search", "agent_http_request", "agent_browser"):
        assert tool in reason, f"拒绝理由没有指出已有替代能力：{tool}"


def test_explicit_network_phrase_rejected_even_without_action_verb():
    """显式出网词单独命中即拒绝（如「发 HTTP 请求」没有取回对象）。"""
    assert network_egress_boundary("做一个发送 HTTP 请求的工具")
    assert network_egress_boundary("做一个联网抓数据的工具")


# ======================================================================
# ①b 受理路径：拒绝必须真的短路，不进入生成阶段
# ======================================================================

class _FakeAudit:
    def __init__(self):
        self.events = []

    def emit(self, name, **kw):
        self.events.append((name, kw))


class _FakeCore:
    def __init__(self, tmp_path):
        self.tasks = TaskStore(PersistenceProvider(str(tmp_path)))
        self.audit = _FakeAudit()


@pytest.mark.asyncio
async def test_intake_rejects_without_running_coder(tmp_path, monkeypatch):
    """受理期拒绝：任务落 `rejected`，且**不得**触碰 Coder 生成路径。"""
    from anima.cognitive import selfcode as sc_mod

    core = _FakeCore(tmp_path)
    mgr = sc_mod.SelfCodeManager.__new__(sc_mod.SelfCodeManager)
    mgr.core = core
    mgr.cfg = type("C", (), {"enabled": True})()
    mgr.workspace = tmp_path / "ws"
    mgr.workspace.mkdir(parents=True, exist_ok=True)
    mgr._task_lock = __import__("asyncio").Lock()

    called = {"coder": 0}

    async def _rec(*a, **k):
        called["coder"] += 1
        raise RuntimeError("entered-generation")

    monkeypatch.setattr(mgr, "_run_claude", _rec, raising=False)

    task = await mgr.create_engineering_task(REAL_NETWORK_GOALS[0], 42)

    assert called["coder"] == 0, "拒绝路径不得进入 Coder 生成阶段"
    assert task.status == "rejected", f"应落 rejected，实际 {task.status}"
    assert task.kind == "selfcode"
    assert task.evidence and "网络出口" in task.evidence[-1]
    assert any(name == "selfcode_rejected_boundary" for name, _ in core.audit.events), \
        "拒绝必须留审计事件"


@pytest.mark.asyncio
async def test_local_goal_still_enters_generation(tmp_path, monkeypatch):
    """反向对照：本地目标**必须**照常进入生成路径（不能把受理期拒绝对所有目标生效）。

    ⚠️ `create_engineering_task` 内部把生成异常吞掉只记日志，所以这里断言
    「Coder 入口被调到」而不是用 `pytest.raises`。
    """
    from anima.cognitive import selfcode as sc_mod

    core = _FakeCore(tmp_path)
    mgr = sc_mod.SelfCodeManager.__new__(sc_mod.SelfCodeManager)
    mgr.core = core
    mgr.cfg = type("C", (), {"enabled": True})()
    mgr.workspace = tmp_path / "ws"
    mgr.workspace.mkdir(parents=True, exist_ok=True)
    mgr._task_lock = __import__("asyncio").Lock()

    reached = {"n": 0}

    async def _rec(*a, **k):
        reached["n"] += 1
        raise RuntimeError("stop-here-after-entry")

    monkeypatch.setattr(mgr, "_run_claude", _rec, raising=False)

    task = await mgr.create_engineering_task("做一个统计本地词频的工具", 42)

    assert reached["n"] >= 1, "本地目标应照常进入 Coder 生成阶段（被误拒了）"
    assert task.status != "rejected", "本地目标不得落 rejected"


# ======================================================================
# ② 故障工单自愈关闭
# ======================================================================

def _make_iter(tmp_path) -> ControlledIteration:
    return ControlledIteration(str(tmp_path))


def _open_ticket(core, *, signals, day, group="g1", status="awaiting_approval"):
    task = core.tasks.create(f"[故障工单/{','.join(signals)}] 测试", 42, ["p"], kind="fault_ticket")
    core.tasks.set_proposal(task.id, {
        "kind": "fault_ticket", "ticket_group": group,
        "signal": signals[0], "signals": list(signals), "date": day,
    }, evidence="scan")
    if status != "awaiting_approval":
        core.tasks.set_status(task.id, status)
    return task


def test_stale_ticket_is_closed_when_signal_healed(tmp_path):
    """跨日 + 信号归零 ⇒ 自动关闭（生产那两张僵尸工单的形状）。"""
    core = _FakeCore(tmp_path)
    it = _make_iter(tmp_path)
    task = _open_ticket(core, signals=["outbound_failures"], day="2026-09-27")

    report = {"date": "2026-10-06", "outbound_failures": 0}
    healed = it.healed_fault_tickets(core, report)
    assert [t.id for t in healed] == [task.id]

    closed = it.close_healed_fault_tickets(core, report)
    assert closed == [task.id]
    assert core.tasks.get(task.id).status == "expired"


def test_ticket_opened_today_is_never_closed(tmp_path):
    """🔴 当天开的单不动：计数按自然日重置，当天读 0 不能证明「没复发」。"""
    core = _FakeCore(tmp_path)
    it = _make_iter(tmp_path)
    _open_ticket(core, signals=["react_max_rounds"], day="2026-10-06")
    report = {"date": "2026-10-06", "react_max_rounds": 0}
    assert it.healed_fault_tickets(core, report) == []


def test_ticket_stays_open_while_signal_still_over_threshold(tmp_path):
    """信号今天仍然超阈值 ⇒ 故障仍在，绝不能关。"""
    core = _FakeCore(tmp_path)
    it = _make_iter(tmp_path)
    _open_ticket(core, signals=["outbound_failures"], day="2026-09-27")
    report = {"date": "2026-10-06", "outbound_failures": 99}
    assert it.healed_fault_tickets(core, report) == []


def test_multi_signal_ticket_needs_all_to_heal(tmp_path):
    """多信号工单必须**全部**低于阈值才关（有一个还在就不关）。"""
    core = _FakeCore(tmp_path)
    it = _make_iter(tmp_path)
    t = _open_ticket(core, signals=["outbound_failures", "react_max_rounds"], day="2026-09-27")
    still = {"date": "2026-10-06", "outbound_failures": 0, "react_max_rounds": 99}
    assert it.healed_fault_tickets(core, still) == []
    both = {"date": "2026-10-06", "outbound_failures": 0, "react_max_rounds": 0}
    assert [x.id for x in it.healed_fault_tickets(core, both)] == [t.id]


def test_unknown_signal_is_not_closed(tmp_path):
    """信号不在规则表里 ⇒ 判不了 ⇒ fail-closed 不关。"""
    core = _FakeCore(tmp_path)
    it = _make_iter(tmp_path)
    _open_ticket(core, signals=["not_a_real_signal"], day="2026-09-27")
    report = {"date": "2026-10-06", "not_a_real_signal": 0}
    assert it.healed_fault_tickets(core, report) == []


def test_already_closed_ticket_is_untouched(tmp_path):
    """已关闭的工单不再处理（幂等）。"""
    core = _FakeCore(tmp_path)
    it = _make_iter(tmp_path)
    t = _open_ticket(core, signals=["outbound_failures"], day="2026-09-27", status="rejected")
    report = {"date": "2026-10-06", "outbound_failures": 0}
    assert it.healed_fault_tickets(core, report) == []
    assert core.tasks.get(t.id).status == "rejected"


def test_auto_closed_ticket_no_longer_blocks_new_ones(tmp_path):
    """自愈关闭后不得再算作「有未关闭工单」（否则会永久压制新单）。"""
    core = _FakeCore(tmp_path)
    it = _make_iter(tmp_path)
    _open_ticket(core, signals=["outbound_failures"], day="2026-09-27")
    assert it.has_open_fault_tickets(core) is True
    it.close_healed_fault_tickets(core, {"date": "2026-10-06", "outbound_failures": 0})
    assert it.has_open_fault_tickets(core) is False


def test_real_fault_rules_are_used_by_the_heal_check(tmp_path):
    """自愈判定必须真的读 `_FAULT_RULES` 的阈值（防止判据与规则表脱钩）。"""
    assert _FAULT_RULES, "规则表不能为空"
    core = _FakeCore(tmp_path)
    it = _make_iter(tmp_path)
    rule = _FAULT_RULES[0]
    sig = str(rule["key"])
    _open_ticket(core, signals=[sig], day="2026-09-27")
    below = {"date": "2026-10-06", sig: max(0, int(rule["threshold"]) - 1)}
    assert len(it.healed_fault_tickets(core, below)) == 1


# ----------------------------------------------------------------------
# ②b 接线判据：`scan` 必须**真的调用**自愈关闭
# ----------------------------------------------------------------------

def _wire_scan_core(core, report):
    """给假 core 补上 scan 需要的接口。"""
    from types import SimpleNamespace
    core.reply_quality = SimpleNamespace(report=lambda: report)
    core.config = SimpleNamespace(permissions=SimpleNamespace(owners=[42]))
    return core


def test_scan_actually_closes_healed_tickets(tmp_path):
    """🔴 接线判据：只测 `close_healed_fault_tickets` 本身**证明不了**它被接上。

    （这条是补写的：最初 8 条自愈判据都直接调 helper，把 `scan` 里的调用点
    删掉后 8 条**全绿** ⇒ 判据假绿。必须走 `scan_quality_and_open_tickets`。）
    """
    core = _wire_scan_core(_FakeCore(tmp_path), {"date": "2026-10-06", "outbound_failures": 0})
    it = _make_iter(tmp_path)
    stale = _open_ticket(core, signals=["outbound_failures"], day="2026-09-27")

    it.scan_quality_and_open_tickets(core, force=True)

    assert core.tasks.get(stale.id).status == "expired", \
        "scan 没有调用自愈关闭 ⇒ 僵尸工单会永久占队列"


def test_scan_leaves_healthy_ticket_open(tmp_path):
    """反向对照：信号仍在阈值之上时，scan 不得关掉工单。"""
    core = _wire_scan_core(_FakeCore(tmp_path), {"date": "2026-10-06", "outbound_failures": 99})
    it = _make_iter(tmp_path)
    live = _open_ticket(core, signals=["outbound_failures"], day="2026-09-27")

    it.scan_quality_and_open_tickets(core, force=True)

    assert core.tasks.get(live.id).status == "awaiting_approval", "故障仍在，不得关闭"
