"""Agent 交付不得把「过程」当「结果」发给用户（2026-10-27）。

## owner 的原话

> 为什么要把步骤给我看....

## 生产实证（2026-10-27 11:43–11:44，任务 8e6c68ab74）

用户在私聊里连续收到三条：

```
11:43:42  本喵自己去干个活喵：
          做什么：推进这个长期目标…（1562290139）
          任务：8e6c68ab74
11:44:00  [重规划 #1] 「分析主人近期的互动习惯与情感状态」未达成
          （verify_failed），改为 2 步替代方案
11:44:15  【步骤1】基于已有的零星话题和状态数据，直接构思一句简短、轻松且
                  带有生活气息的关怀话术
          主人的QQ号是1562290139。构思的关怀话术：「主人今天有好好吃饭嘛？…」
          【步骤2】向主人发送这条主动问候消息（1562290139）
          主人今天有好好吃饭嘛？别太辛苦啦，记得揉揉猫猫～
```

**真正的产物只有最后那一句问候。** 其余全是工程日志：
步骤标题、重规划事件、内部 QQ 号、元话语（「构思的关怀话术：…」）。

## 前两轮修的都是皮毛

- 2026-10-20（11a531c）：把步骤标题加进交付，理由「标题用于溯源」
  ⇒ 用户开始收工程日志。
- 2026-10-25（00ecb04）：只删「内容重复的步骤」，理由「步骤标题不能删，
  它是溯源锚点」⇒ 治的是「同一句话说三遍」，没治「凭什么给用户看步骤」。

溯源是**内部需求**，该走 `chat_memory` 的 `[已交付 …]` 记录，
不该让用户替我们承担。
"""
from __future__ import annotations

import asyncio

import pytest

from anima.agent.owner_agent import OwnerAgent


class _FakeGateway:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    async def send(self, msg):
        text = msg.message if isinstance(msg.message, str) else str(msg.message)
        self.sent.append((msg.detail_type, text))
        return "mid-1"


class _FakeStore:
    def __init__(self):
        self.saved = 0

    def save(self, task):
        self.saved += 1


def _agent(gateway, store=None):
    from types import SimpleNamespace
    a = OwnerAgent.__new__(OwnerAgent)      # 跳过 __init__（不碰真实 persistence）
    a.core = SimpleNamespace(gateway=gateway)
    a.store = store or _FakeStore()
    a.auditor = None
    return a


def _task(steps=(), context=None, status="completed", tid="8e6c68ab74"):
    from types import SimpleNamespace
    return SimpleNamespace(
        id=tid, status=status, creator=1562290139,
        steps=[SimpleNamespace(index=i, description=d, result=r, status="done")
               for i, d, r in steps],
        context=dict(context or {}),
        updated_at=0.0,
    )


def _joined(gw):
    return "\n".join(t for _d, t in gw.sent)


# ── 1. 复刻 11:44 那次真实事故 ──────────────────────────────────────

def test_real_incident_steps_are_not_delivered():
    """复刻 8e6c68ab74：出站只能是那句问候，不能有步骤标签/QQ号。"""
    gw = _FakeGateway()
    a = _agent(gw)
    task = _task(steps=[
        (0, "基于已有的零星话题和状态数据，直接构思一句简短、轻松且带有生活气息的关怀话术",
         "主人的QQ号是1562290139。构思的关怀话术："
         "「主人今天有好好吃饭嘛？别太辛苦啦，记得揉揉猫猫～」"),
        (1, "向主人发送这条主动问候消息（1562290139）",
         "主人今天有好好吃饭嘛？别太辛苦啦，记得揉揉猫猫～"),
    ])
    asyncio.run(a._deliver_text(task, ""))

    joined = _joined(gw)
    assert gw.sent, "完成的任务必须有交付"
    # 产物必须在
    assert "好好吃饭" in joined, "最终产物丢了"
    # 过程必须不在
    assert "【步骤" not in joined, "步骤脚手架又发给用户了"
    assert "1562290139" not in joined, "QQ 号泄漏（人格正本明令绝不报QQ号）"
    assert "基于已有的零星话题" not in joined, "步骤描述是内部脚手架"
    # 只交付一句话：不应把「构思过程 + 定稿」两段都发出去
    assert joined.count("好好吃饭") == 1, f"同一句话发了两遍：{joined!r}"


# ── 2. 净化函数本身 ─────────────────────────────────────────────────

@pytest.mark.parametrize("raw", [
    "【步骤1】分析\n主人的QQ号是1562290139。正文在这里。",
    "【步骤 2】编号带空格\n用户QQ号：123456789 内容",
    "【步骤1】\n内容\n\n\n\n尾巴",
])
def test_sanitize_removes_scaffold(raw):
    out = OwnerAgent._sanitize_delivery(raw)
    assert "【步骤" not in out
    assert "1562290139" not in out
    assert "\n\n\n" not in out


def test_sanitize_keeps_normal_text():
    """正常产物不得被误伤（数字/中文都保留）。

    🔴 反向守卫：首版 `_QQ_DIGITS_RE` 对**任何** 5–12 位数字打码，
    把「积分从 29100 涨到 29600」毁成「积分从 *** 涨到 ***」。
    那是把有效信息当噪声删掉 —— 比漏删一句脚手架更糟。
    现在只在**账号上下文**（QQ号/账号/绑定）里删数字。
    """
    for text in (
        "主人今天有好好吃饭嘛？别太辛苦啦，记得揉揉猫猫～",
        "积分从 29100 涨到 29600，签到成功",
        "我找到 3 条要点：成本、工期、风险",
        "方案A成本 120 元，方案B成本 180 元。",
    ):
        assert OwnerAgent._sanitize_delivery(text) == text, f"误伤：{text}"


def test_sanitize_removes_qq_in_context():
    """账号上下文里的数字必须删（这才是真正的泄漏面）。"""
    for raw in (
        "发给 1562290139 就好",
        "主人的QQ号是1562290139",
        "你的账号：123456789",
        "绑定的号码 987654321",
    ):
        out = OwnerAgent._sanitize_delivery(raw)
        assert "1562290139" not in out and "123456789" not in out \
            and "987654321" not in out, f"账号数字没删干净：{raw!r} -> {out!r}"


def test_sanitize_returns_empty_for_pure_scaffold():
    """全是脚手架 ⇒ 返回空串（调用方据此跳过发送）。"""
    assert OwnerAgent._sanitize_delivery("【步骤1】分析一下\n【步骤2】做一下") == ""


# ── 3. 多步真产物仍要保留 ────────────────────────────────────────────

def test_multiple_distinct_products_are_kept():
    """多步各有不同产物时都要交付（不能因为删标签而吞内容）。"""
    gw = _FakeGateway()
    a = _agent(gw)
    task = _task(steps=[
        (0, "统计成本", "方案A成本 120 元，方案B成本 180 元。"),
        (1, "给出结论", "综合来看选 A，省下 60 元且工期不变。"),
    ])
    asyncio.run(a._deliver_text(task, ""))
    joined = _joined(gw)
    assert "120 元" in joined and "省下 60 元" in joined, "多步产物被吞了"
    assert "【步骤" not in joined, "但仍不该带步骤标签"


def test_duplicate_steps_still_collapsed():
    """内容重复的步骤仍要去重（00ecb04 的行为不能丢）。"""
    gw = _FakeGateway()
    a = _agent(gw)
    same = "主人最近在忙什么呢？记得歇一歇喵~"
    task = _task(steps=[
        (0, "构思", same),
        (1, "再构思", same),
        (2, "定稿", same),
    ])
    asyncio.run(a._deliver_text(task, ""))
    joined = _joined(gw)
    assert joined.count("记得歇一歇") == 1, f"重复步骤没去重：{joined!r}"


# ── 4. 重规划通知必须受节流闸管 ──────────────────────────────────────

def test_replan_notify_is_throttled_for_short_task():
    """短任务的「重规划」是内部调度，不该单独发一条 QQ 消息。

    11:44 那次就是：2 步、十几秒的任务，先发一条 [重规划 #1]，
    再发产物 —— 用户被工程事件占据了一条消息。
    """
    from anima.agent.executor import AgentExecutor

    ex = AgentExecutor.__new__(AgentExecutor)
    ex.NOTIFY_STEPS_MIN_COUNT = 5
    ex.NOTIFY_STEPS_MIN_ELAPSED_SEC = 60.0
    short = _task(steps=[(0, "a", "x"), (1, "b", "y")])
    short.created_at = 0
    assert ex._should_notify_steps(short) is False, "短任务不该逐步推送"


def test_replan_is_not_pushed_at_all():
    """`_replan_or_give_up` **不再发任何消息**。

    历史：这个测试原来断言「`_notify` 调用前有 `_should_notify_steps` 闸门」。
    但闸门本身已被移除 —— 因为 owner 说「这些我都感觉不太好」，
    问题不是「重规划该不该被闸住」，而是**这条消息不该存在**：
    「换了个做法，改为 2 步」是内部调度，用户既不需要知道也无法参与。

    🔴 2026-10-29：断言改为「函数里根本没有 _notify 调用」。
    """
    import inspect
    from anima.agent.executor import AgentExecutor

    src = inspect.getsource(AgentExecutor._replan_or_give_up)
    code = [ln for ln in src.splitlines() if not ln.lstrip().startswith("#")]
    bad = [ln.strip() for ln in code if "await self._notify(" in ln]
    assert not bad, f"重规划不该再发消息，仍有：{bad}"

def test_current_step_metatalk_removed():
    """「当前步骤：…执行：…」只保留「执行：」后的真话。

    生产实证（b2860d152c 步1，10-26 00:01 用户实际收到）：
        当前步骤：如实告知用户目前小鱼干库存为零，无法接受贿赂，并幽默回应其傲娇语气。
        执行：哼，小鱼干库存为零，免谈！…
    """
    raw = ("当前步骤：如实告知用户目前小鱼干库存为零，无法接受贿赂，"
           "并幽默回应其傲娇语气。\n\n"
           "执行：哼，小鱼干库存为零，免谈！不过看在你这么大声的份上，说吧，到底有什么事？")
    out = OwnerAgent._sanitize_delivery(raw)
    assert "当前步骤" not in out, f"工作笔记没删：{out!r}"
    assert "执行：" not in out, f"「执行：」标签没删：{out!r}"
    assert "小鱼干库存为零" in out, "真话被吞了"


def test_next_step_plan_removed():
    """「下一步可直接将该图片…发送给用户」是对下游的计划，不是对用户说的话。

    生产实证（5c86a40cd0 步2，10-27 16:04 用户实际收到）：
        已成功调用生图工具生成了一张…爆笑梗图。下一步可直接将该图片及相应短句发送给用户。
    """
    raw = ("已成功调用生图工具生成了一张极具喜剧效果的搞笑猫咪图片，"
           "图片内容为一只表情滑稽、垮起个批脸的爆笑梗图。"
           "下一步可直接将该图片及相应短句发送给用户。")
    out = OwnerAgent._sanitize_delivery(raw)
    assert "下一步" not in out, f"下游计划没删：{out!r}"
    assert "发送给用户" not in out, f"下游计划没删干净：{out!r}"
    assert "搞笑猫咪图片" in out, "正文被吞了"


def test_metatalk_cleanup_does_not_eat_real_content():
    """清理元话语不得误伤正常叙述（含「下一步」但不是计划的句子）。"""
    # 「下一步」出现在正常对话里（用户视角的行动建议）仍应保留
    assert "下一步" in OwnerAgent._sanitize_delivery(
        "这道题的下一步是求导，我算给你看。")
    # 没有「执行：」时只删「当前步骤：」行
    out = OwnerAgent._sanitize_delivery("当前步骤：分析一下\n真正要给的话在这里。")
    assert "当前步骤" not in out and "真正要给的话" in out
