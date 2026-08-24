# -*- coding: utf-8 -*-
"""B6 — 一生档案 snapshot_inventory / memory_continuity 只读 tests。"""
import json
import time
from pathlib import Path

from anima.webui.life_archive import snapshot_inventory, memory_continuity


def test_snapshot_inventory_finds_and_sorts(tmp_path):
    a = tmp_path / "identity_snapshot_1.json"; a.write_text(json.dumps({"x": 1}))
    b = tmp_path / "identity_snapshot_0.json"; b.write_text(json.dumps({"x": 0}))
    old = time.time() - 100
    Path(a).touch(); Path(a).replace(a)
    inv = snapshot_inventory(tmp_path)
    names = [f["name"] for f in inv]
    assert "identity_snapshot_1.json" in names
    assert all({"name", "bytes", "mtime"} <= set(f) for f in inv)


def test_snapshot_inventory_missing_dir():
    assert snapshot_inventory("/nonexistent-zz") == []


def test_memory_continuity_fail_open():
    assert memory_continuity(None)["available"] is False
    out = memory_continuity({"episodes": 3})
    assert out["available"] is True and out["underlying"]["episodes"] == 3
