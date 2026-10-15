"""R1/R2/R3 回归：出站内部标记泄漏 + 工具描述示例词 + 富媒体静默埋点。

来源：2026-10-15 出入站审核（`reports/2026-10-15_inout-audit-subcall-leak.md`）。

D1（P0）出站正文泄漏 `<<<subcall:…>>>`，24h 内 7/98 = 7.1%，全部骂战场景、
query 值恒为工具描述里的第一个示例词。根因三环：
  ① 工具描述给了可照搬的示例词（sticker.py 共 5 处）
  ② prompt 用 `<<<用户消息开始>>>` 形态包裹用户数据 ⇒ 模型习得该形态
  ③ 出站清洗只覆盖 `[哈基米]:` 类说话人标签，**不覆盖** `<<<…>>>`
⇒ 模型即兴写出幻觉工具调用串，原样进群。

D2（P1）纯富媒体消息走 `memory.py:146` SKIP_REPLY，**无日志/无计数/无出站**。

红线：本修复只做「剥泄漏标记 + 去示例词 + 加埋点」，
     **不新增/改变任何话术内容**，不改产品行为。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GEN = ROOT / "anima" / "pipeline" / "stages" / "generation.py"
STICKER = ROOT / "anima" / "tools" / "builtin" / "sticker.py"
MEM = ROOT / "anima" / "pipeline" / "stages" / "memory.py"


# ═══════════════════════════════════════════════════════════════
# R1：出站清洗必须剥掉内部标记泄漏
# ═══════════════════════════════════════════════════════════════

def _strip(text: str) -> str:
    from anima.pipeline.stages.generation import _strip_speaker_labels
    return _strip_speaker_labels(text)


@pytest.mark.parametrize("raw,expected", [
    # 审核实证的两种形态（含一个少括号的变体）
    ("<<<subcall:pick_sticker{query:无语}>>>[哈基米]: 滚！你才叫哈基米断腿-1！",
     "滚！你才叫哈基米断腿-1！"),
    ("<<<subcall:pick_sticker{query:无语}>>>都说了本喵叫哈基米不叫沙琪玛！",
     "都说了本喵叫哈基米不叫沙琪玛！"),
    ("<<<subcall:pick_sticker{query:无语}>>>本喵好得很，少在这儿胡言乱语的喵！",
     "本喵好得很，少在这儿胡言乱语的喵！"),
    # 泛化：任意 <<<…>>> 形态（含少括号变体）
    ("<<<anything at all>>>正文", "正文"),
    ("<<subcall:x{query:y}>>>正文", "正文"),
    # 剥完内部标记后，行首的说话人标签必须也能剥掉（顺序依赖）
    ("<<<subcall:pick_sticker{query:无语}>>>[哈基米]: 禁言个头啊！", "禁言个头啊！"),
    # 多处出现
    ("<<<a>>>前<<<b>>>后", "前后"),
])
def test_internal_marker_stripped(raw, expected):
    got = _strip(raw)
    assert got == expected, "raw=%r\n got=%r\n expected=%r" % (raw, got, expected)


@pytest.mark.parametrize("text", [
    "普通回复，没有任何标记。",
    "[图片]",                      # NapCat 层占位，不能被误剥
    "他说<<<不是标记因为没闭合",      # 未闭合 ⇒ 不剥
    "本喵今天很开心喵！",
])
def test_no_false_positive(text):
    """不得误伤正常文本与未闭合形态。"""
    assert _strip(text) == text, "误伤了 %r" % text


def test_original_label_behavior_preserved():
    """原有说话人标签剥离行为不得回归。"""
    assert _strip("[哈基米]: 你好") == "你好"
    assert _strip("[你]: 你好") == "你好"
    assert _strip("哈基米: 你好") == "你好"
    # `text:` 前缀只在后接说话人标签时才被剥（原正则如此，非本次改）
    assert _strip("text: [哈基米]: 你好") == "你好"
    assert _strip("text: 你好") == "text: 你好"


def test_marker_regex_exists():
    """必须有专门的内部标记正则（不允许只在测试里硬编码）。"""
    src = GEN.read_text(encoding="utf-8")
    assert "_INTERNAL_MARKER_RE" in src, (
        "generation.py 缺 _INTERNAL_MARKER_RE ⇒ 没有可维护的清洗规则"
    )


def test_marker_stripped_before_label():
    """结构性守卫：内部标记必须在说话人标签**之前**剥（否则 `[哈基米]:` 不在行首）。"""
    src = GEN.read_text(encoding="utf-8")
    body = src[src.index("def _strip_speaker_labels"):]
    body = body[:body.index("\ndef ", 10)]
    mi = body.find("_INTERNAL_MARKER_RE")
    li = body.find("_REPLY_LABEL_RE")
    assert mi >= 0 and li >= 0 and mi < li, (
        "内部标记清洗必须排在说话人标签之前（mi=%d li=%d）" % (mi, li)
    )


# ═══════════════════════════════════════════════════════════════
# R2：工具描述不得再给可照搬的示例词
# ═══════════════════════════════════════════════════════════════

# 曾经的示例词（sticker.py 5 处）。写在这里供判据用；
# 生产源码（含注释）不得再出现这些词（U2 同款教训）。
_SAMPLE_WORDS = ["无语", "狗头", "开心", "赞同"]


def _sticker_src() -> str:
    return STICKER.read_text(encoding="utf-8")


@pytest.mark.parametrize("word", _SAMPLE_WORDS)
def test_sticker_no_sample_words(word):
    """sticker.py 全文（含注释）不得再出现可照搬示例词。"""
    src = _sticker_src()
    assert word not in src, (
        "sticker.py 仍含示例词 %r —— 模型会照搬（D1 根因①）" % word
    )


def test_sticker_keeps_intent_description():
    """必须保留「用中文描述情绪」的意图描述（只删例子，不删指令）。"""
    src = _sticker_src()
    assert "中文描述" in src, "把意图描述也删了 ⇒ 模型不知道该传什么"
    assert "query" in src, "query 参数说明丢失"


def test_sticker_tool_still_registered():
    """三个表情工具必须仍在（不许因改描述而删工具）。"""
    src = _sticker_src()
    for name in ("pick_sticker", "random_sticker"):
        assert '"%s"' % name in src, "%s 工具丢失" % name


# ═══════════════════════════════════════════════════════════════
# R3：富媒体静默必须有可观测埋点
# ═══════════════════════════════════════════════════════════════

def test_media_silent_anchor_exists():
    """memory.py 的 SKIP_REPLY 前必须有埋点。"""
    src = MEM.read_text(encoding="utf-8")
    assert "[富媒体静默]" in src, (
        "memory.py 缺 [富媒体静默] 锚点 ⇒ D2 永远无法量化（无日志/无计数）"
    )


def test_anchor_before_skip_reply():
    """埋点必须排在 return SKIP_REPLY **之前**。"""
    src = MEM.read_text(encoding="utf-8")
    lines = src.splitlines()
    ai = next(i for i, ln in enumerate(lines) if "[富媒体静默]" in ln)
    si = next(i for i, ln in enumerate(lines)
              if "return StageResult.SKIP_REPLY" in ln)
    assert ai < si, "埋点(L%d) 必须在 SKIP_REPLY(L%d) 之前" % (ai + 1, si + 1)


def test_skip_reply_behavior_unchanged():
    """行为不变：仍返回 SKIP_REPLY（不因为加埋点就改成回复）。"""
    src = MEM.read_text(encoding="utf-8")
    assert "return StageResult.SKIP_REPLY" in src, "SKIP_REPLY 分支被删了"


# ═══════════════════════════════════════════════════════════════
# 回归：D1 的判据（出站正文不得再含内部标记）
# ═══════════════════════════════════════════════════════════════

def test_no_internal_marker_in_outbound():
    """端到端判据：经 _strip_speaker_labels 后不得残留 `<<<` 或 `>>>`。"""
    samples = [
        "<<<subcall:pick_sticker{query:无语}>>>[哈基米]: 滚！",
        "<<<x>>>[哈基米]: 你才十八码！",
        "<<subcall:y>>>本喵好得很",
    ]
    for s in samples:
        out = _strip(s)
        assert "<<<" not in out and ">>>" not in out, "仍残留内部标记：%r" % out
        assert "subcall" not in out, "仍残留 subcall 字样：%r" % out
