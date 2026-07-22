"""记忆落盘硬化回归（工单5，P0-5）。

旧逻辑仅 ~10% 概率 save，崩溃窗口可能丢大量对话。新逻辑：累计写入 >=10 条
或距上次落盘 >=60s 必落盘，丢数据窗口有上界。
"""
from __future__ import annotations

import time

from anima.memory.chat_memory import ChatMemory


def test_add_persists_within_count_threshold(tmp_path):
    p = tmp_path / "chat_memory.json"
    mem = ChatMemory(persist_path=str(p))
    for i in range(10):
        mem.add("group", 727631526, "user", "小明", f"消息{i}", user_id=10001)
    assert p.exists(), "达到条数阈值后必须落盘"
    text = p.read_text(encoding="utf-8")
    assert "消息9" in text, "最近写入的内容应已落盘"


def test_add_persists_within_time_window(tmp_path):
    p = tmp_path / "chat_memory.json"
    mem = ChatMemory(persist_path=str(p))
    mem.add("group", 727631526, "user", "小明", "第一条", user_id=10001)
    # 未达条数阈值，但强制时间窗口：把 _last_save 拨回 61s 前
    mem._last_save = time.time() - 61
    mem.add("group", 727631526, "user", "小明", "第二条", user_id=10001)
    assert p.exists(), "超过时间窗口必须落盘"
    assert "第二条" in p.read_text(encoding="utf-8")


def test_save_clears_dirty(tmp_path):
    p = tmp_path / "chat_memory.json"
    mem = ChatMemory(persist_path=str(p))
    mem.add("group", 727631526, "user", "小明", "x", user_id=10001)
    # 手动触发一次 save，_dirty 应被清零
    mem.save()
    assert mem._dirty == 0


def test_restore_backs_up_legacy_buckets(tmp_path):
    """P1-5：旧版群级共享桶（无 uid 段）恢复时丢弃，但先备份到 legacy.bak.json。"""
    import json

    p = tmp_path / "chat_memory.json"
    data = {
        "group_123": {"round_1": [{
            "role": "user", "content": "旧共享桶", "time": 1.0, "ts": 1.0,
            "salience": 0.5, "access_count": 0, "emotion": 0.0, "retention_score": 1.0,
        }]},
        "group_456_10001": {"round_1": [{
            "role": "user", "content": "新桶", "time": 1.0, "ts": 1.0,
            "salience": 0.5, "access_count": 0, "emotion": 0.0, "retention_score": 1.0,
        }]},
    }
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    mem = ChatMemory(persist_path=str(p))

    bak = p.with_name("chat_memory.legacy.bak.json")
    assert bak.exists(), "旧桶应被备份到 legacy.bak.json"
    bak_data = json.loads(bak.read_text(encoding="utf-8"))
    assert "group_123" in bak_data, "旧桶内容应进入备份"
    # 旧桶不应进入内存 store（防串台），新桶保留
    assert "group_123" not in mem._store
    assert "group_456_10001" in mem._store


def test_forget_user_removes_private_and_group_archives(tmp_path):
    mem = ChatMemory(persist_path=str(tmp_path / "chat_memory.json"))
    mem.add("private", 7, "user", "u", "私聊", user_id=7)
    mem.add("group", 100, "user", "u", "群聊", user_id=7)
    mem.add("group", 100, "user", "other", "保留", user_id=8)
    assert mem.forget_user(7) == 2
    assert "private_7" not in mem._store
    assert "group_100_7" not in mem._store
    assert "group_100_8" in mem._store


def test_export_user_only_includes_own_archives(tmp_path):
    mem = ChatMemory(persist_path=str(tmp_path / "chat_memory.json"))
    mem.add("group", 100, "user", "u", "mine", user_id=7)
    mem.add("group", 100, "user", "other", "theirs", user_id=8)
    exported = mem.export_user(7)
    assert list(exported) == ["group_100_7"]
    assert exported["group_100_7"][0]["content"] == "mine"


def test_restore_strips_cq_media_payload_from_context(tmp_path):
    import json

    p = tmp_path / "chat_memory.json"
    p.write_text(json.dumps({"private_7": {"round_1": [
        {"role": "bot", "name": "Anima", "content": "喏 [CQ:image,file=deadbeef.jpg]",
         "time": 1, "ts": 1},
        {"role": "user", "name": "u", "content": "[CQ:image,file=cafebabe.jpg]",
         "time": 2, "ts": 2},
    ]}}, ensure_ascii=False), encoding="utf-8")
    mem = ChatMemory(persist_path=str(p))
    context = mem.get_context("private", 7, user_id=7)
    assert context[0]["content"].endswith("喏")
    assert context[1]["content"].endswith("[用户发送了图片]")
    assert "cafebabe" not in p.read_text(encoding="utf-8")
