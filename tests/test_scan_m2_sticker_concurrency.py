# -*- coding: utf-8 -*-
"""M2.3 并发一致性冒烟：collect 每张图写前重读底座(380)已是近原子；
lock 包装额外把整批 collect/label_pending/import_uploads/reclassify_all
串行化（label_pending/reclassify_all 才是真正"读->await->写"跨窗口的写者）。
此测试验证锁下并发 collect 不丢不坏（不声称复现无锁竞态）。"""
import asyncio

from anima.cognitive.stickers import StickerManager


def _img(path, name, body):
    p = path / name
    p.write_bytes(body)
    return str(p)


def test_concurrent_collect_consistent(tmp_path):
    mgr = StickerManager(str(tmp_path), max_stickers=100, label_throttle_per_user=0)
    a = _img(tmp_path, "aa.jpg", b"\xff\xd8\xff\x00" + b"A" * 96)
    b = _img(tmp_path, "bb.jpg", b"\xff\xd8\xff\x00" + b"B" * 96)

    async def run():
        await asyncio.gather(
            mgr.collect([{"url": "", "file": a, "sub_type": ""}], 1001, 9001, "ha"),
            mgr.collect([{"url": "", "file": b, "sub_type": ""}], 1002, 9001, "hb"),
        )

    asyncio.run(run())
    assert mgr.count() == 2, "并发 collect 后应完整保留两张（无丢失/覆盖）"
    # 元数据文件可正常加载、且 key 与该目录文件一致
    raw = (tmp_path / "stickers.json").read_text(encoding="utf-8")
    import json as _j
    meta = _j.loads(raw)
    assert set(meta.keys()) == set(mgr._read_meta().keys())


def test_collect_still_dedups_and_counts(tmp_path):
    mgr = StickerManager(str(tmp_path), max_stickers=10, label_throttle_per_user=0)
    f = _img(tmp_path, "src.jpg", b"\xff\xd8\xff\x00fakejpg")
    asyncio.run(mgr.collect([{"url": "", "file": f, "sub_type": ""}], 123, 999, "ha"))
    asyncio.run(mgr.collect([{"url": "", "file": f, "sub_type": ""}], 123, 999, "ha"))
    assert mgr.count() == 1
