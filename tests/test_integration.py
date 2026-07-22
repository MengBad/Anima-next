"""端到端集成测试：OneBot 事件 → Pipeline → 模拟 LLM → 回复。

不依赖真实 NapCat / LLM，用内存桩替代渠道与 LLM 客户端，
验证完整处理链路（解析 → 权限 → 记忆 → 认知 → 生成 → 护栏 → 出站）。
"""

import pytest

from anima.core.config import AnimaConfig
from anima.core.anima_core import AnimaCore
from anima.channels.onebot11 import parse_onebot_event
from anima.llm.handler import LLMHandler
from anima.core.gateway import OutboundMsg


class _FakeLLM:
    """内存版 LLM 客户端：固定回复，记录调用。"""

    def __init__(self, reply: str = "你好呀～"):
        self.reply = reply
        self.model = "fake-model"
        self.calls = 0

    async def chat(self, messages, temperature=0.8, max_tokens=512, **kwargs):
        self.calls += 1
        return self.reply

    async def chat_with_tools(self, *args, **kwargs):
        return self.reply

    async def chat_structured(self, messages, tools=None, temperature=0.8,
                              max_tokens=512, **kwargs):
        # ReActLoop 期望 {content, tool_calls} 结构
        self.calls += 1
        return {"content": self.reply, "tool_calls": []}


class _CapturingGateway:
    """捕获出站消息的伪 Gateway。"""

    def __init__(self):
        self.sent: list[OutboundMsg] = []

    async def on_event(self, event):
        return None

    async def send(self, msg: OutboundMsg):
        self.sent.append(msg)
        return len(self.sent)


def _make_core() -> AnimaCore:
    cfg = AnimaConfig()
    core = AnimaCore(cfg)
    core.llm = _FakeLLM()
    core.gateway = _CapturingGateway()
    # 工具注册表（仅需要只读工具即可触发权限校验路径）
    from anima.tools import create_default_registry
    core.tools = create_default_registry(core)
    return core


def _group_event(text: str, user_id: int = 10001, group_id: int = 20001,
                message_id: int = 999) -> dict:
    raw = {
        "post_type": "message",
        "message_type": "group",
        "message_id": message_id,
        "user_id": user_id,
        "group_id": group_id,
        "raw_message": text,
        # 含 @self_id 段，模拟群聊中 @ 了 bot（新回复闸门要求群消息必须 @bot）
        "message": [
            {"type": "at", "data": {"qq": "88888"}},
            {"type": "text", "data": {"text": text}},
        ],
        "sender": {"user_id": user_id, "nickname": "测试用户", "role": "member"},
        "self_id": 88888,
    }
    return parse_onebot_event(raw)


@pytest.mark.asyncio
async def test_end_to_end_group_reply():
    """群消息应触发完整 Pipeline 并产出一条出站回复。"""
    core = _make_core()
    handler = LLMHandler(core)

    event = _group_event("今天天气怎么样")
    await handler.handle(event)

    assert core.gateway.sent, "应至少发送一条出站消息"
    out = core.gateway.sent[0]
    assert out.detail_type == "group"
    assert out.target_id == 20001
    # 出站消息可能是 str 或复合消息段 list（带表情图时）
    msg = out.message
    if isinstance(msg, list):
        msg = "".join(seg.get("data", {}).get("text", "")
                      for seg in msg if seg.get("type") == "text")
    assert "你好" in msg
    assert core.llm.calls >= 1


@pytest.mark.asyncio
async def test_private_message_gets_safe_fallback_when_llm_returns_empty():
    """上游模型超时/空响应时，私聊不能被静默吞掉。"""
    core = _make_core()
    core.llm = _FakeLLM(reply="")
    handler = LLMHandler(core)
    event = parse_onebot_event({
        "post_type": "message", "message_type": "private", "message_id": 778,
        "user_id": 10001, "raw_message": "在吗",
        "message": [{"type": "text", "data": {"text": "在吗"}}],
        "sender": {"user_id": 10001, "nickname": "测试用户"},
    })

    await handler.handle(event)

    assert len(core.gateway.sent) == 1
    segments = core.gateway.sent[0].message
    assert isinstance(segments, list)
    assert segments[0]["data"]["text"]


@pytest.mark.asyncio
async def test_provider_identity_leak_is_corrected_once():
    """上游默认编码助手身份不能直接出站到 QQ。"""
    class _QueuedLLM(_FakeLLM):
        def __init__(self):
            super().__init__()
            self.responses = [
                "Hello! I am Antigravity, an agentic AI coding assistant.",
                "本喵在呀，怎么啦？",
            ]

        async def chat_structured(self, *args, **kwargs):
            self.calls += 1
            return {"content": self.responses.pop(0), "tool_calls": []}

    core = _make_core()
    core.llm = _QueuedLLM()
    handler = LLMHandler(core)
    event = parse_onebot_event({
        "post_type": "message", "message_type": "private", "message_id": 779,
        "user_id": 10001, "raw_message": "你是谁",
        "message": [{"type": "text", "data": {"text": "你是谁"}}],
        "sender": {"user_id": 10001, "nickname": "测试用户"},
    })

    await handler.handle(event)

    sent = str(core.gateway.sent[0].message).lower()
    assert "antigravity" not in sent
    assert "本喵在呀" in str(core.gateway.sent[0].message)
    assert core.llm.calls == 2


@pytest.mark.asyncio
async def test_dedup_repeated_message():
    """同 message_id 重复推送应被适配器去重，只回复一次。"""
    from anima.channels.onebot11 import OneBot11Adapter

    core = _make_core()
    adapter = OneBot11Adapter()
    adapter.core = core
    handler = LLMHandler(core)

    # 模拟真实运行时：将 handler 订阅到事件总线（cli._register_llm_handler 的等价写法）
    async def on_message(event):
        await handler.handle(event)
    core.bus.subscribe("event.message.group", on_message)
    core.bus.subscribe("event.message.private", on_message)

    # 模拟两条完全相同的原始 WS 帧（含 @bot 段，匹配新的回复闸门）
    raw_text = (
        '{"post_type":"message","message_type":"group","message_id":12345,'
        '"user_id":10001,"group_id":20001,'
        '"raw_message":"[CQ:at,qq=88888] 重复测试",'
        '"message":[{"type":"at","data":{"qq":"88888"}},'
        '{"type":"text","data":{"text":"重复测试"}}],'
        '"sender":{"user_id":10001,"nickname":"测试用户","role":"member"},'
        '"self_id":88888}'
    )
    await adapter._handle_message(raw_text)
    await adapter._handle_message(raw_text)  # 重复

    # 出站只应有 1 条（第二条被去重丢弃；handler 每条消息只产 1 条回复）
    assert len(core.gateway.sent) == 1
    assert core.gateway.sent[0].detail_type == "group"
    assert core.gateway.sent[0].target_id == 20001
    assert adapter.events_dropped >= 1
    assert adapter.health()["event_drop_reasons"]["duplicate"] >= 1


@pytest.mark.asyncio
async def test_mention_self_flag():
    """群消息 @自己 应被识别为 mentions_self。"""
    raw = {
        "post_type": "message",
        "message_type": "group",
        "message_id": 555,
        "user_id": 10001,
        "group_id": 20001,
        "self_id": 88888,
        "raw_message": "Anima 在吗",
        "message": [
            {"type": "at", "data": {"qq": "88888"}},
            {"type": "text", "data": {"text": "Anima 在吗"}},
        ],
        "sender": {"user_id": 10001, "nickname": "测试用户", "role": "member"},
    }
    event = parse_onebot_event(raw)
    assert event.get("mentions_self") is True
    assert event.get("reply_to_self") is False


@pytest.mark.asyncio
async def test_group_reply_requires_mention():
    """群消息未 @bot 时，回复闸门应拦截，不产出门站回复（私聊不受影响）。"""
    core = _make_core()
    handler = LLMHandler(core)

    # 群聊消息，未 @bot（无 at 段）→ 应被闸门拦截
    group_evt = _group_event("今天天气怎么样")
    # 移除 @ 段以模拟「未提及 bot」
    group_evt["message"] = [{"type": "text", "data": {"text": "今天天气怎么样"}}]
    group_evt["mentions_self"] = False
    group_evt["reply_to_self"] = False
    await handler.handle(group_evt)
    assert core.gateway.sent == [], "未@bot 的群消息不应触发回复"

    # 私聊消息 → 始终回复
    private_raw = {
        "post_type": "message",
        "message_type": "private",
        "message_id": 777,
        "user_id": 10001,
        "raw_message": "在吗",
        "message": [{"type": "text", "data": {"text": "在吗"}}],
        "sender": {"user_id": 10001, "nickname": "测试用户", "role": "member"},
        "self_id": 88888,
    }
    private_evt = parse_onebot_event(private_raw)
    await handler.handle(private_evt)
    assert core.gateway.sent, "私聊消息应触发回复"
    assert core.gateway.sent[0].detail_type == "private"


@pytest.mark.asyncio
async def test_group_policy_disables_tools_for_that_group_only():
    class _ToolCaptureLLM(_FakeLLM):
        def __init__(self):
            super().__init__()
            self.tool_schema_counts: list[int] = []

        async def chat_structured(self, messages, tools=None, **kwargs):
            self.calls += 1
            self.tool_schema_counts.append(len(tools or []))
            return {"content": self.reply, "tool_calls": []}

    core = _make_core()
    core.llm = _ToolCaptureLLM()
    core.config.permissions.group_policies = {"20001": {"tools_enabled": False}}
    handler = LLMHandler(core)

    await handler.handle(_group_event("帮我查一下", group_id=20001))
    await handler.handle(_group_event("帮我查一下", group_id=20002, message_id=1000))

    assert core.llm.tool_schema_counts[0] == 0
    assert core.llm.tool_schema_counts[1] > 0


@pytest.mark.asyncio
async def test_group_reply_uses_at_segment():
    """群回复必须以独立 at 消息段 @ 触发者，而非把 [CQ:at,...] 当文本拼接
    （数组消息格式下 CQ 码不会被客户端解析，会显示成裸码）。"""
    core = _make_core()
    handler = LLMHandler(core)

    uid = 1620756917
    event = _group_event("今天天气怎么样", user_id=uid)
    await handler.handle(event)

    assert core.gateway.sent, "应至少发送一条出站消息"
    out = core.gateway.sent[0]
    assert out.detail_type == "group"
    msg = out.message
    assert isinstance(msg, list), "群回复应为复合消息段列表（含 at 段）"

    at_segs = [s for s in msg if s.get("type") == "at"]
    assert at_segs, "群回复应含 at 段"
    assert str(at_segs[0].get("data", {}).get("qq")) == str(uid), \
        f"at 段应 @ 触发者 {uid}，实际 {at_segs[0]}"

    # 文本段中不得再出现裸 CQ:at 码
    for s in msg:
        if s.get("type") == "text":
            assert "[CQ:at" not in s.get("data", {}).get("text", ""), \
                "文本段不应含裸 CQ:at 码"


@pytest.mark.asyncio
async def test_private_reply_no_at_segment():
    """私聊回复不应含 at 段（私聊无 @ 概念）。"""
    core = _make_core()
    handler = LLMHandler(core)

    private_raw = {
        "post_type": "message",
        "message_type": "private",
        "message_id": 777,
        "user_id": 10001,
        "raw_message": "在吗",
        "message": [{"type": "text", "data": {"text": "在吗"}}],
        "sender": {"user_id": 10001, "nickname": "测试用户", "role": "member"},
        "self_id": 88888,
    }
    await handler.handle(parse_onebot_event(private_raw))
    out = core.gateway.sent[0]
    assert out.detail_type == "private"
    msg = out.message
    if isinstance(msg, list):
        assert not any(s.get("type") == "at" for s in msg), "私聊不应含 at 段"


@pytest.mark.asyncio
async def test_run_shell_whitelist_bypass_blocked():
    """复合命令（白名单首词 + 元字符）应被拒绝，不再可绕过。"""
    from anima.tools.sandbox import run_shell

    # ls && rm -rf / 明文可绕过旧版，现在应被拒绝
    out = await run_shell("ls && rm -rf /")
    assert "元字符" in out or "不在安全白名单" in out, out

    # 纯白名单只读命令应可正常执行。echo 是 Windows shell 内建命令，
    # 本沙箱刻意不启动 shell，因此改用 Windows/Linux 都存在的独立可执行文件。
    out_ok = await run_shell("whoami")
    assert out_ok and "[沙箱]" not in out_ok


@pytest.mark.asyncio
async def test_write_file_path_traversal_blocked():
    """write_file 应禁止 ../ 目录穿越（把文件名约束在 output 内）。"""
    from anima.tools import _write_file
    from pathlib import Path
    import tempfile, shutil

    tmp = tempfile.mkdtemp()
    try:
        p = _write_file("../../etc/passwd", "x", data_dir=tmp)
        out_dir = Path(tmp) / "output"
        # 解析后必须仍在 output 目录内，不得逃逸到父目录
        assert str(Path(p).resolve()).startswith(str(out_dir.resolve()))
        assert not any(part == ".." for part in Path(p).parts)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
