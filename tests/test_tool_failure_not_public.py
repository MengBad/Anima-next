"""r28 P1-2（RED）：工具故障不得在群里播报，改走 owner 私聊。

生产事实（2026-10-06 20:58:08 → 20:58:13）：

- ``react WARNING 工具调用超时(45s) name=x_search``
- 5 秒后 ``NapCat 发送 -> 群聊「推特那边卡死超时了，转了半天啥图都没吐出来……！」``

同型样本 10-04 15:15 还有一条「推特那边半天没给反应直接卡超时了，气死本喵了」。

根因：``react.py:609`` 把「工具执行超时（超过 45 秒），本次未获得结果」当普通
tool result 回注给模型，模型照着复述 ⇒ **内部故障进了公开群**。既有
``_has_internal_tool_leak`` 只认「工具 + 挂了/卡住」这种**带「工具」二字**的说法，
「推特那边卡死超时了」不含「工具」⇒ 漏网。

判据是**两个条件的合取**，两者缺一不可：
① 本回合确实有工具失败（``tool_trace`` 含 ``ok=False``）；
② 回复里出现超时/卡死一类播报词。

只看 ② 会误杀 —— 生产里就有「护食失败也太惨了吧」这种正常闲聊。
"""
from __future__ import annotations

import asyncio

import pytest

from anima.pipeline import PipelineContext
from anima.pipeline.stages import guardrail as gr

LEAK = "推特那边卡死超时了，转了半天啥图都没吐出来……！"
TRACE_FAIL = [
    {"name": "x_search", "args": {}, "result": "工具执行超时（超过 45 秒）", "ok": False, "round": 1},
]
TRACE_OK = [
    {"name": "x_search", "args": {}, "result": "图来了", "ok": True, "round": 1},
]


def _detector():
    fn = getattr(gr, "_narrates_tool_failure", None)
    assert fn is not None, "guardrail 缺少 _narrates_tool_failure ⇒ P1-2 未修"
    return fn


def test_leak_narration_detected():
    assert _detector()(LEAK, TRACE_FAIL) is True


def test_same_text_without_tool_failure_is_not_leak():
    """没有工具失败 ⇒ 同一句话也只是闲聊，绝不能误杀。"""
    assert _detector()(LEAK, TRACE_OK) is False
    assert _detector()(LEAK, None) is False
    assert _detector()(LEAK, []) is False


def test_tool_failure_but_no_narration_is_not_leak():
    """工具确实失败了，但回复没播报 ⇒ 正常放行（生产「护食失败也太惨了吧」同型）。"""
    assert _detector()("护食失败也太惨了吧，快去向她索赔一个更贵的明天补上喵！",
                       TRACE_FAIL) is False
    assert _detector()("沙奈朵的图给你扒来了，看看这只！", TRACE_FAIL) is False


def test_timeout_result_is_counted_as_failure():
    """🔴 判据锚必须真的存在：``tool_trace.ok`` 对超时必须是 **False**。

    ``_narrates_tool_failure`` 的条件①依赖 ``tool_trace`` 里的 ``ok=False``，
    而 ``ok`` 来自 ``tool_result_ok()``。实测 ``react.py:609`` 生成的超时串
    「工具执行超时（超过 45 秒），本次未获得结果」此前被判 **ok=True** ——
    它不含任何已登记的失败前缀。两个后果：

    ① 整条 P1-2 判据的条件①在生产**恒假** ⇒ 拦截永不触发（写了等于没写）；
    ② ``reply_quality.metrics["tool_failures"]`` 少计所有超时 ⇒ 工具健康度虚高。

    这正是「只读探针报 0 ⇒ 先证判据锚存在于代码里」那条纪律。
    """
    from anima.agent.react import _tool_result_ok

    assert _tool_result_ok("工具执行超时（超过 45 秒），本次未获得结果") is False


def _ctx(reply: str, trace, detail_type: str = "group") -> PipelineContext:
    return PipelineContext(
        event={"group_id": 727631526, "user_id": 1562290139},
        text="帮我搜点沙奈朵的图",
        user_id=1562290139,
        group_id=727631526,
        detail_type=detail_type,
        sender_name="MengWuy",
        reply=reply,
        meta={"tool_trace": trace, "trace": "t-leak"},
    )


def test_group_leak_is_suppressed_and_owner_notified(monkeypatch):
    async def _no_record(ctx, *a, **k):
        return None

    monkeypatch.setattr(gr, "_record_reply_memory", _no_record, raising=False)
    monkeypatch.setattr(gr, "_record_fallback", lambda *a, **k: None, raising=False)

    ctx = _ctx(LEAK, TRACE_FAIL, detail_type="group")
    asyncio.run(gr.guardrail_stage(ctx))

    assert "超时" not in ctx.reply and "卡死" not in ctx.reply, (
        f"工具故障仍在群里播报：{ctx.reply!r}"
    )
    notice = ctx.meta.get("owner_notice") or ""
    assert "x_search" in notice, f"owner 私聊通知没带上失败工具名：{notice!r}"


def test_preserves_persona_tail(monkeypatch):
    """只切故障句，后半句有性格的接话必须留下来（不整段替换）。

    生产原句：「推特那边卡死超时了，转了半天啥图都没吐出来……！雨萌你今天怎么
    这个点还在冒泡……本喵才不帮你们代购！」—— 后半段跟故障无关。
    """
    async def _no_record3(ctx, *a, **k):
        return None

    monkeypatch.setattr(gr, "_record_reply_memory", _no_record3, raising=False)
    monkeypatch.setattr(gr, "_record_fallback", lambda *a, **k: None, raising=False)

    full = (LEAK + "雨萌你今天怎么这个点还在冒泡，一上来就支使猫猫去搜这种奇奇怪怪的"
            "癖好图啊！要看让亦萱自己去翻啦，本喵才不帮你们代购！")
    ctx = _ctx(full, TRACE_FAIL, detail_type="group")
    asyncio.run(gr.guardrail_stage(ctx))

    assert "超时" not in ctx.reply and "卡死" not in ctx.reply
    assert "本喵才不帮你们代购" in ctx.reply, f"有性格的接话被一起抹掉了：{ctx.reply!r}"
    assert "x_search" in (ctx.meta.get("owner_notice") or "")


def test_private_chat_keeps_detail(monkeypatch):
    """私聊（owner）不做抑制 —— 对主人如实说明故障是有价值的。"""
    monkeypatch.setattr(gr, "_record_reply_memory", lambda ctx, *a, **k: None, raising=False)
    monkeypatch.setattr(gr, "_record_fallback", lambda *a, **k: None, raising=False)

    async def _no_record2(ctx, *a, **k):
        return None

    monkeypatch.setattr(gr, "_record_reply_memory", _no_record2, raising=False)
    ctx = _ctx(LEAK, TRACE_FAIL, detail_type="private")
    asyncio.run(gr.guardrail_stage(ctx))
    assert "卡死" in ctx.reply
    assert not ctx.meta.get("owner_notice")
