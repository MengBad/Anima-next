# -*- coding: utf-8 -*-
"""M3：mood 单例默认目录收口（split-brain 修复，镜像 relation M2.4）。"""
import anima.cognitive.mood as mood


def test_set_mood_data_dir_routes_default(tmp_path):
    captured = {}
    orig_cls = mood.MoodSystem
    class Spy(orig_cls):
        def __init__(self, data_dir="data"):
            captured["dir"] = str(data_dir)
            super().__init__(data_dir)
    old_sys = mood._mood_system
    try:
        mood._mood_system = None
        mood.MoodSystem = Spy
        mood.set_mood_data_dir(str(tmp_path / "mood"))
        mood.get_mood_system()          # 无参数默认应落到配置目录
        assert captured.get("dir") == str(tmp_path / "mood")
    finally:
        mood.MoodSystem = orig_cls
        mood._mood_system = old_sys
        mood.set_mood_data_dir("data")
