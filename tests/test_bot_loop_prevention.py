"""Task3: 防 Bot 循环机制（Critical）。"""
import time

from anima.cognitive import bot_social

SELF = 2752719822  # 哈基米
GID = 900001


def _reset():
    bot_social._recent.clear()


def test_loop_third_round_detected():
    _reset()
    t = time.time()
    # Bot A @ Bot B @ Bot A（三连，涉及两个 Bot）
    bot_social.observe_inbound(GID, 111, True, True, t)
    bot_social.observe_inbound(GID, 222, True, True, t + 1)
    bot_social.observe_inbound(GID, 111, True, True, t + 2)
    assert bot_social.detect_bot_loop(GID, SELF, max_depth=2) is True


def test_single_bot_ping_not_loop():
    _reset()
    t = time.time()
    # 同一 Bot 反复 @我（由冷却限频，不应被当成「互 @ 循环」）
    bot_social.observe_inbound(GID, 111, True, True, t)
    bot_social.observe_inbound(GID, 111, True, True, t + 1)
    bot_social.observe_inbound(GID, 111, True, True, t + 2)
    assert bot_social.detect_bot_loop(GID, SELF, max_depth=2) is False


def test_human_interrupts_chain():
    _reset()
    t = time.time()
    bot_social.observe_inbound(GID, 111, True, True, t)
    bot_social.observe_inbound(GID, 222, True, True, t + 1)
    bot_social.observe_inbound(GID, 123456, False, False, t + 2)  # 人类插入
    bot_social.observe_inbound(GID, 111, True, True, t + 3)
    assert bot_social.detect_bot_loop(GID, SELF, max_depth=2) is False


def test_classify_returns_loop_on_third(tmp_path):
    _reset()
    t = time.time()
    bot_social.observe_inbound(GID, 111, True, True, t)
    bot_social.observe_inbound(GID, 222, True, True, t + 1)
    bot_social.observe_inbound(GID, 111, True, True, t + 2)
    ev = {
        "user_id": 111, "group_id": GID, "self_id": SELF,
        "mentions_self": True, "sender_is_bot": True, "post_type": "message",
    }
    reason = bot_social.classify_bot_reply_skip(
        ev, "@哈基米 你怎么看", self_id=SELF, now=t + 3,
        data_dir=str(tmp_path), cooldown_sec=30, max_depth=2,
    )
    assert reason == "bot_loop"


def test_cooldown_suppresses_repeat_ping(tmp_path):
    _reset()
    t = time.time()
    ev = {
        "user_id": 111, "group_id": GID, "self_id": SELF,
        "mentions_self": True, "sender_is_bot": True, "post_type": "message",
    }
    # 第一次：放行并记录熟悉度 + 设冷却
    r1 = bot_social.classify_bot_reply_skip(
        ev, "@哈基米 你好", self_id=SELF, now=t,
        data_dir=str(tmp_path), cooldown_sec=30, max_depth=2,
    )
    assert r1 is None
    # 冷却内第二次：被抑制
    r2 = bot_social.classify_bot_reply_skip(
        ev, "@哈基米 你还好吗", self_id=SELF, now=t + 5,
        data_dir=str(tmp_path), cooldown_sec=30, max_depth=2,
    )
    assert r2 == "bot_cooldown"


def test_bot_not_mentioning_me_is_ignored(tmp_path):
    _reset()
    ev = {
        "user_id": 111, "group_id": GID, "self_id": SELF,
        "mentions_self": False, "sender_is_bot": True, "post_type": "message",
    }
    reason = bot_social.classify_bot_reply_skip(
        ev, "丰川刻子 你说说", self_id=SELF, now=time.time(),
        data_dir=str(tmp_path), cooldown_sec=30, max_depth=2,
    )
    assert reason == "bot_ignore"
