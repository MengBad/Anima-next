"""unified_recall 的 chat 域必须带时间窗（2026-10-27）。

## 事故背景

`get_context` 不传 `max_age_minutes` 时返回该用户**全部存活轮次**的消息。
`unified_recall` 取 `entries[-5:]` 只保证「最后 5 条」，**不保证「最近的」**。

生产里存在 bot 长期不回复造成的累积桶：
    round_55 = 222 小时（9 天）、93 条消息
⇒ 最后 5 条可能是 9 天前的话，被当成「当前上下文」注入 ⇒ 答非所问 / 复读旧话题。

生产 `config.yaml` 已开 `memory.unified_recall_enabled: true`，所以这条路径是活的。
"""
from __future__ import annotations

import time

import pytest

from anima.memory.chat_memory import ChatMemory
from anima.pipeline.stages import _shared


class _Core:
    def __init__(self):
        self.vector_memory = None
        self.crossgroup = None
        self.episodes = None
        self.self_notes = None
        self.world_model = None


@pytest.fixture()
def mem_with_stale(monkeypatch):
    """造一个「9 天前的老消息 + 刚刚的新消息」的记忆库。"""
    m = ChatMemory(max_rounds=20, persist_path="")
    gid, uid = 727631526, 10001
    m.add("group", gid, "user", "u", "九条天前的老话题：我们在聊油库里怪谈", user_id=uid)
    # 手工把 ts 推到 9 天前
    key = f"group_{gid}_{uid}"
    old = time.time() - 9 * 24 * 3600
    m._store[key]["round_1"][0]["ts"] = old
    m._store[key]["round_1"][0]["time"] = old
    # 新消息（会产生新轮，因为时间闸）
    m.add("group", gid, "user", "u", "刚刚的新话题：今天吃什么", user_id=uid)

    monkeypatch.setattr("anima.memory.chat_memory.get_chat_memory", lambda: m)
    return m, gid, uid


@pytest.mark.asyncio
async def test_recall_excludes_stale_messages(mem_with_stale):
    """9 天前的老消息不得进入 chat 域召回结果。"""
    m, gid, uid = mem_with_stale
    out = await _shared.unified_recall(_Core(), "随便问问", group_id=gid, user_id=uid)
    chat = "\n".join(out.get("chat") or [])
    assert "油库里怪谈" not in chat, (
        f"9 天前的老消息被当成当前上下文注入了：{chat!r}")
    assert "今天吃什么" in chat, "刚刚的新消息反而没进去"


@pytest.mark.asyncio
async def test_recall_still_works_without_group(mem_with_stale):
    """无 group_id 时 chat 域为空，不得抛异常。"""
    m, gid, uid = mem_with_stale
    out = await _shared.unified_recall(_Core(), "x", group_id=None, user_id=uid)
    assert out.get("chat") == []


def test_recall_passes_window_params():
    """静态守护：调用处必须显式传 max_age_minutes。"""
    src = (_shared.__file__)
    text = open(src, encoding="utf-8").read()
    i = text.index("async def unified_recall")
    block = text[i:i + 3000]
    assert "max_age_minutes=30" in block, (
        "unified_recall 的 chat 域又没带时间窗 —— 会注入好几天前的聊天")
