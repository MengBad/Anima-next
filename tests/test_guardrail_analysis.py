"""输出层「破角色分析体」护栏回归测试。

守护 2026-07-11 用户贴出的破角色回复：bot 在只被问『怎么说』时，被 world_model
的 GOP 信念 + 跨群记忆 + 历史 GOP 对话合力带偏，吐出一篇『从政治社会学和传播学的
角度来看…从实证科学的角度来说…』式分析长文。角色守卫已禁止此行为，但强 priming
下模型仍会跑偏，故在输出层做最后兜底拦截。
"""

import pytest

from anima.pipeline import PipelineContext
from anima.pipeline.stages import (
    guardrail_stage,
    _looks_like_analysis_essay,
    _looks_derailed,
    _looks_like_unprompted_fixation,
    _has_internal_tool_leak,
    _strip_speaker_labels,
)


# 用户贴出的真实破角色回复（已脱敏关键 CQ 码，结构完整）
REAL_BREAK_ROLE_REPLY = (
    "@[某用户]你是 MengWuy🍥，也就是那个在上海的群友。\n\n"
    "关于你问的 GOP 和那个群体的事，从政治社会学和传播学的角度来看，"
    "这其实是互联网政治模因的一种解构。保守主义政党在传统性别议题上的立场，"
    "跟亚文化符号放在一起会产生强烈的反差，网民就用这种反差来制造娱乐化的网络梗。\n\n"
    "从实证科学的角度来说，没有任何具备统计学信度的数据能支持这种说法。"
    "把一个庞大且复杂的选民群体做单一化的标签绑定是不合逻辑的，"
    "所以这只是网络话语泡沫，算不上什么客观事实。\n\n"
    "本喵觉得这就是个无厘头的网络解构，不用太当真 ( ﾟ∀ ﾟ)"
)

CASUAL_REPLY = "哈哈哈你这人也太逗了，接着聊呗～"
SHORT_ANGLE = "从这个角度来看你赢了"
SINGLE_KEYWORD_LONG = (
    "今天天气真好，我就在想从这个角度你肯定会喜欢的，"
    "所以我们出去玩吧，天气这么好不出去浪费了，真的。"
)


ACADEMIC_GENDER_ESSAY = (
    "根据官方的设定和多项细节线索，关于该角色的生理性别，在严谨的逻辑推导下存在以下两类主流解析："
    "一是“生理男性”的推论，该推论在逻辑上拥有多项间接证据支撑；"
    "二是“非二元性别”的艺术设定。综上所述，从生理线索推导大概率为生理男性。"
)


@pytest.mark.parametrize("text,expect", [
    (REAL_BREAK_ROLE_REPLY, True),
    (CASUAL_REPLY, False),
    (SHORT_ANGLE, False),
    (SINGLE_KEYWORD_LONG, False),
    (ACADEMIC_GENDER_ESSAY, True),
])
def test_looks_like_analysis_essay(text, expect):
    assert _looks_like_analysis_essay(text) is expect


def test_user_requested_analysis_is_not_hard_blocked_when_short():
    short = "本喵觉得大概是官方故意写得含糊啦，别太较真喵。"
    assert _looks_like_analysis_essay(short, user_text="帮我解析一下设定") is False


@pytest.mark.asyncio
async def test_guardrail_rewrites_analysis_essay():
    """分析体长文被改写为轻松接话，绝不输出分析体。"""
    ctx = PipelineContext(
        event={}, text="怎么说", user_id=888, group_id=222,
        detail_type="group", sender_name="Alice",
        meta={},  # 无 chat_id → 跳过记忆写入副作用
    )
    ctx.reply = REAL_BREAK_ROLE_REPLY
    await guardrail_stage(ctx)

    assert "政治社会学" not in ctx.reply, "分析体未被拦截！"
    assert "实证科学" not in ctx.reply, "分析体未被拦截！"
    assert "打住" in ctx.reply, "未改写为轻松接话"


@pytest.mark.asyncio
async def test_guardrail_passes_casual_reply():
    """正常接话不受影响。"""
    ctx = PipelineContext(
        event={}, text="哈哈", user_id=888, group_id=222,
        detail_type="group", sender_name="Alice",
        meta={},
    )
    ctx.reply = CASUAL_REPLY
    await guardrail_stage(ctx)
    assert ctx.reply == CASUAL_REPLY


@pytest.mark.asyncio
async def test_guardrail_strips_speaker_labels():
    ctx = PipelineContext(
        event={}, text="还活着吗", user_id=1, group_id=2,
        detail_type="group", sender_name="u", meta={},
    )
    ctx.reply = "[你]: 活得好好的呢喵！大半夜的突然冒出来问这个"
    await guardrail_stage(ctx)
    assert not ctx.reply.startswith("[你]")
    assert "活得好好的呢喵" in ctx.reply


def test_strip_speaker_labels_unit():
    assert _strip_speaker_labels("[哈基米]: 干嘛喵") == "干嘛喵"
    assert _strip_speaker_labels("正常接话") == "正常接话"
    # text: 前缀也能剥离
    assert _strip_speaker_labels("text:[哈基米]: 喵") == "喵"
    assert _strip_speaker_labels("text: [哈基米]: 喵") == "喵"
    # MengWuy 标签
    assert _strip_speaker_labels("[MengWuy]: 喵") == "喵"
    # 无标签不剥
    assert _strip_speaker_labels("本喵觉得不行") == "本喵觉得不行"


@pytest.mark.asyncio
async def test_guardrail_rewrites_internal_tool_leak():
    ctx = PipelineContext(
        event={}, text="看看内存占用", user_id=1, group_id=2,
        detail_type="group", sender_name="u", meta={},
    )
    ctx.reply = "系统提示不允许本喵看 root 目录，你自己去终端用 sudo 跑 free -h 吧。"

    await guardrail_stage(ctx)

    assert not _has_internal_tool_leak(ctx.reply)
    assert "运行数据" in ctx.reply


@pytest.mark.asyncio
async def test_guardrail_keeps_successful_runtime_status_data():
    ctx = PipelineContext(
        event={}, text="看看内存占用", user_id=1, group_id=2,
        detail_type="group", sender_name="u",
        meta={"tool_trace": [{
            "name": "get_runtime_status", "ok": True,
            "result": "当前运行状态（只读）：\n【内存】\nMem: 3.8Gi 1.0Gi\n【磁盘】\n/dev/sda1 45%",
        }]},
    )
    ctx.reply = "本喵用工具查到根目录占用 45%，系统提示说可以看。"

    await guardrail_stage(ctx)

    assert "本喵查到啦" in ctx.reply
    assert "Mem: 3.8Gi" in ctx.reply
    assert "没拿到完整" not in ctx.reply


@pytest.mark.asyncio
async def test_guardrail_rewrites_vague_derailment():
    """模糊输入 + 长文无问号 = 跑偏自语 → 改写为轻松接话。"""
    ctx = PipelineContext(
        event={}, text="怎么说", user_id=888, group_id=222,
        detail_type="group", sender_name="Alice", meta={},
    )
    ctx.reply = (
        "其实呢关于这个事情我觉得本质上是这样的，你平时喜欢聊的那些东西本喵都能接，"
        "总之就是一句话说不清楚，反正你懂的，这事儿吧要是从头捋的话能说上老半天，"
        "本喵也就是随口一说你别太往心里去，反正群里大家平时怎么聊本喵都记着呢，"
        "哪天你想正经聊了咱再慢慢掰扯，这会儿先这样吧回头再说，反正闲着也是闲着，"
        "你要是真想知道详情咱们改天单独聊，这会儿群里热热闹闹的先嗨着，本喵去瞄两眼别的了。"
    )
    await guardrail_stage(ctx)
    assert "你刚想说啥来着" in ctx.reply, "模糊输入跑偏未被拦截"
    assert "一句话说不清楚" not in ctx.reply, "原长篇未被改写"


@pytest.mark.asyncio
async def test_guardrail_passes_short_input_task_reply():
    """含任务动词的短输入 + 长回复 不被误拦。"""
    ctx = PipelineContext(
        event={}, text="讲个长故事", user_id=888, group_id=222,
        detail_type="group", sender_name="Alice", meta={},
    )
    ctx.reply = "从前有只猫，它每天都要巡逻领地，认识了很多朋友，有一天它遇到了另一只猫……"
    before = ctx.reply
    await guardrail_stage(ctx)
    assert ctx.reply == before


@pytest.mark.asyncio
async def test_guardrail_passes_short_casual_reply():
    """短输入的短回复 不触发。"""
    ctx = PipelineContext(
        event={}, text="在吗", user_id=888, group_id=222,
        detail_type="group", sender_name="Alice", meta={},
    )
    ctx.reply = "在，咋了"
    before = ctx.reply
    await guardrail_stage(ctx)
    assert ctx.reply == before


def test_looks_derailed_unit():
    class _C:
        def __init__(self, text, reply):
            self.text = text
            self.reply = reply

    # 模糊 + 长（>140 字）+ 无问号 → 跑偏
    assert _looks_derailed(_C("怎么说",
        "这事儿本质上就是这样的，本喵都记着呢，" * 20)) is True
    # 已经在反问/抛回 → 正确行为，放行
    assert _looks_derailed(_C("怎么说", "你刚想说啥来着？接着聊～")) is False
    # 含任务动词 → 放行
    assert _looks_derailed(_C("讲个笑话", "从前有只猫……（很长很长）")) is False
    # 短回复 → 放行
    assert _looks_derailed(_C("在吗", "在，咋了")) is False
    # 较长输入 → 放行
    assert _looks_derailed(_C("你觉得这件事应该怎么看呢", "长篇大论……")) is False


def test_unprompted_fixation_detects_old_gags():
    # 用户没提沙琪玛，却连环改名纠正
    assert _looks_like_unprompted_fixation(
        "I ate a dog",
        "本喵再说最后一遍，本喵叫哈基米，不叫沙琪玛喵！",
    )
    # 用户问运势，却塞小鱼干债务
    assert _looks_like_unprompted_fixation(
        "今日运势如何呀本喵",
        "你今天运势一般，先把欠本喵的一百个小鱼干交了喵！",
    )
    # 用户在聊别的，却硬拽朗姆酒
    assert _looks_like_unprompted_fixation(
        "煞笔",
        "本喵还是搜不到沙雕与朗姆酒的冰山视频喵！",
    )
    # 用户自己提到了 → 放行
    assert not _looks_like_unprompted_fixation(
        "沙琪玛是谁",
        "本喵叫哈基米，不叫沙琪玛喵！",
    )
    assert not _looks_like_unprompted_fixation(
        "吴语萌欠你小鱼干吗",
        "对啊，一百个小鱼干还没还喵！",
    )


@pytest.mark.asyncio
async def test_guardrail_rewrites_unprompted_fixation():
    ctx = PipelineContext(
        event={},
        text="I ate a dog",
        user_id=1,
        group_id=1,
        detail_type="group",
        sender_name="t",
    )
    ctx.reply = "本喵再说最后一遍，本喵叫哈基米，不叫沙琪玛喵！还有小鱼干呢！"
    await guardrail_stage(ctx)
    assert "不叫沙琪玛" not in (ctx.reply or "")
    assert "扯远" in (ctx.reply or "") or "重点" in (ctx.reply or "")
