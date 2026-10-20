"""owner 私聊交付契约（D-41，2026-10-21）。

## 背景

心跳里产出的「有用之物」（健康告警 `autonomy.py:800`、自治自检 `:929`）此前只
`tasks.create()` 落进 `data/owner_tasks.json`，**从不推送** —— 每天在写、没人看。

根因不是能力缺失（`send_private_msg` 的 `reply_id` 可空、`handler.py:376/:2095`、
`owner_agent.py:264-283` 三处私聊先例都在），而是 **`propose_agenda` 第一参数就叫
`group_id`** ⇒ 主动产出只能发群。

## 本测试锁死的边界

1. 有 pending 待办 + 到点 → 向 `permissions.owners` 私聊发送，**且只发私聊**；
2. **日切一次**：同一天第二次 tick 不再发（私聊路径不经群侧防刷屏纪律，必须自建节流）；
3. **fail-closed**：`owners` 为空 / 无 gateway → 不发送、不伪造目标，且不抛异常；
4. **无待办就不发**（不做「今日无异常」式自我打卡刷屏）；
5. **未到配置小时不发**；
6. 开关关闭 → 完全不动作；
7. 发送内容含真实待办，超过 `max_items` 时给出「还有 N 件」。

刻意不测：消息文案的具体措辞（属文案，会变）。
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from anima.core.autonomy import Heartbeat


class _P:
    data_dir = "data"


class _Perms:
    def __init__(self, owners):
        self.owners = owners


class _Cfg:
    def __init__(self, owners=(12345,), enabled=True, hour=0, max_items=5):
        self.persistence = _P()
        self.permissions = _Perms(list(owners))
        self.autonomy = MagicMock()
        self.autonomy.owner_digest_enabled = enabled
        self.autonomy.owner_digest_hour = hour
        self.autonomy.owner_digest_max_items = max_items


class _Task:
    def __init__(self, request, status="planned"):
        self.request = request
        self.status = status


class _TaskStore:
    def __init__(self, items):
        self._items = items

    def list(self):
        return list(self._items)


def _make_autonomy(cfg, tasks, sent):
    core = MagicMock()
    core.config = cfg
    core.tasks = tasks

    async def _send(msg):
        sent.append(msg)

    core.gateway.send = _send
    core.audit = None
    auto = Heartbeat.__new__(Heartbeat)
    auto.core = core
    auto._last_owner_digest_date = ""
    auto._bj_date_str = lambda now: "2026-10-21"
    return auto


# ── 1. 正常路径：私聊发送且只走私聊 ──────────────────────────────────────


async def test_digest_sends_private_to_owner():
    cfg = _Cfg(owners=(12345,))
    tasks = _TaskStore([_Task("修一下复读问题"), _Task("查上游抖动")])
    sent = []
    auto = _make_autonomy(cfg, tasks, sent)

    await auto._owner_digest_tick(now=0.0)

    assert len(sent) == 1, f"应发送 1 条，实得 {len(sent)}"
    assert sent[0].detail_type == "private", "必须走私聊，不得发群"
    assert sent[0].target_id == 12345
    assert "修一下复读问题" in sent[0].message
    assert "查上游抖动" in sent[0].message


async def test_digest_skips_group_entirely():
    """🔴 D-41 的核心：这条路径绝不能产生群出站。"""
    cfg = _Cfg(owners=(999,))
    tasks = _TaskStore([_Task("待办A")])
    sent = []
    auto = _make_autonomy(cfg, tasks, sent)

    await auto._owner_digest_tick(now=0.0)

    assert all(m.detail_type == "private" for m in sent), "出现群出站即违反 D-41"


# ── 2. 日切一次（自建节流）─────────────────────────────────────────────


async def test_digest_only_once_per_day():
    cfg = _Cfg(owners=(1,))
    tasks = _TaskStore([_Task("待办A")])
    sent = []
    auto = _make_autonomy(cfg, tasks, sent)

    await auto._owner_digest_tick(now=0.0)
    await auto._owner_digest_tick(now=100.0)   # 同日第二次
    await auto._owner_digest_tick(now=200.0)

    assert len(sent) == 1, f"日切一次：应仅发 1 条，实得 {len(sent)}"


# ── 3. fail-closed ────────────────────────────────────────────────────


async def test_no_owners_means_no_send():
    cfg = _Cfg(owners=())
    tasks = _TaskStore([_Task("待办A")])
    sent = []
    auto = _make_autonomy(cfg, tasks, sent)

    await auto._owner_digest_tick(now=0.0)

    assert sent == [], "owners 为空时不得发送（fail-closed 不伪造目标）"


async def test_send_failure_is_isolated():
    """发送抛异常 ⇒ 不向外抛（心跳不得因此挂掉）。"""
    cfg = _Cfg(owners=(1,))
    tasks = _TaskStore([_Task("待办A")])
    sent = []
    auto = _make_autonomy(cfg, tasks, sent)

    async def _boom(msg):
        raise RuntimeError("QQ 挂了")

    auto.core.gateway.send = _boom
    await auto._owner_digest_tick(now=0.0)   # 不应抛出


async def test_digest_date_not_marked_when_send_fails():
    """发送失败不得标记当日已发 —— 否则当天永久丢失。"""
    cfg = _Cfg(owners=(1,))
    tasks = _TaskStore([_Task("待办A")])
    auto = _make_autonomy(cfg, tasks, [])

    async def _boom(msg):
        raise RuntimeError("boom")

    auto.core.gateway.send = _boom
    await auto._owner_digest_tick(now=0.0)
    assert auto._last_owner_digest_date != "2026-10-21", "失败不应标记已发"


# ── 4. 无待办不发（不做自我打卡刷屏）────────────────────────────────────


async def test_no_pending_tasks_means_silence():
    cfg = _Cfg(owners=(1,))
    tasks = _TaskStore([_Task("已完成的事", status="done")])
    sent = []
    auto = _make_autonomy(cfg, tasks, sent)

    await auto._owner_digest_tick(now=0.0)

    assert sent == [], "没有待办时应保持安静（不发送空摘要）"


# ── 5. 时刻门控 ───────────────────────────────────────────────────────


async def test_before_hour_no_send():
    cfg = _Cfg(owners=(1,), hour=23)   # 当前时刻肯定不到 23 点
    tasks = _TaskStore([_Task("待办A")])
    sent = []
    auto = _make_autonomy(cfg, tasks, sent)

    await auto._owner_digest_tick(now=0.0)

    assert sent == [], "未到配置小时不应发送"


# ── 6. 开关关闭 ───────────────────────────────────────────────────────


async def test_disabled_means_no_action():
    cfg = _Cfg(owners=(1,), enabled=False)
    tasks = _TaskStore([_Task("待办A")])
    sent = []
    auto = _make_autonomy(cfg, tasks, sent)

    await auto._owner_digest_tick(now=0.0)

    assert sent == [], "开关关闭时不得发送"


# ── 7. 超量截断 + 提示 ────────────────────────────────────────────────


async def test_max_items_truncates_and_reports_remainder():
    cfg = _Cfg(owners=(1,), max_items=2)
    tasks = _TaskStore([_Task(f"待办{i}") for i in range(5)])
    sent = []
    auto = _make_autonomy(cfg, tasks, sent)

    await auto._owner_digest_tick(now=0.0)

    assert len(sent) == 1
    text = sent[0].message
    assert "5" in text, "应给出待办总数"
    assert "还有 3 件" in text, f"应给出剩余数量提示：{text!r}"
