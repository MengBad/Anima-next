"""配置默认值校验。"""

from anima.core.config import AnimaConfig, DriftConfig, AutonomyConfig
from anima.core.config_service import MANIFEST


def test_all_defaults_match_design() -> None:
    """所有冻结决策的默认值已编码进配置。"""
    c = AnimaConfig()

    # 护栏（§6.2）
    assert c.autonomy.cooldown_sec == 90
    assert c.autonomy.daily_max_per_group == 20
    assert c.autonomy.ratio_max == 0.30
    # LLM 用量全开：0 = 不限额（仍保留超时/重试/熔断/并发锁与群打扰控制）
    assert c.autonomy.token_budget_per_day == 0
    assert c.qq.event_concurrency == 12
    assert c.qq.max_pending_events == 128
    assert c.llm.fallback_models == []
    assert c.llm.circuit_failure_threshold == 3
    assert c.llm.circuit_cooldown_sec == 30.0
    assert {item["path"] for item in MANIFEST} >= {
        "llm.fallback_models", "llm.circuit_failure_threshold", "llm.circuit_cooldown_sec",
    }

    # 上下文：人格/本轮输入保护，低优先级片段与旧历史按预算退出。
    assert c.context.input_token_budget == 12000
    assert c.context.system_token_budget == 6000
    assert c.context.tool_result_token_budget == 1800

    # 漂移（§15.3）
    assert c.drift.delta_max == 0.10
    assert c.drift.daily_budget == 1.5
    assert c.drift.self_correction_daily_budget == 0.5
    assert c.drift.window_ceiling == 4.0
    assert c.drift.similarity_floor == 0.75
    assert c.drift.valence_min == -0.30
    assert c.drift.valence_max == 0.70
    assert c.drift.checkpoints_keep == 7

    # embedding（§14 已定：远程）
    assert c.vector.embedding_backend == "remote"
    assert c.vector.cache is True

    # aiosqlite 已引
    assert c.persistence.use_sqlite is True

    # NapCat 同机
    assert c.deploy.napcat_topology == "same-host"

    # compat Tier 1
    assert c.compat.astrbot_tier == 1
