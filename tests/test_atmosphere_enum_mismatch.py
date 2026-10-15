"""X3 回归：氛围枚举引用不得失配。

2026-10-14 白天复核发现：`cognition.py` 里有三处引用 ``"tense"`` / ``"quiet"``，
但 `AtmosphereStyle` 枚举**只有 formal / casual / humorous / neutral 四个值** ⇒
那三处是**结构性不可达**的死分支：

  - L847/849  群氛围 → 情绪感染：`failed` / `boring` 永不触发
  - L1165     灵魂 `_group_atm` 负向分量（-0.3）永远取不到
               ⇒「群氛围压抑会拉低灵魂状态」这条设计**从未生效**

白天实测 43 次氛围分布：casual 22 / formal 13 / humorous 8 ⇒ **81% 落在无分支空隙**。

本测试是**引用失配守卫**：今后若有人给枚举加值或改分支，会立刻红。
红线：本文件**不新增任何情绪映射**（formal/casual 该映射成什么属产品决策，须 owner 拍）。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COG = ROOT / "anima" / "pipeline" / "stages" / "cognition.py"
PROFILES = ROOT / "anima" / "crossgroup" / "atmosphere" / "profiles.py"


def _enum_values() -> set[str]:
    text = PROFILES.read_text(encoding="utf-8")
    m = re.search(r"class AtmosphereStyle\(str, Enum\):(.*?)(\n\n|\nclass |\Z)", text, re.S)
    assert m, "找不到 AtmosphereStyle 枚举定义"
    return set(re.findall(r'=\s*"([a-z]+)"', m.group(1)))


def test_enum_has_no_tense_quiet():
    """坐实根因：tense / quiet 从不是合法枚举值。"""
    vals = _enum_values()
    print("枚举值 = %s" % sorted(vals))
    assert "tense" not in vals and "quiet" not in vals, (
        "枚举已含 tense/quiet ⇒ 本测试的根因前提失效，需重新评估（而不是删测试）"
    )


def test_cognition_no_reference_to_absent_enum():
    """cognition.py 中所有氛围字面量比较必须落在合法枚举值内。"""
    text = COG.read_text(encoding="utf-8")
    vals = _enum_values()

    # 形如 ctx.atmosphere_style == "xxx" / _atm_style == "xxx"
    lits = set(re.findall(r'_atm(?:osphere)?_style\s*==\s*"([a-z]+)"', text))
    bad = sorted(lits - vals)

    assert not bad, (
        "cognition.py 引用了不存在的氛围枚举值：%s（合法值 %s）\n"
        "这些分支结构性不可达 ⇒ 死代码。10-14 实测因此导致 failed/boring 恒 0、"
        "_group_atm 负向分量从未生效。"
        % (bad, sorted(vals))
    )


def test_mood_branch_uses_real_enum():
    """群氛围 → 情绪感染分支引用的风格必须真实存在。"""
    text = COG.read_text(encoding="utf-8")
    vals = _enum_values()

    seg = text[text.find("# I1: 群氛围 → 情绪感染"):]
    seg = seg[:seg.find("except Exception")]
    lits = set(re.findall(r'==\s*"([a-z]+)"', seg))
    bad = sorted(lits - vals)
    assert not bad, "I1 情绪分支引用了不存在的氛围值：%s" % bad


def test_group_atm_branch_uses_real_enum():
    """灵魂 _group_atm 分支引用的风格必须真实存在。"""
    text = COG.read_text(encoding="utf-8")
    vals = _enum_values()

    seg = text[text.find("_group_atm = 0.0"):]
    seg = seg[:seg.find("_soul.update(")]
    lits = set(re.findall(r'==\s*"([a-z]+)"', seg))
    bad = sorted(lits - vals)
    assert not bad, "_group_atm 分支引用了不存在的氛围值：%s" % bad


def test_humorous_branch_preserved():
    """唯一真实生效的正向分支不得被误删。"""
    text = COG.read_text(encoding="utf-8")
    assert '_atm_style == "humorous"' in text and 'trigger("interesting"' in text, (
        "humorous → interesting 分支丢失（这是唯一真实生效的氛围情绪映射）"
    )


def test_group_atm_positive_preserved():
    text = COG.read_text(encoding="utf-8")
    assert 'ctx.atmosphere_style == "humorous"' in text and "_group_atm = 0.4" in text, (
        "humorous → _group_atm=0.4 丢失"
    )
