"""CAP 探针接进 `HealthHistory` 后的判据 —— 走**真实健康面**，不测 mock 直调。

对照事件（2026-10-11）：CAP 侧某组合 35/35 全 400，Anima 侧却只能靠
自己累积失败才发现。本测试守住三件事：

1. **字段真的出现在快照里**（不是"代码里写了"——是 `_snapshot()` 的返回值里）；
2. **既有的 llm_* 字段一个都没被改坏**（WebUI 在读它们）；
3. **fail-closed**：探针缺失 / 抛异常 / 返不可达时，都必须是"未知"而非"健康"。
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from anima.monitors.cap_usage import CapModelGroup, CapUsageProbe, CapUsageSnapshot

#: 来自生产 CAP 的真实失败组合（字段名逐字保留）。
_BROKEN_GROUP = {
    "provider": "antigravity",
    "model": "gemini-3.6-flash-high",
    "alias": "gemini-3.6-flash",
    "reasoning_effort": "minimal",
    "failed": True,
    "failure_status": 400,
    "requests": 35,
    "failed_requests": 35,
    "average_latency_ns": 929993500,
}
_HEALTHY_GROUP = {
    "provider": "amd",
    "model": "deepseek-v4-pro",
    "alias": "DeepSeek-V4-Flash-AMD",
    "reasoning_effort": "",
    "failed": False,
    "failure_status": 0,
    "requests": 178,
    "failed_requests": 0,
    "average_latency_ns": 7548697912,
}


class _FakeProbe:
    """最小替身：健康面只走**同步**路径 `fetch_sync()`。

    🔴 不要把它改回 async —— `_cap_usage()` 的调用方跑在事件循环里，
    同步路径是有意为之（2026-10-11 踩过 `asyncio.run` 冲突的坑）。
    """

    def __init__(self, snapshot):
        self._snapshot = snapshot
        self.calls = 0

    def fetch_sync(self):
        self.calls += 1
        if isinstance(self._snapshot, Exception):
            raise self._snapshot
        return self._snapshot

    @property
    def base_url(self):
        return "http://fake"


def _real_probe(items):
    probe = CapUsageProbe("http://h:8317/v1")
    return probe._parse(
        {"items": items, "summary": {"requests": 213}, "range": "24h"},
        latency_ms=3.0,
    )


def _history(core):
    from anima.core.health_history import HealthHistory

    return HealthHistory(core, "/tmp/cap-test-nonexistent", min_interval_sec=0.0)


class _Core:
    """只提供 `_snapshot()` 实际会读到的那些属性。"""

    def __init__(self, cap_probe=None, llm=None):
        self.cap_probe = cap_probe
        self.llm = llm
        self._running = True
        self.gateway = None
        self.llm_handler = None
        self.agenda = None
        self.approvals = None
        self.monitors = None
        self.skill_drafts = None
        self.heartbeat = None
        self.sylann = None
        self.intent_ledger = None
        self.workflows = None
        self.episodes = None


class _FakeLLM:
    """`llm.health()` 的替身 —— 同时用来证明既有字段没被动过。"""

    def health(self):
        return {
            "model": "gemini-3.6-flash",
            "request_count": 100,
            "failure_count": 7,
            "circuit_open_until": None,
            "failover_count": 1,
            "degradation_count": 0,
        }


# ─────────────────────────  字段真的接上了  ─────────────────────────


class TestCapFieldIsWired:
    def test_snapshot_carries_upstream_cap(self):
        probe = _FakeProbe(_real_probe([_BROKEN_GROUP, _HEALTHY_GROUP]))
        snap = _history(_Core(cap_probe=probe))._snapshot(1_700_000_000.0)
        assert "upstream_cap" in snap
        cap = snap["upstream_cap"]
        assert cap["configured"] is True
        assert cap["reachable"] is True
        # 🔴 2026-10-26：`groups` 明细**不再落盘**（25KB/条，占单样本 93%，
        # 49MB → 一年 900MB）。改为只存计数，明细去 CAP 查实时。
        # 判据从「len(groups)==2」改为「groups_count==2 且无 groups 数组」。
        assert cap["groups_count"] == 2
        assert "groups" not in cap, "明细数组不该落盘（体积元凶）"

    def test_broken_group_surfaces_in_snapshot(self):
        probe = _FakeProbe(_real_probe([_BROKEN_GROUP, _HEALTHY_GROUP]))
        snap = _history(_Core(cap_probe=probe))._snapshot(1_700_000_000.0)
        broken = snap["upstream_cap"]["broken_groups"]
        assert len(broken) == 1
        assert broken[0]["model"] == "gemini-3.6-flash-high"
        assert broken[0]["failure_status"] == 400
        assert broken[0]["reasoning_effort"] == "minimal"
        assert broken[0]["all_failed"] is True

    def test_failed_models_surfaces_in_snapshot(self):
        probe = _FakeProbe(_real_probe([_BROKEN_GROUP, _HEALTHY_GROUP]))
        snap = _history(_Core(cap_probe=probe))._snapshot(1_700_000_000.0)
        assert snap["upstream_cap"]["failed_models"]["gemini-3.6-flash"] == 35

    def test_snapshot_is_json_serialisable(self):
        probe = _FakeProbe(_real_probe([_BROKEN_GROUP]))
        snap = _history(_Core(cap_probe=probe))._snapshot(1_700_000_000.0)
        json.dumps(snap)  # 写盘路径要求可序列化

    def test_probe_is_called_once_per_snapshot(self):
        probe = _FakeProbe(_real_probe([_HEALTHY_GROUP]))
        h = _history(_Core(cap_probe=probe))
        h._snapshot(1_700_000_000.0)
        assert probe.calls == 1


# ─────────────────────────  fail-closed（核心）  ─────────────────────────


class TestFailClosed:
    def test_no_probe_is_configured_false_not_healthy(self):
        snap = _history(_Core(cap_probe=None))._snapshot(1_700_000_000.0)
        # 🔴 2026-10-26：`_slim_cap` 会统一补一个 `upstream_reachable=False`
        # （把历史上两种键名 `reachable` / `upstream_reachable` 归一）。
        # 核心判据不变：**没有 reachable=True**，不能被当成"全好"。
        cap = snap["upstream_cap"]
        assert cap["configured"] is False
        assert cap.get("reachable") is not True, "没探针时不得报健康"
        assert cap["upstream_reachable"] is False

    def test_probe_raising_is_reachable_false(self):
        probe = _FakeProbe(RuntimeError("boom"))
        snap = _history(_Core(cap_probe=probe))._snapshot(1_700_000_000.0)
        cap = snap["upstream_cap"]
        assert cap["configured"] is True
        assert cap["upstream_reachable"] is False
        assert "RuntimeError" in cap["error"]

    def test_unreachable_snapshot_is_not_healthy(self):
        snap_obj = CapUsageSnapshot(reachable=False, error="HTTP 503")
        probe = _FakeProbe(snap_obj)
        snap = _history(_Core(cap_probe=probe))._snapshot(1_700_000_000.0)
        cap = snap["upstream_cap"]
        assert cap["reachable"] is False
        assert cap["error"] == "HTTP 503"
        # 2026-10-26：groups 明细不再落盘；计数为 0 等价于「没有可用模型」
        assert cap.get("groups_count", 0) == 0
        assert cap["broken_groups"] == []

    def test_health_sample_never_raises_from_bad_probe(self):
        """探针坏掉绝不能掀翻健康采样 —— 那会让监控本身成为故障源。"""

        class _Weird:
            def fetch_sync(self):
                return object()  # 不是 CapUsageSnapshot

        snap = _history(_Core(cap_probe=_Weird()))._snapshot(1_700_000_000.0)
        assert snap["upstream_cap"]["configured"] is True
        assert snap["upstream_cap"]["upstream_reachable"] is False


# ─────────────────────────  不破坏既有字段  ─────────────────────────


class TestExistingFieldsIntact:
    def test_llm_fields_still_present(self):
        snap = _history(_Core(cap_probe=None, llm=_FakeLLM()))._snapshot(
            1_700_000_000.0
        )
        assert snap["llm_requests"] == 100
        assert snap["llm_failures"] == 7
        assert snap["llm_circuit_open"] is False
        assert snap["llm_failover_count"] == 1
        assert "stage_latency_ms" in snap

    def test_cap_field_added_alongside_not_replacing(self):
        probe = _FakeProbe(_real_probe([_BROKEN_GROUP]))
        snap = _history(_Core(cap_probe=probe, llm=_FakeLLM()))._snapshot(
            1_700_000_000.0
        )
        # 两套字段共存 —— 上游自述（CAP）与自身观测（llm）是**互补**的：
        # llm 只能看见"我被拒了多少次"，CAP 能看见"上游为什么拒"。
        assert snap["llm_failures"] == 7
        assert snap["upstream_cap"]["broken_groups"][0]["failure_status"] == 400
