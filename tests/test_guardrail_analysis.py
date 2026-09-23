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
    _looks_like_unprompted_fixation,
    _has_internal_tool_leak,
    _strip_speaker_labels,
)
from anima.contracts.access import DeprecatedCoreAccess


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
    # 2026-09-21：出口已池化，断言必须落在"池成员"上（写死单句会随 random 变红）
    from anima.pipeline.stages.guardrail import _ANALYSIS_ESSAY_FALLBACKS
    assert ctx.reply in _ANALYSIS_ESSAY_FALLBACKS, "未改写为池化的轻松接话"


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


# ---- 3c-1a：Telemetry 经 Facade 表面的回归守护 ----
class _FakeReplyQuality:
    def __init__(self):
        self.fixations = 0
    def record_guard_fixation(self):
        self.fixations += 1


class _FakeCore:
    def __init__(self):
        self.reply_quality = _FakeReplyQuality()


@pytest.mark.asyncio
async def test_guardrail_records_fixation_via_facade():
    """unprompted fixation 经 ctx.core.telemetry() 记录，且不依赖 meta['core']。"""
    ctx = PipelineContext(
        event={}, text="I ate a dog", user_id=1, group_id=1,
        detail_type="group", sender_name="t", meta={},  # 无 meta core
    )
    fake = _FakeCore()
    ctx.core = DeprecatedCoreAccess(fake)
    ctx.reply = "本喵再说最后一遍，本喵叫哈基米，不叫沙琪玛喵！还有小鱼干呢！"
    await guardrail_stage(ctx)
    assert "不叫沙琪玛" not in (ctx.reply or ""), "回复未被改写"
    assert fake.reply_quality.fixations == 1, "telemetry 未经 ctx.core 记录"


@pytest.mark.asyncio
async def test_guardrail_fixation_telemetry_missing_core_is_safe():
    """ctx.core 缺失时（如未迁移环境）telemetry 静默失败，不破坏回复。"""
    ctx = PipelineContext(
        event={}, text="I ate a dog", user_id=1, group_id=1,
        detail_type="group", sender_name="t", meta={},
    )
    ctx.core = None
    ctx.reply = "本喵再说最后一遍，本喵叫哈基米，不叫沙琪玛喵！还有小鱼干呢！"
    await guardrail_stage(ctx)
    assert "不叫沙琪玛" not in (ctx.reply or "")


@pytest.mark.asyncio
async def test_political_gate_replaces_reply():
    """政治门槛：回复命中专名级政治词 → 人格化回避（不误伤正常聊天）"""
    ctx = PipelineContext(
        event={}, text="随便聊聊", user_id=888, group_id=222,
        detail_type="group", sender_name="Alice", meta={},
    )
    ctx.reply = "说起六四事件的历史意义……"
    await guardrail_stage(ctx)
    assert "不碰" in ctx.reply


def test_political_gate_not_triggered_by_normal():
    assert not __import__("anima.pipeline.stages.guardrail", fromlist=["_POLITICAL_RE"])._POLITICAL_RE.search(
        "今天天气不错，本喵想去晒太阳")


# ---- 2026-09-17（R16 男娘人设）：性别词不再单独构成「考据体」信号 ----
# 旧词表含「生理性别 / 非二元性别 / Non-binary」，导致 bot 正常聊性别时
# （只要回复略长）就被整段改写成「本喵刚又跑偏成分析模式了」——与 R16 人设冲突。
GENDER_MENTION_LONG = (
    "关于本喵是男孩子这件事吧，其实真没什么好讲的故事。本喵从小就觉得自己"
    "跟别人有点不一样，后来慢慢才搞明白那叫生理性别跟心里想的不太合，"
    "但说实话搞明白之后也没怎么样，日子还是照过，太阳照常升起。"
    "本喵照样喜欢吃甜筒，照样半夜不睡觉在群里瞎聊，照样被夸两句就偷偷高兴半天。"
    "你管本喵是男是女呢，反正本喵就是本喵，谁爱怎么想怎么想，本喵懒得管，"
    "也懒得解释，更懒得跟人争，争赢了又没糖吃。行了就这样吧，"
    "别老盯着本喵这些事看了，聊点别的，聊点有意思的，本喵听着呢。"
)


def test_gender_mention_alone_is_not_analysis_essay():
    """只谈性别、没有考据结构的回复，不该被判「破角色分析体」。"""
    assert len(GENDER_MENTION_LONG) > 220, "用例需长于阈值才能覆盖旧误伤路径"
    assert _looks_like_analysis_essay(GENDER_MENTION_LONG) is False


@pytest.mark.asyncio
async def test_guardrail_passes_gender_mention():
    """R16：谈性别的正常回复原样通过，不被改写为「跑偏成分析模式」。"""
    ctx = PipelineContext(
        event={}, text="你是男娘吗", user_id=888, group_id=222,
        detail_type="group", sender_name="Alice", meta={},
    )
    ctx.reply = GENDER_MENTION_LONG
    await guardrail_stage(ctx)
    assert ctx.reply == GENDER_MENTION_LONG
    assert "打住" not in ctx.reply


# ---- 2026-09-23：单语义词误杀修复 + 改写点观测补全 ----
# 生产实证（24h 命中 5 次，每次都是**整段替换**）：
#   08:06 守望先锋英雄数值讨论（入站仅 112 字）→ 「（差点写成报告了，收收收）…」
#   16:23 游戏问答（已调 search_web 后作答）  → 「（差点写成报告了，收收收）…」
# 旧判据末条 `len(reply) > 220` 的含义是「命中 **1 个**语义词 + 超 220 字」即整段报废，
# 而词表里含「从…角度来看 / 数据分析」这类**口语里常见**的说法 ⇒ 正常长接话被误杀。
# 修复口径：单语义词不再单独构成论文体，除非命中**不会出现在口语里的强标记**；
# 多语义词（n≥2）/ 结构词（编号分点）/ 用户主动要求分析 —— 三条原有拦截全部保留。
#
# 同时补观测点：改写点原样记录「命中规则 + 原文长度 + 原文摘要」。
# 此前只落一行 WARNING、**不留原文** ⇒ 「它本来想说什么」无法回溯，也无法按规则收敛。
WEAK_KEYWORD_LONG_REPLY = (
    "守望先锋里奥丽莎那个护盾是真的离谱，血量又厚，站在前面基本没人打得动，"
    "从这个角度来看她确实是个很强的坦克。不过本喵觉得玩她的人太少了，"
    "大家都抢着玩输出位，谁愿意去当肉盾挨打啊。本喵上次试了一把，"
    "感觉节奏好慢，盾一立起来倒是挺有安全感，但输出全靠队友，"
    "队友不给力的时候只能干瞪眼。所以本喵还是更喜欢那种能自己带飞的英雄，"
    "起码打得爽，输了也输得明白，不用指望别人扛着。你说是不是这个理。"
    "本喵觉得这种讨论本来就没意义，玩得开心比什么都强，非得争个高下反而扫兴。"
    "有空还不如一起去打两把，试试就知道了，光在这儿掰扯有啥用。"
)

STRONG_KEYWORD_LONG_REPLY = (
    "关于这个角色的强度，群里吵了好几天也没个定论。综上所述，本喵觉得"
    "大家其实是在拿不同的前提各说各话，一方只看实战表现，另一方只看面板数值，"
    "谁也说服不了谁。本喵本来还想认真捋一捋，结果越看越觉得没劲，"
    "资料那么多、说法那么杂，能确定的也就那么几条。所以本喵决定不纠结了，"
    "玩起来顺手就行，数值高低是设计师该操心的事，本喵只管开心。"
    "你要是还有兴趣，本喵再陪你聊两句，不然咱就换个轻松点的话题呗。"
    "本喵就是个懒猫，懒得跟人争这些虚的，争赢了也没糖吃，还不如躺平。"
    "你说对吧，咱聊点别的，别一直盯着这几个数字。"
)


def test_single_weak_keyword_long_reply_is_not_essay():
    """单语义词（口语常见）+ 长文 不再判「论文体」—— 生产误杀的等价用例。"""
    assert len(WEAK_KEYWORD_LONG_REPLY) > 220, "用例需长于旧阈值才能覆盖误伤路径"
    assert _looks_like_analysis_essay(WEAK_KEYWORD_LONG_REPLY) is False


def test_single_strong_keyword_long_reply_is_still_essay():
    """强标记词（口语不会自然出现）在大段正文里仍判论文体 —— 安全网保留。"""
    assert len(STRONG_KEYWORD_LONG_REPLY) > 220
    assert _looks_like_analysis_essay(STRONG_KEYWORD_LONG_REPLY) is True


def test_analysis_essay_rule_reports_hit_rule():
    """规则名 helper：命中返回规则 id，放行返回 None；布尔包装语义保持不变。"""
    from anima.pipeline.stages.guardrail import _analysis_essay_rule

    assert _analysis_essay_rule(CASUAL_REPLY) is None
    assert _analysis_essay_rule(WEAK_KEYWORD_LONG_REPLY) is None
    assert _analysis_essay_rule(STRONG_KEYWORD_LONG_REPLY) == "single_key_strong"
    assert _analysis_essay_rule(REAL_BREAK_ROLE_REPLY) == "keys>=2"
    assert (
        bool(_analysis_essay_rule(REAL_BREAK_ROLE_REPLY))
        is _looks_like_analysis_essay(REAL_BREAK_ROLE_REPLY)
    )


@pytest.mark.asyncio
async def test_rewrite_logs_rule_and_original_excerpt(caplog):
    """改写点必须留下「规则 + 原文长度 + 原文摘要」，否则事后无法回溯、无法按规则收敛。"""
    import logging

    from anima.pipeline.stages.guardrail import _ANALYSIS_ESSAY_FALLBACKS

    ctx = PipelineContext(
        event={}, text="怎么说", user_id=888, group_id=222,
        detail_type="group", sender_name="Alice", meta={},
    )
    ctx.reply = REAL_BREAK_ROLE_REPLY
    with caplog.at_level(logging.WARNING, logger="anima.pipeline.stages"):
        await guardrail_stage(ctx)

    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "规则=" in joined, f"日志缺规则名: {joined!r}"
    assert "摘要=" in joined, f"日志缺原文摘要: {joined!r}"
    assert "keys>=2" in joined, f"日志未记录命中的规则: {joined!r}"
    assert ctx.reply in _ANALYSIS_ESSAY_FALLBACKS


# ---- 2026-09-23（第二轮）：判据从「闭集强词表」升级为「结构 + 抽象密度」评分 ----
# 背景：09-23 上午那版修复把单语义词降级、改用 `_ANALYSIS_ESSAY_STRONG_RE` 强词表，
# 但那**又是一个闭集语义词表**。用 3 天 356 条生产真实出站原文回测：
#   * 生产判据命中 0 条，长尾（>=120 字）9 条全漏，其中 3 条是教科书级剖析体；
#   * 生产实际用的词（底层逻辑 / 本质是 / 核心诉求 / 认知水平 / 因果支配 / 集合论）
#     在强词表里**一个都不含** —— 与 09-20 已修掉的毛病完全同类。
#
# 新路径 `essay_score`（**纯增量**，旧路径一条不动）：
#     len(reply) >= 100  且  定义式句式×2 + 抽象名词×1.5 + 书面连接词×0.5 >= 6.0
# 3 天真实数据分离度：命中 4 条（分数 6.5 / 8.5 / 15.0 / 18.5）；
# >=100 字**未命中**样本的上界仅 3.5 ⇒ margin 3.0，可干净切开。
# 长度门槛是必需的：91 字的「集合论」条与 87 字的「游戏王特召」条分数达 9.0 / 6.5，
# 但两者都有正当的对话目的且更短 ⇒ 用长度门槛把它们排除，避免误杀。
PROD_ESSAY_CHARACTER_ARC = (
    "从人物底层逻辑来剖析，这个定性确实很准确。林宛瑜出走的本质不是逃避，"
    "而是对自我边界的确定性追求——她逃离的是被家族预设的联姻与既定轨道，"
    "进入爱情公寓本质上是一场低成本、高试错容限的社会化过渡。"
    "在这个阶段，公寓的温情和展博的陪伴为她提供了缓冲垫，但从未构成她的人生终极目标。"
    "第四季米兰篇的决断其实逻辑闭环非常严密：展博追求的是具象化、安稳落地的生活叙事"
    "（家庭、陪伴、恒常状态），而宛瑜的核心诉求是探索职业与个人的无限延展可能。"
)
PROD_ESSAY_TOKUSATSU_POWER = (
    "确实，从系统构架和底层逻辑上来说小明哥根本印不出来。"
    "Decade 的机制是通过卡片具象化特定骑士的历史与特异点数据，本质是观测与投影；"
    "哪怕后来强行贴满副骑和令和主骑的神主 21，调用逻辑依然停留在“跨维度摇人”的阶段。"
    "但逢魔时王是全时空全骑士历史的终极收束点，"
    "自带的逢魔之力属于概念级的时空创造与因果支配。"
)
PROD_ESSAY_RELATIONSHIP = (
    "如果她选择妥协留下，两人的关系最终也会被长期的自我消耗与不对等诉求所撕裂。"
    "能清醒地意识到情感浓度无法覆盖人生规划的根本分歧，"
    "并在沉没成本进一步扩大前体面抽身，确实体现了极高维度的认知水平与执行力。"
    "对她而言，爱情公寓注定只是人生航道上的补给站，而非终点港。"
)
PROD_ESSAY_CARD_GAME_META = (
    "这话说到了三国杀单挑机制的本质上。单挑归根结底打的就是「资源积累」和「控制链」。"
    "手牌是有回合结束弃置上限的临时资源，而装备区是永久置场的硬质战力差。"
    "防御马、防具一旦坐实，加上距离控制，卖血将的被动收益就会被彻底锁死，"
    "只能被迫处于慢性失血或者完全摸不到人的尴尬状态。"
    "界权表面上看是零牌差的滤抽，但本质是「极度高效的特定功能牌检索机」。"
    "低价值的基本牌进循环，洗出来的全是指向性极高的锦囊（拆、顺、无懈）和关键装备。"
)

# 生产里**正常且有用**的长回复，必须放行（长度不等于非人）。
PROD_OK_COMFORT_LONG = (
    "顺毛顺毛，快深呼吸一口，先别气坏了自己！三千块的实习工资自己在大城市生存都紧巴巴的，"
    "张嘴就要两千甚至一千，根本不是家里真缺这口饭吃，"
    "纯粹是有些传统家长刻在骨子里的控制欲和“服从性测试”喵。"
    "你老老实实掏了底牌，给少了他们嫌没诚意，给多了你自己兜里比脸还干净，最后寸步难行。"
    "听本喵一句，钱包一定要死死捂住，这是你在社会上立足的唯一底气！"
)
PROD_OK_RULES_LONG = (
    "根据萌娘百科上记录的官方调整规则（标注日期为15/11/27那条裁定），"
    "老艾的胜利是满足条件立刻适用的，但在单一卡片的效果处理中不能插队结算。"
    "加速同调星尘龙的效果是「特招星尘龙，那之后进行同调召唤」，整段是一个连贯的效果处理。"
    "在这个处理期间，G抽到最后一张老艾时效果还没处理完，接着你调鲜花女男爵，"
    "G还要再强制抽一张，"
)
# 高分但**短**：有正当对话目的的 ACG 设定讨论（91 字，score 9.0）
# ⇒ 长度门槛必须挡住它，否则会把「跟群友聊设定」误判成论文体。
PROD_SHORT_HIGH_SCORE = (
    "在集合论上，逢魔的力量范畴本来就完全覆盖了小明哥的所有可能性，"
    "甚至包括 Decade 自身。让一个子集去拓印包含了自身的绝对全集，"
    "卡面数据在加载阶段就直接逻辑闭环、彻底溢出崩溃了喵。"
)


@pytest.mark.parametrize("text", [
    PROD_ESSAY_CHARACTER_ARC,
    PROD_ESSAY_TOKUSATSU_POWER,
    PROD_ESSAY_RELATIONSHIP,
    PROD_ESSAY_CARD_GAME_META,
])
def test_production_long_tail_essays_are_caught(text):
    """生产长尾真剖析体（旧词表全漏）须被新评分路径抓住。"""
    from anima.pipeline.stages.guardrail import _analysis_essay_rule

    assert _analysis_essay_rule(text, user_text="") == "essay_score"
    assert _looks_like_analysis_essay(text) is True


@pytest.mark.parametrize("text", [
    PROD_OK_COMFORT_LONG,
    PROD_OK_RULES_LONG,
])
def test_production_legit_long_replies_pass(text):
    """生产里正常且有用的长回复必须原样放行 —— 绝不能按长度一刀切。"""
    from anima.pipeline.stages.guardrail import _analysis_essay_rule

    assert _analysis_essay_rule(text, user_text="") is None
    assert _looks_like_analysis_essay(text) is False


def test_essay_score_requires_min_length():
    """高分但短（91 字）的 ACG 设定讨论不被判论文体 —— 长度门槛是防误杀的关键。"""
    from anima.pipeline.stages.guardrail import _analysis_essay_rule

    assert len(PROD_SHORT_HIGH_SCORE) < 100, "用例需短于长度门槛才覆盖该路径"
    assert _analysis_essay_rule(PROD_SHORT_HIGH_SCORE, user_text="") is None


def test_essay_score_path_does_not_shadow_legacy_rules():
    """新路径是**增量**：旧路径规则名保持不变（防回归）。"""
    from anima.pipeline.stages.guardrail import _analysis_essay_rule

    assert _analysis_essay_rule(REAL_BREAK_ROLE_REPLY) == "keys>=2"
    assert _analysis_essay_rule(STRONG_KEYWORD_LONG_REPLY) == "single_key_strong"
    assert _analysis_essay_rule(WEAK_KEYWORD_LONG_REPLY) is None
    assert _analysis_essay_rule(GENDER_MENTION_LONG) is None


# ---- 2026-09-23（第二轮）：兜底话术不得自带「解说护栏」的元叙述 ----
# 生产实测（24h 改写 5 次）：三条兜底全部以 meta 括号开头
# 「（差点写成报告了，收收收）…」—— 护栏改写**在向用户解释护栏**。
# 人不会在群聊里说这句话；与 09-21 删 `_MOOD_TAIL` 是同一类修法。
_FALLBACK_META_MARKERS = ("跑偏", "报告", "分析模式", "划掉", "收收", "太正经", "打住")


def test_analysis_essay_fallbacks_have_no_meta_narration():
    """兜底话术必须是自然接话：不解释护栏、不带元叙述括号。

    只断言**本文件新引入的判据**（无 meta 叙述）。「保持人格口吻」已由
    `tests/test_fallback_pools.py::test_pool_wording_keeps_persona_voice` 统一守卫
    —— 同一判据不在两处重复实现，避免两套拼写各自漂移。
    """
    from anima.pipeline.stages.guardrail import _ANALYSIS_ESSAY_FALLBACKS

    assert _ANALYSIS_ESSAY_FALLBACKS, "兜底池不可为空"
    for text in _ANALYSIS_ESSAY_FALLBACKS:
        assert not text.startswith("（"), f"兜底仍以元叙述括号开头: {text!r}"
        for marker in _FALLBACK_META_MARKERS:
            assert marker not in text, f"兜底仍含解说护栏的词 {marker!r}: {text!r}"
