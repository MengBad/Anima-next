"""M4 /meme 指令集：manager 运行时状态 + handle_meme_command 各子命令。"""
from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import anima.cognitive.stickers as _S
from anima.cognitive.meme_commands import handle_meme_command


def _mgr(tmp_path):
    return _S.StickerManager(str(tmp_path))


@contextmanager
def _installed(tmp_path):
    prev = _S._manager
    mgr = _S.StickerManager(str(tmp_path))
    _S._manager = mgr
    try:
        yield mgr
    finally:
        _S._manager = prev


def _core(tmp_path):
    sc = SimpleNamespace(max_stickers=500, pending_capacity=200,
                         default_scope="local", always_atmosphere=("humorous",))
    pers = SimpleNamespace(data_dir=str(tmp_path))
    cfg = SimpleNamespace(sticker=sc, persistence=pers)
    return SimpleNamespace(config=cfg)


def _seed(mgr):
    import asyncio
    asyncio.run(mgr.import_uploads([("a.png", b"IMGDATA")],
        category="dog", approval_required=False, auto_analyze=False))
    meta = mgr._read_meta()
    for h, it in meta.items():
        it["emotion"] = "开心"
        it["labels"] = ["狗头", "滑稽"]
    mgr._write_meta(meta)
    return mgr


def test_runtime_set_and_get(tmp_path):
    mgr = _mgr(tmp_path)
    mgr.set_auto_collect(False)
    assert mgr.runtime_auto_collect() is False
    mgr.set_capacity(5)
    assert mgr.runtime_capacity() == 5
    mgr.set_send_probability(0.1)
    assert abs(mgr.runtime_send_probability() - 0.1) < 1e-6
    mgr.set_default_scope("public")
    assert mgr.runtime_default_scope() == "public"
    mgr.add_blacklist("group:1")
    assert "group:1" in mgr.runtime_blocklist()
    mgr.remove_blacklist("group:1")
    assert mgr.runtime_blocklist() == []
    rt = mgr.runtime_summary()
    assert rt["auto_collect"] is False and rt["pending_capacity"] == 5


def test_emotion_and_tag_stats(tmp_path):
    mgr = _seed(_mgr(tmp_path))
    st = mgr.emotion_stats()
    assert sum(st.values()) == mgr.count() and st.get("开心") == mgr.count()
    ts = mgr.tag_stats()
    assert ts["categories"]["dog"] == mgr.count()
    assert ts["labels"]["狗头"] == mgr.count()


def test_clear_cache_and_rebuild_no_crash(tmp_path):
    mgr = _mgr(tmp_path)
    assert mgr.clear_emotion_cache() == 0
    mgr.rebuild_index()  # 不崩


def test_meme_status_and_list(tmp_path):
    with _installed(tmp_path) as mgr:
        _seed(mgr)
        core = _core(tmp_path)
        r = handle_meme_command("meme status", "", core)
        assert "表情库" in r and str(mgr.count()) in r
        r2 = handle_meme_command("meme list 5", "", core)
        assert "dog" in r2


def test_meme_runtime_toggles(tmp_path):
    with _installed(tmp_path) as mgr:
        core = _core(tmp_path)
        assert "开启" in handle_meme_command("meme on", "", core)
        assert mgr.runtime_auto_collect() is True
        assert "关闭" in handle_meme_command("meme off", "", core)
        assert mgr.runtime_auto_collect() is False
        assert "开启" in handle_meme_command("meme auto_on", "", core)
        assert abs(mgr.runtime_send_probability() - 0.35) < 1e-6
        assert "关闭" in handle_meme_command("meme auto_off", "", core)
        assert abs(mgr.runtime_send_probability() - 0.0) < 1e-6


def test_meme_capacity_blacklist_scope(tmp_path):
    with _installed(tmp_path) as mgr:
        core = _core(tmp_path)
        assert "已设" in handle_meme_command("meme capacity 3", "", core)
        assert mgr.runtime_capacity() == 3
        assert "已加入" in handle_meme_command("meme blacklist add group:9", "", core)
        assert "group:9" in mgr.runtime_blocklist()
        assert "已设" in handle_meme_command("meme scope public", "", core)
        assert mgr.runtime_default_scope() == "public"


def test_meme_unrecognized_returns_none(tmp_path):
    with _installed(tmp_path):
        assert handle_meme_command("meme no_such_sub", "", _core(tmp_path)) is None
