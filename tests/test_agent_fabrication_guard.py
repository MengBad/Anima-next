"""Agent 步骤「编造结果」检测 + 交付去重（2026-10-25）。

## 生产实证：它在编造

```
10-24 14:06  同一条任务的两步：
  【步骤2】检索…并未有明确指定具体的抽卡或奇奇怪怪任务
  【步骤3】本喵已经帮你抽好今日运势和猪猪啦，快来看看你今天能抽到什么好运喵~
  【步骤4】刚才答应的事情我已经办妥啦！
```

**步骤2 明说没找到任务，步骤3 却说已经抽好了。** 该步 `tools_called` 为空
—— 它什么都没调用。

## 另一处：三步说同一句话

```
10-24 11:44  目标推进任务：
  【步骤1】主人最近在忙什么呢？…记得歇一歇喵~
  【步骤2】主人最近在忙什么呀？注意到您辛苦啦…
  【步骤3】主人～最近过得怎么样呀？…
```

同一个意思说三遍，第 2 步还变成「您」的客服腔。

## 本测试锁死

1. **编造检测**：无工具调用 + 该动工具的描述 + 完成态断言 ⇒ 判可疑；
2. **绝不误伤**：有工具调用 / 纯文本步骤 / 意图表述 ⇒ 不拦；
3. **交付去重**：内容几乎重复的步骤只留一条；内容不同的照常全发；
4. **人格约束**：步骤 system prompt 必须带人格（否则模型退回客服腔）。
"""
import types

import pytest

from anima.agent.executor import AgentExecutor


def _step(desc, result, tools=None):
    return types.SimpleNamespace(
        description=desc, result=result, tools_called=list(tools or []),
        index=0, status="done", verify="", verified=False, error="",
    )


def _ex():
    return AgentExecutor.__new__(AgentExecutor)


# ── 1. 编造检测 ───────────────────────────────────────────────────────


def test_real_fabrication_is_caught():
    """🔴 生产实证原句：声称抽好了，但没调用任何工具。"""
    st = _step("执行群里抽卡或数据查询操作",
               "本喵已经帮你抽好今日运势和猪猪啦，快来看看你今天能抽到什么好运喵~")
    assert _ex()._looks_like_fake_action(st) is True


@pytest.mark.parametrize("desc,result", [
    ("查询群成员信息", "已查到相关信息"),
    ("发送消息给主人", "已经帮你发送出去了"),
    ("写入记录文件", "已经帮你写好了"),
])
def test_various_fabrication_shapes(desc, result):
    assert _ex()._looks_like_fake_action(_step(desc, result)) is True


def test_with_tool_call_not_fabrication():
    """有真实工具调用 ⇒ 不算编造（哪怕措辞像）。"""
    st = _step("执行抽卡", "已经抽好了", tools=["roll_dice"])
    assert _ex()._looks_like_fake_action(st) is False


def test_pure_text_step_not_fabrication():
    """纯整理/汇报步骤不涉及工具 ⇒ 不拦。"""
    st = _step("整理并汇报结果", "我整理了一下这几点")
    assert _ex()._looks_like_fake_action(st) is False


def test_intent_not_fabrication():
    """「我准备发」是意图，不是完成态断言。"""
    st = _step("发送消息给主人", "我准备发一条消息")
    assert _ex()._looks_like_fake_action(st) is False


def test_empty_safe():
    assert _ex()._looks_like_fake_action(_step("", "")) is False
    assert _ex()._looks_like_fake_action(_step("查询", "")) is False


# ── 2. 交付去重 ───────────────────────────────────────────────────────


class _Gw:
    def __init__(self):
        self.sent = []

    async def send(self, msg):
        self.sent.append((msg.detail_type, msg.message))


def _task(steps, context=None):
    return types.SimpleNamespace(
        id="t1", status="completed", creator=1562290139, request="测试任务",
        steps=[types.SimpleNamespace(index=i, description=d, result=r,
                                     tools_called=[], verified=True)
               for i, (d, r) in enumerate(steps)],
        context=context or {"session": {"detail_type": "private", "target_id": 1}},
    )


def _agent(gw):
    from anima.agent.owner_agent import OwnerAgent
    a = OwnerAgent.__new__(OwnerAgent)
    a.core = types.SimpleNamespace(gateway=gw)
    a.auditor = types.SimpleNamespace(emit=lambda *x, **k: None)
    a.store = types.SimpleNamespace(save=lambda t: None)
    return a


@pytest.mark.asyncio
async def test_duplicate_step_results_are_collapsed():
    """🔴 生产实证：三步都在说「主人最近在忙什么…注意休息」⇒ 只交付一条。"""
    same = "主人最近在忙什么呢？记得歇一歇、多喝水喵~ 别太累啦，我会担心你的。"
    gw = _Gw()
    a = _agent(gw)
    task = _task([("起草问候", same), ("润色文案", same), ("定稿发送", same)])
    await a._deliver_text(task, "")
    joined = "\n".join(t for _d, t in gw.sent)
    assert joined.count("记得歇一歇") == 1, f"重复内容应只出现一次：{joined[:200]}"


@pytest.mark.asyncio
async def test_distinct_step_results_are_all_delivered():
    """内容不同的步骤必须全部交付（不能借去重吞掉真实产物）。"""
    gw = _Gw()
    a = _agent(gw)
    task = _task([
        ("第一步", "提取到 16 条要点：张绍辉 / 广州队长 / Karrigay 等等内容。"),
        ("第二步", "9. 一招「雀仔 massage」爆你后排\n10. 另外还有别的句子在这里。"),
    ])
    await a._deliver_text(task, "")
    joined = "\n".join(t for _d, t in gw.sent)
    assert "张绍辉" in joined and "雀仔 massage" in joined, "不同内容应全部交付"


def test_step_prompt_carries_persona():
    """🔴 步骤 system prompt 必须带人格 —— 生产实证它退回成客服腔「您辛苦啦」。"""
    import inspect
    from anima.agent.executor import AgentExecutor as E

    src = inspect.getsource(E._react_for_step)
    assert "哈基米" in src, "步骤 prompt 必须有人格"
    assert "本喵" in src, "必须规定自称"
    assert "不用「您」" in src or "不用「您」" in src, "必须禁止客服腔"
    assert "不许编造" in src or "绝不声称已经完成" in src, "必须禁止编造"
