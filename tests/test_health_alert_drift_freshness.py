"""权限漂移告警的**时效性**契约（2026-10-23）。

## 起因（一个永远清不掉的待办）

`detect_alerts()` 统计的是 **7 天窗口内的历史审计事件**，但那些事件
**可能早已被解决**。生产实证：

```
10-21  9 次漂移事件（基线过期：auto_repair 已开，基线还写 false）
10-22  2 次   ← 我用 ConfigDriftMonitor.repin() 修好基线
10-23  0 次   ← 真漂移已归零

但历史 11 条仍在 7d 窗口内 ⇒ 每 6 小时冷却就重建一张
「[health_alerts] 窗口内检测到 11 次权限漂移事件」待办。
归档一次、重建一次 —— 永远清不掉，owner 看着它反复出现。
```

之前的去重只挡「同主题**已在办**」，一旦归档（expired）就失效。

## 修法

`health_alerts_to_owner` 对 `permission_drift` 类告警，先用
`ConfigDriftMonitor.check()` 查**当前**配置与基线的差异：
- diff = 0 ⇒ 漂移**已解决**，历史计数是陈旧事件 ⇒ **不建单**；
- diff > 0 ⇒ 真的还在漂移 ⇒ 照常建单。

信号不丢：历史审计与观测面（stability_summary）仍能看到。

## 本测试锁死的边界

1. 当前无漂移 ⇒ 不建单（即使历史计数很高）；
2. 当前**有**漂移 ⇒ 照常建单（绝不能因为"历史多"就反而不报）；
3. **fail-closed**：查询失败 ⇒ 当作「仍在漂移」⇒ 仍然告警
   （宁可多报一条待办，也绝不漏报安全问题）；
4. 其他类型告警（如 degraded/anomaly）不受此逻辑影响。
"""
import types

import pytest

from anima.os.control_surface import HumanAIControlSurface


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


def _surface(tasks, *, drift_resolved=True):
    cs = HumanAIControlSurface.__new__(HumanAIControlSurface)
    cs._core = types.SimpleNamespace(
        tasks=tasks,
        config=types.SimpleNamespace(
            persistence=types.SimpleNamespace(data_dir="data"),
        ),
        audit=None,
    )
    # 直接钉住查询结果，隔离 ConfigDriftMonitor 的真实 IO
    cs._permission_drift_currently_resolved = lambda: drift_resolved
    return cs


def _patch_detect(monkeypatch, alerts):
    monkeypatch.setattr("anima.os.long_running.latest_stability",
                        lambda root, w: {"ok": True})
    monkeypatch.setattr("anima.os.long_running.detect_alerts",
                        lambda rec: alerts)


# ── 1. 已解决 ⇒ 不建单 ────────────────────────────────────────────────


def test_resolved_drift_does_not_create_ticket(monkeypatch):
    """🔴 核心：当前无漂移，即使历史计数 11 也不建单。"""
    store = _TaskStore([])
    cs = _surface(store, drift_resolved=True)
    _patch_detect(monkeypatch, [{
        "severity": "warn", "type": "permission_drift",
        "message": "窗口内检测到 11 次权限漂移事件（安全关注）", "value": 11,
    }])

    made = cs.health_alerts_to_owner(window_days=7, owner_id=42)

    assert made == [], "已解决的漂移不得再建单"
    assert store.created == [], f"不得新建，实得 {store.created}"


# ── 2. 仍在漂移 ⇒ 照常建单 ────────────────────────────────────────────


def test_unresolved_drift_still_creates_ticket(monkeypatch):
    """🔴 反面：真的还在漂移时必须报，绝不能漏报安全问题。"""
    store = _TaskStore([])
    cs = _surface(store, drift_resolved=False)
    _patch_detect(monkeypatch, [{
        "severity": "warn", "type": "permission_drift",
        "message": "窗口内检测到 3 次权限漂移事件（安全关注）", "value": 3,
    }])

    made = cs.health_alerts_to_owner(window_days=7, owner_id=42)

    assert len(made) == 1, "仍在漂移时必须建单"
    assert len(store.created) == 1


# ── 3. 其他类型不受影响 ───────────────────────────────────────────────


def test_other_alert_types_unaffected(monkeypatch):
    """degraded / anomaly_spike 不走漂移时效判定。"""
    store = _TaskStore([])
    cs = _surface(store, drift_resolved=True)   # 即使"已解决"
    _patch_detect(monkeypatch, [{
        "severity": "warn", "type": "degraded",
        "message": "近 7 天有 3 天健康证明判定为降级", "value": 3,
    }])

    made = cs.health_alerts_to_owner(window_days=7, owner_id=42)

    assert len(made) == 1, "其他类型告警应照常建单"


# ── 4. fail-closed ────────────────────────────────────────────────────


def test_query_failure_treated_as_still_drifting(monkeypatch):
    """🔴 查询失败 ⇒ 当作仍在漂移 ⇒ 仍然告警（不漏报）。"""
    store = _TaskStore([])
    cs = _surface(store, drift_resolved=True)
    # 覆盖成"抛异常"的查询
    def _boom():
        raise RuntimeError("monitor 不可用")
    cs._permission_drift_currently_resolved = _boom

    _patch_detect(monkeypatch, [{
        "severity": "warn", "type": "permission_drift",
        "message": "窗口内检测到 5 次权限漂移事件", "value": 5,
    }])

    made = cs.health_alerts_to_owner(window_days=7, owner_id=42)

    # 异常被内部吞掉后应落到"保守告警"分支 ⇒ 仍建单
    assert len(made) == 1, "查询失败时必须保守告警（宁可多报）"


def test_helper_is_fail_closed_on_missing_config():
    """拿不到 config ⇒ 返回 False（当作仍在漂移）。"""
    cs = HumanAIControlSurface.__new__(HumanAIControlSurface)
    cs._core = types.SimpleNamespace(config=None, tasks=None, audit=None)
    assert cs._permission_drift_currently_resolved() is False
