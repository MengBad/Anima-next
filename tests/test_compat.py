"""AstrBot 兼容层测试。"""

import pytest

from anima.compat.astrbot import (
    Comp, MessageChain, AstrMessageEvent, Context, Star, filter, hook, llm_tool
)
from anima.compat.loader import _collect_handlers, _collect_hooks, _install_astrbot_modules


# 确保 astrbot.* 命名空间已注入
def test_modules_installed():
    _install_astrbot_modules()
    import sys
    assert "astrbot" in sys.modules


class DummyPlugin(Star):
    def __init__(self, context):
        super().__init__(context)
        self.called = False

    @filter.command("/hello")
    async def hello(self, event: AstrMessageEvent):
        self.called = True
        return event.plain_result("hi")

    @filter.on_group_message(priority=1)
    async def on_group(self, event: AstrMessageEvent):
        return event.plain_result("group")

    @hook.on_llm_request
    async def tweak_prompt(self, ctx):
        ctx.system_prompt += "\n[插件注入]"


def test_collect_handlers():
    handlers = _collect_handlers(DummyPlugin)
    cmds = [h[1].get("cmd") for h in handlers if h[1].get("type") == "command"]
    assert "/hello" in cmds


def test_collect_hooks():
    hooks = _collect_hooks(DummyPlugin)
    assert "tweak_prompt" in hooks
    assert "on_llm_request" in hooks["tweak_prompt"]


def test_message_chain_plain_text():
    chain = MessageChain.chain(Comp.text("hello "), Comp.at(123))
    assert chain.get_plain_text() == "hello "


@pytest.mark.asyncio
async def test_plugin_command_filter():
    ctx = Context()
    plugin = DummyPlugin(ctx)
    plugin._anima_handlers = _collect_handlers(DummyPlugin)

    from anima.compat.astrbot import _match_filter
    event = AstrMessageEvent.from_onebot({
        "raw": {
            "message_type": "private",
            "user_id": 1,
            "message": [{"type": "text", "data": {"text": "/hello world"}}],
        }
    })
    text = event.message.get_plain_text()
    matched = False
    for method_name, filter_info, priority in plugin._anima_handlers:
        if _match_filter(filter_info, event, text):
            matched = True
            break
    assert matched


@pytest.mark.asyncio
async def test_plugin_send_result():
    """验证插件调用 send_result 后会触发发送回调。"""
    from anima.compat.astrbot import _send_callback

    sent = []
    def fake_send(detail_type, target_id, result):
        sent.append((detail_type, target_id, result))

    # 临时替换全局回调
    orig = _send_callback
    from anima.compat import astrbot
    astrbot._send_callback = fake_send
    try:
        event = AstrMessageEvent.from_onebot({
            "raw": {
                "message_type": "group",
                "user_id": 1,
                "group_id": 123,
                "message": [{"type": "text", "data": {"text": "hi"}}],
            }
        })
        event.send_result(event.plain_result("hello"))
        assert len(sent) == 1
        assert sent[0][0] == "group"
        assert sent[0][1] == 123
        assert sent[0][2]["text"] == "hello"
    finally:
        astrbot._send_callback = orig


@pytest.mark.asyncio
async def test_context_bridges_llm_tool_and_unloads_cleanly():
    """AstrBot 风格函数工具应进入 Anima schema，并可在卸载时撤销。"""
    from types import SimpleNamespace
    from anima.tools import ToolRegistry

    core = SimpleNamespace(tools=ToolRegistry())

    @llm_tool("lookup", description="查询一个名称", min_level="user")
    async def lookup(name: str, limit: int = 2):
        return f"{name}:{limit}"

    ctx = Context(core=core, plugin_id="demo-plugin")
    ctx.add_llm_tools(lookup)
    assert "compat_demo_plugin_lookup" in core.tools.tools
    schema = core.tools.get_schemas(2)[0]["function"]
    assert schema["name"] == "compat_demo_plugin_lookup"
    assert schema["parameters"]["required"] == ["name"]
    assert await core.tools.call("compat_demo_plugin_lookup", {"name": "cat"}, 2) == "cat:2"

    ctx.register_web_api("/demo/status", lambda request: {"ok": True}, methods="get")
    assert ctx.web_api_manifest()[0]["methods"] == ["GET"]
    ctx.close()
    assert "compat_demo_plugin_lookup" not in core.tools.tools


def test_context_rejects_unsafe_web_api_path():
    ctx = Context()
    with pytest.raises(ValueError):
        ctx.register_web_api("/../secret", lambda request: None)


@pytest.mark.asyncio
async def test_compat_llm_generate_uses_hakimi_persona():
    class FakeLLM:
        async def chat(self, messages, **kwargs):
            assert messages[0]["role"] == "system"
            assert "哈基米" in messages[0]["content"]
            assert "你是 Anima" not in messages[0]["content"]
            return "收到啦"

    assert await Context(llm_client=FakeLLM()).llm_generate("你好") == "收到啦"
