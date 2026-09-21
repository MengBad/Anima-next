"""情绪供给侧收口 —— 「算了不说了」根因（2026-09-21）。

背景
----
用户报「bot 老是回复：算了不说了」。溯源结论：**这句是代码拼上去的**——
`guardrail.py` 的 C1（情绪→回复长度约束）在 `mood.label == annoyed` 时把回复砍到
120 字，末尾若不是句末标点就硬拼 `_MOOD_TAIL["annoyed"] = "算了不说了"`。
现场：用户问「没看懂，再解释下」，bot 回「啧，麻烦死了……听好了。…算了不说了。」

而 mood 为什么长期是 annoyed，是**供给侧的伪信号**：

1. `generation.py` 的 insult 词表含 `笨/菜/烦/傻/滚` —— 群聊日常字，
   「笨蛋吴雨萌不读群昵称」是在说**人类**，却每条扣 `valence −0.12`（7 天 45 条）；
2. `handler.py` 的 `overwhelmed` 在 `cnt > soft` 后**每条被 @ 的消息**都再扣一次
   （`−0.05 / arousal +0.1`），一个 5 分钟窗口可扣 4–5 次；
3. 正向只有一张小 praise 词表，群里几乎不出现 ⇒ **结构性负漂移** ⇒ 钉在 annoyed。

本文件钉住三处修复：判据收口、去重、以及 C1 尾巴的**彻底移除**。
"""
from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from anima.llm.handler import (
    LLMHandler,
    _FATIGUE_MOOD_COOLDOWN_SEC,
    _TIRED_COOLDOWN_SEC,
)
from anima.pipeline import PipelineContext
from anima.pipeline.stages import guardrail_stage
from anima.pipeline.stages.generation import (
    _INSULT_STRONG,
    _INSULT_WEAK,
    _PRAISE_KW,
    _detect_insult,
)

_ROOT = Path(__file__).resolve().parents[1]
_GR_SRC = (_ROOT / "anima" / "pipeline" / "stages" / "guardrail.py").read_text(encoding="utf-8-sig")
_GEN_SRC = (_ROOT / "anima" / "pipeline" / "stages" / "generation.py").read_text(encoding="utf-8-sig")
_HANDLER_SRC = (_ROOT / "anima" / "llm" / "handler.py").read_text(encoding="utf-8-sig")


def _code(src: str) -> str:
    """剥掉注释后再断言 —— 否则解释性注释本身会把源码守卫变成假红/假绿。"""
    return "\n".join(ln.split("#")[0] for ln in src.splitlines())


def _annoyed_ctx(reply: str, text: str = "没看懂，再解释下") -> PipelineContext:
    ctx = PipelineContext(
        event={}, text=text, user_id=888, group_id=222,
        detail_type="group", sender_name="Alice",
        meta={},  # 无 chat_id → 跳过记忆写入副作用
    )
    ctx.reply = reply
    return ctx


class _FakeMood:
    label = "annoyed"

    def get_state(self) -> SimpleNamespace:
        return SimpleNamespace(label=self.label)


@pytest.fixture
def annoyed_mood(monkeypatch):
    """把 C1 读到的情绪钉成 annoyed（不碰真实 mood.json）。"""
    from anima.cognitive import mood as mood_mod
    monkeypatch.setattr(mood_mod, "get_mood_system", lambda *a, **k: _FakeMood())
    return _FakeMood


# ── 1. insult 判据必须指向 bot ──────────────────────────────────────────
@pytest.mark.parametrize("text,addressed,expect,why", [
    # 生产原文：旧裸词表把 valence 扣掉了（7 天 3401 条入站里 35 条命中）
    ("rezon是aki的那个号，我没改名字，笨蛋吴雨萌不读群昵称读qq昵称", False, False, "说人类，非指向 bot"),
    ("天天本喵来本喵去的", False, False, "话题结构，无辱骂词指向"),
    ("少来，本喵帮不了你", False, False, "拒绝 ≠ 被骂"),
    ("今天好烦啊", False, False, "日常抱怨，无 bot 指代"),
    ("这菜真好吃", False, False, "「菜」= 食物"),
    ("这周末去哪玩啊", False, False, "无辱骂词"),
    ("", False, False, "空串"),
    # 7 天生产入站回测抓到的三个高危误伤（收口前会被算成「被骂」）
    ("@MengKing (2752719822) 去给我炒俩菜", True, False, "「菜」= 食物，即使 @ 了 bot 也不算被骂"),
    ("@纯晶冰洁 (0) 今天吃上汤娃娃菜", False, False, "「菜」= 食物，@ 的不是 bot"),
    ("把我的斗笠气歪了，害我从梯田上滚了下来", False, False, "「滚」= 滚动"),
    # 真被骂必须留住
    ("@MengKing 你好笨", True, True, "被 @ 且含高危弱词"),
    ("@MengKing (2752719822) 你是笨蛋", True, True, "被 @ 且含高危弱词（QQ 号占位会拉长距离）"),
    ("哈基米，你为什么这么笨", False, True, "与 bot 自称共现"),
    ("你这只笨猫又跑了", False, True, "与 bot 自称（猫）共现"),
    ("本喵真是个蠢货", False, True, "自称 + 弱词共现"),
    ("傻逼", False, True, "强侮辱词不要求指向"),
    ("你给我去死", False, True, "强侮辱词不要求指向"),
])
def test_detect_insult_requires_bot_reference(text, addressed, expect, why):
    assert _detect_insult(text, addressed_to_bot=addressed) is expect, why


def test_insult_lexicon_partition_is_disjoint():
    """强词与弱词不得重叠，否则「指向」约束会被强词分支绕过。"""
    assert not (set(_INSULT_STRONG) & set(_INSULT_WEAK))


def test_praise_and_insult_lexicons_do_not_overlap():
    """同一个词不能既算被夸又算被骂（旧表里「可爱」与「烦」分属两边，此处只是防未来手滑）。"""
    assert not (set(_PRAISE_KW) & set(_INSULT_WEAK))


def test_generation_source_uses_shared_predicate():
    """源码级：A2 调用点必须走收口后的判据，不得再内联裸词表。"""
    code = _code(_GEN_SRC)
    assert "_detect_insult(" in code
    # 旧内联元组（含裸「笨」「菜」「烦」）不得回来
    assert '"笨", "垃圾", "滚", "傻", "菜"' not in code


# ── 2. 社交疲劳情绪扣分去重 ─────────────────────────────────────────────
def _handler() -> LLMHandler:
    return LLMHandler.__new__(LLMHandler)


def test_fatigue_mood_dedup_blocks_repeat_in_same_window():
    h = _handler()
    assert h._fatigue_mood_allow(727631526) is True
    assert h._fatigue_mood_allow(727631526) is False, "同一 5 分钟窗口内不得重复扣情绪分"


def test_fatigue_mood_dedup_is_per_group():
    h = _handler()
    assert h._fatigue_mood_allow(111) is True
    assert h._fatigue_mood_allow(222) is True, "A 群去重不应冷却 B 群"


def test_fatigue_mood_dedup_expires():
    h = _handler()
    h._fatigue_mood_at = {1: time.time() - _FATIGUE_MOOD_COOLDOWN_SEC - 1}
    assert h._fatigue_mood_allow(1) is True, "窗口过后应恢复扣分"


def test_fatigue_mood_dedup_is_fail_open():
    """异常时放行 —— 宁可多扣一次分，也不可卡住消息处理路径。"""
    h = _handler()
    h._fatigue_mood_at = None  # 让 `.get` 抛 AttributeError
    assert h._fatigue_mood_allow(1) is True


def test_fatigue_mood_window_is_independent_from_tired_cooldown():
    """两个窗口语义不同（别再发罐头 vs 别重复扣分），不得互相顶替。"""
    assert _FATIGUE_MOOD_COOLDOWN_SEC != _TIRED_COOLDOWN_SEC
    code = _code(_HANDLER_SRC)
    assert "self._fatigue_mood_allow(group_id)" in code


# ── 3. C1 不得再拼「拒绝语义」尾巴 ──────────────────────────────────────
def test_guardrail_source_no_longer_carries_mood_tail():
    """源码级回归守卫：情绪尾巴（含「算了不说了」）不得再出现在 guardrail 里。"""
    code = _code(_GR_SRC)
    assert "_MOOD_TAIL" not in code, "情绪尾巴表又回来了"
    for literal in ("算了不说了", "嗯…不想多说了", "就这吧"):
        assert literal not in code, f"拒绝语义文案又写回了 guardrail: {literal!r}"


def test_guardrail_caps_allow_a_full_explanation():
    """上限必须装得下一段解释（旧口径 annoyed/tired=120 会把答案砍半）。"""
    import re as _re
    cap_block = _GR_SRC.split("_MOOD_LENGTH_CAP = {", 1)[1].split("}", 1)[0]
    caps = {k: int(v) for k, v in _re.findall(r'"(\w+)":\s*(\d+)', cap_block)}
    assert caps["annoyed"] >= 200, f"annoyed 上限仍过短: {caps}"
    assert caps["tired"] >= 160, f"tired 上限仍过短: {caps}"
    assert caps["bored"] >= 100, f"bored 上限仍过短: {caps}"


@pytest.mark.asyncio
async def test_c1_truncation_ends_with_ellipsis_not_refusal(annoyed_mood):
    """行为级：被截断时只以省略号收尾，绝不输出「算了不说了」。"""
    long_reply = "啧，麻烦死了……听好了。" + "判定标准只有一个，看现代科技能不能做到，" * 12
    assert len(long_reply) > 220
    ctx = _annoyed_ctx(long_reply)
    await guardrail_stage(ctx)

    assert "算了不说了" not in ctx.reply
    assert "不想多说了" not in ctx.reply
    assert len(ctx.reply) <= 220
    assert ctx.reply.endswith(("…", "。", "！", "？", "~")), f"收尾不干净: {ctx.reply[-12:]!r}"


@pytest.mark.asyncio
async def test_c1_keeps_a_medium_explanation_intact(annoyed_mood):
    """120 < len <= 220 的解释在旧口径下会被砍 —— 现在必须原样通过。"""
    medium = "啧，麻烦死了。" + "这个是判定标准，看现代科技能不能做到。" * 8
    assert 120 < len(medium) <= 220, f"用例长度不在区间内: {len(medium)}"
    ctx = _annoyed_ctx(medium)
    await guardrail_stage(ctx)
    assert ctx.reply == medium, "中等长度解释被 C1 误砍"


@pytest.mark.asyncio
async def test_c1_does_not_ask_user_to_repeat_themselves(annoyed_mood):
    """用户明确求解释时，回复里不得出现索取复述的句式。"""
    long_reply = "嗯……" + "这个机制其实分两层，第一层看意图，第二层看上下文，" * 12
    ctx = _annoyed_ctx(long_reply, text="解释一下这个机制")
    await guardrail_stage(ctx)
    assert "你刚想说啥" not in ctx.reply
    assert "再说一遍" not in ctx.reply


# ── 4. 情绪触发可观测 ───────────────────────────────────────────────────
def test_mood_trigger_emits_a_log_line(tmp_path, caplog):
    """此前 trigger() 完全无日志 ⇒「为何长期 annoyed」不可归因。"""
    from anima.cognitive.mood import MoodSystem
    ms = MoodSystem(str(tmp_path))
    with caplog.at_level("INFO", logger="anima.cognitive.mood"):
        ms.trigger("insulted", 0.4)
    assert "情绪触发" in caplog.text
    assert "insulted" in caplog.text
