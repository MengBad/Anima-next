# -*- coding: utf-8 -*-
"""P2-F：图片生成额度 TOCTOU——并发调用在同一把锁内「判定→生成→消耗」串行，日限内不超配额；
图片格式魔数具名常量化。"""
import asyncio
from types import SimpleNamespace

from anima.media_generation import ImageGenerationManager, _image_suffix

_PNG = b"\x89PNG\r\n\x1a\n" + b"abc"


def _core(tmp_path, daily_limit):
    class _LLM:
        module_models = None
        async def generate_image(self, prompt, *, model=None, max_bytes=None):
            await asyncio.sleep(0.05)   # 制造 check 与 spend 之间的竞态窗口
            return _PNG
    core = SimpleNamespace(
        config=SimpleNamespace(
            persistence=SimpleNamespace(data_dir=str(tmp_path)),
            image_generation=SimpleNamespace(enabled=True, daily_limit=daily_limit, model=None,
                                             max_output_bytes=1000),
        ),
        llm=_LLM(),
        audit=SimpleNamespace(emit=lambda *a, **k: None),
    )
    return core


async def test_quota_check_and_spend_serialized_no_overspend(tmp_path):
    mgr = ImageGenerationManager(_core(tmp_path, daily_limit=1))
    results = await asyncio.gather(
        mgr._render("a", input_image_b64=None, llm_method="generate_image", tag="t"),
        mgr._render("b", input_image_b64=None, llm_method="generate_image", tag="t"),
        return_exceptions=True,
    )
    ok = [r for r in results if isinstance(r, str)]
    err = [r for r in results if isinstance(r, Exception)]
    assert len(ok) == 1, "并发下日限=1 只能成功 1 次（TOCTOU 已封死）"
    assert len(err) == 1
    assert isinstance(err[0], ValueError) and "额度已用完" in str(err[0])


def test_image_suffix_named_constants():
    assert _image_suffix(b"\x89PNG\r\n\x1a\nxyz") == ".png"
    assert _image_suffix(b"\xff\xd8\xffabc") == ".jpg"
    assert _image_suffix(b"RIFFxxxxWEBPrrrr") == ".webp"
