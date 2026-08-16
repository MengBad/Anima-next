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
