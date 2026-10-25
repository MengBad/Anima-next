# -*- coding: utf-8 -*-
"""人格「示范」的写法纪律（2026-10-25）。

## 背景：一个真实的两难

2026-10-25 17:20 第一条经过全部改动后的出站（用户只发了一个「喵」）：

```
哼，学我叫什么叫，爪子痒了是不是喵~ 今天怎么这么有空跑来戳我呀，罐头准备好了没~
                                                              49 字 / 2 句 / 3 个反问
```

长度降了（中位 84 → 49），但**语气还是盘问**：4 个分句里 3 个在质问对方动机。

对照另一份成熟人格设定（DeepSeek 鲸鱼娘），它的 7 条示例：

```
平均 11.0 字 · 中位 9 · **没有一条是反问句**
```

⇒ 它是用**正面示范**定住「短 + 陈述」这个形状的。

## 但正面示范撞上本项目的一条铁律

`test_ux_fixes_u1_u2_u3.py` 记录（U2 定律，2026-10-16 实测）：

> 人格里写「别用『大半夜的/大清早的/大中午的』」——
> 结果 24h 内 8 条作息起手式里 **7 条是「大清早」**，其中 4 条与真实时段不符。
> **prompt 里出现的原句，模型会原样搬进出站。**

## 本轮的解法：示范「分寸」，不示范「台词」

新增的「说话的分寸」节写成**场景 → 该怎么做**：

```
- 对方说一句很短的话（打招呼、一个语气词、发个表情）
  → 接一句就够，不用追问、不用盘问为什么
```

**整节零引号、零具体台词。** 它教的是「有反应、一句、不反问」这个**形状**，
而不是给句子去抄。

## 本轮踩到的坑（两次，同一天）

1. 第一版写了「不要反问一句『你问这个干嘛』再回答」
   —— 引用了一个**假想的负面台词**，仍然会教坏模型。
2. 第一版写了「（『你怎么突然问这个』『你爪子痒了是不是』『今天怎么这么有空』）」
   —— 直接抄了那条真实出站的片段。**这是把 bug 写进 prompt。**

## 判据

1. 示范节里**不得出现任何引号包裹的正文台词**；
2. 人格里**不得出现任何一条真实出站的原话片段**（尤其是不想要的那种）；
3. 「最多一个问句」这类**数字约束**必须保留（禁令不可执行，数字可执行）；
4. 长度约束与「展开」的触发条件不得回退。
"""
from __future__ import annotations

import pathlib
import re

import pytest
import yaml

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_PERSONA = _ROOT / "config" / "persona_core.yaml"


@pytest.fixture(scope="module")
def persona() -> str:
    return yaml.safe_load(_PERSONA.read_text(encoding="utf-8"))["system_prompt"]


def _demo_section(persona: str) -> str:
    """取「说话的分寸（示范）」节正文。

    ⚠️ 不能用 `\\n  ## ` 切：yaml 里后续标题是 `  ## xxx`（两空格缩进），
    但换行符在 yaml 解析后可能被折叠。⇒ 用**下一个 `## ` 出现位置**切，
    并把模板占位符也一起截掉。
    """
    marker = "## 说话的分寸"
    assert marker in persona, "示范节被删了（它是这一轮解决「盘问」的关键）"
    tail = persona.split(marker, 1)[1]
    # 到下一个二级标题或模板占位符为止
    for stop in ("## ", "{time_context}"):
        idx = tail.find(stop)
        if idx > 0:
            tail = tail[:idx]
    return tail


# ── 1. 示范节：只教分寸，不给台词 ───────────────────────────────────

def test_demo_section_has_no_quotable_lines(persona):
    """🔴 核心判据：示范节里**不得有任何可照抄的台词**。

    一旦出现「你：那完了」这种，模型就会原样搬（U2 定律已实证）。
    """
    section = _demo_section(persona)
    quoted = re.findall(r"「([^」]{2,})」", section)
    assert not quoted, (
        f"示范节里出现了可照抄的台词 {quoted} —— 改为描述「分寸」而非给句子")


def test_demo_section_has_no_you_colon_pattern(persona):
    """禁止 `你：xxx` / `群友A：xxx` 这种对话示例格式。"""
    section = _demo_section(persona)
    assert not re.search(r"(你|群友|对方)[A-Za-z]?\s*[：:]", section), \
        "示范节出现了对话示例格式（会变成模板）"


def test_demo_section_teaches_three_scenarios(persona):
    """至少要覆盖三种会触发「盘问」的场面。

    ⚠️ 判据用**关键字**而非整句：yaml 会把换行折叠成空格，
    整句匹配会因为空白差异假失败（本轮实测踩过）。
    """
    section = _demo_section(persona)
    for kw in ("很短的话", "分享一件事", "你知道的事"):
        assert kw in section, f"缺场景关键字「{kw}」"


def test_demo_section_states_it_is_not_for_copying(persona):
    """必须自己在 prompt 里声明「不要照抄」——否则模型会把示范当模板。"""
    section = _demo_section(persona)
    assert "不是给你照抄" in section or "照抄就变成复读" in section


def test_demo_section_names_the_shared_shape(persona):
    """示范的共同特征必须**写出来**（短 / 陈述 / 无反问 / 不点评）。"""
    section = _demo_section(persona)
    assert "短" in section
    assert "陈述" in section
    assert "没有反问" in section


# ── 2. 🔴 不得把真实出站原话（尤其是不想要的）写进 prompt ────────────

_LEAKED_FROM_PRODUCTION = (
    "爪子痒了",          # 17:20 那条出站的真实片段
    "怎么这么有空",
    "学我叫什么叫",
    "罐头准备好了没",
    "你问这个干嘛",      # 第一版写进去的假想负面台词
    "你发这个干嘛",
)


@pytest.mark.parametrize("frag", _LEAKED_FROM_PRODUCTION)
def test_no_production_output_leaked_into_persona(persona, frag):
    """🔴 把「不想要的那句话」写进 prompt = 教它这么说。

    本项目踩过两次（2026-10-16 的作息词、2026-10-25 的盘问句）。
    """
    assert frag not in persona, (
        f"人格里混进了 {frag!r} —— 那是真实出站的片段，"
        "写进 prompt 等于教它重复")


# ── 3. 数字约束保留（禁令不可执行，数字可执行）──────────────────────

def test_one_question_mark_limit_is_numeric(persona):
    """「不要反问」不可执行；「最多一个问句」可执行。"""
    assert "最多一个问句" in persona


def test_length_constraints_intact(persona):
    """长度与句数约束不得被这轮改动冲掉。"""
    assert "25 字" in persona
    assert "默认一句，最多两句" in persona


def test_expansion_trigger_still_explicit(persona):
    """「展开」仍必须由对方明确触发（上一轮关掉的后门不得重开）。"""
    assert "只发生在对方明确说" in persona
    assert "任务类该详细就详细" not in persona


# ── 4. 人格总长有界（防无限膨胀挤掉别的片段）────────────────────────

def test_persona_length_is_bounded(persona):
    """人格越长，越挤占 ContextComposer 的 system 预算。

    生产 system 预算 16000 token，人格占比过大时会挤掉记忆/关系等片段
    （实测 dropped= 一直为空，但那是 11k/16k 的余量）。
    """
    assert len(persona) < 5000, (
        f"人格已 {len(persona)} 字 —— 超过 5000 会显著挤压其它注入片段的预算")


# ── 5. 反向可归因：这轮新增的三条约束必须真的在 ──────────────────────

def test_no_motive_questioning_rule_exists(persona):
    assert "不点评对方问问题的动机" in persona


def test_talk_back_rule_exists(persona):
    assert "接话就接话" in persona
