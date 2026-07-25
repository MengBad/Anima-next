"""自升级熔断与任务压力统计（纯函数，驱动 shipped danger 入口）。"""

from types import SimpleNamespace

from anima.cognitive.danger import (
    count_selfcode_task_pressure,
    should_throttle_self_improvement,
)


def test_should_throttle_too_many_running():
    skip, reason = should_throttle_self_improvement(
        running_count=2, recent_failed_count=0, max_running=2, max_recent_failed=3,
    )
    assert skip and reason == "too_many_running"


def test_should_throttle_failure_circuit():
    skip, reason = should_throttle_self_improvement(
        running_count=0, recent_failed_count=3, max_running=2, max_recent_failed=3,
    )
    assert skip and reason == "failure_circuit"


def test_should_not_throttle_when_under_limits():
    skip, reason = should_throttle_self_improvement(
        running_count=1, recent_failed_count=2, max_running=2, max_recent_failed=3,
    )
    assert not skip and reason == ""


def test_count_selfcode_task_pressure_window():
    now = 1_000_000.0
    tasks = [
        SimpleNamespace(kind="selfcode", status="running", updated_at=now),
        SimpleNamespace(kind="selfcode", status="running", updated_at=now - 10),
        SimpleNamespace(kind="selfcode", status="failed", updated_at=now - 100),
        SimpleNamespace(kind="selfcode", status="failed", updated_at=now - 10_000),  # outside 7200
        SimpleNamespace(kind="fault_ticket", status="running", updated_at=now),
        SimpleNamespace(kind="selfcode", status="completed", updated_at=now),
    ]
    running, failed = count_selfcode_task_pressure(
        tasks, now=now, failure_window_sec=7200,
    )
    assert running == 2
    assert failed == 1


def test_count_selfcode_task_pressure_updated_at_zero():
    """updated_at=0 的 failed 任务应使用 created_at 兜底，不再无条件计入。"""
    now = 1_000_000.0
    tasks = [
        # updated_at=0, created_at 在窗口内 → 应计入
        SimpleNamespace(kind="selfcode", status="failed", updated_at=0, created_at=now - 100),
        # updated_at=0, created_at 也在窗口外 → 不应计入
        SimpleNamespace(kind="selfcode", status="failed", updated_at=0, created_at=now - 10_000),
        # updated_at=0, created_at=0 → 不应计入（旧数据不再永久阻塞）
        SimpleNamespace(kind="selfcode", status="failed", updated_at=0, created_at=0),
        # updated_at=0, created_at 缺失 → 不应计入
        SimpleNamespace(kind="selfcode", status="failed", updated_at=0),
    ]
    running, failed = count_selfcode_task_pressure(
        tasks, now=now, failure_window_sec=7200,
    )
    assert running == 0
    assert failed == 1  # 只有 created_at 在窗口内的那条
