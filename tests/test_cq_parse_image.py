"""CQ 码解析：image 段独立（任务：修复 CQ 码文本化）。"""
from anima.channels.onebot11 import build_message_segments


def test_image_segment_parsed():
    segs = build_message_segments("看这张图[CQ:image,file=abc.jpg]")
    types = [s["type"] for s in segs]
    assert "image" in types
    assert "text" in types
    img = next(s for s in segs if s["type"] == "image")
    assert img["data"]["file"] == "abc.jpg"
    assert segs[0]["type"] == "text"
    assert segs[0]["data"]["text"] == "看这张图"


def test_plain_text_single_segment():
    segs = build_message_segments("就普通一句话")
    assert len(segs) == 1
    assert segs[0]["type"] == "text"
    assert segs[0]["data"]["text"] == "就普通一句话"
