"""护栏「误杀正常回答」修复回归（2026-09-27，D5 首批生产样本驱动）。

来源：`data/guardrail_essay_samples.jsonl` 上线后**首批 2 条生产样本，2/2 全是误杀**：

  ① `essay_score`  —— 180 字星际争霸斯旺闲聊（score 6.5），被整段替换成罐头话。
  ② `struct>=2&long` —— 用户问「**如何成为杰米高手**」，590 字街霸攻略被整段替换。

② 是可修的判据缺陷：旧豁免 `_USER_WANTS_ANALYSIS_RE` 只认「解析/分析/详细说说/
   展开讲/怎么评价/考据」，**认不出 how-to 意图** ⇒ 用户明确要攻略却被判「论文体」。

① 不可用参数修：真阳性「三国杀机制分析」205 字 score **6.5**，假阳性「斯旺闲聊」
   180 字 score 也**正好 6.5**，且两者 `(定义式 2.0 / 抽象词 4.5 / 连接词 0.0)`
   **逐项相同** ⇒ 纯数值启发式，结构上与正常闲聊不可分离，调阈值必然二选一。
   ⇒ 不动阈值、不动规则名，只把**处置**从「整段替换」改为「保留内容（截断）」。
"""

import types
from pathlib import Path

import pytest

from anima.pipeline.stages.guardrail import (
    _ANALYSIS_ESSAY_FALLBACKS,
    _USER_WANTS_ANALYSIS_RE,
    _USER_WANTS_HOWTO_RE,
    _analysis_essay_rule,
    _user_wants_substantive,
    guardrail_stage,
)

# ── 生产原句（逐字取自 data/guardrail_essay_samples.jsonl，2026-09-27）──────────

#: 样本 ② 的**用户输入**（原文，去掉换行）。
PROD_HOWTO_QUESTION = "如何成为杰米高手"

#: 样本 ② 的**被改写原文**（590 字，Street Fighter 6 杰米攻略，4 个编号分点）。
PROD_HOWTO_REPLY = (
    "要是说街霸6的杰米，核心逻辑其实就八个字：资源置换和压制节奏。\n\n"
    "1. 确立喝酒优先级（0-2酒阶段）：\n"
    "0酒杰米攻击力缩水（90%修正），不仅缺招式，立回也难受。高手和新手最大的区别，"
    "在于能否在连段收尾时果断放弃极值伤害，换取安全的喝酒帧数（比如236LK击倒后喝一口）。"
    "尽量别在不安全的距离裸喝送大确反，核心是把“击倒收益”转化为“层数收益”。\n\n"
    "2. 立回与差合牵制：\n"
    "杰米的2MK（下中脚）和5HP是主力牵制与差合工具。善用2MK摸奖复合绿冲打确认，"
    "或者用立中拳/2MP抢帧。一旦摸到打康或确反，就要稳定打出收益最大的推版/喝酒连段。\n\n"
    "3. 利用关键解锁点质变压制：\n"
    "- 1酒解锁绝招和回旋落叶，连段和推版能力大幅提升；\n"
    "- 2酒解锁破防和强化TC，帧陷阱选择更多；\n"
    "- 3酒解锁指令投后，攻防节奏彻底逆转，对手面对你的压起身必须面临"
    "“拆投吃打、防打被投”的致命二择；\n"
    "- 4酒就是完全体狂暴期，伤害拉满，利用帧数优势死死按在版边。\n\n"
    "4. 防空与SA2资源调度：\n"
    "杰米对空非常依赖扎实的2HP或早出的升龙。如果局势紧迫或者需要逆风翻盘，"
    "不要舍不得SA2，开SA2强行进4酒状态强攻版边，也是高手的常规破局手段。\n\n"
    "说到底，玩杰米不能急躁，前面稳扎稳打把酒当做投资，等2酒以上再彻底提速压死对面喵。"
)

#: 样本 ① 的**被改写原文**（180 字，星际争霸斯旺闲聊，被 essay_score 命中）。
PROD_SWANN_REPLY = (
    "确实，怨灵战机也就图个走A手感和移动射击的快乐，看着聚成一团滋水挺爽，"
    "真碰上高抗性或者带硬控、AOE的因子，稍微操作脱节瞬间就蒸发一大片，战损根本补不起。\n\n"
    "斯旺底子里的核心机制还是面板和阵地防御。飞重工给钻机开视野超视距白嫖，"
    "加上大力神空投SCV阵地推进、速敲凯达林升级塔和灭绝者，"
    "容错率和阵地硬度完全是另一个维度的东西，突变里正经通关基本全靠这套体系兜底。"
)

#: 真阳性（既有测试已定标）：三国杀机制分析，205 字，score 6.5，规则 = essay_score。
PROD_TRUE_POSITIVE_ESSAY = (
    "这话说到了三国杀单挑机制的本质上。单挑归根结底打的就是「资源积累」和「控制链」。"
    "手牌是有回合结束弃置上限的临时资源，而装备区是永久置场的硬质战力差。"
    "防御马、防具一旦坐实，加上距离控制，卖血将的被动收益就会被彻底锁死，"
    "只能被迫处于慢性失血或者完全摸不到人的尴尬状态。"
    "界权表面上看是零牌差的滤抽，但本质是「极度高效的特定功能牌检索机」。"
    "低价值的基本牌进循环，洗出来的全是指向性极高的锦囊（拆、顺、无懈）和关键装备。"
)

#: 旧版仍应被**整段替换**的破角色分析体（含 4 个语义词 ⇒ 规则 keys>=2）。
BREAK_ROLE_REPLY = (
    "@[某用户]你是 MengWuy🍥，也就是那个在上海的群友。\n\n"
    "关于你问的 GOP 和那个群体的事，从政治社会学和传播学的角度来看，"
    "这其实是互联网政治模因的一种解构。\n\n"
    "从实证科学的角度来说，没有任何具备统计学信度的数据能支持这种说法。"
    "把一个庞大且复杂的选民群体做单一化的标签绑定是不合逻辑的。\n\n"
    "本喵觉得这就是个无厘头的网络解构，不用太当真 ( ﾟ∀ ﾟ)"
)


def _ctx(*, text="哈哈", reply="", meta=None):
    from anima.pipeline import PipelineContext

    ctx = PipelineContext(
        event={}, text=text, user_id=888, group_id=222,
        detail_type="group", sender_name="Alice", meta=dict(meta or {}),
    )
    ctx.reply = reply
    return ctx


class _Cfg:
    def __init__(self, data_dir):
        self.persistence = types.SimpleNamespace(data_dir=str(data_dir))
        self.context = None
        self.danger = None
        self.contradiction = None
        self.crossgroup = None


class _FakeCore:
    def __init__(self, data_dir):
        self.config = _Cfg(data_dir)
        self.vector_memory = None
        self.llm = None


# ─────────────────────────────────────────────────────────────────────────────
# ① how-to 意图豁免
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "如何成为杰米高手",
    "怎么才能变强啊",
    "教我打一下这个",
    "求个出装思路",
    "这个英雄怎么玩",
    "如何快速入门",
    "有没有攻略",
    "怎么配队",
])
def test_howto_intent_is_recognized(text):
    assert _USER_WANTS_HOWTO_RE.search(text), f"漏识别 how-to 意图: {text}"
    assert _user_wants_substantive(text) is True


@pytest.mark.parametrize("text", [
    "在吗",
    "怎么了",
    "哈哈哈哈哈",
    "什么鬼",
    "为什么啊",
    "今天好累啊",
    "你说得对",
    "本喵刚打了个嗝",
    "这个怎么说呢",
])
def test_generic_chatter_is_not_howto(text):
    """🔴 泛疑问/寒暄**不得**被豁免 —— 否则等于给所有口语回复开后门。"""
    assert _USER_WANTS_HOWTO_RE.search(text) is None, f"过度豁免: {text}"
    assert _user_wants_substantive(text) == bool(_USER_WANTS_ANALYSIS_RE.search(text))


def test_howto_question_exempts_guide_from_essay_rule():
    """🔴 生产原句：用户明确问攻略 ⇒ 590 字攻略**不该**被判论文体。"""
    assert _analysis_essay_rule(PROD_HOWTO_REPLY, user_text=PROD_HOWTO_QUESTION) is None


def test_same_guide_without_intent_is_still_caught():
    """反向对照：**没有** how-to 意图时，同样的长分点攻略**仍**被判论文体。

    确保豁免是「意图驱动」，不是「长分点一律放行」。
    """
    assert _analysis_essay_rule(PROD_HOWTO_REPLY, user_text="哈哈哈哈") == "struct>=2&long"


def test_true_positive_rule_name_unchanged():
    """真阳性仍被捕获，且**规则名不变**（改的是处置，不是判据）。"""
    assert _analysis_essay_rule(PROD_TRUE_POSITIVE_ESSAY) == "essay_score"


@pytest.mark.asyncio
async def test_howto_guide_reaches_user_intact(tmp_path):
    """端到端：how-to 问题 ⇒ 攻略原样送达（不被替换、不被 420 字截断）。"""
    core = _FakeCore(tmp_path)
    ctx = _ctx(text=PROD_HOWTO_QUESTION, reply=PROD_HOWTO_REPLY, meta={"core": core})
    await guardrail_stage(ctx)

    assert ctx.reply not in _ANALYSIS_ESSAY_FALLBACKS, "攻略被整段替换成罐头话"
    assert "1. 确立喝酒优先级" in ctx.reply, "攻略内容丢失"
    assert "4. 防空与SA2资源调度" in ctx.reply, "攻略被截断（hard_limit 未放宽）"
    assert len(ctx.reply) >= 500, f"攻略被砍短: {len(ctx.reply)}"


# ─────────────────────────────────────────────────────────────────────────────
# ② essay_score 的处置：保留内容，不再整段替换
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_essay_score_keeps_content_instead_of_replacing(tmp_path):
    """🔴 生产样本 ①：essay_score 命中时**不得**整段替换成罐头话。"""
    core = _FakeCore(tmp_path)
    ctx = _ctx(text="玩了快一千把斯旺，我只能说怨灵强度不行", reply=PROD_SWANN_REPLY,
               meta={"core": core})
    await guardrail_stage(ctx)

    assert ctx.reply not in _ANALYSIS_ESSAY_FALLBACKS, "仍是整段罐头替换"
    assert "核心机制" in ctx.reply and "容错率" in ctx.reply, "正常内容被删"
    assert ctx.reply.endswith("大概就这样喵。"), "未按保留内容分支收尾"


@pytest.mark.asyncio
async def test_long_essay_score_is_trimmed_not_replaced(tmp_path):
    """长文仍受约束：只截断到 280，不整段替换。"""
    core = _FakeCore(tmp_path)
    long_essay = PROD_TRUE_POSITIVE_ESSAY + "补一句：低价值牌进循环，洗出来全是指向性极高的锦囊。" * 6
    assert len(long_essay) > 300
    ctx = _ctx(text="随便聊聊", reply=long_essay, meta={"core": core})
    await guardrail_stage(ctx)

    assert ctx.reply not in _ANALYSIS_ESSAY_FALLBACKS
    assert "单挑归根结底打的就是" in ctx.reply, "前半段内容必须保留"
    assert len(ctx.reply) <= 300, f"截断失效: {len(ctx.reply)}"


@pytest.mark.asyncio
async def test_other_rules_still_replace_destructively(tmp_path):
    """🔴 反向对照：显式语义词路径（keys>=2）**仍**整段替换 —— 不得顺手全放松。"""
    core = _FakeCore(tmp_path)
    ctx = _ctx(text="哈哈哈哈", reply=BREAK_ROLE_REPLY, meta={"core": core})
    await guardrail_stage(ctx)

    assert ctx.reply in _ANALYSIS_ESSAY_FALLBACKS, "语义词路径被误放松"


@pytest.mark.asyncio
async def test_essay_score_still_sampled_and_logged(tmp_path, caplog):
    """处置变了，但**观测不能丢** —— 仍要落样本、仍要留规则名（否则无法继续定标）。"""
    import json

    core = _FakeCore(tmp_path)
    with caplog.at_level("WARNING", logger="anima.pipeline.stages"):
        ctx = _ctx(text="随便聊聊", reply=PROD_TRUE_POSITIVE_ESSAY, meta={"core": core})
        await guardrail_stage(ctx)

    assert any("规则=essay_score" in r.getMessage() for r in caplog.records), \
        "命中日志丢了规则名"
    path = tmp_path / "guardrail_essay_samples.jsonl"
    rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(rows) == 1
    assert rows[0]["rule"] == "essay_score"
    assert rows[0]["reply"] == PROD_TRUE_POSITIVE_ESSAY, "样本必须是完整原文"
    assert rows[0]["rewritten_to"] == ctx.reply, "样本须记改写结果以便对比"
