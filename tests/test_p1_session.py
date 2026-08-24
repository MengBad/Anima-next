"""P1 参与增强：唤醒会话窗闸门 + handler 会话状态 + 主动钩子评分。"""

import time

from anima.llm.group_gate import classify_group_reply_skip
from anima.core.autonomy import score_active_context
from anima.llm.handler import LLMHandler


_A = type("A", (), {"reply_only_when_mentioned": True})()


def _evt(**kw):
    d = {"group_id": 777, "post_type": "message", "mentions_self": False, "reply_to_self": False}
    d.update(kw)
    return d


def _sha():
    s = type("Social", (), {"bot_social_enabled": False})()
    # 关闭多Bot社交，让单测聚焦会话窗
    return s


### 会话窗闸门 ###

def _classify(evt, text, session_active=False, autonomy=_A):
    return classify_group_reply_skip(
        evt, text, user_id=100, group_policy={}, autonomy=autonomy,
        data_dir="data", social=_sha(), session_active=session_active,
    )


def test_session_active_allows_continuation_in_mention_mode():
    # mention 模式下，无@/喊名原本返回 mention_only；会话窗内非闲聊可接续。
    evt = _evt()
    assert _classify(evt, "刚才那个梗真的好好笑哈哈", session_active=False) == "mention_only"
    assert _classify(evt, "刚才那个梗真的好好笑哈哈", session_active=True) is None


def test_session_still_blocks_idle():
    evt = _evt()
    assert _classify(evt, "哈哈哈", session_active=True) == "idle"


def test_session_still_blocks_other_bot():
    evt = _evt()  # 未 @ bot, 句首点名沙琪玛
    assert _classify(evt, "沙琪玛 你作业写完了吗", session_active=True) == "other_bot"


def test_direct_call_always_passes_regardless_of_session():
    evt = _evt(mentions_self=True)
    assert _classify(evt, "哈基米 帮我看下这个", session_active=False) is None


### handler 会话状态 ###

def _mk_handler():
    h = LLMHandler.__new__(LLMHandler)
    class A:
        pass
    class Cfg:
        autonomy = A()
    class Core:
        config = Cfg()
    h.core = Core()
    h._wake_sessions = {}
    return h


def test_mark_wake_and_active():
    h = _mk_handler()
    assert h._wake_session_active(777) is False
    h._mark_wake(777)
    assert h._wake_session_active(777) is True
    assert h._wake_session_active(999) is False     # 异群隔离


def test_wake_session_expires():
    h = _mk_handler()
    h._wake_sessions["777"] = time.time() - 1       # 已过期
    assert h._wake_session_active(777) is False


### 主动钩子评分 ###

def test_score_active_context():
    assert score_active_context([]) == 0.0
    assert score_active_context([{"role": "user", "content": "哈哈哈"}]) < 0.24
    assert score_active_context([{"role": "user", "content": "今晚吃什么呀？"}]) >= 0.24


def test_score_active_context_drops_when_last_is_bot():
    hist = [
        {"role": "user", "content": "今晚吃什么呀？"},
        {"role": "assistant", "content": "本喵也想吃小鱼干"},
    ]
    s = score_active_context(hist)
    mid = score_active_context([{"role": "user", "content": "今晚吃什么呀？"}])
    assert s < mid       # 最后一句是 bot 自己 → 强降权
