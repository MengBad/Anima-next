"""不可跟进承诺的清理契约（2026-10-23）。

## 起因（生产实证：18 小时里每 30 秒一次噪音）

```
00:57:19 [自主触发] 跳过不可跟进的承诺（语气词/过短）：本大爷这就帮你去瞅瞅……
00:57:50 [自主触发] 跳过不可跟进的承诺（语气词/过短）：本大爷这就帮你去瞅瞅……
00:58:20 ...（每 30 秒一次，持续 18 小时）
```

质量闸**生效了**（没启动废任务），但它「只跳过、不清理」⇒ 那条语气词承诺
一直 `pending` ⇒ 心跳每 tick 都判定一次并打日志。自主触发因此**从未产出
任何价值**，只产生了日志噪音。

## 修法

新增 `quarantine_unfollowable_debts()`：**唯一的写操作**，把判定为不可跟进
的到期承诺标 `cancelled`，使它们不再进入后续任何判定。

职责分离（重要）：
- `collect_debt_triggers`：**保持只读**采集（既有契约不变）；
- 本函数：只负责把垃圾移出队列。

## 本测试锁死的边界

1. 语气词承诺 ⇒ 被移出（返回其 id）；
2. **真实承诺绝不被误清**（宁可漏清也不误杀）；
3. 未到期的不动（只处理 check_due 的到期项）；
4. 幂等：已 cancelled 的不会重复清理（check_due 不再返回它）；
5. 异常安全：manager 抛错 ⇒ 返回空、不崩。
"""
import types

import pytest

from anima.agent.auto_trigger import (
    is_followable_debt_text, quarantine_unfollowable_debts,
)


class _Debt:
    def __init__(self, did, content):
        self.id = did
        self.content = content


class _Mgr:
    def __init__(self, due):
        self._due = list(due)
        self.cancelled = []

    def check_due(self, now=None):
        # 模拟：已 cancel 的不再出现在到期列表里
        return [d for d in self._due if d.id not in self.cancelled]

    def cancel(self, debt_id):
        if debt_id not in self.cancelled:
            self.cancelled.append(debt_id)
            return True
        return False


def _core(mgr):
    return types.SimpleNamespace(
        config=types.SimpleNamespace(
            persistence=types.SimpleNamespace(data_dir="data"),
        )
    )


def test_quarantine_removes_filler_debt(monkeypatch):
    """语气词承诺 ⇒ 移出队列。"""
    filler = _Debt("d1", "本大爷这就帮你去瞅瞅……")
    mgr = _Mgr([filler])
    monkeypatch.setattr(
        "anima.cognitive.debt.get_debt_manager", lambda d: mgr)

    got = quarantine_unfollowable_debts(_core(mgr))

    assert got == ["d1"], f"应清理该条，实得 {got}"
    assert "d1" in mgr.cancelled


def test_quarantine_keeps_real_promises(monkeypatch):
    """🔴 真实承诺绝不能被误清（宁可漏清也不误杀）。"""
    real = _Debt("d2", "明天帮你查一下那个番剧的更新时间")
    mgr = _Mgr([real])
    monkeypatch.setattr(
        "anima.cognitive.debt.get_debt_manager", lambda d: mgr)

    got = quarantine_unfollowable_debts(_core(mgr))

    assert got == [], f"真实承诺不得被清理，实得 {got}"
    assert mgr.cancelled == []


def test_quarantine_mixed_batch(monkeypatch):
    """混合批次：只清垃圾，保留真实承诺。"""
    batch = [
        _Debt("f1", "这就去看看"),
        _Debt("r1", "下周三提醒大家交作业"),
        _Debt("f2", "好的喵"),
    ]
    mgr = _Mgr(batch)
    monkeypatch.setattr(
        "anima.cognitive.debt.get_debt_manager", lambda d: mgr)

    got = quarantine_unfollowable_debts(_core(mgr))

    assert sorted(got) == ["f1", "f2"], f"只应清垃圾，实得 {got}"
    assert "r1" not in mgr.cancelled, "真实承诺不得被误清"


def test_quarantine_is_idempotent(monkeypatch):
    """重复调用不重复清理（已 cancel 的不在 check_due 里）。"""
    mgr = _Mgr([_Debt("d3", "本大爷这就帮你去瞅瞅……")])
    monkeypatch.setattr(
        "anima.cognitive.debt.get_debt_manager", lambda d: mgr)

    first = quarantine_unfollowable_debts(_core(mgr))
    second = quarantine_unfollowable_debts(_core(mgr))

    assert first == ["d3"]
    assert second == [], "第二次不应再清理（幂等）"


def test_quarantine_survives_manager_error(monkeypatch):
    """manager 抛错 ⇒ 返回空、不崩（绝不拖垮心跳）。"""
    def _boom(d):
        raise RuntimeError("debts 文件损坏")

    monkeypatch.setattr("anima.cognitive.debt.get_debt_manager", _boom)

    assert quarantine_unfollowable_debts(_core(None)) == []


def test_quarantine_uses_same_gate_as_collection():
    """清理与采集必须用同一把尺子（否则会清掉采集愿意跑的）。"""
    # 采集会接受的，清理绝不能清
    assert is_followable_debt_text("明天帮你查一下那个番剧的更新时间") is True
    assert is_followable_debt_text("本大爷这就帮你去瞅瞅……") is False
