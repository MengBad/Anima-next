"""2026-11-27 P2 推进的守卫测试。"""
from __future__ import annotations

import asyncio
from pathlib import Path


def test_society_review_enabled():
    """多 Agent 评议已启用。

    原判「会显著增加 token 消耗」经复核不成立：multi_agent_society 是
    确定性论坛，全文件零 LLM 调用。开启边际成本≈0。
    """
    from anima.agent import self_evolution

    assert self_evolution.SOCIETY_REVIEW_ENABLED is True


def test_multi_agent_society_has_no_llm():
    """这条是本轮决策的依据：一旦这里引入 LLM 调用，开启评议就不再是零成本，
    必须重新评估是否继续开启。"""
    src = Path("anima/agent/multi_agent_society.py").read_text(encoding="utf-8")
    for marker in ("llm.", "chat(", "chat_structured", "await llm"):
        assert marker not in src, f"多 Agent 论坛出现了 LLM 调用痕迹: {marker}"


def test_society_review_runs_and_is_advisory():
    """评议必须能真实跑出结论，且结果只作参考（不改任何状态）。"""
    from anima.agent.society_review import run_society_review

    res = asyncio.run(run_society_review("测试目标", context="守卫测试"))
    assert res.get("available") is True
    assert len(res.get("arguments") or []) == 3, "应有正/反/中立三条论点"
    assert "verdict" in res


def test_hooks_installation_script_exists():
    """core.hooksPath 是本地配置，不随仓库分发 —— 必须有安装脚本兜底。"""
    inst = Path("scripts/install-hooks.sh")
    assert inst.is_file()
    body = inst.read_text(encoding="utf-8")
    assert "core.hooksPath" in body
    assert "pre-commit.sh" in body
