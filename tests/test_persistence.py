"""原子写测试。"""

import json
from anima.core.persistence import PersistenceProvider


def test_atomic_roundtrip(tmp_path) -> None:
    p = PersistenceProvider(str(tmp_path))
    p.write_json("state/a.json", {"x": 1})

    raw = json.loads((tmp_path / "state" / "a.json").read_text("utf-8"))
    assert raw["x"] == 1

    loaded = p.read_json("state/a.json")
    assert loaded["x"] == 1


def test_missing_default(tmp_path) -> None:
    p = PersistenceProvider(str(tmp_path))
    assert p.read_json("nope.json", {"d": True})["d"] is True
    assert p.read_text("nope.txt", "fallback") == "fallback"
