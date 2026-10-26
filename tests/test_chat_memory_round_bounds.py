"""轮边界双闸门回归（2026-10-27）。

## 事故

`round` 原本**只在 bot 回复后**才切下一轮（`add()` 里 `if role == "bot"`）。
bot 不回复的群聊时段会把这些用户消息**无限堆进同一轮** ⇒「一轮」不是一轮
对话，而是按用户累积的桶。生产实测：

    146 个轮的跨度 > 1 小时
    最长 round_55：222 小时（9 天）、93 条消息
    round_156：17:12→18:40 近 1.5 小时、15 条消息、含 6 次 /say
    round_146：10-25 22:18 与 10-26 11:54 同处一轮（跨 13 小时）

后果：`get_context(recent_rounds=8)` 的 8 个「轮」可能横跨好几天，
`memory_stage` 的 `max_age_minutes=30` 把超龄条目滤掉后，
真实可用上下文被严重稀释；且模型会把 9 天前的话题当成仍在继续。
"""
from __future__ import annotations

from anima.memory.chat_memory import ChatMemory


def _mk(tmp_path):
    return ChatMemory(max_rounds=20, persist_path=str(tmp_path / "cm.json"))


def test_size_gate_splits_round_without_bot_reply(tmp_path):
    """bot 一直不回复时，用户消息也不能无限堆进同一轮（条数闸）。"""
    mem = _mk(tmp_path)
    gid, uid = 727631526, 10001
    n = mem.MAX_ENTRIES_PER_ROUND * 2 + 5
    for i in range(n):
        mem.add("group", gid, "user", f"u{i}", f"消息{i}", user_id=uid)

    od = mem._store[f"group_{gid}_{uid}"]
    assert len(od) >= 2, "条数闸没有生效 —— 所有用户消息还在同一轮里"
    for rk, entries in od.items():
        if entries:  # 末尾可能有 bot 留下的空轮
            assert len(entries) <= mem.MAX_ENTRIES_PER_ROUND, (
                f"{rk} 有 {len(entries)} 条，超过上限 {mem.MAX_ENTRIES_PER_ROUND}")


def test_size_gate_does_not_lose_messages(tmp_path):
    """切轮不能吞消息：切完后所有消息仍可召回（去重后条数一致）。"""
    mem = _mk(tmp_path)
    gid, uid = 727631526, 10001
    n = mem.MAX_ENTRIES_PER_ROUND + 10
    for i in range(n):
        mem.add("group", gid, "user", f"u{i}", f"消息{i}", user_id=uid)

    ctx = mem.get_context("group", gid, recent_rounds=20, user_id=uid)
    # get_context 会给每条加 `[名字]: ` 前缀，故用子串判定而非集合精确匹配
    merged = "\n".join(str(c.get("content") or "") for c in ctx)
    missing = [f"消息{i}" for i in range(n) if f"消息{i}" not in merged]
    assert not missing, f"切轮丢了 {len(missing)} 条消息：{missing[:5]}"


def test_span_gate_splits_stale_round(tmp_path):
    """跨度超过 MAX_ROUND_SPAN_SEC 时必须开新轮（时间闸）。"""
    mem = _mk(tmp_path)
    gid, uid = 727631526, 10001
    key = f"group_{gid}_{uid}"

    # 造一条 2 小时前的老消息（直接塞进 round_1，模拟进程刚起来时的存量）
    old_ts = 1_000_000.0
    mem.add("group", gid, "user", "u", "老消息", user_id=uid)
    mem._store[key]["round_1"][0]["ts"] = old_ts
    mem._store[key]["round_1"][0]["time"] = old_ts

    # 现在再发一条（真实时间 = 很久之后）
    mem.add("group", gid, "user", "u", "新消息", user_id=uid)

    od = mem._store[key]
    rounds = [rk for rk, e in od.items() if e]
    assert len(rounds) >= 2, "时间闸没有生效 —— 2 小时前的消息仍与新消息同轮"
    # 新消息必须落在最新一轮，且老消息仍在老轮
    last = od[rounds[-1]]
    assert any(e["content"] == "新消息" for e in last), "新消息没进新轮"
    assert any(e["content"] == "老消息" for e in od["round_1"]), "老消息被弄丢了"


def test_span_gate_not_triggered_for_normal_chat(tmp_path):
    """正常连续对话（跨度远小于 30 分钟）不应被切碎。"""
    mem = _mk(tmp_path)
    gid, uid = 727631526, 10001
    for i in range(20):
        mem.add("group", gid, "user", f"u{i}", f"消息{i}", user_id=uid)
    od = mem._store[f"group_{gid}_{uid}"]
    filled = [rk for rk, e in od.items() if e]
    assert len(filled) == 1, f"正常对话被切成了 {len(filled)} 轮"


def test_bot_reply_still_starts_new_round(tmp_path):
    """原有行为保留：bot 回复后开新轮（一轮 = 一问一答）。"""
    mem = _mk(tmp_path)
    gid, uid = 727631526, 10001
    mem.add("group", gid, "user", "u", "问题1", user_id=uid)
    mem.add("group", gid, "bot", "Anima", "回答1", user_id=uid)
    mem.add("group", gid, "user", "u", "问题2", user_id=uid)
    mem.add("group", gid, "bot", "Anima", "回答2", user_id=uid)

    od = mem._store[f"group_{gid}_{uid}"]
    assert od["round_1"][0]["content"] == "问题1"
    assert od["round_1"][1]["content"] == "回答1"
    assert od["round_2"][0]["content"] == "问题2"
    assert od["round_2"][1]["content"] == "回答2"


def test_round_numbers_stay_continuous_after_gates(tmp_path):
    """两个闸门都用『最大轮号 +1』，不得产生编号碰撞（ANM-007 纪律）。"""
    mem = _mk(tmp_path)
    gid, uid = 727631526, 10001
    for i in range(mem.MAX_ENTRIES_PER_ROUND * 3):
        mem.add("group", gid, "user", f"u{i}", f"m{i}", user_id=uid)
    od = mem._store[f"group_{gid}_{uid}"]
    nums = sorted(int(rk[len("round_"):]) for rk in od if rk.startswith("round_"))
    assert nums == list(range(nums[0], nums[-1] + 1)), f"round 编号不连续：{nums}"
    assert len(nums) == len(set(nums)), "round 编号有重复"


def test_dirty_ts_does_not_crash_or_over_split(tmp_path):
    """ts 脏数据（None / 字符串）不得让 add 抛异常，也不得误切。"""
    mem = _mk(tmp_path)
    gid, uid = 727631526, 10001
    key = f"group_{gid}_{uid}"
    mem.add("group", gid, "user", "u", "a", user_id=uid)
    mem._store[key]["round_1"][0]["ts"] = None       # 脏
    mem.add("group", gid, "user", "u", "b", user_id=uid)   # 不得抛
    mem._store[key]["round_1"][0]["ts"] = "not-a-float"    # 更脏
    mem.add("group", gid, "user", "u", "c", user_id=uid)   # 不得抛
    assert sum(len(e) for e in mem._store[key].values()) == 3


def test_gate_constants_are_sane():
    """阈值必须落在「能消灭病态桶」和「不切碎正常对话」之间。"""
    assert 20 <= ChatMemory.MAX_ENTRIES_PER_ROUND <= 200, (
        "条数上限过小会切碎正常连聊，过大拦不住累积桶")
    assert 600 <= ChatMemory.MAX_ROUND_SPAN_SEC <= 7200, (
        "时间闸应与 memory_stage 的 30 分钟注入窗口同量级")
