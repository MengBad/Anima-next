"""「造能力」路由（design §10.4）。

背景：owner 说「帮我做个 XX 工具」时，请求会被 `is_agent_task` 判成多步任务，
进 `owner_agent` 的 ReAct 规划器。规划器在 61 个工具里挑 search_web/run_python
草草了事，**永远想不起 propose_skill_draft**。

生产实证（2026-10-29）：
  · 「帮我做个查天气的工具」→ owner_agent + search_web → 废文本
  · 「帮我做塔罗牌工具，加到能力里」→ owner_agent + run_python → 废文本

修法：`is_skill_creation_request` 命中即**直接**走 propose_skill_draft，
由 SkillManifest 的 pydantic 校验兜底合法性，不依赖模型在 61 个工具里的选择偏好。
"""
from __future__ import annotations

import pytest

from anima.agent.intent import is_skill_creation_request

OWNER = 4
USER = 2


@pytest.mark.parametrize("text", [
    "帮我做一个能查询今天塔罗牌的工具，加到你的能力里",
    "帮我做个查天气的工具",
    "加一个每日早八提醒喝水的能力",
    "给我写个查询快递的插件",
    "新增一个能查股价的功能",
    "创建个天气预报技能",
])
def test_detects_skill_creation(text):
    assert is_skill_creation_request(text, OWNER) is True


@pytest.mark.parametrize("text", [
    "今天群里都聊什么了",          # 普通对话
    "画一只在云上睡觉的猫",        # 生图，有自己的交付通道
    "帮我生成一张吴雨萌在天上飞的图",  # 生图
    "帮我做个查天气的工具",        # 下面单独测权限
])
def test_rejects_non_creation(text):
    if "查天气的工具" in text:
        return  # 权限用例另测
    assert is_skill_creation_request(text, OWNER) is False


def test_image_request_is_exempt():
    """生图不该被 SkillDraft 劫持——它需要真实的图片交付通道。"""
    for t in ("帮我画一张猫的图片", "做个表情包", "给我画个头像"):
        assert is_skill_creation_request(t, OWNER) is False


def test_non_owner_rejected():
    """propose_skill_draft 本身是 min_level=OWNER，非 owner 不该被路由到那里。"""
    assert is_skill_creation_request("帮我做个查天气的工具", USER) is False
    assert is_skill_creation_request("帮我做个查天气的工具", 0) is False


def test_empty_and_none():
    assert is_skill_creation_request("", OWNER) is False
    assert is_skill_creation_request(None, OWNER) is False


def test_verbal_executor_does_not_intercept():
    """`_try_verbal_request` 在路由之前执行；它必须对这些句子返回 chat，
    否则请求根本走不到 SkillDraft 分支。"""
    from anima.llm.request_executor import classify_request_intent

    for t in ("帮我做一个能查询今天塔罗牌的工具，加到你的能力里", "帮我做个查天气的工具"):
        assert classify_request_intent(t) == "chat"
