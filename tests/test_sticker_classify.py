"""LLM 视觉分类闸门（导入时判定是否为表情包）单测。

通过 mock anima.cognitive.vision.get_vision_manager 注入假视觉，验证：
- 非表情包被拒收（不入库）
- 表情包被接收并写入 labels/category/is_sticker
- 视觉调用异常时按 unknown 兜底入库存（不丢图、不崩）
"""
import asyncio
import pytest

from anima.cognitive.stickers import StickerManager

JPG = b"\xff\xd8\xff\x00\x01\x02\x03"  # 最小 jpg magic，_guess_ext -> .jpg


class FakeVision:
    def __init__(self, verdict):
        self.verdict = verdict
        self.calls = 0

    async def classify(self, llm, data, img, model=""):
        self.calls += 1
        return self.verdict

    async def select(self, *a, **k):
        return -1


@pytest.fixture
def mgr(tmp_path):
    return StickerManager(str(tmp_path / "data"))


async def _collect(mgr, vision, llm=None):
    import anima.cognitive.vision as V

    async def _resolver(img):
        return JPG

    orig = V.get_vision_manager
    V.get_vision_manager = lambda *a, **k: vision
    try:
        await mgr.collect(
            [{"url": "", "file": "", "file_id": ""}], "u", "g",
            resolver=_resolver, llm=llm)
    finally:
        V.get_vision_manager = orig


@pytest.mark.asyncio
async def test_classify_rejects_photo(mgr):
    vision = FakeVision({"is_sticker": False, "labels": [], "category": "照片"})
    await _collect(mgr, vision, llm=object())
    assert mgr.count() == 0
    assert vision.calls == 1


@pytest.mark.asyncio
async def test_classify_accepts_sticker(mgr):
    vision = FakeVision({"is_sticker": True, "labels": ["狗头", "嘲讽"],
                         "category": "梗图"})
    await _collect(mgr, vision, llm=object())
    assert mgr.count() == 1
    item = next(iter(mgr._read_meta().values()))
    assert item["labels"] == ["狗头", "嘲讽"]
    assert item["category"] == "梗图"
    assert item["is_sticker"] is True


@pytest.mark.asyncio
async def test_classify_failure_keeps_unknown(mgr):
    class BoomVision(FakeVision):
        async def classify(self, *a, **k):
            raise RuntimeError("boom")

    await _collect(mgr, BoomVision({"is_sticker": True}), llm=object())
    assert mgr.count() == 1  # 视觉挂了也不丢图，按 unknown 入库存
    item = next(iter(mgr._read_meta().values()))
    assert item["is_sticker"] is None
    assert item["labels"] == []


@pytest.mark.asyncio
async def test_classify_stores_emotion(mgr):
    """视觉分类应把 emotion 一并写入元数据。"""
    vision = FakeVision({"is_sticker": True, "labels": ["狗头"],
                         "category": "梗图", "emotion": "嘲讽"})
    await _collect(mgr, vision, llm=object())
    assert mgr.count() == 1
    item = next(iter(mgr._read_meta().values()))
    assert item["emotion"] == "嘲讽"


@pytest.mark.asyncio
async def test_pick_by_emotion_accepts_english_canonical(mgr):
    # sticker_emotion 输出英文 canonical，pick_by_emotion 应归一为中文字表（happy->开心）
    vision = FakeVision({'is_sticker': True, 'labels': ['狗头'],
                         "category": "梗图", "emotion": "开心"})
    await _collect(mgr, vision, llm=object())
    h = next(iter(mgr._read_meta()))
    mgr.approve(h)
    zh = mgr.pick_by_emotion("开心")
    en = mgr.pick_by_emotion("happy")
    assert zh is not None and en == zh


@pytest.mark.asyncio
async def test_classify_stores_scenes_desc(mgr):
    # 复刻 stealer 富分类：scenes(1-2) 与 desc 应产出并落库
    vision = FakeVision({
        "is_sticker": True, "labels": ["狗头", "嘲讽"],
        "category": "梗图", "emotion": "嘲讽",
        "scenes": ["群聊斗图"], "desc": "一只狗头在嘲讽"})
    await _collect(mgr, vision, llm=object())
    assert mgr.count() == 1
    item = next(iter(mgr._read_meta().values()))
    assert item["scenes"] == ["群聊斗图"]
    assert item["desc"] == "一只狗头在嘲讽"


def test_classify_contract_has_scenes_desc():
    # VLM 富分类契约应声明 scenes/desc（源级回归护栏）
    import pathlib
    src = pathlib.Path("anima/cognitive/vision.py").read_text(encoding="utf-8")
    assert '"scenes"' in src and '"desc"' in src
    assert '1-2 个场景' in src
