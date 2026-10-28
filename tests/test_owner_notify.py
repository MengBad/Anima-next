"""Owner 私聊通道（D-41 扩展，2026-10-28）。

## 背景

D-41 原本只解决了「每日一次把待办摘要推到 owner 私聊」
（`autonomy._owner_digest_tick`，生产实证 09:00:29 有推送）。

但心跳里还有多处「只 `tasks.create()` 落 WebUI、从不推送」的产出：

    autonomy.py:1901           DevIssue → 诊断任务
    os/control_surface.py:400  健康告警 → 诊断任务

它们只能等每日 digest 汇总（最多 5 条、只在 09:00）。
「权限漂移 9 次」「LLM 全挂」这类告警等不到明早。

## 为什么不复用 agenda / digest

- **agenda**：`propose_agenda(group_id, ...)` 第一参数是群 id，消费端走
  `_pick_group` / 冷却 / 占比 / 日预算 —— 防群刷屏的闸门，套在「给 owner 的
  告警」上语义完全错误（owner 私聊根本没有群）。
- **digest**：语义是**每日摘要**（日切一次、按主题合并、最多 5 条），
  紧急告警需要即时送达，且不该被「今天已推过摘要」挡住。

## 三条硬约束（与 D-41 验收口径一致）

1. **fail-closed**：`owners` 为空 ⇒ 不发送、不伪造、不记「已发」
2. **自建节流**：私聊不经群侧防刷屏纪律 ⇒ 必须自己限流
3. **群侧零变化**：本模块只发私聊
"""
from __future__ import annotations

import asyncio

import pytest

from anima.core.owner_notify import (
    MAX_BODY_CHARS,
    NORMAL_QUEUE_MAX,
    URGENT_MIN_INTERVAL_SEC,
    OwnerNotifier,
    get_owner_notifier,
    reset_owner_notifier,
)


# ── 测试替身 ────────────────────────────────────────────────────────

class _Gw:
    def __init__(self, fail: bool = False):
        self.sent: list[tuple[str, int, str]] = []
        self.fail = fail

    async def send(self, msg):
        if self.fail:
            raise RuntimeError("send failed")
        self.sent.append((msg.detail_type, msg.target_id, msg.message))
        return "mid-1"


class _Persist:
    def __init__(self):
        self.store: dict = {}

    def read_json(self, path, default):
        return self.store.get(path, default)

    def write_json(self, path, obj):
        self.store[path] = obj


class _Perm:
    def __init__(self, owners):
        self.owners = owners


class _Cfg:
    def __init__(self, owners):
        self.permissions = _Perm(owners)


class _Core:
    def __init__(self, owners=(1562290139,), fail_send=False):
        self.gateway = _Gw(fail=fail_send)
        self.persistence = _Persist()
        self.config = _Cfg(list(owners))


@pytest.fixture(autouse=True)
def _clean():
    reset_owner_notifier()
    yield
    reset_owner_notifier()


# ── 1. 三种 kind 的语义 ─────────────────────────────────────────────

def test_urgent_sends_immediately():
    core = _Core()
    n = OwnerNotifier(core)
    assert asyncio.run(n.notify("urgent", "QQ 出站失败 3 次")) is True
    assert len(core.gateway.sent) == 1
    assert core.gateway.sent[0][0] == "private"
    assert core.gateway.sent[0][1] == 1562290139


def test_normal_is_queued_not_sent():
    core = _Core()
    n = OwnerNotifier(core)
    assert asyncio.run(n.notify("normal", "回复质量退化")) is True
    assert core.gateway.sent == [], "normal 不该立即发"
    assert n.status()["queue_len"] == 1


def test_digest_is_not_handled_here():
    """`digest` 保留给 autonomy —— 本模块必须返回 False，不重复实现。"""
    core = _Core()
    n = OwnerNotifier(core)
    assert asyncio.run(n.notify("digest", "摘要")) is False
    assert core.gateway.sent == []
    assert n.status()["queue_len"] == 0


# ── 2. fail-closed ──────────────────────────────────────────────────

def test_no_owners_fails_closed():
    """owners 为空 ⇒ 不发送、不伪造、不记「已发」。"""
    core = _Core(owners=())
    n = OwnerNotifier(core)
    assert asyncio.run(n.notify("urgent", "告警")) is False
    assert core.gateway.sent == []
    assert n.status()["sent_today"] == {}


def test_no_owners_keeps_queue():
    """owners 为空时**队列保留**（不清空）—— 配好后还能补发。"""
    core = _Core(owners=())
    n = OwnerNotifier(core)
    asyncio.run(n.notify("normal", "第一条"))
    asyncio.run(n.notify("normal", "第二条"))
    assert n.status()["queue_len"] == 2
    assert asyncio.run(n.flush()) == 0, "无 owner 时不该发送"
    assert n.status()["queue_len"] == 2, "队列被清空了（应该保留）"


def test_send_failure_keeps_item_in_queue():
    core = _Core(fail_send=True)
    n = OwnerNotifier(core)
    asyncio.run(n.notify("normal", "发不出去的"))
    assert asyncio.run(n.flush()) == 0
    assert n.status()["queue_len"] == 1, "失败项应保留重试"


# ── 3. 节流 ─────────────────────────────────────────────────────────

def test_urgent_has_min_interval():
    """urgent 有最短间隔 —— 否则一次巡检 5 条告警就是 5 条消息。"""
    core = _Core()
    n = OwnerNotifier(core)
    t0 = 1_000_000.0
    assert asyncio.run(n.notify("urgent", "第一条", now=t0)) is True
    assert len(core.gateway.sent) == 1
    # 间隔内：降级入队，不丢
    assert asyncio.run(n.notify("urgent", "第二条", now=t0 + 10)) is True
    assert len(core.gateway.sent) == 1, "冷却期内不该再发"
    assert n.status()["queue_len"] == 1, "冷却内的 urgent 应降级入队而非丢弃"


def test_urgent_after_interval_sends():
    core = _Core()
    n = OwnerNotifier(core)
    t0 = 1_000_000.0
    asyncio.run(n.notify("urgent", "第一条", now=t0))
    asyncio.run(n.notify("urgent", "第二条", now=t0 + URGENT_MIN_INTERVAL_SEC + 1))
    assert len(core.gateway.sent) == 2


def test_queue_has_cap():
    """队列有上限，且**丢最旧**（新的更能反映当下状态）。"""
    core = _Core()
    n = OwnerNotifier(core)
    for i in range(NORMAL_QUEUE_MAX + 5):
        asyncio.run(n.notify("normal", f"第{i}条"))
    assert n.status()["queue_len"] == NORMAL_QUEUE_MAX
    # 最早那条应被丢弃
    st = n._load()
    assert not any("第0条" in (x or {}).get("text", "") for x in st["queue"])


def test_flush_has_per_tick_limit():
    """flush 单次最多 3 条 —— 避免一次把 20 条全倒出去。"""
    core = _Core()
    n = OwnerNotifier(core)
    for i in range(10):
        asyncio.run(n.notify("normal", f"第{i}条"))
    assert asyncio.run(n.flush()) == 3
    assert len(core.gateway.sent) == 3
    assert n.status()["queue_len"] == 7


# ── 4. 同步入队（给同步调用方）──────────────────────────────────────

def test_enqueue_is_sync_and_returns_immediately():
    """`enqueue()` 不 await、不发网络 —— 给 `control_surface` 那种同步链用。"""
    core = _Core()
    n = OwnerNotifier(core)
    assert n.enqueue("urgent", "[健康告警] 权限漂移 9 次") is True
    assert core.gateway.sent == [], "enqueue 不该立即发"
    assert n.status()["queue_len"] == 1


def test_enqueue_urgent_goes_to_front():
    """urgent 入队时排最前 —— flush 按顺序发，先发急的。"""
    core = _Core()
    n = OwnerNotifier(core)
    n.enqueue("normal", "普通1")
    n.enqueue("normal", "普通2")
    n.enqueue("urgent", "紧急")
    st = n._load()
    assert st["queue"][0]["text"] == "紧急"


def test_enqueue_empty_text_rejected():
    core = _Core()
    n = OwnerNotifier(core)
    assert n.enqueue("normal", "   ") is False
    assert n.status()["queue_len"] == 0


# ── 5. 正文长度保护 ─────────────────────────────────────────────────

def test_body_is_truncated():
    core = _Core()
    n = OwnerNotifier(core)
    long_text = "x" * (MAX_BODY_CHARS + 500)
    asyncio.run(n.notify("urgent", long_text))
    assert len(core.gateway.sent[0][2]) == MAX_BODY_CHARS


# ── 6. 单例与持久化 ─────────────────────────────────────────────────

def test_singleton_per_core():
    core = _Core()
    a = get_owner_notifier(core)
    b = get_owner_notifier(core)
    assert a is b, "同一 core 应复用同一 notifier"


def test_reset_creates_new():
    core = _Core()
    a = get_owner_notifier(core)
    reset_owner_notifier()
    b = get_owner_notifier(core)
    assert a is not b


def test_state_survives_reload():
    """队列必须落盘 —— 否则重启丢通知。"""
    core = _Core()
    n = OwnerNotifier(core)
    asyncio.run(n.notify("normal", "重启前的"))
    # 新实例，共用同一 persistence
    n2 = OwnerNotifier(core)
    assert n2.status()["queue_len"] == 1


# ── 7. 群侧零变化（反向守卫）─────────────────────────────────────────

def test_never_sends_to_group():
    """本模块**只发私聊**，绝不碰群出站路径（D-41 验收口径第 3 条）。"""
    core = _Core()
    n = OwnerNotifier(core)
    asyncio.run(n.notify("urgent", "告警"))
    n.enqueue("normal", "普通")
    asyncio.run(n.flush())
    for detail_type, _tid, _msg in core.gateway.sent:
        assert detail_type == "private", f"发出了 {detail_type}，违反群侧零变化"


def test_status_reports_owned_targets():
    core = _Core(owners=(1562290139, 1371704917))
    n = OwnerNotifier(core)
    st = n.status()
    assert st["owners"] == [1562290139, 1371704917]
    assert st["urgent_min_interval_sec"] == URGENT_MIN_INTERVAL_SEC
    assert st["queue_max"] == NORMAL_QUEUE_MAX
