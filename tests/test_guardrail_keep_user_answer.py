# -*- coding: utf-8 -*-
"""护栏 essay 处置：从「关键词豁免」扩到「用户确实在问东西」（2026-10-04）。

## 生产证据：`data/guardrail_essay_samples.jsonl` 7 条样本**条条是误杀**

| # | 用户原句 | bot 实际答了 | 被替换成 |
|---|---|---|---|
| ③ | 如何成为街霸6杰米高手 | 590 字连段教学 | 本喵还是适合唠嗑… |
| ④ | 有带胸口的纸质材料的嘛 | 4 种纸质胸牌方案（398 字） | 害，本喵想聊点别的了… |
| ⑤ | 前列腺液都有哪些成分 | 成分列表（258 字） | 换个话头吧… |
| ⑥ | **通宵后感觉心慌心悸…怎么办** | **急救步骤**（345 字） | 换个话头吧… |
| ⑦ | （粘贴三国杀技能规则） | 规则吐槽（372 字） | 本喵还是适合唠嗑… |

## 根因

处置分支是 `if _essay_rule == "essay_score" or _user_wants_substantive(user_text)` 才
**保留内容**，否则**整段替换**成罐头话。而 `_user_wants_substantive` 只认
「解析/分析/如何/教我/攻略…」这类 **how-to 关键词**
⇒ **日常提问根本不说这些词** ⇒ 实质回答几乎必然被整段销毁。

🔴 这与「LLM 不想接」的主观感受完全吻合：模型**愿意答、也答对了**，
是护栏在出站前把它整段换成一句客套话。

## 修法（判据与规则名一律不动，只改「谁该被销毁」的判别）

把条件从「用户用了 how-to 关键词」扩到「**用户确实在问东西**」
（疑问词 / 问号 / 长问句）。

**反向对照必须仍绿**：`tests/test_guardrail_false_kill_fix.py` 里
`test_other_rules_still_replace_destructively` 用的是**真·破角色跑偏**
（用户只发「哈哈哈哈」，bot 却去分析这个人）⇒ 新条件**不得**把它一起放松。
本判据钉死这一点。
"""
import types

import pytest

from anima.pipeline.stages.guardrail import (
    _ANALYSIS_ESSAY_FALLBACKS,
    _user_asked_question,
    guardrail_stage,
)

# ── 生产误杀样本的**用户原句**（逐字取自 data/guardrail_essay_samples.jsonl）──
PROD_FK = [
    # (用户原句, 期望在回复里保住的关键词, 主题提示)
    ("有带胸口的纸质材料的嘛", ("纸质", "展会"), "胸牌"),
    ("前列腺液都有哪些成分", ("成分", "卵磷脂"), "前列腺液"),
    ("通宵后感觉心慌心悸，肠胃不适，冒冷汗，极度疲劳伴呼吸困难，怎么办",
     ("呼吸", "心跳"), "急救"),
    ("你可以弃置至少两张牌，并从牌堆里获得一张点数为x的牌。"
     "（x为你弃置的牌点数之和与13对余）", ("牌堆", "点数"), "三国杀"),
    # 📌 生产样本 ③「如何成为街霸6杰米高手」**不在此列**：它已含 how-to 关键词，
    # 由 `_user_wants_substantive` 豁免覆盖，回归见
    # `tests/test_guardrail_false_kill_fix.py::test_howto_guide_reaches_user_intact`。
    # 本批要修的是**不含 how-to 关键词**的那批（此前必然被销毁）。
]


def _long_numbered(topic: str, k1: str, k2: str, *, n: int = 4) -> str:
    """造一条与生产样本同构的回复：≥2 编号分点 + **≥280 字** + 含主题关键词。

    🔴 正文用生产**同构的合成**内容（保持判据可读），但 `user_text` 是**生产原句逐字**。
    ⚠️ 长度必须 **> 220**（`struct>=2&long` 的门槛）——第一版只造到 201 字，
    规则根本不触发，测试会以"没落盘"的形式假失败。
    """
    parts = [
        f"要说{topic}，核心其实就那么几件事，本喵掰开给你讲清楚喵。\n\n",
        f"1. {k1}：这是最关键的一步，也是最容易被人一开始就走歪的地方。"
        f"顺序要是错了，后面每一步都在给上一步擦屁股，所以先把{k1}摸透，"
        f"别急着往后冲。\n\n",
        f"2. {k2}：接着是{k2}，这一步千万别偷懒。偷懒的后果不是慢一点，"
        f"而是后面会非常难受 —— 很多人就是在这里放弃了，然后跑来问本喵为什么不行。\n\n",
    ]
    for i in range(3, n + 1):
        parts.append(
            f"{i}. 补充第{i}点：细节决定成败。{k1}和{k2}之间还有取舍，"
            f"得按你自己的实际情况调整，别照搬别人的答案，"
            f"照搬的人通常只是抄了个形状回去，然后发现完全对不上。"
            f"这一点本喵见过太多人踩了。\n\n"
        )
    parts.append(
        "说到底，本喵还是那句：先抓主线，再抠细节，稳着来别急喵。"
        "把顺序理对了，后面自然就顺了。"
    )
    return "".join(parts)


def _ctx(*, text, reply, meta):
    from anima.pipeline import PipelineContext

    ctx = PipelineContext(
        event={}, text=text, user_id=888, group_id=222,
        detail_type="group", sender_name="Alice", meta=meta,
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


# ── 1. 疑问识别本身 ────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "有带胸口的纸质材料的嘛",          # 生产原句 ④
    "前列腺液都有哪些成分",            # 生产原句 ⑤
    "通宵后感觉心慌心悸…怎么办",       # 生产原句 ⑥
    "这个怎么配",
    "为什么啊",
    "能不能发我看看",
    "哪个更好点？",
    # 🔴 2026-10-05 生产漏网实证：用户问图片里是谁，bot 答 2322 字（内容正确），
    # 却被整段换成「本喵还是适合唠嗑，说点轻松的吧喵～」。
    # 它栽在两处：① 疑问词表有「哪」没「谁」；② 只有 9 字，差 1 字够不到 10 字兜底。
    "这张图里三个人是谁",
    "这张照片里穿红衣服的是谁",
])
def test_question_is_detected(text):
    assert _user_asked_question(text) is True, f"未识别为问句: {text}"


@pytest.mark.parametrize("text", [
    "哈哈哈哈",                        # 既有反向对照：纯 chatter
    "牛批",
    "早",
    "睡了睡了",
    "",
    # 🔴 「怎么说」是**既有判据** `test_guardrail_rewrites_analysis_essay` 的输入：
    # 用户只打 3 字而 bot 吐了一篇「政治社会学/实证科学」分析体 ⇒ 真·破角色。
    # 「用户极短 + bot 极长」正是跑偏的特征 ⇒ 极短的疑问词**不足以**判为在问东西。
    "怎么说",
    "怎么讲",
    # 🔴 2026-10-05：补「谁」**刻意不动**长度阈值。若把 `_SUBSTANTIVE_INPUT_MIN`
    # 从 10 降到 9 来捞那句 9 字问句，这类 11 字闲聊就会被误判成提问 ⇒ 给破角色开门。
    "今天真的好累啊感觉要猝死",        # 11 字闲聊，无疑问词 ⇒ 不得判为提问
    "我今天被领导骂了一顿好烦",        # 12 字 ⇒ 同上
    "这个游戏我还真的挺喜欢的",        # 12 字 ⇒ 同上
])
def test_chatter_is_not_a_question(text):
    """🔴 反向对照：纯 chatter / 极短开口 / 长闲聊**不得**被当成问句 —— 否则等于给破角色开门。"""
    assert _user_asked_question(text) is False, f"过度识别为问句: {text}"


# ── 2. 生产误杀样本：内容必须留住 ──────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("user_text,keywords,topic", PROD_FK)
async def test_productive_question_keeps_content(tmp_path, user_text, keywords, topic):
    core = _FakeCore(tmp_path)
    reply = _long_numbered(topic, *keywords)
    # 🔴 自证前提：必须真的命中 struct>=2&long，否则本用例测的是"规则没触发"
    from anima.pipeline.stages.guardrail import _analysis_essay_rule
    assert len(reply) >= 280, f"合成回复太短，未过 220 门槛: {len(reply)}"
    assert _analysis_essay_rule(reply, user_text=user_text) == "struct>=2&long", \
        f"未命中 struct>=2&long（判据变了？）: {_analysis_essay_rule(reply, user_text=user_text)}"
    ctx = _ctx(text=user_text, reply=reply, meta={"core": core})
    await guardrail_stage(ctx)

    assert ctx.reply not in _ANALYSIS_ESSAY_FALLBACKS, "🔴 实质回答被整段替换成罐头话"
    for kw in keywords:
        assert kw in ctx.reply, f"内容丢失（{kw} 不在回复里）: {ctx.reply[:120]}"


# ── 3. 🔴 反向对照：真·破角色跑偏**仍**整段替换 ────────────────────

@pytest.mark.asyncio
async def test_break_role_runaway_still_replaced(tmp_path):
    """用户只发「哈哈哈哈」，bot 却跑去分析这个人 ⇒ 破角色，**必须仍被销毁**。

    这是 `tests/test_guardrail_false_kill_fix.py` 里同款守卫的延伸：
    新条件只认「用户在问东西」，不认「bot 跑偏得长」。
    """
    break_role = (
        "@[某用户]你是 MengWuy，也就是那个在上海的群友。\n\n"
        "关于你问的 GOP 和那个群体的事，从政治社会学和传播学的角度来看，"
        "这其实是互联网政治模因的一种解构。\n\n"
        "从实证科学的角度来说，没有任何具备统计学信度的数据能支持这种说法。"
        "把一个庞大且复杂的选民群体做单一化的标签绑定是不合逻辑的。\n\n"
        "本喵觉得这就是个无厘头的网络解构，不用太当真 ( ﾟ∀ ﾟ)"
    )
    core = _FakeCore(tmp_path)
    ctx = _ctx(text="哈哈哈哈", reply=break_role, meta={"core": core})
    await guardrail_stage(ctx)

    assert ctx.reply in _ANALYSIS_ESSAY_FALLBACKS, "🔴 破角色跑偏被误放松（真阳性漏网）"


# ── 4. 观测不能丢：规则仍命中、样本仍落盘 ──────────────────────────

@pytest.mark.asyncio
async def test_still_detected_and_sampled(tmp_path):
    """处置变了，但**判据不动** —— 仍要命中、仍要落样本、仍要留规则名（否则无法继续定标）。"""
    import json

    core = _FakeCore(tmp_path)
    reply = _long_numbered("胸牌", "纸质", "展会")
    ctx = _ctx(text="有带胸口的纸质材料的嘛", reply=reply, meta={"core": core})
    await guardrail_stage(ctx)

    path = tmp_path / "guardrail_essay_samples.jsonl"
    rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(rows) == 1, f"样本未落盘（无法继续定标）：{rows}"
    assert rows[0]["rule"], "样本必须带规则名"
    assert rows[0]["reply"] == reply, "样本必须是完整原文"
    assert rows[0]["rewritten_to"] == ctx.reply


# ── 5. 生产漏网回归：图片识别长答（2026-10-05）──────────────────────

@pytest.mark.asyncio
async def test_image_id_long_answer_is_kept(tmp_path):
    """🔴 生产实证回归：用户问「这张图里三个人是谁」。

    2026-10-04 21:16 线上真实发生：bot 答了 2322 字（东方Project 角色分析，
    **内容是对的**），却被整段替换成「本喵还是适合唠嗑，说点轻松的吧喵～」。
    用户看到的就是"问正经问题却被敷衍"。

    漏网根因：原句 9 字、无标点 ⇒ 问号/疑问词都不匹配（表里有「哪」没「谁」），
    又差 1 字够不到 10 字的陈述式兜底。
    """
    core = _FakeCore(tmp_path)
    # 生产同构：识别图片 ⇒ 分点介绍每个角色，篇幅长
    reply = _long_numbered(
        "这张图里的三个人", "东风谷早苗", "宇佐见莲子",
    )
    ctx = _ctx(text="这张图里三个人是谁", reply=reply, meta={"core": core})
    await guardrail_stage(ctx)

    assert ctx.reply not in _ANALYSIS_ESSAY_FALLBACKS, \
        "🔴 识别图片的长答又被整段换成罐头话（2026-10-04 生产实证）"
    assert "东风谷早苗" in ctx.reply, "🔴 识别结果被丢掉"
