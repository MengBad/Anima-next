# -*- coding: utf-8 -*-
"""P2-B：中央 Paths 门面——唯一接线点、子路径解析、未接线回退 data、anima_core 唯一接线。"""
import inspect
from pathlib import Path

import anima.core.paths as paths


def test_set_data_dir_and_child(tmp_path):
    paths.set_data_dir(str(tmp_path))
    assert paths.data_dir() == Path(str(tmp_path))
    assert paths.child("crossgroup", "g") == Path(str(tmp_path)) / "crossgroup" / "g"
    paths.set_data_dir("data")


def test_paths_named_resolve(tmp_path):
    p = paths.Paths(str(tmp_path))
    assert p.base == Path(str(tmp_path))
    assert p.chat_memory == Path(str(tmp_path)) / "chat_memory.json"
    assert p.crossgroup == Path(str(tmp_path)) / "crossgroup"
    assert p.resolve("selfcode", "x") == Path(str(tmp_path)) / "selfcode" / "x"


def test_unset_fallback_to_data():
    paths._cached_base = None
    assert paths.data_dir() == Path("data"), "未接线应 fail-safe 回退 data"
    paths.set_data_dir("data")


def test_anima_core_wires_facade_once():
    import anima.core.anima_core as ac
    src = inspect.getsource(ac.AnimaCore._init_cognitive_state)
    assert "paths.set_data_dir" in src, "anima_core 必须经中央 Paths 门面接线 persistence.data_dir"
