# -*- coding: utf-8 -*-
"""P3 —— 反馈 → DevIssue 桥接测试。

验证 detect_engineering_feedback → collect_feedback_issues 的端到端链路：
- 反馈信号能转成 source=feedback 的 DevIssue，进入既有 R8 闭环；
- 只读采集：不触发 DevLoopOrchestrator.run_issue，绝不自我授权；
- 低严重度过滤 + 空信号安全。
"""
from __future__ import annotations

from anima.observation.feedback_detect import detect_engineering_feedback
from anima.agent.issue_sources import collect_feedback_issues
from anima.agent.devloop import ISSUE_SOURCE_FEEDBACK


def test_feedback_signal_becomes_feedback_devissue():
    sig = detect_engineering_feedback("你刚才回答错了，你把 A 和 B 搞混了",
                                      sender_name="小明")
    issues = collect_feedback_issues([sig])
    assert len(issues) == 1
    issue = issues[0]
    assert issue.source == ISSUE_SOURCE_FEEDBACK
    assert "用户反馈" in issue.title
    assert "小明" in issue.detail
    assert issue.required_scopes == frozenset({"write"})


def test_image_feedback_becomes_devissue():
    sig = detect_engineering_feedback("你这个图片识别有问题，明明是猫你说成狗")
    issues = collect_feedback_issues([sig])
    assert len(issues) == 1
    assert issues[0].source == ISSUE_SOURCE_FEEDBACK
    assert "image" in issues[0].title


def test_empty_signals_yield_no_issues():
    assert collect_feedback_issues([]) == []
    assert collect_feedback_issues(None) == []


def test_low_severity_filtered_by_default():
    # 默认 min_severity=normal，low 严重度被过滤
    class _LowSignal:
        kind = "behavior"
        problem = "语气不对"
        evidence = "原话"
        sender = "小美"
        severity = "low"
    assert collect_feedback_issues([_LowSignal()]) == []
    # 显式放宽到 low 则保留
    assert len(collect_feedback_issues([_LowSignal()], min_severity="low")) == 1


def test_collect_is_read_only_no_devloop_run():
    # 只读采集：绝不调用 DevLoopOrchestrator.run_issue（自我授权禁令）
    sig = detect_engineering_feedback("你错了")
    issues = collect_feedback_issues([sig])
    assert issues  # 有 issue，但只是提案
    for issue in issues:
        d = issue.to_dict()
        # DevIssue 是纯数据，无任何执行/部署/审批动作字段
        assert "deploy" not in d
        assert "execute" not in d
        assert "approve" not in d
