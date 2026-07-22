"""图片/表情段提取健壮性测试：数组段 + CQ 码字符串双格式，覆盖 image/bface/sface。"""
import pytest

from anima.channels.onebot11 import parse_onebot_event


def _group_event(message, self_id=10001):
    return {
        "post_type": "message",
        "message_type": "group",
        "message_id": 555,
        "user_id": 20002,
        "group_id": 777,
        "self_id": self_id,
        "sender": {"nickname": "tester", "user_id": 20002},
        "message": message,
    }


def test_array_image_segment_extracted():
    """数组段格式：image 段应被提取到 event['images']。"""
    msg = [
        {"type": "text", "data": {"text": "看这个"}},
        {"type": "image", "data": {"url": "http://x/y.jpg", "file": "/tmp/y.jpg", "subType": ""}},
    ]
    ev = parse_onebot_event(_group_event(msg))
    assert ev["text"] == "看这个"
    assert "images" in ev and len(ev["images"]) == 1
    assert ev["images"][0]["url"] == "http://x/y.jpg"
    assert ev["images"][0]["file"] == "/tmp/y.jpg"


def test_cq_string_image_extracted():
    """CQ 码字符串格式（NapCat 偶发）：[CQ:image,...] 应被正则拆出。"""
    msg = "看这个[CQ:image,file=/tmp/z.png,url=http://x/z.png]"
    ev = parse_onebot_event(_group_event(msg))
    # 文本也应正确提取（不含 CQ 码）
    assert ev["text"] == "看这个"
    assert "images" in ev and len(ev["images"]) == 1
    assert ev["images"][0]["file"] == "/tmp/z.png"
    assert ev["images"][0]["url"] == "http://x/z.png"


def test_bface_segment_captured():
    """QQ 系统表情 bface 也应被捕获（即使后续下载可能失败，至少进收集队列）。"""
    msg = [
        {"type": "text", "data": {"text": "哈哈"}},
        {"type": "bface", "data": {"file": "bface-id-123"}},
    ]
    ev = parse_onebot_event(_group_event(msg))
    assert "images" in ev and len(ev["images"]) == 1
    assert ev["images"][0]["sub_type"] == "bface"


def test_no_image_no_key():
    """无图片消息不应写入 event['images']。"""
    ev = parse_onebot_event(_group_event([{"type": "text", "data": {"text": "纯文本"}}]))
    assert "images" not in ev


def test_file_segment_keeps_only_safe_lookup_metadata():
    ev = parse_onebot_event(_group_event([
        {"type": "file", "data": {"file_id": "opaque-file-id", "name": "notes.md", "url": "https://secret.example/file"}},
    ]))
    assert ev["files"] == [{"file_id": "opaque-file-id", "name": "notes.md"}]
    assert ev["rich_blocks"][0]["type"] == "file"


def test_cq_string_text_only_extracted():
    """CQ 字符串纯文本：文本应被提取且不残留 CQ 码。"""
    msg = "你好[CQ:at,qq=10001]在吗"
    ev = parse_onebot_event(_group_event(msg))
    assert ev["text"] == "你好在吗"
    # 文本段外的 at 不应进 images
    assert "images" not in ev


def test_share_and_forward_are_summarized_without_raw_payload():
    ev = parse_onebot_event(_group_event([
        {"type": "share", "data": {"title": "一篇文章", "url": "https://example.com/x"}},
        {"type": "forward", "data": {}},
    ]))
    blocks = ev["rich_blocks"]
    assert blocks[0]["type"] == "share"
    assert blocks[0]["title"] == "一篇文章"
    assert blocks[0]["url"] == "https://example.com/x"
    assert blocks[1]["type"] == "forward"
    assert "合并转发" in blocks[1]["title"]
    assert "一篇文章" in ev["text"]
    assert "example.com/x" in ev["text"]
    assert "合并转发" in ev["text"]
    assert ev["media_observation"]["rich_block_types"] == ("share", "forward")


def test_media_observation_only_contains_safe_metadata():
    ev = parse_onebot_event(_group_event([
        {"type": "image", "data": {"file": "secret-guid", "url": "https://private.example/a"}},
        {"type": "bface", "data": {"file": "face-guid"}},
    ]))
    observation = ev["media_observation"]
    assert observation["image_count"] == 2
    assert observation["sticker_count"] == 1
    assert "secret-guid" not in str(observation)
    assert "private.example" not in str(observation)
