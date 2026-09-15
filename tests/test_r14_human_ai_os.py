"""R14 Human-AI OS 测试：控制面聚合 + 受限自然语言指令解析。

验证：全景聚合、只读指令直接返回、特权指令只产出「待确认动作」（指向既有 Owner 授权
端点，不自我授权）、未识别指令 unknown。纯确定性、无 LLM、不触生产。
"""

import pytest

from anima.os.control_surface import (
    HumanAIControlSurface,
    OSOverview,
    ParsedCommand,
)


def _surface():
    return HumanAIControlSurface()


def test_overview_aggregates():
    s = _surface()
    ov = s.overview(
        health={"snapshot": {"runtime_running": 1.0}, "degraded": False, "plans": []},
        recovery_plans=[{"action": "restart_runtime", "severity": "critical",
                         "requires_owner": True}],
        evolution_proposals=[{"id": "evo-1", "status": "proposed"}],
        observation_events=3,
    )
    assert isinstance(ov, OSOverview)
    assert ov.observation_events == 3
    assert ov.degraded is True  # 含 critical 恢复计划
    d = ov.to_dict()
    assert d["degraded"] is True
    assert len(d["recovery_plans"]) == 1


def test_parse_health_readonly():
    s = _surface()
    c = s.parse_command("查看健康状态")
    assert c.intent == "health"
    assert c.needs_confirmation is False


def test_parse_list_proposals_readonly():
    s = _surface()
    c = s.parse_command("列出演化提案")
    assert c.intent == "list_proposals"
    assert c.needs_confirmation is False


def test_parse_approve_requires_confirmation():
    s = _surface()
    c = s.parse_command("批准提案 evo-abc123")
    assert c.intent == "approve"
    assert c.args["proposal_id"] == "evo-abc123"
    assert c.needs_confirmation is True
    assert "approve" in c.confirmation_endpoint
    # R14 不自我授权：仅描述既有端点，不调用
    assert c.confirmation_endpoint.startswith("POST /api/evolution/")


def test_parse_reject_requires_confirmation():
    s = _surface()
    c = s.parse_command("拒绝 evo-xyz789")
    assert c.intent == "reject"
    assert c.args["proposal_id"] == "evo-xyz789"
    assert c.needs_confirmation is True
    assert "reject" in c.confirmation_endpoint


def test_parse_recover_readonly():
    s = _surface()
    c = s.parse_command("运行健康检查")
    assert c.intent == "recover"
    assert c.needs_confirmation is False


def test_parse_unknown_returns_unknown():
    s = _surface()
    c = s.parse_command("今天天气怎么样")
    assert c.intent == "unknown"
    assert c.needs_confirmation is False


def test_parse_empty_unknown():
    s = _surface()
    assert s.parse_command("").intent == "unknown"
    assert s.parse_command("   ").intent == "unknown"


def test_parsed_command_serializable():
    c = ParsedCommand(intent="approve", args={"proposal_id": "x"},
                     needs_confirmation=True,
                     confirmation_endpoint="POST /api/evolution/x/approve",
                     preview="p")
    d = c.to_dict()
    assert set(d) == {"intent", "args", "needs_confirmation",
                      "confirmation_endpoint", "preview"}


def test_parse_stability_readonly():
    s = _surface()
    c = s.parse_command("近 30 天稳定性怎么样？稳不稳？")
    assert c.intent == "stability"
    assert c.needs_confirmation is False


def test_stability_summary_no_core():
    s = _surface()
    # 无 core 注入 → 诚实返回不可用，不崩溃
    out = s.stability_summary(7)
    assert out.get("available") is False


def test_parse_observe_readonly():
    s = _surface()
    c = s.parse_command("观察一下最近环境异常和趋势")
    assert c.intent == "observe"
    assert c.needs_confirmation is False


def test_environment_report_no_core():
    s = _surface()
    out = s.environment_report()
    assert out.get("available") is False


def test_parse_impact_readonly():
    s = _surface()
    c = s.parse_command("演化影响评分怎么样？效果值不值？")
    assert c.intent == "impact"
    assert c.needs_confirmation is False


def test_evolution_impact_no_core():
    s = _surface()
    out = s.evolution_impact()
    assert out.get("available") is False


def test_parse_society_readonly():
    s = _surface()
    c = s.parse_command("让 Agent 社会辩论一下：这个架构改动合不合理？")
    assert c.intent == "society"
    assert c.needs_confirmation is False


def test_society_debate_returns_verdict():
    s = _surface()
    out = s.society_debate("是否引入自动依赖升级")
    assert out.get("available") is not False
    assert "verdict" in out
    assert out["verdict"]["judge_id"] == "judge"
    assert out["verdict"]["decision"] in ("support", "reject", "inconclusive")


def test_parse_memory_readonly():
    s = _surface()
    c = s.parse_command("你还记得我的习惯吗？为什么这么设计？")
    assert c.intent == "memory"
    assert c.needs_confirmation is False


def test_long_term_memory_no_core():
    s = _surface()
    out = s.long_term_memory(query="中文")
    assert out.get("available") is False


def test_parse_maintenance_readonly():
    s = _surface()
    c = s.parse_command("做一次依赖自检，看看有没有过期依赖")
    assert c.intent == "maintenance"
    assert c.needs_confirmation is False


def test_parse_skill_readonly():
    s = _surface()
    c = s.parse_command("个人能力生态里，新技能怎么演进评估？")
    assert c.intent == "skill"
    assert c.needs_confirmation is False


async def test_skill_evolution_no_request():
    s = _surface()
    out = await s.skill_evolution()
    assert out.get("available") is False
