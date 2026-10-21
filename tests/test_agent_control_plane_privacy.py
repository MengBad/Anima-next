"""Agent 控制面回执不得出现在群聊（09-28 人视角审计 P0-1）。

生产实证（群 727631526，09-26 19:16–19:17）：owner 在群里发起多步任务后，**全体
群成员**看到了两条内部回执——

* ``已创建 Agent 草稿 2157269a7b，回复「!anima agent approve 2157269a7b」后执行。``
* ``已启动 Agent 任务 2157269a7b``

任务 ID、``!anima`` 命令语法、草稿/审批两步流因此全部外泄。判据很直白：
**群里其他人能不能从这一条推断出它不是人** —— 能，就是 P0。

本文件锁死两条出口：

1. ``!anima agent …`` 在**群聊**里发起 ⇒ 回执只走私聊，群里**零出站**；
2. 多步任务自动路由的「草稿已创建」通知同理，**绝不发回群**。

注意区分：**产物交付不算在内**（``agent/owner_agent.py:_deliver_media`` 会把生图
发回发起会话）—— 那是 owner 要的结果本身，不是内部机制。
"""

from __future__ import annotations

import types
from unittest.mock import AsyncMock, MagicMock

import pytest

from anima.llm.handler import LLMHandler

OWNER = 10001
GROUP = 727631526


# ----------------------------------------------------------------------
# 夹具
# ----------------------------------------------------------------------

def _make_handler(with_agent: bool = True) -> LLMHandler:
    """最小 core：只满足 `!anima agent` 路由与自动路由分支所需。"""
    agent = None
    if with_agent:
        agent = MagicMock()
        agent.approve = AsyncMock(return_value="已启动 Agent 任务 t1")
        agent.cancel = MagicMock(return_value="已请求取消 Agent 任务 t1")
        agent.status_text = MagicMock(return_value="t1 [done] 整理一下资料")
        agent.submit = AsyncMock(return_value=types.SimpleNamespace(id="t9"))
        agent.store = MagicMock()
        agent.store.create = MagicMock(return_value=types.SimpleNamespace(id="t1"))

    core = types.SimpleNamespace()
    core.config = types.SimpleNamespace(
        permissions=types.SimpleNamespace(owners=[OWNER]),
        persistence=types.SimpleNamespace(data_dir="data"),
        compat=types.SimpleNamespace(astrbot_enabled=False),
        crossgroup=types.SimpleNamespace(enabled=False),
        autonomy=types.SimpleNamespace(enabled=True, reply_only_when_mentioned=True),
        sticker=types.SimpleNamespace(enabled=False, auto_collect=False,
                                      collect_min_interval=0, always_atmosphere=[]),
        conversation=types.SimpleNamespace(enabled=False),
    )
    core.gateway = MagicMock()
    core.gateway.send = AsyncMock()
    core.agent = agent
    core.agenda = MagicMock()
    core.agenda.pending = MagicMock(return_value=[])
    core.audit = None
    handler = LLMHandler(core)
    return handler


def _sends(handler: LLMHandler) -> list:
    return [c.args[0] for c in handler.core.gateway.send.await_args_list]


def _group_sends(handler: LLMHandler) -> list:
    return [m for m in _sends(handler) if getattr(m, "detail_type", None) == "group"]


# ----------------------------------------------------------------------
# 出口 1：`!anima agent` 命令
# ----------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("cmd", [
    "!anima agent list",
    "!anima agent approve t1",
    "!anima agent cancel t1",
    "!anima agent 帮我查一下今天的天气",
])
async def test_agent_command_in_group_never_writes_to_group(cmd):
    handler = _make_handler()
    consumed = await handler._handle_command({}, cmd, OWNER, GROUP, "group")

    assert consumed is True, "命令必须仍被消费（不能再落回正常对话管线）"
    assert _group_sends(handler) == [], f"{cmd} 的回执泄漏进了群聊"
    private = [m for m in _sends(handler) if m.detail_type == "private"]
    assert len(private) == 1, f"{cmd} 应恰好产生 1 条私聊回执，实际 {len(private)}"
    assert private[0].target_id == OWNER


@pytest.mark.asyncio
async def test_agent_command_in_group_keeps_no_command_trace():
    """群里不该留下任务 ID 或 `!anima agent approve` 语法。"""
    handler = _make_handler()
    await handler._handle_command({}, "!anima agent approve t1", OWNER, GROUP, "group")

    blob = " ".join(str(m.message) for m in _sends(handler))
    assert "t1" in blob, "私聊里必须拿到任务 ID（否则 owner 无法审批）"
    group_blob = " ".join(str(m.message) for m in _group_sends(handler))
    assert group_blob == "", f"群里不该有任何回执，实际：{group_blob!r}"


@pytest.mark.asyncio
@pytest.mark.parametrize("detail_type", ["private"])
async def test_agent_command_in_private_is_unchanged(detail_type):
    handler = _make_handler()
    await handler._handle_command({}, "!anima agent approve t1", OWNER, None, detail_type)

    sent = _sends(handler)
    assert len(sent) == 1
    assert sent[0].detail_type == "private"
    assert sent[0].target_id == OWNER
    assert "已启动 Agent 任务 t1" in sent[0].message


@pytest.mark.asyncio
async def test_non_agent_owner_commands_still_answer_in_group():
    """只有控制面命令改走私聊；普通命令的群内回执行为不得被顺带改掉。"""
    handler = _make_handler()
    handler.core.heartbeat = None
    await handler._handle_command({}, "!anima agenda", OWNER, GROUP, "group")

    sent = _sends(handler)
    assert len(sent) == 1
    assert sent[0].detail_type == "group"
    assert sent[0].target_id == GROUP


# ----------------------------------------------------------------------
# 出口 2：多步任务自动路由的「草稿已创建」通知
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_auto_routed_draft_notice_never_reaches_group():
    handler = _make_handler()
    # 隔离掉 handle() 前半段的旁支，只留自动路由分支
    handler._try_verbal_request = AsyncMock(return_value=None)
    handler._maybe_record_feedback = MagicMock(return_value=None)
    handler.core.group_presence = types.SimpleNamespace(
        observe=lambda *a, **k: types.SimpleNamespace(state="quiet"))

    event = {
        "text": "帮我查一下今天有哪些新番",
        "user_id": OWNER,
        "group_id": GROUP,
        "raw": {"message_type": "group", "sender": {"nickname": "owner"}},
    }
    await handler.handle(event)

    assert _group_sends(handler) == [], "任务草稿通知泄漏进了群聊"
    private = [m for m in _sends(handler) if m.detail_type == "private"]
    assert len(private) == 1
    assert private[0].target_id == OWNER
    # 🔴 2026-10-21：文案改为自然语言（owner 要求不用命令），但核心守护不变：
    #   ① 群里零痕迹（上一行已锁）；② 私聊回执**不得再要求 owner 回复命令**。
    msg = private[0].message
    assert "多步任务" in msg or "记下" in msg, f"回执应说明已记下任务：{msg!r}"
    assert "!anima agent approve" not in msg, (
        f"不得把命令语法塞回给 owner（owner 要求自然语言闭环）：{msg!r}"
    )


@pytest.mark.asyncio
async def test_control_notice_helper_is_always_private():
    handler = _make_handler()
    await handler._agent_control_notice(OWNER, "已创建 Agent 草稿 t1")
    sent = _sends(handler)
    assert len(sent) == 1
    assert sent[0].detail_type == "private"
    assert sent[0].target_id == OWNER
