from anima.core.anima_core import AnimaCore
from anima.core.config import AnimaConfig, PersistenceConfig


def test_health_history_is_bounded_and_restores(tmp_path):
    core = AnimaCore(AnimaConfig(persistence=PersistenceConfig(data_dir=str(tmp_path))))
    history = core.health_history
    history.max_samples = 3
    first = history.record(force=True)
    assert first and "channel_connected" in first
    assert first["subscriptions_total"] == 0
    assert first["skill_drafts_pending"] == 0
    history.record(force=True)
    history.record(force=True)
    history.record(force=True)
    assert len(history.query(limit=10)) == 3

    restored = AnimaCore(AnimaConfig(persistence=PersistenceConfig(data_dir=str(tmp_path))))
    assert restored.health_history.health()["samples"] == 3
    assert restored.health_history.query(limit=1)[0]["ts"]


def test_health_history_rate_limits_non_forced_samples(tmp_path):
    core = AnimaCore(AnimaConfig(persistence=PersistenceConfig(data_dir=str(tmp_path))))
    history = core.health_history
    history.min_interval_sec = 3600
    history.record(force=True)
    assert history.record() is None
