"""P0 回归（2026-07-29）：分段发送前过滤——中间独白段被截断，只发正常段。"""
from anima.core.reply_splitter import split_reply


def test_split_drops_middle_monologue_segment():
    normal_a = "刚才那个bug已经修好啦。"
    monologue = "本喵倒要看看需不需要跟进，之前答应了那家伙一件事，我看她就是单纯闲得慌。"
    normal_b = "你下次直接找本喵就行喵～"
    full = normal_a + monologue + normal_b
    result = split_reply(full)
    joined = "".join(result.segments)
    # 独白段不应出现
    assert "本喵倒要看看" not in joined
    assert "需不需要跟进" not in joined
    assert "我看她就是单纯闲得慌" not in joined
    # 正常段保留
    assert "已经修好啦" in joined
    assert "直接找本喵" in joined
    # 至少应发出两段正常内容（独白被剔除后不单独成段）
    assert len(result.segments) >= 1
