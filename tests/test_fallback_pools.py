"""兜底/降级话术池守卫（2026-09-19 去模板化；2026-09-21 补分析体出口）。

`react.py` 的三池有自己的守护文件（`test_react_empty_fallback_wording.py`）。
本文件守护另外几处**同一句硬编码在多出口复用**的罐头话术：

| 池 | 文件 | 出口数 |
|---|---|---|
| `_DEGRADE_FALLBACKS` | `pipeline/stages/generation.py` | 3（熔断 / ReAct 异常 / 空回复） |
| `_MECHANICAL_REWRITES` | `pipeline/stages/guardrail.py` | 2（机械短语 / 「作为…AI」变体） |
| `_ANALYSIS_ESSAY_FALLBACKS` | `pipeline/stages/guardrail.py` | 1（破角色分析体；09-21 由写死单句改池化） |

为什么值得钉：这些字符串**不是模型输出**，是硬编码罐头。同一句在多处出口复用时，
用户侧读到的就是复读（实测 09-18 单句被复用 6 次）。断言分四层：

1. 每句场景无关、不暴露内部机制（护栏改写尤其不能提「被过滤了」）；
2. 每句保持人格口吻；
3. **不得索取原样复述**（"你再说一遍"= 用"没听清"表达"我不接"，最伤观感）；
4. 源码级：出口走 `random.choice(池)`，不再写死单句。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from anima.pipeline.stages.generation import _DEGRADE_FALLBACKS
from anima.pipeline.stages.guardrail import _ANALYSIS_ESSAY_FALLBACKS, _MECHANICAL_REWRITES

_ROOT = Path(__file__).resolve().parents[1]

# 场景绑定词（同 D-19 口径）+ 会暴露内部机制的词
_FORBIDDEN = ("BV", "bv号", "视频链接", "工具", "接口", "API", "模型", "报错", "错误码", "过滤")

# 🔴 生产实测（09-21 12:23:18）：用户求「帮我找点皮物小说」，被回
#   「（本喵刚又跑偏成分析模式了，打住打住）你刚想说啥来着？接着聊～」⇒ 诉求完全没接住。
#   兜底可以"换个话头"，但不能假装没听见。
_REPEAT_ASKS = ("再说一遍", "重发", "再说一次", "重复一遍", "想说啥来着")

_ALL = (*_DEGRADE_FALLBACKS, *_MECHANICAL_REWRITES, *_ANALYSIS_ESSAY_FALLBACKS)


@pytest.mark.parametrize("text", _ALL)
def test_pool_wording_is_scene_agnostic(text):
    hits = [w for w in _FORBIDDEN if w in text]
    assert not hits, f"兜底话术含场景/机制词 {hits}: {text!r}"


@pytest.mark.parametrize("text", _ALL)
def test_pool_wording_keeps_persona_voice(text):
    assert "本喵" in text, f"丢了人格口吻: {text!r}"


@pytest.mark.parametrize("text", _ALL)
def test_pool_wording_does_not_ask_for_verbatim_repeat(text):
    hits = [w for w in _REPEAT_ASKS if w in text]
    assert not hits, f"兜底话术仍在索取原样复述 {hits}: {text!r}"


def test_pools_have_multiple_variants():
    for name, pool in (("降级", _DEGRADE_FALLBACKS),
                       ("机械改写", _MECHANICAL_REWRITES),
                       ("分析体兜底", _ANALYSIS_ESSAY_FALLBACKS)):
        assert len(set(pool)) >= 2, f"[{name}] 池内不足 2 句变体: {pool!r}"
        assert len(set(pool)) == len(pool), f"[{name}] 池内有重复句: {pool!r}"


def test_no_hardcoded_single_line_reply_remains():
    """源码级：出口必须池化，不得再写死同一句。"""
    gen = (_ROOT / "anima" / "pipeline" / "stages" / "generation.py").read_text(encoding="utf-8-sig")
    gr = (_ROOT / "anima" / "pipeline" / "stages" / "guardrail.py").read_text(encoding="utf-8-sig")
    assert 'ctx.reply = "本喵刚刚脑子卡了一下，等会儿再跟你说嘛…"' not in gen
    assert 'ctx.reply = "啊这，脑子卡了一下…"' not in gr
    # ⚠️ 源码断言必须**剥注释** —— 解释这条缺陷的注释里正好含该措辞（会假红）
    gr_code = "\n".join(ln.split("#")[0] for ln in gr.splitlines())
    assert "你刚想说啥来着" not in gr_code, "分析体兜底又写回了索取复述的句式"
    assert gen.count("random.choice(_DEGRADE_FALLBACKS)") == 3, "generation 三处出口都应池化"
    assert gr.count("random.choice(_MECHANICAL_REWRITES)") == 2, "guardrail 两处出口都应池化"
    assert gr.count("random.choice(_ANALYSIS_ESSAY_FALLBACKS)") == 1, "分析体出口应池化"
