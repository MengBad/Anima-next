"""大图转码内容哈希 LRU 缓存。"""

from __future__ import annotations

import io

from anima.media import (
    clear_prepare_cache,
    prepare_cache_stats,
    prepare_images_for_llm,
)


def _make_png(w: int = 200, h: int = 200) -> bytes:
    from PIL import Image

    img = Image.new("RGB", (w, h), color=(40, 120, 200))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_prepare_cache_hit_same_bytes():
    clear_prepare_cache()
    data = _make_png(1600, 1200)  # 强制走转码路径（超 max_dimension 默认 1280 或体积）
    img = {"file": "x.png"}
    # 强制小预算，必转码
    r1 = prepare_images_for_llm(data, img, max_bytes=80 * 1024, max_dimension=800)
    st1 = prepare_cache_stats()
    assert r1
    assert st1["misses"] >= 1

    r2 = prepare_images_for_llm(data, img, max_bytes=80 * 1024, max_dimension=800)
    st2 = prepare_cache_stats()
    assert st2["hits"] >= 1
    assert len(r2) == len(r1)
    assert r2[0].data == r1[0].data


def test_prepare_cache_miss_different_budget():
    clear_prepare_cache()
    data = _make_png(1600, 1200)
    img = {}
    prepare_images_for_llm(data, img, max_bytes=80 * 1024, max_dimension=800)
    prepare_images_for_llm(data, img, max_bytes=40 * 1024, max_dimension=800)
    st = prepare_cache_stats()
    assert st["misses"] >= 2


def test_prepare_disk_cache_persists_across_memory_clear(tmp_path):
    """P2-3：启用磁盘缓存后，清空进程内 LRU 再算同内容仍命中磁盘缓存，字节一致。"""
    from anima import media

    media.disable_prepare_disk_cache()
    media.enable_prepare_disk_cache(tmp_path)
    try:
        clear_prepare_cache()
        data = _make_png(1600, 1200)
        img = {"file": "x.png"}
        r1 = prepare_images_for_llm(data, img, max_bytes=80 * 1024, max_dimension=800)
        assert r1

        # 清空进程内缓存，模拟「跨重启」：只靠磁盘缓存
        clear_prepare_cache()
        r2 = prepare_images_for_llm(data, img, max_bytes=80 * 1024, max_dimension=800)
        assert len(r2) == len(r1)
        assert r2[0].data == r1[0].data
        assert r2[0].ext == r1[0].ext
    finally:
        media.disable_prepare_disk_cache()
        clear_prepare_cache()


def test_prepare_disk_cache_disabled_by_default():
    """默认不启用磁盘缓存（避免无提示占盘）。"""
    from anima import media

    media.disable_prepare_disk_cache()
    try:
        assert media._DISK_CACHE_DIR is None
    finally:
        media.disable_prepare_disk_cache()
