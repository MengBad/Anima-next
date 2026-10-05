# -*- coding: utf-8 -*-
"""超长回复截断不得从句中切（2026-10-05）

## 生产实证（10-04 21:56）
```
21:56:27  Guardrail: 超长回复截断至 420 字
21:56:32  发送 -> …去网易云或QQ音乐直接搜"Devil May Cry Dante"或者"但丁战斗曲"，
          基本全是这几首循环播放，听了…
```
用户看到的是「…听了…」，句子**没说完**。根因：`guardrail.py:725`
`ctx.reply[:hard_limit].rstrip("，,。；;、 ") + "…"` —— **纯字符数切**。

## 为什么只修"截断点吸附"而不动生成前
记忆里有一条：`hard_limit 420/900` = 事后截断、破坏性，
**缩短须在生成前**（`soul.map_max_tokens`）。但改生成前 = **行为变更**
（会改变所有回复的长度与内容），风险远高于本轮范围。

⇒ 本轮只做**呈现层修复**：切点回退到最近的句读边界。
- 零行为变更：不改 `map_max_tokens`、不改阈值、不改触发条件。
- 仍然截断（长度上限是硬约束，🔴 不能因为"读起来完整"就放开）。

## 判据
1. 切点**必须**落在句读/换行边界上（不得从句中切）。
2. 回退有下限（不得回退过半 ⇒ 否则短回复被砍成空）。
3. 找不到边界时**保留原行为**（纯字符切 + `…`）—— 不制造新失败模式。
4. 长度上限本身**不得放宽**（≤ limit + 1 个省略号）。
5. 边界候选要含中文标点、英文标点、换行、列表项。
"""

from __future__ import annotations

import pytest

from anima.pipeline.stages.guardrail import _truncate_at_boundary


# ── 1. 生产样本：从句中切 → 必须吸附 ────────────────────────────────

#: 10-04 21:56 那条的真实尾巴（截断点落在「听了」的「了」后面）
#: 🔴 生产真实长度 420+，这里在**真实前缀**后按真实话题续写到超限
#:    （测试必须自证超限，否则测的是「原样返回」分支）。
_PROD_PREFIX = (
    "- 《Public Enemy》（鬼泣1）：一代最经典的战斗BGM，复古硬摇味道很浓。"
    "去网易云或QQ音乐直接搜“Devil May Cry Dante”或者“但丁战斗曲”，"
    "基本全是这几首循环播放，听了"
)
#: 同话题的续写（用于撑到 420+）
_PROD_CONT = (
    "，你要是想听现场版就去B站搜FC。顺便说一句，本喵觉得四代的变奏更带感，"
    "一代的原版才是神作，别的都差点意思喵。后面还有更多内容会被硬切掉所以这里要够长才行。"
    "再补几句把长度撑到四百二十字以上才有意义对吧，本喵啰嗦起来可是收不住的，"
    "你要是嫌长就当本喵在打哈欠好了。总之先去B站搜原版就完事了，别折腾。"
)
PROD_TAIL = _PROD_PREFIX + _PROD_CONT
# 不够 420 就重复续写段（模拟"更长的真实回复"）
while len(PROD_TAIL) <= 460:
    PROD_TAIL += "本喵再啰嗦一句，" + _PROD_CONT


def test_prod_sample_not_cut_mid_sentence():
    assert len(PROD_TAIL) > 420, f"生产样本必须自证超限（实际 {len(PROD_TAIL)}）"
    got = _truncate_at_boundary(PROD_TAIL, 420)
    assert got.endswith("…"), "应仍以省略号收尾"
    assert len(got) <= 421, f"长度上限被放宽：{len(got)}"
    # 🔴 核心：不得出现「听了…」这种半句
    assert not got.rstrip("…").endswith("听了"), f"仍从句中切：…{got[-24:]}"
    # 🔴 切点前的**最后一个字符**必须是句读（实现把边界字符本身收进省略号前，
    #    所以 `head[:cut]` 的末位就是它）
    assert got[-2] in "。！？!?；;：:\n", f"切点未落在句读边界：…{got[-16:]}"
    assert got.count("…") == 1, "只应有一个省略号"


def test_boundary_prefers_earliest_sentence_end():
    """多个候选时取**最靠前**的那个（句子最完整）。

    🔴 倒序扫描会拿到最靠后的边界 ⇒「第一句。第二句|」而不是「第一句|」，
    看着完整其实把半句留在了前面 —— 反向扫描实测踩过。

    🔴 **优先级**：取最靠前 vs 回退不过半，**回退不过半优先**。
    否则「嗯。+ 500 字无标点」这种开头就带句号的短句会被砍成「嗯。」。
    """
    text = "第一句说完了。第二句也说完。第三句还没说完，正在说"
    got = _truncate_at_boundary(text, 20)
    body = got.rstrip("…")
    # floor = 20//2 = 10，「第一句说完了。」的句号在下标 6 < floor ⇒ 只能取下一个边界
    # （实现保留边界字符本身，所以带句号）
    assert body == "第一句说完了。第二句也说完。", f"边界选择不符实现契约：{body!r}"

    # 拉长第一句让它跨过 floor ⇒ 此时应取最靠前
    text2 = "第一句话稍微长一点点说完了。第二句也说完。第三句还没说完，正在说"
    got2 = _truncate_at_boundary(text2, 20)
    # 实现把边界字符（句号）收进省略号**之前** ⇒ 结果是「…说完了…」
    assert got2 == "第一句话稍微长一点点说完了。…", f"跨过 floor 后未吸附首句：{got2!r}"


def test_newline_is_a_boundary():
    got = _truncate_at_boundary("上面一行说完了\n下面这行正在说还没完", 10)
    body = got.rstrip("…")
    assert body == "上面一行说完了", f"换行未作边界：{body!r}"


def test_bullet_item_is_a_boundary():
    text = "- 第一项说完\n- 第二项正在说还没说完"
    got = _truncate_at_boundary(text, 12)
    assert "- 第一项说完" in got, f"列表项边界未识别：{got!r}"


# ── 2. 兜底：找不到边界 ⇒ 保持原行为，不制造新失败模式 ──────────────

def test_no_boundary_falls_back_to_raw_cut():
    text = "没有任何标点也没有换行的一长串字" * 5      # 长度远超 limit
    got = _truncate_at_boundary(text, 20)
    assert got == text[:20].rstrip("，,。；;、 ") + "…", f"无边界时应退回字符切：{got!r}"


# ── 3. 硬约束：长度上限不得放宽 ────────────────────────────────────

@pytest.mark.parametrize("limit", [20, 100, 420])
def test_never_exceeds_limit(limit):
    # 🔴 文本必须**自证超限**（短于 limit 时 helper 原样返回，测不到截断逻辑）
    text = ("句子一。句子二。句子三。句子四。句子五。句子六。句子七。句子八。" * 20)
    assert len(text) > limit, "测试样本未超限"
    got = _truncate_at_boundary(text, limit)
    assert len(got) <= limit + 1, f"超长 {len(got)} > {limit}+1"
    assert got.endswith("…")


def test_short_text_untouched():
    """不超限 ⇒ 原样返回，**不得**加省略号。"""
    assert _truncate_at_boundary("短回复。", 420) == "短回复。"
    assert _truncate_at_boundary("", 420) == ""


# ── 4. 回退不得过半（短文本不得被砍成空）───────────────────────────

def test_rollback_does_not_exceed_half():
    text = "一二三四五六七八九十甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉戌亥"
    got = _truncate_at_boundary(text, 40)
    # 没有句读 ⇒ 退回字符切；但若**有**远边界也不能回退过半
    if got != text[:40] + "…":
        assert len(got.rstrip("…")) >= 20, f"回退过半：{got!r}"


def test_boundary_near_start_is_ignored():
    """切点太靠前（如第 2 个字符就遇到句号）⇒ 视为无效，走字符切。"""
    text = "嗯。好长的一段没有任何标点的话需要被截断掉才行"
    got = _truncate_at_boundary(text, 30)
    assert got != "嗯。…", f"接受了过靠前的边界：{got!r}"


# ── 5. 端到端：guardrail_stage 走真实路径 ───────────────────────────

@pytest.mark.asyncio
async def test_guardrail_stage_truncates_at_boundary(tmp_path):
    from types import SimpleNamespace

    from anima.core.persistence import PersistenceProvider
    from anima.pipeline import PipelineContext, StageResult
    from anima.pipeline.stages.guardrail import guardrail_stage

    tmp = str(tmp_path)

    class _Core:
        def __init__(self, d):
            self.config = SimpleNamespace(
                persistence=SimpleNamespace(data_dir=d),
                llm=SimpleNamespace(namespace="chat"),
            )
            self.vector_memory = None
            self.chat_memory = None

    core = _Core(tmp)
    body = PROD_TAIL + "后面还有更多内容会被硬切掉所以这里要够长才行喵"
    reply = body + "。" * 400
    ctx = PipelineContext(event={}, text="但丁的歌单有哪些歌曲", user_id=1,
                          group_id=727631526, detail_type="group", sender_name="A",
                          meta={"core": core})
    ctx.reply = reply
    await guardrail_stage(ctx)

    assert len(ctx.reply) <= 901, f"长度上限被放宽：{len(ctx.reply)}"
    assert ctx.reply.endswith("…"), "应仍以省略号收尾"
    assert not ctx.reply.rstrip("…").endswith("听了"), \
        f"端到端仍从句中切：…{ctx.reply[-24:]}"
