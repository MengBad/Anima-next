# -*- coding: utf-8 -*-
"""M3：time_sense 单例默认目录收口（split-brain 修复）。"""
import anima.cognitive.time_sense as ts


def test_set_time_sense_data_dir_routes_default(tmp_path, monkeypatch):
    captured = {}
    orig_cls = ts.TimeSense
    class Spy(orig_cls):
        def __init__(self, data_dir="data"):
            captured["dir"] = str(data_dir)
            super().__init__(data_dir)
    old = ts._time_sense
    try:
        ts._time_sense = None
        monkeypatch.setattr(ts, "TimeSense", Spy)
        ts.set_time_sense_data_dir(str(tmp_path / "ts"))
        ts.get_time_sense()          # 无参数默认应落到配置目录
        assert captured.get("dir") == str(tmp_path / "ts")
    finally:
        ts._time_sense = old
        ts.set_time_sense_data_dir("data")
