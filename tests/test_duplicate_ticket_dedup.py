"""重复建单去重契约（2026-10-22）。

## 背景（owner 连续两天收到同一条推送）

```
10-21 17:18  本喵这边攒了 2 件还等着处理的事喵：
             · [health_alerts] 窗口内检测到 9 次权限漂移事件（同主题共 7 条，此处合并）
```

任务库里翻出来：**9 条 `planned` 全是同一件事**（权限漂移计数 3→4→…→9），
以及 **11 张 `rejected` 的 selfcode 单**全是同一个「失效链接查快照」需求。

两处根因不同、病根相同 —— **去重只做了一半**：

1. `control_surface.health_alerts_to_owner`：**完全无去重**，
   每次 tick（6h 冷却）无条件 `tasks.create()`。标题带计数 ⇒ 全等比较也没用。
2. `selfcode.create_engineering_task`：昨天加的 `_dup` **只决定要不要通知**，
   仍然每次都建新单 ⇒ 同一需求被建 11 张 `rejected`。

## 本测试锁死的边界

1. `_health_topic_key` 把「3 次」与「9 次」判为同主题；不同病灶不误判；
2. health_alerts 建单前查未关闭同主题 ⇒ 已有就不建（信号不丢，只不重复建单）；
3. selfcode 重复需求 ⇒ **复用既有 rejected 单**，不新建、不重报；
4. 新需求（不同主题）仍然正常建单（不误杀）。
"""
import types

import pytest

from anima.cognitive.selfcode import SelfCodeManager
from anima.os.control_surface import _health_topic_key


# ── 1. health_alerts 主题归一 ─────────────────────────────────────────


def test_health_topic_key_normalizes_counts():
    a = _health_topic_key("[health_alerts] 窗口内检测到 3 次权限漂移事件（安全关注）")
    b = _health_topic_key("[health_alerts] 窗口内检测到 9 次权限漂移事件（安全关注）")
    assert a == b, f"不同计数应判同主题：{a!r} vs {b!r}"


def test_health_topic_key_distinguishes_issues():
    a = _health_topic_key("[health_alerts] 窗口内检测到 9 次权限漂移事件（安全关注）")
    b = _health_topic_key("[health_alerts] 窗口内检测到 4 次内存异常（安全关注）")
    assert a != b, "不同病灶不得误判为同一主题（否则会漏报）"


# ── 2. health_alerts 不重复建单 ───────────────────────────────────────


class _Task:
    def __init__(self, request, status="planned", kind="diagnostic"):
        self.request = request
        self.status = status
        self.kind = kind
        self.id = f"t-{request[:8]}"


class _TaskStore:
    def __init__(self, items=()):
        self._items = list(items)
        self.created = []

    def list(self):
        return list(self._items)

    def create(self, request, creator, plan=None, kind="diagnostic"):
        t = _Task(request, "planned", kind)
        self._items.append(t)
        self.created.append(request)
        return t


def _surface(tasks):
    from anima.os.control_surface import HumanAIControlSurface
    cs = HumanAIControlSurface.__new__(HumanAIControlSurface)
    cs._core = types.SimpleNamespace(
        tasks=tasks,
        config=types.SimpleNamespace(
            persistence=types.SimpleNamespace(data_dir="data"),
        ),
        audit=None,
    )
    return cs


def test_health_alerts_skips_same_topic(monkeypatch):
    """已有同主题在办 ⇒ 不再建单。"""
    existing = [_Task("[health_alerts] 窗口内检测到 7 次权限漂移事件（安全关注）")]
    store = _TaskStore(existing)
    cs = _surface(store)

    monkeypatch.setattr(
        "anima.os.long_running.latest_stability",
        lambda root, w: {"ok": True},
    )
    monkeypatch.setattr(
        "anima.os.long_running.detect_alerts",
        lambda rec: [{"severity": "warn", "type": "permission_drift",
                      "message": "窗口内检测到 9 次权限漂移事件（安全关注）",
                      "value": 9}],
    )

    made = cs.health_alerts_to_owner(window_days=7, owner_id=42)

    assert made == [], "同主题已在办时不应再建单"
    assert store.created == [], f"不得新建任何单，实得 {store.created}"


def test_health_alerts_creates_for_new_topic(monkeypatch):
    """新病灶照常建单（不误杀）。"""
    existing = [_Task("[health_alerts] 窗口内检测到 7 次权限漂移事件（安全关注）")]
    store = _TaskStore(existing)
    cs = _surface(store)

    monkeypatch.setattr(
        "anima.os.long_running.latest_stability",
        lambda root, w: {"ok": True},
    )
    monkeypatch.setattr(
        "anima.os.long_running.detect_alerts",
        lambda rec: [{"severity": "warn", "type": "memory",
                      "message": "窗口内检测到 4 次内存异常（安全关注）",
                      "value": 4}],
    )

    made = cs.health_alerts_to_owner(window_days=7, owner_id=42)

    assert len(made) == 1, "新主题应照常建单"
    assert len(store.created) == 1


def test_health_alerts_ignores_closed_tasks(monkeypatch):
    """已关闭（expired/失败）的同主题不算「在办」⇒ 可再建。"""
    closed = [_Task("[health_alerts] 9 次权限漂移事件", status="expired")]
    store = _TaskStore(closed)
    cs = _surface(store)

    monkeypatch.setattr(
        "anima.os.long_running.latest_stability",
        lambda root, w: {"ok": True},
    )
    monkeypatch.setattr(
        "anima.os.long_running.detect_alerts",
        lambda rec: [{"severity": "warn", "type": "permission_drift",
                      "message": "9 次权限漂移事件", "value": 9}],
    )

    made = cs.health_alerts_to_owner(window_days=7, owner_id=42)
    assert len(made) == 1, "已关闭的不应阻止新建（否则问题复发永远收不到）"


# ── 3. selfcode 复用既有单，不重复建 ──────────────────────────────────


class _ScStore:
    def __init__(self, items=()):
        self._items = list(items)
        self.created = []

    def list(self):
        return list(self._items)

    def create(self, request, creator, plan=None, kind="selfcode"):
        t = _Task(request, "awaiting_approval", kind)
        self._items.append(t)
        self.created.append(request)
        return t

    def set_status(self, tid, status, evidence=""):
        for t in self._items:
            if t.id == tid:
                t.status = status
                t.status_evidence = evidence
        return None

    def get(self, tid):
        for t in self._items:
            if t.id == tid:
                return t
        return None


def _sc(store):
    m = SelfCodeManager.__new__(SelfCodeManager)
    m.core = types.SimpleNamespace(
        config=types.SimpleNamespace(
            permissions=types.SimpleNamespace(owners=[42]),
        ),
        tasks=store,
        gateway=None,
        audit=None,
    )
    # 受理期边界判定在生成之前，但函数开头会检查 cfg.enabled
    from anima.core.config import SelfCodeConfig
    m.cfg = SelfCodeConfig(enabled=True)
    m.workspace = None
    return m


async def test_selfcode_duplicate_reuses_existing_task():
    """🔴 同一需求第 2 次受理 ⇒ 复用既有 rejected 单，不再新建。

    生产实证的措辞差异范围（相似度实测）：
      「做一个输入失效链接自动查询网络历史快照并提取正文内容的工具」
      「做一个输入失效链接自动查询网络历史快照的工具」  ≈ 0.69 ⇒ 判重复
      「做一个输入失效网页URL可以自动用Wayback Machine查询其历史快照的工具」
      「做一个查询失效网页URL可以自动调用Wayback Machine查询历史快照的工具」 ≈ 0.75 ⇒ 判重复
    """
    prior = _Task("做一个输入失效链接自动查询网络历史快照并提取正文内容的工具",
                  status="rejected", kind="selfcode")
    store = _ScStore([prior])
    m = _sc(store)

    got = await m.create_engineering_task(
        "做一个输入失效链接自动查询网络历史快照的工具", 42
    )

    assert got.id == prior.id, "应复用既有单"
    assert store.created == [], f"不得新建，实得 {store.created}"


async def test_selfcode_far_apart_wording_is_treated_as_new():
    """🔴 保守取向的另一面：措辞差太远 ⇒ 判为新需求（宁可多发一次通知）。

    这保证「真实新需求」不会因为去重被静默 —— 比误杀更可接受。
    """
    prior = _Task("做一个输入失效链接查快照的工具", status="rejected", kind="selfcode")
    store = _ScStore([prior])
    m = _sc(store)

    _prior_hit = m._find_same_boundary_rejection("写一个本地日志分析脚本")
    assert _prior_hit is None, "不相关需求不得判为重复"



async def test_selfcode_new_goal_still_creates():
    """不同需求照常受理（不误杀）——不得复用它那条 rejected 单。"""
    prior = _Task("做一个输入失效链接查快照的工具", status="rejected", kind="selfcode")
    store = _ScStore([prior])
    m = _sc(store)

    # 让生成阶段快速失败，只验证「没走复用分支」
    async def _boom(goal):
        raise RuntimeError("测试桩：跳过真实生成")

    m._run_claude = _boom
    try:
        await m.create_engineering_task("写一个本地日志分析脚本", 42)
    except Exception:
        pass

    # 关键断言：新建了单（不是复用 prior）
    assert store.created, "新目标应有新建单"
    assert store.created[0] != prior.request

