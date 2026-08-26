# -*- coding: utf-8 -*-
"""P2-A：chat_memory 单例默认落盘路径收口——set_chat_memory_persist_path 后无参调用不再锚死空路径(永不落盘)。"""
import anima.memory.chat_memory as cm


def test_set_chat_memory_persist_path_default(tmp_path, monkeypatch):
    captured = {}
    orig = cm.ChatMemory
    class Spy(orig):
        def __init__(self, max_rounds=20, persist_path=""):
            captured["path"] = persist_path
            super().__init__(max_rounds=max_rounds, persist_path=persist_path)
    old = cm._chat_memory
    try:
        cm._chat_memory = None
        monkeypatch.setattr(cm, "ChatMemory", Spy)
        cm.set_chat_memory_persist_path(str(tmp_path / "chat_memory.json"))
        cm.get_chat_memory()          # 无参默认应落到配置目录的落盘路径
        assert captured.get("path") == str(tmp_path / "chat_memory.json")
    finally:
        cm._chat_memory = old
        cm.set_chat_memory_persist_path("")
