"""Agent 推送节奏契约（2026-10-23）。

## 起因（生产实测：23 秒任务连推 6 条）

```
07:55:38  本喵自己去干个活喵：做什么…为什么…任务：578f8b7f10
07:55:40  [1/3] 调用 read_group_history 读取群…
07:55:46  [2/3] 提取消息中的关键讨论主题…
07:55:53  [3/3] 将群动态总结写入…
07:56:01  [完成] 任务已全部执行
07:56:01  【步骤1】…【步骤2】…【步骤3】…（产物全文）
```

**一个 23 秒的任务，私聊里连推 6 条。** 这不是「主动交付」，这是刷屏。

而这个代价很实在：**一个每 23 秒发 6 条消息的助理，owner 会直接关掉通知**
—— 一旦关掉，前面做的所有「主动交付」（owner 私聊交付 / 报备 / 补丁审批）
价值全部归零。**可用性问题比功能缺失更紧急。**

## 本测试锁死的边界

1. 短任务（步数少、耗时短）⇒ **不逐步推送**；
2. 长任务（步数 ≥5 或耗时 ≥60s）⇒ 照常逐步推送；
3. 判定异常 ⇒ **倾向于不推**（宁可少一条，不要因判据崩了而刷屏）；
4. 跑完但没有可展示产出 ⇒ 仍发一句完成（否则用户不知道结束了）；
5. 有产出 ⇒ 不再单独发 `[完成]`（产物交付本身即完成信号）。
"""
import time
import types

import pytest

from anima.agent.executor import AgentExecutor


def _ex():
    ex = AgentExecutor.__new__(AgentExecutor)
    ex.NOTIFY_STEPS_MIN_COUNT = 5
    ex.NOTIFY_STEPS_MIN_ELAPSED_SEC = 60.0
    return ex


def _task(steps=3, age=0.0):
    return types.SimpleNamespace(
        steps=[types.SimpleNamespace(index=i) for i in range(steps)],
        created_at=time.time() - age,
    )


# ── 1. 短任务静默 ─────────────────────────────────────────────────────


@pytest.mark.parametrize("steps", [1, 2, 3, 4])
def test_short_task_does_not_notify_steps(steps):
    """🔴 生产实证：3 步 23 秒的任务连推 6 条 ⇒ 必须静默。"""
    assert _ex()._should_notify_steps(_task(steps=steps, age=5)) is False


# ── 2. 长任务照常推送 ─────────────────────────────────────────────────


def test_many_steps_notifies():
    assert _ex()._should_notify_steps(_task(steps=6, age=1)) is True


def test_long_elapsed_notifies_even_if_few_steps():
    """跑着跑着变长了（已耗时 ≥60s）⇒ 也该让 owner 知道在推进。"""
    assert _ex()._should_notify_steps(_task(steps=2, age=90)) is True


# ── 3. 异常安全：倾向不推 ─────────────────────────────────────────────


def test_missing_steps_is_safe():
    t = types.SimpleNamespace(steps=None, created_at=0)
    assert _ex()._should_notify_steps(t) is False


def test_garbage_task_does_not_raise():
    class _Bad:
        @property
        def steps(self):
            raise RuntimeError("boom")

        created_at = 0
    assert _ex()._should_notify_steps(_Bad()) is False


# ── 4. 完成消息不重复 ─────────────────────────────────────────────────


def test_no_completion_notice_when_output_exists():
    """有产出 ⇒ 产物交付即完成信号，不再单独发 `[完成]`。

    生产实证序列：… [3/3] → [完成] → 产物全文
    中间那条 `[完成]` 纯属噪音。
    """
    import inspect
    from anima.agent import executor as ex_mod

    src = inspect.getsource(ex_mod.AgentExecutor.execute)
    assert "_has_output" in src, "应有产出判定"
    assert "没有可展示的产出" in src, "无产出时应如实说明"


def test_completion_notice_still_sent_without_output():
    """跑完但什么都没产出 ⇒ 必须发一句，否则用户不知道结束了。"""
    import inspect
    from anima.agent import executor as ex_mod

    src = inspect.getsource(ex_mod.AgentExecutor.execute)
    i = src.find("if task.status == \"completed\"")
    assert i > 0
    seg = src[i:i + 800]
    assert "[完成]" in seg, "无产出分支仍应发完成提示"
