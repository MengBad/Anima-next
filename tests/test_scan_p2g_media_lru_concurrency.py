# -*- coding: utf-8 -*-
"""P2-G：media 转码 LRU 工作线程并发安全——同锁下不越界、不崩、计数一致。"""
import concurrent.futures
import io

from PIL import Image

import anima.media as media


def _png64():
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (120, 90, 60)).save(buf, format="PNG")
    return buf.getvalue()


def test_concurrent_prepare_cache_lru_safe():
    media.clear_prepare_cache()
    png, img = _png64(), {"type": "image"}
    def work(_):
        return media.prepare_images_for_llm(png, img, use_cache=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(work, range(24)))
    assert results and all(isinstance(r, list) and len(r) >= 1 for r in results)
    stats = media.prepare_cache_stats()
    assert stats["size"] <= stats["max"], "LRU 不得越界"
    # 再次单线程应命中缓存
    media.prepare_images_for_llm(png, img, use_cache=True)
    assert media.prepare_cache_stats()["hits"] >= 1
    media.clear_prepare_cache()
