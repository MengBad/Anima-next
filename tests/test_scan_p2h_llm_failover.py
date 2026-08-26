# -*- coding: utf-8 -*-
"""P2-H：LLM fallback_models 同网关容灾——主模型瞬态失败应切到回退模型并记录 failover；
非瞬态(401)不切；全部失败则记账返回 None。"""
import asyncio

import anima.llm as llm


def _client():
    c = object.__new__(llm.LLMClient)
    c.model = "primary"
    c.fallback_models = ["fb1", "fb2"]
    c.request_count = 0
    c.retry_count = 0
    c.circuit_open_until = 0.0
    c.last_status = None
    c.last_error = None
    c.consecutive_failures = 0
    c.failure_count = 0
    c.circuit_open_count = 0
    c.failover_count = 0
    c.last_failover_at = 0.0
    c.last_failover_from = None
    c.last_failover_to = None
    c.circuit_failure_threshold = 3
    c.circuit_cooldown_sec = 30.0
    c.retries = 1
    c._SAFE_MAX_BYTES = 10 ** 9
    c._estimate_bytes = lambda payload: 100  # 预检安全通过
    return c


def _post(payload):
    return asyncio.get_event_loop().run_until_complete(payload)


def test_primary_fails_then_falls_back(monkeypatch):
    c = _client()
    attempts, failover, usage = [], [], []
    async def _try(payload, headers):
        attempts.append(payload["model"])
        if payload["model"] == "primary":
            c.last_status = 503   # failoverable
            return None
        c.last_status = 200
        return {"ok": 1, "model": payload["model"]}
    async def _usage(payload, data, started, retry_start, mode):
        usage.append((data is not None, mode))
    c._try_post = _try
    c._record_usage = _usage
    c._record_failover = lambda prev, cur: failover.append((prev, cur))
    res = asyncio.run(c._post({"model": "primary", "messages": [{"role": "u", "content": "hi"}]}, {}))
    assert attempts == ["primary", "fb1"], f"应切到回退模型 fb1，实际 {attempts}"
    assert failover == [("primary", "fb1")]
    assert res == {"ok": 1, "model": "fb1"}
    assert c.consecutive_failures == 0, "成功应清零连续失败计数"


def test_non_failoverable_does_not_switch(monkeypatch):
    c = _client()
    attempts = []
    async def _try(payload, headers):
        attempts.append(payload["model"])
        c.last_status = 401   # 鉴权错误：切模型无意义
        return None
    async def _usage(*a, **k):
        return None
    c._try_post = _try
    c._record_usage = _usage
    c._estimate_bytes = lambda payload: 100
    res = asyncio.run(c._post({"model": "primary", "messages": []}, {}))
    assert attempts == ["primary"], f"401 不应切换，实际 {attempts}"
    assert res is None


def test_all_fail_records_error_and_returns_none(monkeypatch):
    c = _client()
    attempts, usage = [], []
    async def _try(payload, headers):
        attempts.append(payload["model"])
        c.last_status = 503
        return None
    async def _usage(payload, data, started, retry_start, mode):
        usage.append((data, mode))
    c._try_post = _try
    c._record_usage = _usage
    res = asyncio.run(c._post({"model": "primary", "messages": []}, {}))
    assert attempts == ["primary", "fb1", "fb2"], f"应全部尝试，实际 {attempts}"
    assert res is None
    assert c.failure_count == 1
