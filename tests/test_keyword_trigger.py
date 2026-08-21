"""P10.2-C keyword trigger (owner decision: "哈基米" answers without @).

Rules verified:
  * keyword inside an ALLOWED group + no @ -> admitted (keyword_triggered)
  * keyword message still goes through the full pipeline -> ANSWER (LLM),
    never execution
  * keyword in a NON-allowed group -> group_not_allowed (silent)
  * no keyword configured -> pure PASSIVE unchanged
  * plain untriggered chatter (no keyword) -> passive_not_triggered
"""

import pathlib

import pytest

from anima.pilot.entry_gate import EntryGate, GateConfig
from anima.pilot.pilot_runtime import LivePilotConfig, PilotRuntime


def _gate(**cfg):
    base = dict(allowed_groups=("123456",), keyword_triggers=("哈基米",))
    base.update(cfg)
    return EntryGate(GateConfig(**base))


def test_keyword_admits_without_at():
    d = _gate().admit(group_id="123456", user_id="10001", message_id="m1",
                      content="哈基米 今天也好可爱", triggered=False)
    assert d.admitted is True


def test_keyword_passthrough_full_pipeline(tmp_path):
    rt = PilotRuntime(
        config=LivePilotConfig(allowed_groups=("123456",),
                               min_interval_sec=0.0,
                               keyword_triggers=("哈基米",)),
        data_dir=str(tmp_path / "data"),
    )
    r = rt.handle({
        "topic": "event.message.group", "user_id": "10001",
        "group_id": "123456", "message_id": "m1",
        "text": "哈基米 在吗", "time": 1786450000, "mentions_self": False,
        "raw": {"message_type": "group"},
    })
    # admitted -> ANSWER -> LLM fallback answers (never executed)
    assert r.handled is False
    assert r.denied_reason == "llm_answer"
    assert rt._evidence_log.read()[0]["intent"] == "ANSWER"


def test_keyword_outside_allowed_group_denied():
    d = _gate().admit(group_id="99999", user_id="10001", message_id="m1",
                      content="哈基米", triggered=False)
    assert d.admitted is False
    assert d.reason == "group_not_allowed"


def test_no_keyword_config_pure_passive(tmp_path):
    rt = PilotRuntime(
        config=LivePilotConfig(allowed_groups=("123456",), min_interval_sec=0.0),
        data_dir=str(tmp_path / "data"),
    )
    r = rt.handle({
        "topic": "event.message.group", "user_id": "10001",
        "group_id": "123456", "message_id": "m1",
        "text": "哈基米 在吗", "time": 1786450000, "mentions_self": False,
        "raw": {"message_type": "group"},
    })
    assert r.handled is False
    assert r.denied_reason == "passive_not_triggered"  # unchanged


def test_plain_chatter_still_silent(tmp_path):
    rt = PilotRuntime(
        config=LivePilotConfig(allowed_groups=("123456",),
                               min_interval_sec=0.0,
                               keyword_triggers=("哈基米",)),
        data_dir=str(tmp_path / "data"),
    )
    r = rt.handle({
        "topic": "event.message.group", "user_id": "10001",
        "group_id": "123456", "message_id": "m1",
        "text": "今天天气不错", "time": 1786450000, "mentions_self": False,
        "raw": {"message_type": "group"},
    })
    assert r.denied_reason == "passive_not_triggered"
