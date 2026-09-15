"""第四轮体验测试修复回归（C1/C2/C3 + X 系列）。"""
import asyncio

import pytest

from anima.agent.intent import is_agent_task
from anima.core.autonomy import _is_ungrounded_proactive


def test_file_op_not_agent_task():
    """C3：单步文件写/读不劫持进 Agent 草稿流（owner 级）"""
    assert is_agent_task("帮我把「测试内容」写进 r4.txt 文件", 4) is False
    assert is_agent_task("读一下 r4.txt 文件里写了什么", 4) is False
    # 复合多步任务仍走 Agent
    assert is_agent_task("帮我把配置写进文件，然后再跑一遍测试", 4) is True


def test_ungrounded_still_blocked():
    assert _is_ungrounded_proactive("输入数据解析异常，请提供坐标")


def test_firefox_proxy_plumbed():
    """X4：firefox 分支的 launch 参数包含代理（源码级断言）"""
    src = open("anima/cognitive/x_browser.py", encoding="utf-8").read()
    assert '({"proxy": {"server": self.proxy}}' in src
    assert "wait_for_selector(\"article\"" in src
