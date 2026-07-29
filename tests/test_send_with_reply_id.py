"""出站 send_* 把 reply_id / CQ 码解析为消息段（任务：修复 CQ 码文本化）。

验证点：
- send_group_msg(text, reply_id) 发出的 message 首段是 reply 段（而非字符串前缀）
- send_group_msg(text 含 [CQ:reply,...]) 自动拆成独立 reply 段，不依赖 reply_id 参数
- send_private_msg(text, reply_id) 同样注入 reply 段
"""
import asyncio
from types import SimpleNamespace

from anima.channels.onebot11 import OneBot11Adapter


def _make_adapter(captured: dict) -> OneBot11Adapter:
    """构造一个不真正联网的适配器：_ws_send_action 被替换成捕获 params 的假实现。

    给一个假的 _ws（closed=False）让 send_message 走 WS 主路径，从而触发被替换的
    _ws_send_action；不真正连接网络。
    """
    adapter = OneBot11Adapter(ws_url="ws://127.0.0.1:3001")
    adapter._ws = SimpleNamespace(closed=False)  # type: ignore[assignment]

    async def fake_ws_send_action(action, params):
        captured["action"] = action
        captured["params"] = params
        return 999  # 假 message_id

    # 直接替换实例方法，绕过真实 _ws 连接检查
    adapter._ws_send_action = fake_ws_send_action  # type: ignore[assignment]
    return adapter


async def test_send_group_msg_injects_reply_segment():
    captured: dict = {}
    adapter = _make_adapter(captured)
    msg_id = await adapter.send_group_msg(group_id=12345, message="你好", reply_id="123")
    assert msg_id == 999
    segs = captured["params"]["message"]
    assert isinstance(segs, list), "message 必须是消息段列表，不能是含 CQ 码的文本串"
    assert segs[0]["type"] == "reply"
    assert segs[0]["data"]["id"] == "123"
    # 后续文本段保留
    texts = [s.get("data", {}).get("text") for s in segs if s["type"] == "text"]
    assert "你好" in texts


async def test_send_group_msg_parses_inline_cq_reply():
    captured: dict = {}
    adapter = _make_adapter(captured)
    await adapter.send_group_msg(group_id=12345, message="[CQ:reply,id=1052582]你好")
    segs = captured["params"]["message"]
    assert segs[0]["type"] == "reply"
    assert segs[0]["data"]["id"] == "1052582"
    texts = [s.get("data", {}).get("text") for s in segs if s["type"] == "text"]
    assert "你好" in texts


async def test_send_private_msg_injects_reply_segment():
    captured: dict = {}
    adapter = _make_adapter(captured)
    msg_id = await adapter.send_private_msg(user_id=998877, message="在的", reply_id="555")
    assert msg_id == 999
    segs = captured["params"]["message"]
    assert segs[0]["type"] == "reply"
    assert segs[0]["data"]["id"] == "555"
    texts = [s.get("data", {}).get("text") for s in segs if s["type"] == "text"]
    assert "在的" in texts


async def test_send_with_preexisting_list_and_reply_id():
    captured: dict = {}
    adapter = _make_adapter(captured)
    await adapter.send_group_msg(
        group_id=12345,
        message=[{"type": "text", "data": {"text": "原文本"}}],
        reply_id="777",
    )
    segs = captured["params"]["message"]
    assert segs[0]["type"] == "reply"
    assert segs[0]["data"]["id"] == "777"
    assert segs[1]["type"] == "text"
    assert segs[1]["data"]["text"] == "原文本"


if __name__ == "__main__":
    asyncio.run(test_send_group_msg_injects_reply_segment())
    asyncio.run(test_send_group_msg_parses_inline_cq_reply())
    asyncio.run(test_send_private_msg_injects_reply_segment())
    asyncio.run(test_send_with_preexisting_list_and_reply_id())
    print("ok")
