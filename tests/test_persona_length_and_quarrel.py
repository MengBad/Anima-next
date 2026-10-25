"""人格正本的「长度」与「不连环抬杠」约束（2026-10-25）。

## 生产实证（部署后 4.5 小时，10 条真实回复）

```
① 自称漂移: 0 次      ✅ 已修
② 贬称: 0 次          ✅ 已修
③ 元认知泄露: 0 次    ✅ 已修
④ 长度: 平均 70 字    ❌ 完全没变（目标 ≤25，达标率仅 10%）
```

**为什么长度没变**：我上轮只在**主动发言**的 prompt 里加了「≤25 字」，
而这 10 条里 **8 条是被动回复**（有人 @ 它）—— 被动回复走人格正本，
而人格正本只写「默认一句话到两句」，**没有数字约束、也没有反例**。

## 另一个问题：变得爱抬杠

```
「讨打是不是喵~」
「你要是蜘蛛侠，本喵就是复仇者联盟总教头了」
「本喵才懒得当那个天天擦屁股的黑眼圈大叔」
「要不是老蚁人…托尼大叔还在家抱着女儿岁月静好呢」
```

**连续 4 条都在怼回去**——对方只是聊电影、分享图，不是挑衅。
既有规则「被冒犯可以短怼一句就收」只约束了「被冒犯」，没约束「日常闲聊」。

## 本测试锁死

1. 人格正本必须有**数字长度约束**（被动回复也受约束）；
2. 必须有**不连环抬杠**的明确规则；
3. 必须有「分享/闲聊时不用较劲」的规则；
4. 上轮的主动发言约束不得被回退。
"""
import pathlib

import pytest

import yaml

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_PERSONA = _ROOT / "config" / "persona_core.yaml"


@pytest.fixture(scope="module")
def persona() -> str:
    return yaml.safe_load(_PERSONA.read_text(encoding="utf-8"))["system_prompt"]


def test_persona_has_numeric_length_limit(persona):
    """🔴 被动回复走人格正本 —— 必须在这里写死长度，不能只写在主动发言里。"""
    assert "25 字" in persona, "人格正本必须有明确的字数上限（被动回复也受约束）"


def test_persona_forbids_chained_quarrel(persona):
    """🔴 生产实证：连续 4 条都在怼回去（对方只是聊电影）。"""
    assert "不要连续抬杠" in persona, "必须禁止连环抬杠"


def test_persona_says_casual_share_needs_no_fight(persona):
    """对方只是分享/闲聊时不用较劲 —— 说一句真实反应就好。"""
    assert "不需要较劲" in persona or "不需要接梗回怼" in persona


def test_persona_still_forbids_wrong_self_alias(persona):
    """上轮的自称禁令不得回退。"""
    for w in ("本大爷", "老子", "老娘", "本座", "朕"):
        assert w in persona, f"必须点名禁止：{w}"


def test_persona_still_forbids_slurs(persona):
    """上轮的贬称禁令不得回退。"""
    for w in ("两脚兽", "铲屎官"):
        assert w in persona, f"必须点名禁止：{w}"


def test_persona_keeps_soft_tone_rule(persona):
    """上轮的语气约束不得回退。"""
    assert "不要居高临下" in persona


def test_active_speech_still_has_length_rule():
    """主动发言的 25 字约束不得被回退（上轮加的）。"""
    import inspect
    from anima.core.autonomy import Heartbeat

    src = inspect.getsource(Heartbeat._speak_locked)
    assert "最多 25 字" in src or "25 字" in src
