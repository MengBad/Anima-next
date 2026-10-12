"""`anima.monitors.cap_usage` 的判据 —— 重点在 fail-closed 与"不伪造健康"。

对照的真实事件（2026-10-11）：CAP 侧 `gemini-3.6-flash-high`（alias
`gemini-3.6-flash`）在 `reasoning_effort=minimal` 档上 35 次调用 **35 次全 400**，
而 Anima 侧只能等自己被打回来才知道。本模块的价值就是让这份"上游自述"
可读、可判、且**读不到时不会谎报正常**。
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from anima.monitors.cap_usage import (
    CAP_PREFIX,
    CapModelGroup,
    CapUsageProbe,
    CapUsageSnapshot,
)

#: 取自 2026-10-11 生产 CAP 的真实返回形状（字段名逐字保留）。
_REAL_ITEMS = [
    {
        "provider": "antigravity",
        "executor_type": "AntigravityExecutor",
        "model": "gemini-3.6-flash-high",
        "alias": "gemini-3.6-flash",
        "source": "Antigravity-antigravity-migrated@example.com",
        "auth_type": "oauth",
        "service_tier": "auto",
        "reasoning_effort": "minimal",
        "failed": True,
        "failure_status": 400,
        "requests": 35,
        "failed_requests": 35,
        "input_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "cached_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "total_tokens": 0,
        "total_latency_ns": 32549772509,
        "total_ttft_ns": 32093312611,
        "latency_samples": 35,
        "ttft_samples": 35,
        "average_latency_ns": 929993500,
        "average_ttft_ns": 916951788,
    },
    {
        "provider": "antigravity",
        "model": "gemini-3.8-flash-high",
        "alias": "gemini-3.8-flash-high",
        "reasoning_effort": "low",
        "failed": False,
        "failure_status": 0,
        "requests": 724,
        "failed_requests": 4,
        "average_latency_ns": 5078000000,
    },
    {
        "provider": "amd",
        "model": "deepseek-v4-pro",
        "alias": "DeepSeek-V4-Flash-AMD",
        "reasoning_effort": "",
        "failed": False,
        "failure_status": 0,
        "requests": 178,
        "failed_requests": 0,
        "average_latency_ns": 7548697912,
    },
]


def _snapshot(items=None, **kwargs) -> CapUsageSnapshot:
    payload = {
        "schema_version": 2,
        "range": "24h",
        "retained_since": "2026-09-30T04:36:48Z",
        "last_used": "2026-10-11T11:03:23Z",
        "summary": {"requests": 1240, "failed_requests": 44},
        "items": _REAL_ITEMS if items is None else items,
    }
    probe = CapUsageProbe("http://127.0.0.1:8317/v1")
    return probe._parse(payload, latency_ms=12.5)


class TestBaseUrl:
    def test_strips_v1_suffix(self):
        assert CapUsageProbe("http://54.168.242.65:8317/v1").base_url == (
            "http://54.168.242.65:8317"
        )

    def test_keeps_plain_root(self):
        assert CapUsageProbe("http://host:8317/").base_url == "http://host:8317"

    def test_prefix_is_the_measured_route(self):
        # 这条路由是实测出来的（/cap-token-usage-tracker/dashboard 是 404，
        # 真正的数据面在 /v0/resource/plugins/... 下面）。
        assert CAP_PREFIX == "/v0/resource/plugins/cap-token-usage-tracker"


class TestParseRealShape:
    def test_parses_all_groups(self):
        snap = _snapshot()
        assert snap.reachable is True
        assert len(snap.groups) == 3
        assert snap.summary["requests"] == 1240
        assert snap.retained_since.startswith("2026-09-30")

    def test_group_fields_preserved(self):
        snap = _snapshot()
        g = snap.groups[0]
        assert g.model == "gemini-3.6-flash-high"
        assert g.alias == "gemini-3.6-flash"
        assert g.reasoning_effort == "minimal"
        assert g.requests == 35
        assert g.failed_requests == 35
        assert g.failure_status == 400

    def test_success_rate(self):
        snap = _snapshot()
        assert snap.groups[0].success_rate == 0.0
        assert snap.groups[2].success_rate == 1.0
        assert snap.groups[1].success_rate == pytest.approx(1 - 4 / 724)

    def test_zero_requests_does_not_divide_by_zero(self):
        g = CapModelGroup("p", "m", "a", "low", 0, 0, 0, 0)
        assert g.success_rate == 0.0
        assert g.all_failed is False

    def test_to_dict_is_json_serialisable(self):
        snap = _snapshot()
        json.dumps(snap.to_dict())  # 不许抛
        json.dumps(snap.groups[0].to_dict())


class TestBrokenGroupAttribution:
    """核心判据：把"这个组合已经废了"从噪声里挑出来。"""

    def test_picks_the_all_failed_group(self):
        snap = _snapshot()
        broken = snap.broken_groups()
        assert len(broken) == 1
        b = broken[0]
        assert b.model == "gemini-3.6-flash-high"
        assert b.failure_status == 400
        assert b.reasoning_effort == "minimal"

    def test_high_traffic_noise_is_not_broken(self):
        # 724 次里 4 次失败 —— 不是事故，不能被算作"废了"。
        snap = _snapshot()
        names = [g.model for g in snap.broken_groups()]
        assert "gemini-3.8-flash-high" not in names

    def test_tiny_sample_is_not_broken(self):
        # 1 次全失败可能是首次试错，不该据此判死。
        items = [dict(_REAL_ITEMS[0], requests=1, failed_requests=1)]
        snap = _snapshot(items)
        assert snap.broken_groups() == []

    def test_failed_models_aggregates_by_alias(self):
        snap = _snapshot()
        fm = snap.failed_models()
        assert fm["gemini-3.6-flash"] == 35
        assert fm["gemini-3.8-flash-high"] == 4
        assert "DeepSeek-V4-Flash-AMD" not in fm  # 零失败不进表

    def test_healthy_snapshot_has_no_broken_groups(self):
        items = [dict(_REAL_ITEMS[2])]
        assert _snapshot(items).broken_groups() == []


class TestFailClosed:
    """读不到 ≠ 没事。每一条都要明确是 `reachable=False`。"""

    def test_missing_items_is_not_reachable(self):
        probe = CapUsageProbe("http://h:8317/v1")
        snap = probe._parse({"summary": {}}, latency_ms=1.0)
        assert snap.reachable is False
        assert "items" in snap.error

    def test_non_dict_body_is_not_reachable(self):
        probe = CapUsageProbe("http://h:8317/v1")
        snap = probe._parse(["nope"], latency_ms=1.0)
        assert snap.reachable is False

    def test_no_endpoint_configured(self):
        probe = CapUsageProbe("")
        import asyncio

        snap = asyncio.run(probe.fetch())
        assert snap.reachable is False
        assert "endpoint" in snap.error

    def test_unreachable_snapshot_has_no_groups(self):
        snap = CapUsageSnapshot(reachable=False, error="HTTP 503")
        assert snap.groups == []
        assert snap.broken_groups() == []
        assert snap.failed_models() == {}
        d = snap.to_dict()
        assert d["reachable"] is False and d["error"] == "HTTP 503"


class TestFetchNetworkBehaviour:
    """fetch() 的失败路径必须转成数据，不许抛。"""

    def test_http_error_maps_to_unreachable(self):
        import asyncio

        probe = CapUsageProbe("http://h:8317/v1")

        class _Resp:
            status = 503

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def json(self, content_type=None):
                return {}

        class _Session:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def get(self, url, headers=None):
                return _Resp()

        fake_aiohttp = MagicMock()
        fake_aiohttp.ClientTimeout = lambda total: None
        fake_aiohttp.ClientSession = _Session

        with patch.dict("sys.modules", {"aiohttp": fake_aiohttp}):
            snap = asyncio.run(probe.fetch())
        assert snap.reachable is False
        assert snap.error == "HTTP 503"

    def test_exception_maps_to_unreachable(self):
        import asyncio

        probe = CapUsageProbe("http://h:8317/v1")

        class _Boom:
            def __init__(self, *a, **k):
                raise RuntimeError("connection refused")

        fake_aiohttp = MagicMock()
        fake_aiohttp.ClientTimeout = lambda total: None
        fake_aiohttp.ClientSession = _Boom

        with patch.dict("sys.modules", {"aiohttp": fake_aiohttp}):
            snap = asyncio.run(probe.fetch())
        assert snap.reachable is False
        assert "RuntimeError" in snap.error

    def test_success_path_sends_expected_url_and_headers(self):
        import asyncio

        probe = CapUsageProbe("http://h:8317/v1", token="tok")
        seen = {}

        class _Resp:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def json(self, content_type=None):
                return {"items": _REAL_ITEMS, "summary": {}, "range": "24h"}

        class _Session:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def get(self, url, headers=None):
                seen["url"] = url
                seen["headers"] = headers
                return _Resp()

        fake_aiohttp = MagicMock()
        fake_aiohttp.ClientTimeout = lambda total: None
        fake_aiohttp.ClientSession = _Session

        with patch.dict("sys.modules", {"aiohttp": fake_aiohttp}):
            snap = asyncio.run(probe.fetch())

        assert snap.reachable is True
        assert seen["url"].startswith("http://h:8317" + CAP_PREFIX + "/stats/groups")
        assert "range=24h" in seen["url"]
        assert seen["headers"]["Authorization"] == "Bearer tok"


class TestSyncPathInsideEventLoop:
    """🔴 回归：健康采样的调用方**跑在事件循环里**。

    2026-10-11 实测踩坑：`health_history._cap_usage()` 最初写
    `asyncio.run(probe.fetch())` ⇒ 在心跳（async）里必抛
    `RuntimeError: asyncio.run() cannot be called from a running event loop`，
    被 except 吞掉后**每次都静默返回"不可达"**
    （症状：`upstream_cap.reachable=None`、groups=0，stderr 打
    `coroutine 'fetch' was never awaited`）。
    ⇒ 必须以同步路径提供服务，并在**真实事件循环内**验证可用。
    """

    def test_fetch_sync_exposes_status_url(self):
        probe = CapUsageProbe("http://h:8317/v1", token="tok")
        url = probe._status_url
        assert url.startswith("http://h:8317" + CAP_PREFIX + "/stats/groups")
        assert "range=24h" in url

    def test_fetch_sync_parses_ok(self):
        probe = CapUsageProbe("http://h:8317/v1")
        body = json.dumps({"items": _REAL_ITEMS, "summary": {"requests": 1},
                           "range": "24h"}).encode()

        class _Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return body

        with patch("urllib.request.urlopen", return_value=_Resp()):
            snap = probe.fetch_sync()
        assert snap.reachable is True
        assert len(snap.groups) == 3
        assert snap.broken_groups()[0].failure_status == 400

    def test_fetch_sync_inside_running_loop_does_not_raise(self):
        """决定性判据：在真实运行的 loop 里调用同步路径，必须拿到数据。"""
        import asyncio

        probe = CapUsageProbe("http://h:8317/v1")
        body = json.dumps({"items": _REAL_ITEMS, "summary": {}}).encode()
        result = {}

        class _Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return body

        async def _inner():
            # 这里就是心跳的处境：循环在跑。
            with patch("urllib.request.urlopen", return_value=_Resp()):
                result["snap"] = probe.fetch_sync()

        asyncio.run(_inner())
        snap = result["snap"]
        assert snap.reachable is True, "同步路径在事件循环内必须可用"
        assert snap.groups, "必须真的解析出组合"

    def test_fetch_sync_http_error_is_unreachable(self):
        import urllib.error

        probe = CapUsageProbe("http://h:8317/v1")
        err = urllib.error.HTTPError("http://h", 503, "boom", {}, None)
        with patch("urllib.request.urlopen", side_effect=err):
            snap = probe.fetch_sync()
        assert snap.reachable is False
        assert snap.error == "HTTP 503"

    def test_fetch_sync_bad_json_is_unreachable(self):
        probe = CapUsageProbe("http://h:8317/v1")

        class _Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b"<html>not json"

        with patch("urllib.request.urlopen", return_value=_Resp()):
            snap = probe.fetch_sync()
        assert snap.reachable is False
        assert "JSON" in snap.error

    def test_fetch_sync_no_endpoint(self):
        snap = CapUsageProbe("").fetch_sync()
        assert snap.reachable is False
