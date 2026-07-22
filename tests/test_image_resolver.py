"""测试 OneBot11 get_image 解析链路：echo 回执关联 + resolve_image 三级兜底。

修复前：NapCat 的入站 image 段 file 是裸 GUID，_fetch_bytes/fetch_image_bytes
两端都失败 → 图"看得到却存不下来/读不出"。修复后通过 get_image API 兜底。
"""
import asyncio
import json
import os
import tempfile

import pytest

from anima.channels.onebot11 import OneBot11Adapter, parse_onebot_event


def _make_adapter() -> OneBot11Adapter:
    a = OneBot11Adapter()
    return a


@pytest.mark.asyncio
async def test_upload_file_prefers_ws_action_over_unconfigured_http(tmp_path, monkeypatch):
    path = tmp_path / "export.json"
    path.write_text("{}", encoding="utf-8")
    adapter = _make_adapter()
    adapter._ws = type("WS", (), {"closed": False})()
    seen = {}

    async def ws_call(action, params, timeout=15.0):
        seen.update(action=action, params=params, timeout=timeout)
        return {"file_id": "file-1"}

    async def http_call(action, params):
        raise AssertionError("WS 可用时不应访问 HTTP action 端口")

    monkeypatch.setattr(adapter, "_call_action", ws_call)
    monkeypatch.setattr(adapter, "_http_post", http_call)
    result = await adapter.upload_file("private", 123, path)
    assert result == {"status": "ok", "data": {"file_id": "file-1"}}
    assert seen["action"] == "upload_file"
    assert seen["params"]["user_id"] == 123


async def test_call_action_echo_correlation():
    """_call_action 发 WS action 后能等回匹配 echo 的响应。"""
    a = _make_adapter()

    class FakeWS:
        closed = False

        async def send_json(self, msg):
            # 模拟 NapCat 回送带 echo 的响应
            await a._handle_message(json.dumps({"echo": msg["echo"], "data": {"path": "/abs/img.jpg"}}))

    a._ws = FakeWS()
    resp = await a._call_action("get_image", {"file": "abc.jpg"})
    assert resp == {"path": "/abs/img.jpg"}


async def test_call_action_strips_status_wrapper():
    """部分实现把结果包在 {status,data,retcode} 里，应剥到 data 层。"""
    a = _make_adapter()

    class FakeWS:
        closed = False

        async def send_json(self, msg):
            await a._handle_message(json.dumps({
                "status": "ok", "retcode": 0, "echo": msg["echo"],
                "data": {"path": "/x/y.png"},
            }))

    a._ws = FakeWS()
    resp = await a._call_action("get_image", {"file": "x"})
    assert resp == {"path": "/x/y.png"}


async def test_call_action_ignores_unrelated_echo():
    """未挂起等待的 echo（如 send_msg 回执）不进 pipeline、不报错。"""
    a = _make_adapter()

    class FakeWS:
        closed = False

        async def send_json(self, msg):
            pass  # 不回执

    a._ws = FakeWS()
    # send_msg 回执带 echo="ws_..."，_handle_message 直接丢弃，不影响 pending
    # 用真实 _ws_send_action 触发（其 echo 不在 _pending）
    await a._ws_send_action("send_group_msg", {"group_id": 1, "message": "hi"})
    assert "ws_" not in a._pending  # 没有被当成挂起请求残留
    # 未连接的 action 调用安全返回 None
    a._ws = None
    assert await a._call_action("get_image", {"file": "x"}) is None


async def test_channel_health_tracks_real_connection_and_outbound_receipt():
    a = _make_adapter()

    class FakeWS:
        closed = False

        async def send_json(self, msg):
            await a._handle_message(json.dumps({
                "status": "ok", "retcode": 0, "echo": msg["echo"],
                "data": {"message_id": 321},
            }))

    a._running = True
    a._ws = FakeWS()
    assert a.health()["connected"] is True
    assert await a.send_message("group", 123, "test") == 321
    health = a.health()
    assert health["outbound_attempts"] == 1
    assert health["outbound_successes"] == 1
    assert health["outbound_failures"] == 0
    assert health["last_outbound_success_at"] is not None


async def test_background_event_keeps_ws_free_for_get_msg_reply():
    """业务事件等待 get_msg 时，接收循环仍须能处理同连接的 echo 回执。

    这是引用图片场景的关键回归：若在 WS 接收循环中同步 await handler，
    get_msg 回执无法被读取，最终必然超时。
    """
    a = _make_adapter()
    completed = asyncio.Event()

    class FakeCore:
        class _Bus:
            async def publish(self, topic, event):
                result = await a._call_action("get_msg", {"message_id": event["message_id"]})
                assert result == {"message_id": 123}
                completed.set()

        event_bus = _Bus()

        class _Gateway:
            async def on_event(self, event):
                return None

        gateway = _Gateway()

    class FakeWS:
        closed = False

        async def send_json(self, msg):
            await a._handle_message(json.dumps({
                "echo": msg["echo"], "data": {"message_id": 123},
            }))

    a.core = FakeCore()
    a._ws = FakeWS()
    await a._handle_message(json.dumps({
        "post_type": "message", "message_type": "group", "message_id": 123,
        "user_id": 9, "group_id": 8, "message": "引用图片", "sender": {},
    }), background=True)
    await asyncio.wait_for(completed.wait(), timeout=1.0)


async def test_resolve_image_local_file_shortcuts_get_image(monkeypatch):
    """url 失败但 file 是真实本地路径 → 直接读，不调 get_image。"""
    a = _make_adapter()
    calls = {}

    async def fake_download(url, max_bytes=0):
        calls["url"] = url
        return None

    async def fake_get_image_path(guid):
        calls["guid"] = guid
        return "/abs/img.jpg"

    monkeypatch.setattr(a, "_download", fake_download)
    monkeypatch.setattr(a, "get_image_path", fake_get_image_path)

    tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
    tmp.write(b"\xff\xd8\xff fakejpg")
    tmp.close()
    try:
        data = await a.resolve_image({"url": "http://x/y.jpg", "file": tmp.name})
        assert data == b"\xff\xd8\xff fakejpg"
        assert "guid" not in calls  # 没走到 get_image
    finally:
        os.unlink(tmp.name)


async def test_resolve_image_guid_falls_to_get_image(monkeypatch):
    """url 空 + file 是 GUID（非路径）→ 调 get_image 解析真实路径并读取。"""
    a = _make_adapter()
    calls = {}

    # resolve_image 直接调 _call_action 拿 get_image 响应（含 path）
    async def fake_call_action(action, params, timeout=15.0):
        calls["action"] = action
        calls["guid"] = params.get("file")
        t = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        t.write(b"\x89PNG\r\n\x1a\n fakepng")
        t.close()
        calls["path"] = t.name
        return {"path": t.name, "url": ""}

    monkeypatch.setattr(a, "_call_action", fake_call_action)
    data = await a.resolve_image({"url": "", "file": "guid123.jpg"})
    try:
        assert data == b"\x89PNG\r\n\x1a\n fakepng"
        assert calls["action"] == "get_image"
        assert calls["guid"] == "guid123.jpg"
    finally:
        if "path" in calls:
            os.unlink(calls["path"])


async def test_resolve_image_all_fail_returns_none():
    """url/file/get_image 全失败 → 返回 None（绝不抛）。"""
    a = _make_adapter()

    async def _no_download(url, max_bytes=0):
        return None

    async def _no_get_image(guid):
        return None

    a._download = _no_download
    a.get_image_path = _no_get_image
    assert await a.resolve_image({"url": "", "file": "/no/such/file.jpg"}) is None


def test_parse_image_segment_captures_file_id():
    """image 段解析应把 file_id/subType 一并带入 event['images']。"""
    raw = {
        "post_type": "message", "message_type": "group",
        "group_id": 99, "user_id": 7, "self_id": 1,
        "message": [
            {"type": "text", "data": {"text": "看这个"}},
            {"type": "image", "data": {"file": "abc.jpg", "url": "http://x/a.jpg",
                                       "subType": 1, "file_id": "FID123"}},
        ],
    }
    ev = parse_onebot_event(raw)
    assert ev is not None
    imgs = ev.get("images", [])
    assert len(imgs) == 1
    assert imgs[0]["file_id"] == "FID123"
    assert imgs[0]["sub_type"] == 1
    assert imgs[0]["url"] == "http://x/a.jpg"


def test_parse_cq_image_segment():
    """CQ 码字符串里的 [CQ:image,...] 也要被提取。"""
    raw = {
        "post_type": "message", "message_type": "group",
        "group_id": 99, "user_id": 7, "self_id": 1,
        "message": "看这个[CQ:image,file=xyz.jpg,url=http://x/b.jpg,subType=1]",
    }
    ev = parse_onebot_event(raw)
    assert ev is not None
    imgs = ev.get("images", [])
    assert len(imgs) == 1
    assert imgs[0]["file"] == "xyz.jpg"
