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


def test_chat_memory_no_round_collision_after_eviction(tmp_path):
    """ANM-007 回归：LRU 淘汰后轮号不得碰撞，后期用户消息必须仍可召回。

    旧实现用 len(od) 命名新 round：淘汰使 od 长度 < 最大轮号，下一次 bot 回复
    会用已存在的 round 键覆盖最新非空轮，导致该轮用户消息静默丢失（上下文断裂）。
    构造 25 轮（max_rounds=20）验证：第 21–25 轮用户消息在 get_context 仍可召回，
    且 round 编号连续无碰撞。
    """
    mem = ChatMemory(max_rounds=20, persist_path=str(tmp_path / "chat_memory.json"))
    gid, uid = 727631526, 10001
    for i in range(1, 26):
        mem.add("group", gid, "user", f"用户{i}", f"用户消息第{i}轮", user_id=uid)
        mem.add("group", gid, "bot", "Anima", f"bot第{i}轮回复", user_id=uid)

    # 1) 第 21–25 轮用户消息必须仍可召回（证明未被碰撞覆盖丢）
    ctx = mem.get_context("group", gid, recent_rounds=20, user_id=uid)
    merged = "\n".join(c["content"] for c in ctx)
    for i in range(21, 26):
        assert f"用户消息第{i}轮" in merged, f"第 {i} 轮用户消息丢失（轮碰撞）"

    # 2) 内部 round 编号必须连续、无碰撞
    key = f"group_{gid}_{uid}"
    od = mem._store[key]
    nums = sorted(int(rk[len("round_"):]) for rk in od if rk.startswith("round_"))
    assert nums == list(range(nums[0], nums[-1] + 1)), f"round 编号不连续：{nums}"
    # 3) 第 21–25 轮桶内应确实含对应用户条目（直接验证存储未被覆盖）
    for i in range(21, 26):
        rk = f"round_{i}"
        assert rk in od, f"{rk} 不存在"
        assert any(e.get("role") == "user" and f"用户消息第{i}轮" in e.get("content", "")
                   for e in od[rk]), f"{rk} 内用户消息被覆盖"


def test_restore_backs_up_corrupt_file(tmp_path):
    """ANM-007 回归：记忆文件损坏时，_restore 先备份到 .corrupt.bak.json 再跳过，不崩溃。"""
    p = tmp_path / "chat_memory.json"
    p.write_text("{ 这不是合法 json,,,", encoding="utf-8")
    mem = ChatMemory(persist_path=str(p))  # 不应抛异常
    assert len(mem._store) == 0, "损坏文件恢复应回退空记忆"
    bak = p.with_name("chat_memory.corrupt.bak.json")
    assert bak.exists(), "损坏文件应被备份到 .corrupt.bak.json"
    assert "这不是合法" in bak.read_text(encoding="utf-8")


# ── 兜底落盘路径（2026-10-28）────────────────────────────────────────

def test_get_chat_memory_has_fallback_path():
    """无参 `get_chat_memory()` 必须有兜底路径，不能锚死空串。

    ## 问题

    `set_chat_memory_persist_path()` 只在 `AnimaCore.start()` 里调。
    服务进程正常 —— 但**任何外部脚本 / 调试工具 / 一次性 CLI 都拿到空 store**：

        >>> from anima.memory.chat_memory import get_chat_memory
        >>> len(get_chat_memory()._store)
        0                      ← 而 data/chat_memory.json 有 1.3MB

    根因：`persist_path=None` ⇒ `ChatMemory._restore()` 直接返回，不读文件。

    ## 修法

    未显式设置时回退到 `data/chat_memory.json`（与 config 默认一致）。
    生产若用非默认 data_dir，`set_chat_memory_persist_path()` 仍会覆盖它
    —— 服务进程走那条路，行为零变化。
    """
    import anima.memory.chat_memory as cm_mod

    cm_mod.reset_chat_memory()
    cm_mod._default_persist_path = ""        # 模拟「未设置」
    m = cm_mod.get_chat_memory()
    assert m._persist_path is not None, "兜底路径没生效（仍是 None）"
    assert str(m._persist_path).endswith("chat_memory.json")
    cm_mod.reset_chat_memory()


def test_explicit_path_overrides_fallback():
    """显式传入的路径优先级最高。"""
    import anima.memory.chat_memory as cm_mod

    cm_mod.reset_chat_memory()
    m = cm_mod.get_chat_memory("/tmp/custom_cm.json")
    assert str(m._persist_path).replace("\\", "/") == "/tmp/custom_cm.json"
    cm_mod.reset_chat_memory()


def test_set_path_overrides_fallback():
    """`set_chat_memory_persist_path()` 设置后优先于兜底。"""
    import anima.memory.chat_memory as cm_mod

    cm_mod.reset_chat_memory()
    cm_mod.set_chat_memory_persist_path("/tmp/set_cm.json")
    m = cm_mod.get_chat_memory()
    assert str(m._persist_path).replace("\\", "/") == "/tmp/set_cm.json"
    cm_mod.reset_chat_memory()
    cm_mod._default_persist_path = ""
