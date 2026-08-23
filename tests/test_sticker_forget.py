"\"\"\"表情包自审查/自过审 + 自然遗忘 落地测试(隔离 tmp_path)。\"\"\""
import asyncio
import json
import time

from anima.cognitive.stickers import StickerManager


def _img(tmp_path, name, body):
    p = tmp_path / name
    p.write_bytes(body)
    return str(p)


def test_self_review_pass_helper():
    p = StickerManager._self_review_pass
    assert p(["开心"], "reaction", "开心", True) is True
    assert p([], "reaction", "", None) is True
    assert p([], "unknown", "", None) is False
    assert p([], "unknown", "", True) is False
    assert p(["开心"], "reaction", "", False) is False


def test_self_review_keeps_ambiguous_pending(tmp_path):
    mgr = StickerManager(str(tmp_path), max_stickers=10, label_throttle_per_user=0,
                         self_review=True)
    f = _img(tmp_path, "amb.jpg", b"x" * 64)
    asyncio.run(mgr.collect([{"url": "", "file": f}], 123, 999, "x",
                            approval_required=True))
    item = list(mgr._read_meta().values())[0]
    assert item["status"] == "pending_review"
    assert mgr.pick("任意") is None


def _label_llm():
    class L:
        def __init__(self):
            self.labels = ["开心"]
            self.calls = 0
        async def chat_structured(self, *a, **k):
            self.calls += 1
            return {"tool_calls": [{"function": {"name": "record_sticker_labels",
                                          "arguments": json.dumps({"labels": self.labels})}}]}
    return L()


def test_label_pending_auto_approves_with_self_review(tmp_path):
    mgr = StickerManager(str(tmp_path), max_stickers=10, label_throttle_per_user=0,
                         self_review=True)
    f = _img(tmp_path, "s.jpg", b"y" * 64)
    asyncio.run(mgr.collect([{"url": "", "file": f}], 123, 999, "笑死",
                            approval_required=True))
    key = list(mgr._read_meta())[0]
    assert mgr._read_meta()[key]["status"] == "pending_review"
    n = asyncio.run(mgr.label_pending(_label_llm()))
    assert n == 1
    item = mgr._read_meta()[key]
    assert item["status"] == "approved"
    assert item.get("review_method") == "self"
    assert item.get("reviewed_at")
    assert mgr.random_one(group_id=999).endswith(".jpg")


def test_natural_forget_removes_old_cold_only(tmp_path):
    mgr = StickerManager(str(tmp_path), max_stickers=50, label_throttle_per_user=0,
                         natural_forget=True, forget_age_days=1.0,
                         forget_used_max=0, forget_last_used_days=1.0)
    names = ["a", "b", "c", "d"]
    files = [_img(tmp_path, n + ".jpg", (n * 40).encode()) for n in names]
    asyncio.run(mgr.collect([{"url": "", "file": fp} for fp in files], 123, 999, "x"))
    meta = mgr._read_meta()
    ha, hb, hc, hd = sorted(meta)
    old = time.time() - 10 * 86400
    meta[ha]["time"] = old; meta[ha]["used"] = 0; meta[ha]["last_used"] = 0
    meta[hb]["src_user"] = "owner_upload"; meta[hb]["time"] = old
    meta[hc]["time"] = old; meta[hc]["used"] = 9
    meta[hd]["time"] = old; meta[hd]["used"] = 0; meta[hd]["last_used"] = time.time() - 3600
    mgr._write_meta(meta)

    forgotten = mgr.natural_forget()
    meta_now = mgr._read_meta()
    assert forgotten == 1
    assert ha not in meta_now
    assert hb in meta_now
    assert hc in meta_now
    assert hd in meta_now
    assert mgr.stats()["forgotten_total"] == 1
