# -*- coding: utf-8 -*-
"""Agent 纯文本任务交付（D-36 / D-36b，2026-10-18）。

生产事故（owner 私聊 11:06–11:08 原文）：
```
11:07:25  [完成] 任务已全部执行        ← 宣告完成
          ── 之后零内容出站 ──
11:07:46  user 没输出东西给我呢？
11:07:48  bot  明明瞅着发过去了呀！是不是被这破网络给吞了…
```
任务其实完成了（`agent_tasks.json::36bcd32ab6` completed / 6 步全 done / 后 8 句 578 字）。

根因链：
  `executor.py:355-356` 产物是**返回值** → `owner_agent.py:122` `await execute(task)` **丢弃**
  → `finally` 只 `_deliver_media`（媒体）⇒ 纯文本任务零交付。
叠加：任务通知走 `gateway.send` 直发、**不入 chat_memory** ⇒ 对话层不知情 ⇒ 编造网络借口。

本文件钉死三件事：
  1. `_deliver_text` 真的把**步骤结果**发出去（不是 `result_parts` 那张"步骤N ✓"清单）；
  2. 分片、幂等、失败任务不交付；
  3. 交付后写一条**摘要**进 chat_memory（D-36b），让下一轮对话知道"我交过什么"。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from anima.agent.owner_agent import OwnerAgent, _split_text_chunks


class _FakeGateway:
    def __init__(self, fail_after: int | None = None):
        self.sent: list[tuple[str, str]] = []
        self.fail_after = fail_after

    async def send(self, msg):
        if self.fail_after is not None and len(self.sent) >= self.fail_after:
            raise RuntimeError("send failed")
        text = msg.message if isinstance(msg.message, str) else str(msg.message)
        self.sent.append((msg.detail_type, text))
        return "mid-1"


class _FakeStore:
    def __init__(self):
        self.saved = 0

    def save(self, task):
        self.saved += 1


class _FakeAuditor:
    def __init__(self):
        self.events = []

    def emit(self, name, **kw):
        self.events.append((name, kw))


def _agent(gateway=None, store=None, auditor=None):
    core = SimpleNamespace(gateway=gateway or _FakeGateway())
    a = OwnerAgent.__new__(OwnerAgent)          # 跳过 __init__（不碰真实 persistence）
    a.core = core
    a.store = store or _FakeStore()
    a.auditor = auditor
    return a


def _task(status="completed", steps=(), context=None, creator=1562290139):
    return SimpleNamespace(
        id="36bcd32ab6",
        status=status,
        creator=creator,
        steps=[SimpleNamespace(index=i, description=d, result=r) for i, d, r in steps],
        context=dict(context or {}),
        updated_at=0.0,
    )


# ── 1. 核心：完成的任务必须交付步骤结果 ──────────────────────────────────────

def test_completed_text_task_is_delivered():
    gw = _FakeGateway()
    a = _agent(gateway=gw)
    task = _task(steps=[
        (0, "解析语料", "提取到 16 条要点：张绍辉 / 广州队长 / Karrigay…"),
        (1, "输出后8句并拼接", "9. 一招「雀仔 massage」爆你后排\n10. …"),
    ])
    asyncio.run(a._deliver_text(task, ""))
    assert gw.sent, "完成的任务必须有出站（这就是事故：一个字都没发）"
    joined = "\n".join(t for _d, t in gw.sent)
    assert "雀仔 massage" in joined, "必须交付**步骤结果**正文，而不是标题清单"
    assert "【步骤2】" in joined, "步骤标题用于溯源"
    assert task.context["text_delivered"] is True


def test_result_parts_checklist_is_not_what_we_send():
    """反向守卫：`result_parts` 只是「步骤N: 描述 ✓」清单，不得作为交付内容。"""
    gw = _FakeGateway()
    a = _agent(gateway=gw)
    task = _task(steps=[(0, "解析语料", "")], context={
        "result_parts": ["步骤1: 解析语料 ✓", "步骤2: 编排顺序 ✓"],
    })
    asyncio.run(a._deliver_text(task, ""))
    assert gw.sent == [], "只有清单没有正文时宁可不说，也不能把清单当结果发出去"


def test_empty_stub_steps_are_skipped():
    gw = _FakeGateway()
    a = _agent(gateway=gw)
    task = _task(steps=[
        (0, "生成图片", "图片已生成"),          # < 8 字 ⇒ 空壳
        (1, "写结论", "结论是：综合来看应当选 B 方案，因为成本更低。"),
    ])
    asyncio.run(a._deliver_text(task, ""))
    joined = "\n".join(t for _d, t in gw.sent)
    assert "B 方案" in joined
    assert "图片已生成" not in joined


# ── 2. 不交付的情形 ────────────────────────────────────────────────────────

def test_failed_task_is_not_delivered():
    gw = _FakeGateway()
    a = _agent(gateway=gw)
    task = _task(status="failed", steps=[(0, "x", "有内容但任务失败")])
    asyncio.run(a._deliver_text(task, ""))
    assert gw.sent == [], "失败任务已有 [失败] 通知，不应再发半成品"


def test_delivery_is_idempotent():
    gw = _FakeGateway()
    a = _agent(gateway=gw)
    task = _task(steps=[(0, "x", "这是一段足够长的结果正文，用于验证幂等。")])
    asyncio.run(a._deliver_text(task, ""))
    n = len(gw.sent)
    asyncio.run(a._deliver_text(task, ""))
    assert len(gw.sent) == n, "同一任务不得重复交付"


def test_no_gateway_does_not_raise():
    a = _agent()
    a.core = SimpleNamespace(gateway=None)
    task = _task(steps=[(0, "x", "内容足够长的一段正文，用于验证无 gateway 时不炸。")])
    asyncio.run(a._deliver_text(task, ""))
    assert task.context.get("text_delivered") is None


# ── 3. 分片 ────────────────────────────────────────────────────────────────

def test_long_body_is_split_on_sentence_boundary():
    chunks = _split_text_chunks("一二三四五。六七八九十。" * 200, 120)
    assert len(chunks) > 1
    assert all(len(c) <= 120 for c in chunks)
    assert all(c.endswith(("。", "\n", "…")) or i == len(chunks) - 1
               for i, c in enumerate(chunks)), "分片必须落在句读上，不能切半句"


def test_short_body_single_chunk():
    assert _split_text_chunks("短句。", 120) == ["短句。"]
    assert _split_text_chunks("", 120) == []


def test_delivery_respects_send_failure():
    gw = _FakeGateway(fail_after=1)
    a = _agent(gateway=gw)
    long_text = "这是一句足够长的内容。" * 200
    task = _task(steps=[(0, "长文", long_text)])
    asyncio.run(a._deliver_text(task, ""))
    assert len(gw.sent) == 1, "分片发送失败时停手，不无限重试"


# ── 4. 源码守卫：`_run` 必须接住返回值 ─────────────────────────────────────

def test_run_never_discards_execute_result():
    import inspect

    src = inspect.getsource(OwnerAgent._run)
    assert "result_text = await self.executor.execute(task)" in src, \
        "_run 必须接住 execute() 的返回值（D-36 的根因就是丢弃它）"
    assert "_deliver_text(" in src, "_run 必须在 finally 里交付文本"


def test_deliver_text_does_not_use_result_parts_only():
    import inspect

    src = inspect.getsource(OwnerAgent._deliver_text)
    assert "getattr(st, \"result\"" in src, "交付主体必须是步骤结果"
    assert "result_parts" not in src.split('"""')[-1], \
        "实现体（去掉 docstring 后）不得依赖 result_parts 清单"


# ── 5. 群聊防刷屏（2026-10-18 回放发现：2217 字原样进群 = 2 连发）───────────

def test_group_long_body_is_capped_and_full_copy_goes_private():
    gw = _FakeGateway()
    a = _agent(gateway=gw)
    long_text = "这一句内容是用于测试的正文。" * 120          # ≈ 1800 字
    task = _task(steps=[(0, "长产物", long_text)],
                 context={"session": {"detail_type": "group", "target_id": 727631526}})
    asyncio.run(a._deliver_text(task, ""))

    group_msgs = [t for dt, t in gw.sent if dt == "group"]
    priv_msgs = [t for dt, t in gw.sent if dt == "private"]
    assert len(group_msgs) == 1, "群里只许一条，不许连发刷屏"
    assert len(group_msgs[0]) <= OwnerAgent._TEXT_GROUP_MAX + 40, "群里那条必须被截断"
    assert "完整结果已私聊" in group_msgs[0]
    assert priv_msgs and "测试的正文" in priv_msgs[0], "完整版必须落到 creator 私聊"


def test_private_session_gets_all_chunks_directly():
    gw = _FakeGateway()
    a = _agent(gateway=gw)
    # 每步上限 1000 字 ⇒ 要跨分片就必须**多步**（单步永远 ≤1000 < 1200 一片）
    # 🔴 2026-10-25：三步内容必须**各不相同**，否则会被「重复步骤去重」合并成
    #   一条（那是新加的正确行为）；本测试验证的是**分片能力**，不是去重。
    seg1 = "第一段内容用于测试正文。" * 80
    seg2 = "第二段内容用于测试正文。" * 80
    seg3 = "第三段内容用于测试正文。" * 80
    task = _task(steps=[(0, "第一步", seg1), (1, "第二步", seg2), (2, "第三步", seg3)],
                 context={"session": {"detail_type": "private", "target_id": 1562290139}})
    asyncio.run(a._deliver_text(task, ""))
    assert all(dt == "private" for dt, _t in gw.sent)
    assert len(gw.sent) > 1, "私聊按分片发全"


def test_session_missing_falls_back_to_private_creator():
    gw = _FakeGateway()
    a = _agent(gateway=gw)
    task = _task(steps=[(0, "x", "内容足够长的一段正文，用于验证兜底会话选择。")])
    asyncio.run(a._deliver_text(task, ""))
    assert gw.sent and all(dt == "private" for dt, _t in gw.sent)
