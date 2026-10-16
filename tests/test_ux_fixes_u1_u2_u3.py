"""U1/U2/U3 RED 测试 —— 2026-10-13 使用者视角排查产出的三个修复。

## 修什么（全部来自 `reports/2026-10-13_ux-audit-user-perspective.md`）

**U1（P1）兜底话术「引用 + 道歉」形态**
7 天 12 次兜底、**100% 带 `[回复消息]` 引用块** ⇒ 使用者看到的是
「它引用我的原话，然后自称没接住」；实测还有答非所问
（问「你复活了」→ 答「我打了个磕巴」）。
根因：两个池（`react.py:_NO_TOOL_FALLBACKS` / `generation.py:_DEGRADE_FALLBACKS`）
的出口都在**正常回复路径**上，`_build_reply_segments` 无条件前插 reply 段。
修法：兜底文本**不挂引用块**（严格等值判定，不用 `in`）。

**U2（P1）prompt 把「示例句」直接发给模型 ⇒ 句式复读**
`cognition.py` 的 `unusual_hour` / `long_lost_reunion` 两个片段把可照搬的
例句写在 prompt 里（如「可以随口提一句（'诶，今天怎么这个点还在'）」）⇒
7 天实测该句复读 5 次（不同日子/不同用户）。
修法：只给**意图描述**，不给具体句子。

**U3（P2）转发正文展开不可观测**
`[合并转发内容]` 只进 `ctx.text`、不入 journal ⇒ 拿它 grep 恒得 0
（我因此差点误判成「从没成功过」）。修法：补一条 `[转发展开]` 埋点。

## 本文件钉住的行为

1. 兜底文本（两个池 + tool-no-result 那句）出站**不得带 reply 段**；
2. 正常回复**仍带** reply 段（回归保护，不许把引用功能砍掉）；
3. 兜底判定是**严格等值**——部分相似句不得误剥；
4. `cognition.py` 的这两个片段**不得再含可直接照搬的例句**（源码守卫）；
5. `_extract_forward_text` 必须打 `[转发展开]` 埋点（成功与失败都要可观测）。
"""
from __future__ import annotations

import asyncio
import logging
import re
import types

import pytest

from anima.agent.react import _NO_TOOL_FALLBACKS
from anima.llm.handler import (
    LLMHandler,
    _build_reply_segments,
    _is_degrade_fallback,
)
from anima.pipeline.stages.generation import (
    _DEGRADE_FALLBACKS,
    _TOOL_NO_RESULT_FALLBACK,
)

# 🔴 池子**从源码 import**，不手抄 —— 手抄会抄错（10-13 我就把
# 「没答上来」抄成「没答不上来」，导致严格等值判定静默漏一句）。
POOL_A = tuple(_NO_TOOL_FALLBACKS)
POOL_B = tuple(_DEGRADE_FALLBACKS)
TOOL_NO_RESULT = _TOOL_NO_RESULT_FALLBACK
ALL_FALLBACKS = POOL_A + POOL_B + (TOOL_NO_RESULT,)


class _Ctx:
    def __init__(self, reply: str):
        self.reply = reply
        self.generated_image_paths = []
        self.sticker_to_send = None
        self.meta = {}


def _core_stub():
    """最小 core 桩：只满足 `_build_reply_segments` 真正摸到的四个属性。"""
    return types.SimpleNamespace(
        config=types.SimpleNamespace(
            sticker=types.SimpleNamespace(
                send_allowlist=[], send_blocklist=[],
                always_atmosphere=[], send_probability=0.0,
                send_by_emotion=False, send_cooldown_sec=0.0,
            ),
            persistence=types.SimpleNamespace(data_dir="data"),
            reply_split=None,
        ),
        reply_splitter=None,
    )


def _seg_types(segs: list) -> list:
    return [s.get("type") for s in segs if isinstance(s, dict)]


@pytest.mark.parametrize("text", ALL_FALLBACKS)
def test_degrade_fallback_never_carries_quote(text):
    """🔴 U1 核心判据：任一兜底句出站都不得带 reply 段。"""
    assert _is_degrade_fallback(text) is True
    segs = _build_reply_segments(
        _Ctx(text), "group", user_id=1, group_id=2, core=_core_stub(),
        message_id="99",
    )
    assert "reply" not in _seg_types(segs), (
        "兜底话术仍挂引用块 ⇒ 用户会看到「引用我的原话 + 自称没接住」：%r" % (segs,)
    )
    # 文本本身必须照发（只剥引用，不吞回复）
    texts = [s.get("data", {}).get("text") for s in segs if s.get("type") == "text"]
    assert texts == [text]


def test_normal_reply_still_carries_quote():
    """回归保护：正常回复必须仍带 reply 段（不许把引用功能砍掉）。"""
    segs = _build_reply_segments(
        _Ctx("今天天气不错喵"), "group", user_id=1, group_id=2,
        core=_core_stub(), message_id="99",
    )
    assert "reply" in _seg_types(segs), segs


def test_degrade_detection_is_strict_equality():
    """严格等值：部分相似句不得误剥（`in` 会把「含尾巴」误判成整句）。"""
    for near in (
        "本喵这边刚卡了一下喵",                                  # 前缀
        "本喵这边刚卡了一下喵，这条没接住，不赖你。喵？",            # 带尾巴
        "我刚卡了一下",                                          # 无关句
        "",
        "  本喵刚打了个磕巴，缓一下再说喵…  ",                    # 两侧空白应被容忍
    ):
        want = near.strip() in ALL_FALLBACKS
        assert _is_degrade_fallback(near) is want, near


def test_private_chat_unaffected():
    """私聊本就不挂 reply 段 ⇒ 行为不变（回归保护）。"""
    segs = _build_reply_segments(
        _Ctx("在呢喵"), "private", user_id=1, group_id=None,
        core=_core_stub(), message_id="99",
    )
    assert "reply" not in _seg_types(segs)


# ---------------------------------------------------------------- U2

_SRC = None


def _cognition_src() -> str:
    global _SRC
    if _SRC is None:
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        _SRC = (root / "anima" / "pipeline" / "stages" / "cognition.py").read_text(
            encoding="utf-8")
    return _SRC


def test_no_quotable_example_sentence_in_prompt_fragments():
    """🔴 U2 核心判据：prompt 片段不得再含「可直接照搬的例句」。

    历史形态：``可以随口提一句（'<一整句口头语>'）`` —— 引号里就是一句
    完整话，模型会直接照搬（7 天实测某句深夜问候复读 5 次）。
    修法 = 只给意图描述。

    判据收紧到「**引号内是完整口语句**」：必须含 CJK、且以语气词/中文标点
    收尾。否则 `("vector_recall")` / `("Sylann 观测失败，已隔离")` 这类
    **正常代码**会被误判（第一版正则就踩了这个，报了几十个假警报）。
    与 `test_react_placeholder_escape.py` 同风格：**连同注释一起扫**。
    """
    src = _cognition_src()
    quoted = re.findall(r"[（(]\s*['\"]([^'\"\n]{6,})['\"]\s*[）)]", src)
    bad = [q for q in quoted
           if re.search(r"[\u4e00-\u9fff]", q)
           and re.search(r"[。！？…~喵呀吧呢]$", q)]
    assert not bad, "prompt 里仍有可照搬的例句：%r" % (bad,)


def test_unusual_hour_fragment_states_intent():
    """片段必须仍在（不许把功能删掉），且表达的是意图而非例句。"""
    src = _cognition_src()
    assert "unusual_hour" in src, "unusual_hour 片段被删了"
    assert "这个点还在" not in src, (
        "仍写着深夜问候的示例句 —— 它会变成模板句复读"
    )
    assert "好久没看到你了" not in src, "久违重逢片段仍写着示例句"


# ---- U2 扩面：persona 文件从未被扫过（2026-10-16 补）----

def _persona_src() -> str:
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    return (root / "config" / "persona_core.yaml").read_text(encoding="utf-8")


@pytest.mark.parametrize("cliche", ["大半夜的", "大清早的", "大中午的", "大晚上的"])
def test_persona_has_no_mood_cliche(cliche):
    """🔴 U2 同病扩面：persona 不得把作息词列举进 prompt。

    历史形态（`persona_core.yaml:33`，2026-09-20 加入，存活 26 天）::

        别把作息当固定开场：「大半夜的 / 大清早的 / 大中午的」这类起手偶尔用就好…

    为了说明「**别用**什么」而把三个具体词写进 prompt —— 与 U2 完全同病原：
    ``cognition.py`` 的注释已记下同一条教训（引号里的例句会被模型原样搬进出站）。
    实测（10-16，24h 8 条作息起手式）：**7 条是「大清早」**，其中 13:22 / 15:48 /
    16:15 / 16:52 四条与真实时段不符 —— 是「抓 prompt 里的词」而非「读时间选表达」。

    ⇒ 判据收紧为**字面禁词**：这三个词属「可自由替换的表达」，写成描述性说法即可，
      不需要出现在 prompt 里。这样即便模型本就会说时段词，也不再是我们教它的。
    """
    src = _persona_src()
    assert cliche not in src, (
        "persona 仍列举作息词 %r ⇒ 模型会照搬（U2 同病）。"
        "改成只给意图、不举例。" % cliche
    )


# ---------------------------------------------------------------- U3

class _FakeChannel:
    def __init__(self, resp):
        self._resp = resp

    async def get_forward_msg(self, fid, retries=1):
        return self._resp


def _stub_self(channel):
    return types.SimpleNamespace(core=types.SimpleNamespace(
        gateway=types.SimpleNamespace(channel=channel)))


def test_forward_expansion_is_observable(caplog):
    """🔴 U3 核心判据：拉到正文必须打 `[转发展开]` 埋点（此前完全不可观测）。"""
    resp = {"content": {"messages": [
        {"message": [{"type": "text", "data": {"text": "甲：明天三点集合"}}]},
    ]}}
    ev = {"forward_ids": ["abc123"]}
    with caplog.at_level(logging.INFO, logger="anima.llm.handler"):
        asyncio.run(LLMHandler._extract_forward_text(_stub_self(_FakeChannel(resp)), ev))
    assert "[转发展开]" in caplog.text, "转发展开没有埋点 ⇒ 这条链路永远不可观测"


def test_forward_expansion_failure_is_observable(caplog):
    """拉不到正文也要留痕（区分「空转发」与「NapCat 没返回」）。"""
    ev = {"forward_ids": ["abc123"]}
    with caplog.at_level(logging.INFO, logger="anima.llm.handler"):
        asyncio.run(LLMHandler._extract_forward_text(
            _stub_self(_FakeChannel({"content": {"messages": []}})), ev))
    assert "[转发展开]" in caplog.text
