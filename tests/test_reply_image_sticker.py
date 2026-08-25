"""回归测试：P0 sticker 路径提取 + 引用回复取图。"""
from pathlib import Path

import pytest

from anima.pipeline.stages import _extract_sticker_path
from anima.channels.onebot11 import OneBot11Adapter
from anima.memory.chat_memory import reset_chat_memory


@pytest.fixture(autouse=True)
def _isolate_chat_memory_singleton():
    """F3: 隔离进程级 chat_memory 单例，消除跨测试状态污染。"""
    reset_chat_memory()
    yield
    reset_chat_memory()


# ---------------------------------------------------------------------------
# P0：sticker 工具返回必须是真实图像路径，绝非错误提示文本
# ---------------------------------------------------------------------------

def test_extract_sticker_path_ok():
    s = "已选图：/opt/anima/data/stickers/abc123.png（请在回复末尾自然附带）"
    assert _extract_sticker_path(s) == "/opt/anima/data/stickers/abc123.png"


def test_extract_sticker_path_rejects_error_text():
    # 库为空时工具返回错误提示——绝不能当路径
    assert _extract_sticker_path("库里暂无贴合《无语》的表情，可改调 random_sticker 随便带一张，或本次回复不带图。") is None
    assert _extract_sticker_path("表情库为空，暂无表情可用，本次回复不带图。") is None
    assert _extract_sticker_path("") is None
    assert _extract_sticker_path(None) is None


def test_extract_sticker_path_strips_trailing_punct():
    s = "已选图：/opt/anima/data/stickers/x.jpg（请在回复末尾自然附带这张表情，不要特意解释）。"
    assert _extract_sticker_path(s) == "/opt/anima/data/stickers/x.jpg"


# ---------------------------------------------------------------------------
# adapter.get_msg：拉取被引用消息，返回其中的 image 段
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_msg_parses_image_segment():
    a = OneBot11Adapter()
    replied = {
        "message_id": 777,
        "message": [
            {"type": "text", "data": {"text": "看这张"}},
            {"type": "image", "data": {"file": "guid999.jpg", "url": "http://x/y.jpg", "subType": ""}},
        ],
    }

    async def fake_call(action, params, timeout=10.0):
        assert action == "get_msg"
        assert params == {"message_id": 777}
        # 模拟 _call_action 剥 data 层的行为
        return replied

    a._call_action = fake_call
    msg = await a.get_msg(777)
    assert isinstance(msg, dict)
    segs = msg.get("message", [])
    imgs = [s for s in segs if s.get("type") == "image"]
    assert len(imgs) == 1
    assert imgs[0]["data"]["file"] == "guid999.jpg"


@pytest.mark.asyncio
async def test_get_msg_returns_none_on_empty():
    a = OneBot11Adapter()
    a._call_action = lambda *a, **k: None
    assert await a.get_msg(None) is None
    assert await a.get_msg(123) is None  # _call_action 返回 None


# ---------------------------------------------------------------------------
# handler._fetch_reply_images：从被引用消息抽取 image 段
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_reply_images_extracts():
    from anima.llm.handler import LLMHandler
    from anima.core.anima_core import AnimaCore
    from anima.core.config import AnimaConfig
    from anima.llm.handler import LLMHandler

    core = AnimaCore(AnimaConfig())
    handler = LLMHandler(core)

    class _FakeChannel:
        async def get_msg(self, mid):
            return {
                "message": [
                    {"type": "image", "data": {"file": "g1.jpg", "url": "u1"}},
                    {"type": "text", "data": {"text": "hi"}},
                ]
            }

    class _FakeGateway:
        channel = _FakeChannel()

    core.gateway = _FakeGateway()
    imgs = await handler._fetch_reply_images(555)
    assert len(imgs) == 1
    assert imgs[0]["file"] == "g1.jpg"


@pytest.mark.asyncio
async def test_fetch_reply_images_reuses_prefetched_message():
    """引用发送者识别已经 get_msg 后，取图不得重复调用渠道。"""
    from anima.llm.handler import LLMHandler
    from anima.core.anima_core import AnimaCore
    from anima.core.config import AnimaConfig

    core = AnimaCore(AnimaConfig())
    handler = LLMHandler(core)

    class _FakeChannel:
        calls = 0

        async def get_msg(self, mid):
            self.calls += 1
            return None

    class _FakeGateway:
        channel = _FakeChannel()

    core.gateway = _FakeGateway()
    prefetched = {"message": [
        {"type": "image", "data": {"file": "cached.jpg", "url": "u"}},
    ]}
    imgs = await handler._fetch_reply_images(555, message=prefetched)
    assert imgs[0]["file"] == "cached.jpg"
    assert core.gateway.channel.calls == 0


@pytest.mark.asyncio
async def test_fetch_reply_images_empty_when_no_channel():
    from anima.llm.handler import LLMHandler
    from anima.core.anima_core import AnimaCore
    from anima.core.config import AnimaConfig

    core = AnimaCore(AnimaConfig())
    core.gateway = None  # 无 channel
    handler = LLMHandler(core)
    assert await handler._fetch_reply_images(555) == []


@pytest.mark.asyncio
async def test_get_msg_retries_on_timeout():
    """get_msg 偶发 WS action 超时（返回 None）时应重试，而非直接丢引用图。"""
    a = OneBot11Adapter()
    calls = {"n": 0}

    async def fake_call(action, params, timeout=10.0):
        calls["n"] += 1
        # 第一次模拟回执超时（_call_action 返回 None），第二次成功
        if calls["n"] == 1:
            return None
        return {"message_id": 42, "message": [
            {"type": "image", "data": {"file": "ok.jpg", "url": "u"}},
        ]}

    a._call_action = fake_call
    msg = await a.get_msg(42)
    assert isinstance(msg, dict)
    assert calls["n"] == 2  # 确实重试了一次而非只调一次就放弃


@pytest.mark.asyncio
async def test_get_msg_exhausts_retries_then_none():
    """两次重试都超时仍返回 None（绝不抛），保证主流程不被阻塞。"""
    a = OneBot11Adapter()
    a._call_action = lambda *a, **k: None
    assert await a.get_msg(999) is None


# ---------------------------------------------------------------------------
# 私聊图片提取（修复 bot 对私聊图失明）：parse_onebot_event 必须给私聊也填 images
# ---------------------------------------------------------------------------

def _raw_private_image(file="priv.jpg", url="http://x/priv.jpg"):
    return {
        "post_type": "message",
        "message_type": "private",
        "message_id": 12345,
        "user_id": 1562290139,
        "message": [
            {"type": "image", "data": {"file": file, "url": url, "subType": ""}},
        ],
        "raw_message": f"[CQ:image,file={file},url={url}]",
        "sender": {"user_id": 1562290139, "nickname": "阿布"},
    }


def _raw_group_image(file="g.jpg", url="http://x/g.jpg", group_id=552994229):
    return {
        "post_type": "message",
        "message_type": "group",
        "message_id": 999,
        "user_id": 123,
        "group_id": group_id,
        "message": [
            {"type": "image", "data": {"file": file, "url": url, "subType": ""}},
        ],
        "raw_message": f"[CQ:image,file={file},url={url}]",
        "sender": {"user_id": 123, "nickname": "某人"},
    }


def test_parse_private_image_fills_event_images():
    """私聊图必须进 event['images']，否则 handler 拿到空文本+空图 → SKIP_REPLY。"""
    from anima.channels.onebot11 import parse_onebot_event
    ev = parse_onebot_event(_raw_private_image())
    assert ev["topic"] == "event.message.private"
    imgs = ev.get("images") or []
    assert len(imgs) == 1
    assert imgs[0]["file"] == "priv.jpg"
    assert imgs[0]["url"] == "http://x/priv.jpg"


def test_parse_group_image_still_fills_event_images():
    """回归：群图照常进 event['images'] 且带 group_id。"""
    from anima.channels.onebot11 import parse_onebot_event
    ev = parse_onebot_event(_raw_group_image())
    assert ev["topic"] == "event.message.group"
    assert ev["group_id"] == 552994229
    imgs = ev.get("images") or []
    assert len(imgs) == 1
    assert imgs[0]["file"] == "g.jpg"


def test_parse_private_text_only_has_no_images():
    """私聊纯文本不应有 images 键污染。"""
    from anima.channels.onebot11 import parse_onebot_event
    ev = parse_onebot_event({
        "post_type": "message", "message_type": "private",
        "message_id": 1, "user_id": 1562290139,
        "message": [{"type": "text", "data": {"text": "在吗"}}],
        "raw_message": "在吗", "sender": {"user_id": 1562290139},
    })
    assert not ev.get("images")


# ---------------------------------------------------------------------------
# handler 出站：选中的表情必须作为 OneBot image 段附在群回复之后
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_group_reply_appends_selected_sticker_image(tmp_path):
    """覆盖真实出站组包，防止选图成功却在 handler 中被静默丢掉。"""
    from anima.channels.onebot11 import parse_onebot_event
    from anima.core.anima_core import AnimaCore
    from anima.core.config import AnimaConfig
    from anima.core.gateway import OutboundMsg
    from anima.llm.handler import LLMHandler

    sticker = tmp_path / "reaction.gif"
    sticker.write_bytes(b"GIF89a")
    cfg = AnimaConfig.model_validate({
        "persistence": {"data_dir": str(tmp_path)},
        "sticker": {"always_atmosphere": ["casual"]},
        "crossgroup": {"enabled": False},
        # 测试契约修正：关闭睡眠模式（默认开、判定凌晨 1-5 点），使群回复与时钟无关、确定性可达；
        # 关闭群聊防抖以消除 ~2s 异步计时依赖。
        "autonomy": {"sleep_enabled": False},
        "debounce": {"group_enabled": False},
    })
    core = AnimaCore(cfg)

    class _Gateway:
        sent = []

        async def send(self, message: OutboundMsg):
            self.sent.append(message)
            return 1

    class _Pipeline:
        async def run(self, ctx):
            ctx.reply = "这张正好"
            ctx.sticker_to_send = str(sticker)
            ctx.atmosphere_style = "casual"
            return ctx

    core.gateway = _Gateway()
    handler = LLMHandler(core)
    handler.pipeline = _Pipeline()
    event = parse_onebot_event({
        "post_type": "message", "message_type": "group", "message_id": 1,
        "group_id": 20001, "user_id": 10001, "self_id": 88888,
        "message": [
            {"type": "at", "data": {"qq": "88888"}},
            {"type": "text", "data": {"text": "发个表情"}},
        ],
        "raw_message": "[CQ:at,qq=88888] 发个表情",
        "sender": {"user_id": 10001, "nickname": "测试用户"},
    })

    await handler.handle(event)

    # 消息与表情分离：正文（引用+text）在第一条，表情图在独立第二条
    # 引用优先优化：有 message_id -> 引用锚定而非 @(引用自带通知，避免双提醒)
    body_msg = core.gateway.sent[0].message
    assert [segment["type"] for segment in body_msg] == ["reply", "text"]
    img_msg = core.gateway.sent[1].message
    assert [segment["type"] for segment in img_msg] == ["image"]
    assert img_msg[0]["data"]["file"] == str(sticker)


@pytest.mark.asyncio
async def test_private_reply_appends_selected_sticker_image(tmp_path):
    """私聊选择表情后必须实际发送 image 段，不能只在文字里说“发了”。"""
    sticker = tmp_path / "reaction.gif"
    sticker.write_bytes(b"GIF89a")
    sent = []

    class _Gateway:
        async def send(self, msg):
            sent.append(msg)
            return 1

    from anima.core.anima_core import AnimaCore
    from anima.core.config import AnimaConfig
    from anima.llm.handler import LLMHandler
    core = AnimaCore(AnimaConfig.model_validate({
        "persistence": {"data_dir": str(tmp_path)},
        "crossgroup": {"enabled": False},
    }))
    core.gateway = _Gateway()
    handler = LLMHandler(core)

    async def _run(ctx):
        ctx.reply = "喏 [CQ:image,file=deadbeef.jpg]"
        ctx.sticker_to_send = str(sticker)
        return ctx

    handler.pipeline.run = _run
    await handler.handle({
        "text": "发个表情", "user_id": 10001, "group_id": None,
        "raw": {"message_type": "private", "sender": {"nickname": "主人"}},
    })
    segments = sent[-1].message
    assert segments[-1]["type"] == "image"
    assert Path(segments[-1]["data"]["file"]).is_absolute()
