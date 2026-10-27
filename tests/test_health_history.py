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


# ── 读入时精简（2026-10-27）────────────────────────────────────────

def test_restore_slims_legacy_upstream_cap(tmp_path):
    """读入存量样本时也要精简 `upstream_cap`，不能只在写入时精简。

    ## 为什么

    `_slim_cap` 原来只在 `_snapshot()`（写新样本）里调用 ⇒
    **存量样本永远不被精简**，而且每次启动把旧文件整份读进内存、
    下次落盘又整份写回 —— 手工清理也没用。

    生产实测：清到 6.4MB，重启后又回到 42.7MB，
    含 `groups` 的样本仍有 1086 条。

    `upstream_cap.groups` 是 106 个模型的完整明细数组，单条约 25KB，
    占单条体积 93%。健康历史要的是趋势，不是模型明细快照。
    """
    import json

    from anima.core.health_history import HealthHistory

    legacy = {
        "version": 1,
        "samples": [
            {
                "ts": 1000.0,
                "upstream_cap": {
                    "reachable": True,
                    "range": "1h",
                    "groups": [
                        {"model": f"m{i}", "ok": True, "latency_ms": 12.3,
                         "detail": "x" * 200}
                        for i in range(106)
                    ],
                    "failed_models": {"m7": 3},
                },
            },
            {"ts": 1001.0, "upstream_cap": {"reachable": False, "groups": []}},
        ],
    }
    p = tmp_path / "health_history.json"
    p.write_text(json.dumps(legacy), encoding="utf-8")

    class _Core:
        gateway = None
        llm = None

    h = HealthHistory.__new__(HealthHistory)
    h.core = _Core()
    from anima.core.persistence import PersistenceProvider
    h.persistence = PersistenceProvider(str(tmp_path))
    h.path = "health_history.json"
    h.max_samples = 2880
    h.min_interval_sec = 60.0
    h._samples = []
    h._last_record_at = 0.0
    h._restore()

    assert len(h._samples) == 2
    for s in h._samples:
        cap = s.get("upstream_cap") or {}
        assert "groups" not in cap, "存量样本的 groups 没被精简"
    # 趋势字段必须保留
    assert h._samples[0]["upstream_cap"].get("groups_count") == 106
    assert h._samples[0]["upstream_cap"].get("failed_models") == {"m7": 3}


def test_restore_slim_does_not_drop_samples(tmp_path):
    """精简不得丢样本，也不得因脏数据抛异常。"""
    import json

    from anima.core.health_history import HealthHistory

    data = {
        "version": 1,
        "samples": [
            {"ts": 1.0, "upstream_cap": {"groups": [{"a": 1}] * 50}},
            {"ts": 2.0, "upstream_cap": None},          # 脏
            {"ts": 3.0, "upstream_cap": "not-a-dict"},  # 更脏
            {"ts": 4.0},                                # 无字段
        ],
    }
    p = tmp_path / "health_history.json"
    p.write_text(json.dumps(data), encoding="utf-8")

    class _Core:
        gateway = None
        llm = None

    h = HealthHistory.__new__(HealthHistory)
    h.core = _Core()
    from anima.core.persistence import PersistenceProvider
    h.persistence = PersistenceProvider(str(tmp_path))
    h.path = "health_history.json"
    h.max_samples = 2880
    h.min_interval_sec = 60.0
    h._samples = []
    h._last_record_at = 0.0
    h._restore()

    assert len(h._samples) == 4, "精简丢样本了"
    assert "groups" not in (h._samples[0].get("upstream_cap") or {})
