"""收敛计划 M2/E1 回归。"""
import asyncio

import pytest

from anima.agent.issue_sources import collect_self_monitor_issues
from anima.core.config import AnimaConfig


def test_e1_collector_files_issue_on_degradation():
    rep = {"tool_calls": 12, "tool_failures": 3, "unavailable_replies": 0}
    issues = collect_self_monitor_issues(reply_quality_report=rep)
    assert issues and "工具失败 3/12" in issues[0].title
    assert issues[0].severity == "normal"


def test_e1_collector_high_severity_and_clean_pass():
    issues = collect_self_monitor_issues(
        reply_quality_report={"tool_calls": 10, "tool_failures": 6, "unavailable_replies": 3})
    assert issues and issues[0].severity == "high"
    assert collect_self_monitor_issues(
        reply_quality_report={"tool_calls": 10, "tool_failures": 0,
                              "unavailable_replies": 0}) == []


def test_m2_unified_recall_shape():
    """M2 聚合层：各域失败静默，能取到的域返回行列表"""
    from anima.pipeline.stages._shared import unified_recall

    class FakeVM:
        async def search(self, q, top_k=3, source_uin=None, source_gid=None):
            return ["向量要点A", "向量要点B"]

    class FakeCore:
        vector_memory = FakeVM()

    async def run():
        return await unified_recall(FakeCore(), "柯基", group_id=998, user_id=999)

    out = asyncio.run(run())
    assert "vector" in out and out["vector"][0].startswith("向量要点")


def test_m2_memory_stage_flag_gate():
    """flag 关闭时 memory 阶段不注入聚合片段"""
    src = open("anima/pipeline/stages/memory.py", encoding="utf-8").read()
    assert "unified_recall_enabled" in src and 'unified_memory_recall' in src
    cfg = AnimaConfig.model_validate({})
    assert cfg.memory.unified_recall_enabled is False
    assert cfg.memory.retention_layers["detail_days"] == 7
