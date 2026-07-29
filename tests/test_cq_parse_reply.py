"""CQ 码解析：reply 段独立（任务：修复 CQ 码文本化）。"""
from anima.channels.onebot11 import build_message_segments


def test_reply_segment_parsed():
    segs = build_message_segments("@アリア[CQ:reply,id=1052582]你好")
    types = [s["type"] for s in segs]
    assert "reply" in types
    assert "text" in types
    reply = next(s for s in segs if s["type"] == "reply")
    assert reply["data"]["id"] == "1052582"
    texts = [s["data"]["text"] for s in segs if s["type"] == "text"]
    assert "@アリア" in texts
    assert "你好" in texts


def test_reply_id_param_prepends_reply():
    segs = build_message_segments("你好", reply_id="1052582")
    assert segs[0]["type"] == "reply"
    assert segs[0]["data"]["id"] == "1052582"
    assert segs[1]["type"] == "text"
    assert segs[1]["data"]["text"] == "你好"
