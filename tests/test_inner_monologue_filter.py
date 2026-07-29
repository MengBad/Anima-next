"""P0 回归：出站回复的内心独白（自我反思 / 自言自语）必须被过滤。

根因：拟人化 LLM 偶尔会把「本喵对自己说」的内容混进对外消息，
表现为括号自语、以「说起来」开头的反思段、以及零散自言自语短语，
发出去很出戏且泄露内部状态。本测试覆盖 filter_inner_monologue 与
split_reply 在分段前的过滤。
"""
import pytest

from anima.core.reply_splitter import filter_inner_monologue, split_reply


def test_parenthetical_self_talk_removed():
    text = "今天天气真好（本喵暗自想：其实本喵更想睡觉）。我们一起去玩吧。"
    out = filter_inner_monologue(text)
    assert "本喵暗自想" not in out
    assert "我们一起去玩吧" in out


def test_leading_shuoshuoqilai_reflection_removed():
    text = "说起来，之前那个bug本喵还没理清楚，不过现在好啦。这是新的功能。"
    out = filter_inner_monologue(text)
    assert "说起来" not in out
    assert "这是新的功能" in out


def test_ask_self_phrase_removed():
    text = "方案A不错。用不用去问问他们呀，算了还是先这样。"
    out = filter_inner_monologue(text)
    assert "用不用去问问他们呀" not in out
    assert "方案A不错" in out
    assert "还是先这样" in out


def test_drift_phrase_removed():
    text = "结论是这样。本喵刚又扯远了，拉回来：明天见。"
    out = filter_inner_monologue(text)
    assert "本喵刚又扯远了" not in out
    assert "结论是这样" in out
    assert "明天见" in out


def test_normal_text_untouched():
    text = "本喵今天超开心！你周末有空一起打游戏吗？"
    out = filter_inner_monologue(text)
    assert out == text


def test_idempotent():
    text = "说起来，本喵还没理清楚（本喵在心里嘀咕：好难）。收尾。"
    once = filter_inner_monologue(text)
    twice = filter_inner_monologue(once)
    assert once == twice


def test_split_reply_filters_inner_monologue():
    # 长文本触发分段，且内含独白，分段结果不应含独白
    text = ("这是第一段很长的内容用来触发分段逻辑。" * 6) + \
           "说起来，本喵还没理清楚这件事（本喵暗自纠结）。结尾。"
    result = split_reply(text)
    joined = "".join(result.segments)
    assert "本喵暗自纠结" not in joined
    assert "说起来" not in joined
    assert len(result.segments) >= 1


# ---------------------------------------------------------------------------
# 出站统一过滤（gateway.send 入口）：覆盖主动发言 / 生活模拟 / 任何未显式过滤的路径
# ---------------------------------------------------------------------------

def test_gateway_filters_outbound_string():
    from anima.core.gateway import _filter_outbound_message
    text = "今天天气真好（本喵暗自想：其实本喵更想睡觉）。我们一起去玩吧。"
    out = _filter_outbound_message(text)
    assert isinstance(out, str)
    assert "本喵暗自想" not in out
    assert "我们一起去玩吧" in out


def test_gateway_filters_outbound_text_segment():
    from anima.core.gateway import _filter_outbound_message
    segs = [
        {"type": "reply", "data": {"id": "1052582"}},
        {"type": "text", "text": "说起来，本喵还没理清楚（本喵在心里嘀咕：好难）。不过现在收尾啦，明天见！"},
        {"type": "image", "data": {"file": "abc.jpg"}},
    ]
    out = _filter_outbound_message(segs)
    assert isinstance(out, list)
    # reply / image 段原样保留
    assert out[0] == segs[0]
    assert out[2] == segs[2]
    # 仅 text 段被过滤（独白短语移除，但「收尾啦/明天见」正常内容保留）
    assert "说起来" not in out[1]["text"]
    assert "本喵在心里嘀咕" not in out[1]["text"]
    assert "收尾" in out[1]["text"]


def test_gateway_filters_outbound_keeps_non_text_untouched():
    from anima.core.gateway import _filter_outbound_message
    # 兜底：非 str/list 的消息原样返回（理论上不会传入，但防御性）
    assert _filter_outbound_message(None) is None
    assert _filter_outbound_message(123) == 123

