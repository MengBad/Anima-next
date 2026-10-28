"""纯标点/纯符号消息不该被回复（2026-10-28）。

## 事故

```
18:53:08  零点知音 @亦萱：你会拿○○蹭抱着的枕头吗    ← 对亦萱的提问
18:54:03  亦萱：？！                                ← 对她的反应，没 @ bot
18:54:12  bot：干嘛突然这种语气看着我……难道被本喵说中了，心虚啦？
18:55:07  零点知音：（引用 bot 这句）                ← 在嘲讽它
18:55:12  诹访子 @亦萱：我帮你把他 at 出来了         ← 群友都看出它插错话
```

bot 把两个标点读成「被说中的反应」，凭空接了一整句。

## 根因：三层短语表全落空

`compact`（去掉标点/空白/符号）是**空串**，而空串不在 `_FAMILIAR_IDLE_PHRASES` 里
⇒ 三个 if 全部不成立 ⇒ 放行。

两个闸门（familiar / session）**都有这个漏洞**，必须都修。

## 为什么之前没被发现

「？」（单问号）在短语表里 ⇒ 被拦；「哦」「嗯」也在 ⇒ 被拦。
只有**多个标点组合**（「？！」「。。。」）会变成空串，
而单标点是被短语表兜住的 —— 漏洞只在组合形态暴露。
"""
from __future__ import annotations

import pytest

from anima.llm.group_gate import _is_session_thread_idle, is_familiar_idle_unaddressed


def _ev(**kw):
    base = {"group_id": 727631526, "post_type": "message", "user_id": 1,
            "at_uids": [], "mentions_self": False, "reply_to_self": False}
    base.update(kw)
    return base


#: 纯标点/纯符号 —— 两个闸门都应该判 idle（= 不回复）
PURE_SYMBOLS = ["？！", "！？", "！", "？", "。。。", "。。", "...", "!!!",
                "……", "~~~", "、、、", "，。", "—"]


@pytest.mark.parametrize("text", PURE_SYMBOLS)
def test_pure_symbols_are_idle_in_both_gates(text):
    """两个闸门都必须拦住纯符号（防单边修复）。"""
    ev = _ev()
    assert is_familiar_idle_unaddressed(ev, text) is True, \
        f"familiar 闸放过了纯符号 {text!r}"
    assert _is_session_thread_idle(ev, text) is True, \
        f"session 闸放过了纯符号 {text!r}"


def test_incident_text_specifically():
    """具体复现事故那三个字符。"""
    t = "？！"
    assert is_familiar_idle_unaddressed(_ev(), t) is True
    assert _is_session_thread_idle(_ev(), t) is True


# ── 反向守卫：真实短句仍要放行 ──────────────────────────────────────

@pytest.mark.parametrize("text", [
    "为什么？",        # 真实短问句
    "今天吃什么",
    "你看了什么视频",
    "在干嘛呢",
])
def test_real_questions_still_pass(text):
    """真实问句不能被「纯符号」判据误伤。"""
    ev = _ev()
    assert is_familiar_idle_unaddressed(ev, text) is False
    assert _is_session_thread_idle(ev, text) is False


def test_phrase_table_still_works():
    """闲聊短语表的行为不变 —— 「在吗」这类仍按表拦。"""
    for t in ("在吗？", "哦", "嗯", "好的", "哈哈"):
        assert is_familiar_idle_unaddressed(_ev(), t) is True
        assert _is_session_thread_idle(_ev(), t) is True


def test_at_other_still_passes_through_symbols():
    """@ 了别人时，即使是纯符号也不由本闸处理（那是「两人对话」场景）。"""
    ev = _ev(at_uids=["123456"])
    assert is_familiar_idle_unaddressed(ev, "？！") is True
    assert _is_session_thread_idle(ev, "？！") is True
