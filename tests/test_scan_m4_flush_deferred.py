# -*- coding: utf-8 -*-
"""M4：flush_deferred 写失败项回退延迟队列，不再静默丢失。"""
from anima.core.persistence import PersistenceProvider


def test_flush_deferred_retries_failed_items(tmp_path):
    p = PersistenceProvider(str(tmp_path))
    p.write_json_deferred("a.json", {"x": 1})
    p.write_json_deferred("b.json", {"y": 2})

    calls = {"n": 0}
    original = p.write_json

    def flaky(rel_path, obj):
        calls["n"] += 1
        if rel_path == "a.json" and calls["n"] == 1:
            raise OSError("boom")
        return original(rel_path, obj)

    p.write_json = flaky
    assert p.flush_deferred() == 1      # a.json 失败，b.json 成功
    assert p.has_deferred is True       # 失败项已回退待重试
    assert p.flush_deferred() == 1      # 重试成功写出 a.json
    assert p.has_deferred is False
    assert (tmp_path / "a.json").exists()
    assert (tmp_path / "b.json").exists()
