"""F6 回归：社交疲劳计数按群隔离，A 群被刷屏不拖累 B 群正常交互。"""
from types import SimpleNamespace

from anima.llm.handler import LLMHandler


def _fake_handler():
    return SimpleNamespace()


def test_social_fatigue_per_group_isolated():
    """群 A 被 @ 7 次触发 overwhelmed，群 B 仅 1 次须独立、不触发。"""
    h = _fake_handler()
    cnt_a = 0
    for _ in range(7):
        cnt_a = LLMHandler._record_group_mention(h, "100")
    assert cnt_a > 6, "群 A 应触发疲劳（>6）"

    cnt_b = LLMHandler._record_group_mention(h, "200")
    assert cnt_b == 1, "群 B 应独立于群 A 计数，不触发疲劳"

    cnt_a2 = LLMHandler._record_group_mention(h, "100")
    assert cnt_a2 > 6, "群 A 疲劳态应保持"


def test_social_fatigue_private_uses_zero_key():
    """私聊（group_id=None）归入 key=0，不与任何群混淆。"""
    h = _fake_handler()
    LLMHandler._record_group_mention(h, None)
    assert 0 in h._mention_times_by_group, "私聊应归入 key=0"


def test_social_fatigue_expires_old_entries():
    """超过 5 分钟（300s）的旧记录应在计数时被清理。"""
    import time

    h = _fake_handler()
    h._mention_times_by_group = {123: [time.time() - 400]}  # 注入一条过期记录
    cnt = LLMHandler._record_group_mention(h, 123)
    assert cnt == 1, "过期记录应被清理，仅剩 1 条当前"
