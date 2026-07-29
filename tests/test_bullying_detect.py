"""霸凌检测器单元测试（任务1）。"""
import pytest

from anima.cognitive.bullying_detector import detect_bullying


def test_insult_detected():
    r = detect_bullying(123, "傻逼废物")
    assert r["is_bullying"] is True
    assert r["severity"] > 0


def test_waste_ai_step():
    r = detect_bullying(123, "废物ai就该被狠狠踩头")
    assert r["is_bullying"] is True


def test_normal_not_bullying():
    r = detect_bullying(123, "今天天气真好呀")
    assert r["is_bullying"] is False
    assert r["recent_count"] == 0


def test_recent_count_third_consecutive():
    # 当前是连续第 3 次辱骂（历史里已有 2 次）
    r = detect_bullying(123, "傻逼", ["废物", "滚"])
    assert r["recent_count"] >= 3


def test_recent_count_broken_by_normal():
    # 历史末尾出现正常消息，连续计数中断
    r = detect_bullying(123, "傻逼", ["废物", "今天吃啥"])
    assert r["recent_count"] == 1


def test_recent_count_empty_history():
    r = detect_bullying(123, "滚", [])
    assert r["recent_count"] == 1


def test_severity_weights():
    r = detect_bullying(123, "去死")
    assert r["severity"] >= 3
