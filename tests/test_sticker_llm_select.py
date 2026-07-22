"""LLM 视觉终选（发送时从候选池挑最贴的）单测。

mock anima.cognitive.vision.get_vision_manager，验证：
- 多候选时 LLM 终选编号生效
- 单候选短路直返（不烧视觉）
- 视觉异常时本地兜底仍能选出
- 候选池遵守尺寸过滤（大图被排除）
"""
import asyncio
import pytest

from anima.cognitive.stickers import StickerManager

JPG = b"\xff\xd8\xff\x00\x01\x02\x03"


class FakeVision:
    def __init__(self, select_index=-1):
        self.select_index = select_index
        self.select_calls = 0

    async def classify(self, *a, **k):
        return {"is_sticker": True, "labels": [], "category": "表情包"}

    async def select(self, llm, query, candidates, model=""):
        self.select_calls += 1
        return self.select_index


def _seed(mgr, n=3, large_idx=None):
    meta = {}
    for i in range(n):
        h = f"abc{i:037d}"
        ext = ".jpg"
        (mgr._dir / f"{h}{ext}").write_bytes(JPG)
        sz = 800 * 1024 if i == large_idx else 1000
        meta[h] = {
            "hash": h, "ext": ext, "src_user": "u", "src_group": "g",
            "time": 1.0, "size_bytes": sz,
            "labels": ["开心"] if i == 0 else ["无语"],
            "category": "表情包", "is_sticker": True,
            "ctx_text": "", "used": 0, "status": "approved",
        }
    mgr._write_meta(meta)


@pytest.fixture
def mgr(tmp_path):
    return StickerManager(str(tmp_path / "data"))


@pytest.mark.asyncio
async def test_llm_select_picks_index(mgr):
    _seed(mgr, n=3)
    import anima.cognitive.vision as V
    vision = FakeVision(select_index=1)
    orig = V.get_vision_manager
    # 先抓取选择前的候选池作为基准（选择会改变 recent，重算顺序会变）
    pool = mgr.candidate_pool("想发个无语的", "g", False, k=3)
    V.get_vision_manager = lambda *a, **k: vision
    try:
        path = await mgr.llm_select(object(), "想发个无语的", group_id="g", k=3)
    finally:
        V.get_vision_manager = orig
    assert path == pool[1][0]
    assert vision.select_calls == 1


@pytest.mark.asyncio
async def test_llm_select_single_candidate_shortcircuits(mgr):
    _seed(mgr, n=1)
    import anima.cognitive.vision as V
    vision = FakeVision(select_index=0)
    orig = V.get_vision_manager
    V.get_vision_manager = lambda *a, **k: vision
    try:
        path = await mgr.llm_select(object(), "随便", group_id="g", k=8)
    finally:
        V.get_vision_manager = orig
    assert path is not None
    assert vision.select_calls == 0  # 单候选不调视觉


@pytest.mark.asyncio
async def test_llm_select_failure_falls_back(mgr):
    _seed(mgr, n=3)
    import anima.cognitive.vision as V

    class BoomVision(FakeVision):
        async def select(self, *a, **k):
            raise RuntimeError("boom")

    orig = V.get_vision_manager
    V.get_vision_manager = lambda *a, **k: BoomVision()
    try:
        path = await mgr.llm_select(object(), "无语", group_id="g", k=3)
    finally:
        V.get_vision_manager = orig
    assert path is not None  # 视觉挂了仍本地兜底选出


@pytest.mark.asyncio
async def test_candidate_pool_respects_size_filter(mgr):
    _seed(mgr, n=2, large_idx=1)  # idx1 是 800KB 大图
    pool = mgr.candidate_pool("无语", "g", False, k=8)
    paths = [p for p, _, _ in pool]
    # 大图（idx1）应被 _eligible 的尺寸过滤排除
    assert len(pool) == 1
    assert "abc0000000000000000000000000000000001" not in paths[0]
