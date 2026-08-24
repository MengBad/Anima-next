"""M5：智能选图权重与否定词惩罚可配置。"""
from __future__ import annotations

import random

import pytest

from anima.cognitive.sticker_select import _resolve_weights, _query_negated_hit, score_entry


@pytest.fixture(autouse=True)
def _keep_random_state():
    state = random.getstate()
    yield
    random.setstate(state)


def test_resolve_weights_defaults_and_overrides():
    w = _resolve_weights(None)
    assert w["desc"] == 0.35 and w["tag"] == 0.25 and w["top_k"] == 3
    assert w["negation"] == 0.5
    ow = _resolve_weights({"desc": 0.8, "top_k": 5})
    assert ow["desc"] == 0.8 and ow["top_k"] == 5
    assert ow["tag"] == 0.25 and ow["scene"] == 0.20


def test_negation_hit_detector():
    assert _query_negated_hit("别笑死", "笑死") is True
    assert _query_negated_hit("笑死", "笑死") is False
    assert _query_negated_hit("没开心", "开心") is True


def test_score_entry_uses_custom_desc_weight():
    item = {"category": "dog", "desc": "一只狗头在嘲讽", "labels": ["狗头"]}
    random.seed(7)
    s_hi = score_entry("狗头 嘲讽 哈哈", dict(item), weights={"desc": 0.9})
    random.seed(7)
    s_lo = score_entry("狗头 嘲讽 哈哈", dict(item), weights={"desc": 0.1})
    assert s_hi > s_lo


def test_score_entry_negation_penalty_lowers():
    item = {"category": "x", "desc": "笑死了", "labels": ["笑死"]}
    random.seed(11)
    s_pen = score_entry("别笑死", dict(item), weights={"negation": 0.9})
    random.seed(11)
    s_nopen = score_entry("别笑死", dict(item), weights={"negation": 0.0})
    assert s_pen < s_nopen


def test_config_has_select_weights_defaults():
    from anima.core.config import AnimaConfig
    w = AnimaConfig().sticker.select_weights
    assert w["desc"] == 0.35 and w["tag"] == 0.25 and w["scene"] == 0.20
    assert w["negation"] == 0.5 and w["top_k"] == 3