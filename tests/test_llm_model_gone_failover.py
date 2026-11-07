"""主模型下架时，400 也必须允许切到 fallback 候选。"""

from __future__ import annotations

import pytest

from anima.llm import LLMClient


def _client(status, err):
    c = LLMClient.__new__(LLMClient)
    c.last_status = status
    c.last_error = err
    return c


def test_model_not_found_allows_failover():
    c = _client(400, "http_400:{\"code\":\"model_not_found\",\"message\":\"unknown provider for model X\"}")
    assert c._is_model_gone() is True


def test_unknown_provider_allows_failover():
    c = _client(400, "unknown provider for model THIS-MODEL-DOES-NOT-EXIST")
    assert c._is_model_gone() is True


def test_plain_400_does_not_failover():
    c = _client(400, "invalid parameter: temperature")
    assert c._is_model_gone() is False


def test_other_status_does_not_failover():
    c = _client(429, "model_not_found")
    assert c._is_model_gone() is False


def test_none_error_is_safe():
    c = _client(400, "")
    assert c._is_model_gone() is False