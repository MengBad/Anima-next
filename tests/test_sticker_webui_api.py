"""M8：WebUI 双区管理的服务端契约 —— search_assets 筛选/排序/分页切片 + update_assets 分类 CRUD + delete_assets。"""
from __future__ import annotations

from anima.cognitive.stickers import StickerManager


def _seed(tmp_path):
    mgr = StickerManager(str(tmp_path), label_throttle_per_user=0)
    meta = {}
    rows = {"happy": "开心", "sad": "委屈", "angry": "生气", "calm": "平静"}
    for j, (cat, emo) in enumerate(rows.items()):
        hj = f"h{j:030d}"
        (mgr._dir / f"{hj}.jpg").write_bytes(b"\xff\xd8\xff" + str(j).encode())
        meta[hj] = {"hash": hj, "ext": ".jpg", "src_user": "u", "src_group": "g",
                    "time": float(j), "size_bytes": 10, "labels": ["x"],
                    "category": cat, "emotion": emo, "is_sticker": True,
                    "ctx_text": "", "used": j, "status": "approved", "scope": "local"}
    # 把 happy 设为 public，供 scope 筛选
    next(iter(meta))  # noqa
    h0 = sorted(meta)[0]
    meta[h0]["scope"] = "public"
    meta[h0]["used"] = 99
    mgr._write_meta(meta)
    return mgr


def test_search_assets_filters_category_scope(tmp_path):
    mgr = _seed(tmp_path)
    rows = mgr.search_assets(category="happy")
    assert rows and all(r["category"] == "happy" for r in rows)
    pub = mgr.search_assets(scope="public")
    assert pub and all(r["scope"] == "public" for r in pub)


def test_search_assets_sort_and_page_slice(tmp_path):
    mgr = _seed(tmp_path)
    by_used = mgr.search_assets(sort="used")
    # used 降序：happy(99) 应排最前
    assert by_used[0]["category"] == "happy"
    by_oldest = mgr.search_assets(sort="oldest")
    # oldest 升序：time 最小(0) 在最前
    assert by_oldest[0]["created_at"] == 0.0
    page = mgr.search_assets(sort="newest")[:2]
    assert len(page) == 2  # 服务端分页切片


def test_update_assets_classification_crud(tmp_path):
    mgr = _seed(tmp_path)
    h = mgr.search_assets(category="happy")[0]["id"]
    n = mgr.update_assets([h], category="萌点", scope="public", status="approved")
    assert n == 1
    it = mgr._read_meta()[h]
    assert it["category"] == "萌点" and it["scope"] == "public"


def test_delete_assets(tmp_path):
    mgr = _seed(tmp_path)
    before = mgr.count()
    h = mgr.search_assets(category="sad")[0]["id"]
    n = mgr.delete_assets([h])
    assert n == 1 and mgr.count() == before - 1