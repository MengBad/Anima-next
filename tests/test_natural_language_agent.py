"""自然语言闭环契约（2026-10-21）。

## 起因

owner 原话：「能不能不用命令 !anima 而是用自然语言」。

审计后发现：自然语言**本来就能触发** Agent 多步任务（`handler.py` 的
`is_agent_task` 路由），但只建「草稿」就停住，且回执要求 owner
**回复命令** `!anima agent approve <id>` 才执行 —— 等于把用户又推回命令。

## 本改动

1. **私聊**下 owner 用自然语言提多步任务 ⇒ **直接执行**（auto_run），
   回执用自然语言，**不出现任何命令语法**；
2. **群聊保持原策略**（只建草稿）—— 09-28 人视角审计 P0-1 的判据是
   「群里其他人能不能据这条推断出它不是人」，群里出现草稿 ID 就是破绽；
3. 新增自然语言**叫停**（`stop` 意图）—— 既然启动不用命令，叫停也不该用。

## 本测试锁死的边界

1. stop 意图能识别明确叫停；**不误伤**「停下来歇会」类闲聊
   （实测踩过：「今天好累想停下来歇会」曾因含「停下」被误判）；
2. `_stop` 只取消**本人**在跑的任务，不碰别人的；
3. 没有在跑任务 ⇒ 如实说没有，**不假装取消成功**；
4. 无 agent / 列表读取失败 ⇒ 不抛异常、不谎报。
"""
import types

import pytest

from anima.llm.request_executor import (
    classify_request_intent, RequestExecutor,
)


# ── 1. 意图识别 ───────────────────────────────────────────────────────


@pytest.mark.parametrize("text", [
    "别做了", "算了别做了", "先别做这个了", "取消任务", "停手", "别干了",
])
def test_stop_intent_recognized(text):
    assert classify_request_intent(text) == "stop", f"{text!r} 应识别为叫停"


@pytest.mark.parametrize("text", [
    "今天好累想停下来歇会",   # ← 实测踩过的误伤样本
    "你先休息一下吧",
    "今天天气不错",
    "帮我看看这个",
])
def test_stop_intent_no_false_positive(text):
    """🔴 裸「停下」不得作为叫停词 —— 会把闲聊误判成取消任务。"""
    assert classify_request_intent(text) != "stop", f"{text!r} 误判成叫停"


# ── 2. _stop 行为 ────────────────────────────────────────────────────


class _Task:
    def __init__(self, tid, creator, status="running"):
        self.id = tid
        self.creator = creator
        self.status = status


class _Agent:
    def __init__(self, tasks):
        self.store = types.SimpleNamespace(list=lambda: list(tasks))
        self.cancelled = []

    def cancel(self, tid):
        self.cancelled.append(tid)
        return f"已取消 {tid}"


def _exec(tasks, agent=True):
    core = types.SimpleNamespace(
        agent=_Agent(tasks) if agent else None,
        config=types.SimpleNamespace(persistence=types.SimpleNamespace(data_dir="data")),
    )
    ex = RequestExecutor.__new__(RequestExecutor)
    ex.core = core
    return ex


async def test_stop_cancels_only_own_running_tasks():
    tasks = [
        _Task("mine1", 42, "running"),
        _Task("mine2", 42, "executing"),
        _Task("other", 99, "running"),      # 别人的
        _Task("done", 42, "completed"),     # 已完成的不该动
    ]
    ex = _exec(tasks)

    reply = await ex._stop(42)

    assert ex.core.agent.cancelled == ["mine1", "mine2"], (
        f"只应取消本人且在跑的任务，实得 {ex.core.agent.cancelled}"
    )
    assert "停下" in reply or "停" in reply
    assert "other" not in ex.core.agent.cancelled, "不得取消别人的任务"
    assert "done" not in ex.core.agent.cancelled, "不得取消已完成任务"


async def test_stop_with_nothing_running_is_honest():
    ex = _exec([_Task("x", 42, "completed")])
    reply = await ex._stop(42)
    assert "没有在办" in reply, f"没有在跑任务时应如实说，实得 {reply!r}"
    assert ex.core.agent.cancelled == []


async def test_stop_without_agent_does_not_raise():
    ex = _exec([], agent=False)
    reply = await ex._stop(42)      # 不应抛出
    assert "没有在办" in reply


async def test_stop_never_lies_when_cancel_fails():
    """取消抛异常 ⇒ 绝不谎报「已停下」。"""
    class _BadAgent:
        store = types.SimpleNamespace(list=lambda: [_Task("a", 42)])

        def cancel(self, tid):
            raise RuntimeError("取消失败")

    core = types.SimpleNamespace(
        agent=_BadAgent(),
        config=types.SimpleNamespace(persistence=types.SimpleNamespace(data_dir="data")),
    )
    ex = RequestExecutor.__new__(RequestExecutor)
    ex.core = core

    reply = await ex._stop(42)
    assert "没停下来" in reply, f"取消失败不得谎报成功，实得 {reply!r}"


# ── 3. 私聊自然语言直接执行（handler 行为契约）─────────────────────────


def test_private_natural_language_runs_without_command():
    """源码级契约：私聊分支必须调 submit(auto_run=True)，且回执不含命令语法。"""
    import inspect
    from anima.llm import handler as h

    src = inspect.getsource(h)
    i = src.find("is_agent_task(text, 4)")
    assert i > 0, "应存在 is_agent_task 路由"
    seg = src[i:i + 2600]
    assert 'detail_type == "private"' in seg, "应区分私聊与群聊"
    assert "auto_run=True" in seg, "私聊应直接执行（auto_run），不再等命令批准"
    # 私聊回执不得把 approve 命令塞回给用户
    _private_seg = seg.split("群聊路径")[0]
    assert "!anima agent approve" not in _private_seg, (
        "私聊回执不得出现命令语法（owner 要求自然语言闭环）"
    )
