"""全量多模态视觉理解测试：media 工具 / vision 模块 / chat_memory vision_desc /
stages 多模态注入 / memory 纯图片消息不跳过回复。"""
import asyncio
import base64
import json

import pytest

from anima.media import (
    PreparedImage, guess_ext, to_data_uri, build_multimodal_user_content,
    fetch_image_bytes, prepare_images_for_llm,
)
from anima.cognitive.vision import (
    CLASSIFY_TOOLS, STICKER_EMOTIONS, VisionManager,
    get_vision_manager, close_vision_manager,
)
from anima.memory.chat_memory import ChatMemory


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20


# ---------------------------------------------------------------------------
# media 工具
# ---------------------------------------------------------------------------

def test_guess_ext_by_magic():
    assert guess_ext(PNG, {}) == ".png"
    assert guess_ext(b"\xff\xd8\xffrest", {}) == ".jpg"
    assert guess_ext(b"GIF89a", {}) == ".gif"


def test_guess_ext_by_suffix_fallback():
    assert guess_ext(b"????", {"url": "http://x/a.JPEG"}) == ".jpg"
    assert guess_ext(b"????", {"file": "/tmp/b.webp"}) == ".webp"


def test_to_data_uri_format():
    uri = to_data_uri(PNG, {})
    assert uri.startswith("data:image/png;base64,")
    body = uri.split(",", 1)[1]
    assert base64.b64decode(body) == PNG


def test_prepare_large_image_for_llm_preserves_visual_input():
    """超大原图应被缩小为可内联图，而不是整张丢弃。"""
    from PIL import Image
    import io

    source = Image.new("RGB", (3200, 2200), color=(32, 120, 240))
    raw = io.BytesIO()
    source.save(raw, format="PNG")
    frames = prepare_images_for_llm(raw.getvalue(), {}, max_bytes=100 * 1024, max_dimension=800)
    assert len(frames) == 1
    assert frames[0].ext == ".webp"
    assert len(frames[0].data) <= 100 * 1024


def test_prepare_animated_image_uses_keyframes_with_shared_budget():
    from PIL import Image
    import io

    raw = io.BytesIO()
    Image.new("RGB", (80, 80), "red").save(
        raw,
        format="GIF",
        save_all=True,
        append_images=[Image.new("RGB", (80, 80), "green"), Image.new("RGB", (80, 80), "blue")],
        duration=80,
    )
    frames = prepare_images_for_llm(raw.getvalue(), {}, max_bytes=80 * 1024, max_frames=3)
    assert [frame.frame_index for frame in frames] == [0, 1, 2]
    assert sum(len(frame.data) for frame in frames) <= 80 * 1024


def test_build_multimodal_user_content():
    parts = build_multimodal_user_content("看这张", ["data:image/png;base64,XXX"])
    assert parts[0] == {"type": "text", "text": "看这张"}
    assert parts[1]["type"] == "image_url"
    assert parts[1]["image_url"]["url"] == "data:image/png;base64,XXX"


@pytest.mark.asyncio
async def test_fetch_image_bytes_from_file(tmp_path):
    p = tmp_path / "x.png"
    p.write_bytes(PNG)
    data = await fetch_image_bytes({"url": "", "file": str(p)})
    assert data == PNG


# ---------------------------------------------------------------------------
# vision 模块
# ---------------------------------------------------------------------------

class FakeLLM:
    def __init__(self, reply="ok", tool_calls=None):
        self.reply = reply
        self.tool_calls = tool_calls or []
        self.calls = []
        self.model = "fake"

    async def chat(self, messages, **kw):
        self.calls.append(messages)
        return self.reply

    async def chat_structured(self, messages, tools=None, **kw):
        self.calls.append(messages)
        return {"content": self.reply, "tool_calls": self.tool_calls}


@pytest.mark.asyncio
async def test_vision_describe_sends_image():
    llm = FakeLLM(reply="一只橘猫趴着")
    vm = VisionManager()
    out = await vm.describe(llm, PNG, {"url": "u"})
    assert out == "一只橘猫趴着"
    # 末条 user 消息应为多模态（含 image_url）
    user_msg = llm.calls[-1][-1]
    assert isinstance(user_msg["content"], list)
    assert any(p.get("type") == "image_url" for p in user_msg["content"])


@pytest.mark.asyncio
async def test_vision_label_parses_tool_calls():
    tc = {
        "function": {
            "name": "record_sticker_labels",
            "arguments": json.dumps({"labels": ["无语", "狗头"]}),
        }
    }
    llm = FakeLLM(tool_calls=[tc])
    vm = VisionManager()
    labels = await vm.label(llm, PNG, {"url": "u"}, "哈哈哈")
    assert labels == ["无语", "狗头"]


@pytest.mark.asyncio
async def test_vision_label_empty_on_no_toolcall():
    llm = FakeLLM(reply="没图")
    vm = VisionManager()
    assert await vm.label(llm, PNG, {"url": "u"}) is None


def test_vision_singleton():
    m1 = get_vision_manager(123)
    m2 = get_vision_manager(456)
    assert m1 is m2
    close_vision_manager()
    m3 = get_vision_manager(789)
    assert m3 is not m1


def test_vision_classification_schema_has_no_empty_enum_member():
    """Gemini function declarations rejects empty enum values (HTTP 400)."""
    emotion = CLASSIFY_TOOLS[0]["function"]["parameters"]["properties"]["emotion"]
    assert emotion["enum"] == STICKER_EMOTIONS
    assert all(value for value in emotion["enum"])


# ---------------------------------------------------------------------------
# chat_memory vision_desc
# ---------------------------------------------------------------------------

def test_chat_memory_vision_desc_roundtrip(tmp_path):
    cm = ChatMemory(persist_path=str(tmp_path / "cm.json"))
    cm.add("group", 123, "user", "Bob", "[用户发送了图片]", user_id=9)
    cm.set_vision_desc("group", 123, 9, "一只橘猫")
    ctx = cm.get_context("group", 123, user_id=9)
    assert "图片:一只橘猫" in ctx[0]["content"]


def test_chat_memory_add_with_vision_desc(tmp_path):
    cm = ChatMemory(persist_path=str(tmp_path / "cm.json"))
    cm.add("private", 5, "user", "Al", "发图", vision_desc="夕阳下的海")
    ctx = cm.get_context("private", 5, user_id=5)
    assert "图片:夕阳下的海" in ctx[0]["content"]


# ---------------------------------------------------------------------------
# stages 多模态注入 + memory 纯图片不跳过
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_generation_injects_multimodal(monkeypatch):
    from anima.pipeline.stages import generation_stage
    from anima.pipeline import PipelineContext, StageResult

    async def fake_fetch(img, **kwargs):
        return [PreparedImage(PNG, ".png", len(PNG))]

    monkeypatch.setattr("anima.pipeline.stages.generation.fetch_prepared_images", fake_fetch)

    class FakeCore:
        llm = FakeLLM(reply="好看!")
        tools = None

        class config:
            class vision:
                enabled = True
                model = ""
                max_inline_bytes = 4 * 1024 * 1024
                comprehend_in_chat = True

    core = FakeCore()
    ctx = PipelineContext(
        event={}, text="", user_id=1, group_id=10, detail_type="group",
        sender_name="Bob", inbound_images=[{"url": "u", "file": "", "sub_type": ""}],
        meta={"core": core},
    )
    # 预置一条 system 模拟 cognition 之后
    ctx.messages = [{"role": "system", "content": "sys"}]
    res = await generation_stage(ctx)
    assert ctx.reply == "好看!"
    user_msg = ctx.messages[-1]
    assert isinstance(user_msg["content"], list)
    assert any(p.get("type") == "image_url" for p in user_msg["content"])


@pytest.mark.asyncio
async def test_memory_skips_only_when_no_text_and_no_image():
    from anima.pipeline.stages import memory_stage
    from anima.pipeline import PipelineContext, StageResult

    class FakeCore:
        config = None
        vector_memory = None

    # 无文本无图 → 跳过
    ctx1 = PipelineContext(event={}, text="", user_id=1, group_id=10,
                            detail_type="group", sender_name="Bob",
                            meta={"core": FakeCore()})
    assert await memory_stage(ctx1) == StageResult.SKIP_REPLY

    # 无文本但有图 → 继续（不再误跳过）
    ctx2 = PipelineContext(event={}, text="", user_id=1, group_id=10,
                            detail_type="group", sender_name="Bob",
                            inbound_images=[{"url": "u"}],
                            meta={"core": FakeCore()})
    # memory_stage 会调用全局 get_chat_memory（无文件，内存态），不应抛
    result = await memory_stage(ctx2)
    assert result != StageResult.SKIP_REPLY
