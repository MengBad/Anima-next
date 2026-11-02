"""依据类任务的路由与 pending 清理（2026-11-02 生产实证）。

## 实证 1：依据句式没进 agent

  owner 私聊：「根据这个文件https://.../SKILL.md，执行华为云获取积分的任务」
  日志：ReAct text_len=124 tools=69 → **零工具调用** → 只回一句反问

  根因：TASK_TRIGGER_RE 要求**开头就是动作词**（帮我/请/去…），
  这句以「根据」开头 ⇒ 判为普通聊天 ⇒ 任务从未执行。
  修法：新增 _DOC_DRIVEN_RE，依据引导词 + 动作词独立成判据。

## 实证 2：pending 清理被挂在「新反问」下面

  guardrail 里是 `if _why: clear_stale(...)` —— 只有又产生一次反问才清理。
  结果 10-31 17:54 挂的任务活到 11-02 19:54，中间两次对话被它劫持：
    11-02 11:20 你：suzume        -> 它答「按现在就签到来」
    11-02 19:54 你：华为云任务...  -> 又挂到同一个 2 天前的 pending
  修法：挪到 handler 每条私聊入口，无条件执行。
"""
from __future__ import annotations

import pathlib

import pytest

from anima.agent.intent import is_agent_task

HANDLER = (pathlib.Path(__file__).resolve().parent.parent
           / "anima" / "llm" / "handler.py")

#: 生产原文（含 URL 与中文逗号）
REAL_HUAWEI = (
    "根据这个文件https://szb123.com/files/huawei-cloud-grow-daily-SKILL.md，"
    "执行华为云获取积分的任务"
)


@pytest.mark.parametrize("text", [
    REAL_HUAWEI,
    "按照文档做一下",
    "依据这份说明处理一下",
    "参照这份材料完成它",
])
def test_doc_driven_goes_to_agent(text):
    assert is_agent_task(text, 4) is True, f"依据类任务没进 agent: {text}"


@pytest.mark.parametrize("text", [
    "今天天气不错",
    "你是谁",
    "我觉得这个按照道理来说不错",
    "帮我去推特上找一个画师的联系方式",
])
def test_normal_chat_not_hijacked(text):
    assert is_agent_task(text, 4) is False, f"闲聊被误判成任务: {text}"


def test_doc_driven_requires_owner():
    """非 owner 不该进入 agent 路径。"""
    assert is_agent_task(REAL_HUAWEI, 2) is False


def test_clear_stale_is_unconditional():
    """clear_stale 必须在私聊入口无条件调用，不能挂在 `if _why` 之下。"""
    src = HANDLER.read_text(encoding="utf-8")
    idx = src.find("_au.clear_stale(self.core)")
    assert idx > 0, "handler 里没有无条件 clear_stale"
    # 它必须出现在 resolve_reply 之前（先清理再消费）
    idx_resolve = src.find("_au.resolve_reply(self.core, text)")
    assert idx < idx_resolve, "clear_stale 必须在 resolve_reply 之前"


def test_stale_pending_does_not_hijack():
    """2 天前的 pending 必须被 TTL 清掉，不能劫持新对话。"""
    from types import SimpleNamespace
    from anima.agent import ask_user as au
    from anima.agent.ask_backstop import build_pending_question

    core = SimpleNamespace(agent=SimpleNamespace(store=SimpleNamespace(
        items={}, save=lambda t: None)))
    core._active_agent_task = None
    task = au.attach_pending(core, build_pending_question("你要找哪位画师呀？"))
    # 模拟 2 天后
    import time
    task.pending_question["_attached_at"] = time.time() - 2 * 86400
    assert au.clear_stale(core) is True
    assert core._active_agent_task is None
    # 之后的新消息不该被当成回答
    assert au.resolve_reply(core, "根据这个文件执行任务") is None
