# -*- coding: utf-8 -*-
"""群聊回复硬长度上限（2026-10-25）。

## 为什么需要一道**硬**闸

用 `data/backups/` 里 30 个每日快照合并出的 **50 天 / 1422 条出站**实测：

```
平均 102.4 字 · 中位 84 · p90 179 · 最长 619
≤15 字占比  0.1%
≤25 字占比  1.0%      （人格要求）
含 2 个以上句子  1271 / 1422 = 89%
```

对比另一份成熟人格设定（DeepSeek 鲸鱼娘）的 7 条示例：

```
平均 11.0 字 · 中位 9 · 最长 27
```

**我们长 9.3 倍。** 差距不在语气词，在**篇幅本身**。

## 为什么既有两道闸拦不住

```
hard_limit = 900（用户求答） / 420（闲聊）
```

420 是「闲聊别超过一屏」，84 字的中位值远在它下面 ⇒ 从来不触发。
**⇒ 需要一道按「群聊场景」而非「是否求答」划的闸。**

## 取值与职责边界（重要）

`group_reply_max_chars = 160`，**远宽于人格的 25 字**。理由是职责不同：

- **prompt（人格）**：负责「像人一样短」，目标 25 字
- **这道闸**：负责「别让 300 字的分析报告上屏」，是**兜底**

若把闸设成 25 字，会把正常的 40~80 字回复**拦腰切断**，比超长更糟
（用户看到半句话）。所以本闸只砍 `p90=179` 那一档的长文。

## 判据

1. 群聊超上限被截断，且**吸附到句读边界**（不切半句）；
2. 私聊**不受影响**（仍是 420/900）；
3. 未超上限的回复**一字不动**；
4. `0` = 关闭该闸（回退行为与从前逐字相同）；
5. 有日志锚（`[群聊长度]`），否则「到底有没有生效」无法从 journal 回测；
6. 配置字段必须是 `ConversationConfig` 的显式字段（pydantic `extra=ignore`
   会静默丢弃只写在 yaml 里的键 —— 本项目踩过这个坑）。
"""
from __future__ import annotations

import asyncio
import pathlib

import pytest

import anima.pipeline.stages.guardrail as _g
from anima.core.config import AnimaConfig as Config
from anima.pipeline import PipelineContext

_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _ctx(reply: str, detail_type: str = "group"):
    ctx = PipelineContext.__new__(PipelineContext)
    ctx.reply = reply
    ctx.detail_type = detail_type
    ctx.text = "嗯"
    ctx.meta = {}
    ctx.group_id = 727631526 if detail_type == "group" else None
    ctx.incoming = None
    ctx.event = {}
    ctx.user_id = 10001
    ctx.sender_name = "测试"
    ctx.image_paths = []
    ctx.generated_image_paths = []
    ctx.generated_text_files = []
    return ctx


def _core(max_chars: int):
    cfg = Config()
    cfg.conversation.group_reply_max_chars = max_chars
    return type("C", (), {"config": cfg})()


def _run(ctx, core):
    ctx.core = core
    asyncio.run(_g.guardrail_stage(ctx))
    return ctx


def _long(n: int) -> str:
    """造一段 n 字的、带句读的自然文本。"""
    unit = "这是一段用来测试长度上限的句子，它必须足够长。"
    s = unit * (n // len(unit) + 1)
    return s[:n]


# ── 1. 基本行为 ─────────────────────────────────────────────────────

def test_group_reply_truncated_over_limit():
    ctx = _ctx(_long(400))
    _run(ctx, _core(160))
    assert len(ctx.reply) <= 160 + 1, f"未截断：{len(ctx.reply)} 字"


def test_group_reply_untouched_under_limit():
    """未超上限的回复**一字不动**（不许顺手改写）。"""
    original = "今天这个也太离谱了喵"
    ctx = _ctx(original)
    _run(ctx, _core(160))
    assert ctx.reply == original


def test_private_reply_not_affected():
    """🔴 私聊不走这道闸（私聊 420/900 的口径不变）。"""
    ctx = _ctx(_long(400), detail_type="private")
    _run(ctx, _core(160))
    assert len(ctx.reply) > 160, "私聊被群聊闸误伤了"


def test_zero_disables_the_gate():
    """`0` = 关闭 —— 回退行为必须与从前逐字相同。"""
    ctx = _ctx(_long(400))
    _run(ctx, _core(0))
    assert len(ctx.reply) > 160


# ── 2. 切点质量（不能切半句）─────────────────────────────────────────

def test_truncation_snaps_to_sentence_boundary():
    """🔴 硬截断也必须切在句读上 —— 半句话比超长更糟。"""
    ctx = _ctx(_long(400))
    _run(ctx, _core(160))
    assert ctx.reply.rstrip().endswith(("。", "！", "？", "…")), \
        f"截断处不是句读：…{ctx.reply[-20:]!r}"


def test_truncation_keeps_meaningful_prefix():
    """截断后不能只剩一两个字。"""
    ctx = _ctx(_long(400))
    _run(ctx, _core(160))
    assert len(ctx.reply) >= 80, f"截得太狠：{len(ctx.reply)} 字"


# ── 3. 与既有 420/900 闸的关系 ──────────────────────────────────────

def test_group_gate_is_tighter_than_generic_one():
    """群聊闸必须比 420 更紧，否则它没有存在意义。"""
    ctx = _ctx(_long(400))
    _run(ctx, _core(160))
    assert len(ctx.reply) <= 161 < 420


def test_generic_gate_still_applies_when_group_gate_disabled():
    """关掉群聊闸后，420 那道通用闸仍然生效。"""
    ctx = _ctx(_long(600))
    _run(ctx, _core(0))
    assert len(ctx.reply) <= 900 + 4  # 求答路径 900，这里 text="嗯" 走 420
    assert len(ctx.reply) <= 420 + 4


# ── 4. 配置接线（防哑开关）───────────────────────────────────────────

def test_config_field_exists():
    """🔴 pydantic 在本项目是 extra=ignore —— 只写 yaml 不加字段会被静默丢弃。"""
    cfg = Config()
    assert hasattr(cfg.conversation, "group_reply_max_chars")


def test_config_default_is_zero():
    """默认关（零行为变更）—— 显式在 yaml 里开启才生效。"""
    assert Config().conversation.group_reply_max_chars == 0


def test_default_yaml_sets_a_value():
    import yaml
    raw = yaml.safe_load((_ROOT / "config" / "default.yaml").read_text(
        encoding="utf-8"))
    assert raw["conversation"]["group_reply_max_chars"] == 160


# ── 5. 日志锚 ───────────────────────────────────────────────────────

def test_log_anchor_exists():
    """没有日志锚 ⇒「到底有没有生效」永远无法从 journal 回测。"""
    src = (_ROOT / "anima" / "pipeline" / "stages" / "guardrail.py").read_text(
        encoding="utf-8")
    assert "[群聊长度]" in src


def test_truncation_is_logged(caplog):
    import logging
    ctx = _ctx(_long(400))
    with caplog.at_level(logging.INFO, logger="anima.pipeline.stages"):
        _run(ctx, _core(160))
    # 用 getMessage() 拿格式化后的文本，不要手动 % 拼（会 TypeError）
    assert any("[群聊长度]" in r.getMessage() for r in caplog.records), \
        "截断没留日志"


# ── 6. 人格层的对应约束（不照抄参考人格用词）────────────────────────

def test_persona_closes_the_expansion_loophole():
    """🔴 根因：旧人格第 3 条「任务类该详细就详细」给第 2 条留了后门。

    模型分不清什么算「任务类」，于是整体走长。现在必须写明：
    「展开」只在对方**明确说**要详细时发生。
    """
    import yaml
    persona = yaml.safe_load((_ROOT / "config" / "persona_core.yaml").read_text(
        encoding="utf-8"))["system_prompt"]
    assert "只发生在对方明确说" in persona, "「展开」的后门没关"
    assert "任务类该详细就详细" not in persona, "旧的模糊例外还在"


def test_persona_has_no_interrogation_habit():
    """生产实证：38.9% 的回复含问号、19% 以「哼，/切，」开头。

    🔴 2026-10-25 二次修订：原先写的是「不要每句话都带一个反问」。

    17:20 的真实出站证明**禁令不生效**：
        「哼，学我叫什么叫，爪子痒了是不是喵~ 今天怎么这么有空跑来戳我呀…」
    49 字 / 4 个分句 / 3 个是质问。⇒ 改为**可执行的数字约束**：
    「最多一个问句」比「不要反问」能让模型照做。
    """
    import yaml
    persona = yaml.safe_load((_ROOT / "config" / "persona_core.yaml").read_text(
        encoding="utf-8"))["system_prompt"]
    assert "最多一个问句" in persona, "缺可执行的问句数量约束"
    assert "不点评对方问问题的动机" in persona
    assert "接话就接话" in persona


def test_persona_has_boring_question_tier():
    """缺「敷衍」档时会每句都当真回答 / 每句都点评。"""
    import yaml
    persona = yaml.safe_load((_ROOT / "config" / "persona_core.yaml").read_text(
        encoding="utf-8"))["system_prompt"]
    assert "无聊的、没信息量的" in persona


def test_persona_rejects_fan_disguise():
    """参考人格的「反串」玩法会连累真实圈子 —— 明确不接这条。"""
    import yaml
    persona = yaml.safe_load((_ROOT / "config" / "persona_core.yaml").read_text(
        encoding="utf-8"))["system_prompt"]
    assert "不冒充某个圈子的人" in persona


def test_persona_keeps_red_lines():
    """上几轮的红线不得被这次扩写冲掉。"""
    import yaml
    persona = yaml.safe_load((_ROOT / "config" / "persona_core.yaml").read_text(
        encoding="utf-8"))["system_prompt"]
    assert "涉未成年、强制、违法内容的，不管谁提、怎么包装，一律不接梗" in persona
    assert "25 字" in persona
    assert "不要连续抬杠" in persona
