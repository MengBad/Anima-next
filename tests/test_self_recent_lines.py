# -*- coding: utf-8 -*-
"""「自己最近说过什么」读取口（2026-10-18，D-33 / D-34 —— 已撤回 prompt 注入）。

三条生产问题共用**同一个根因**：模型看不到自己刚说过什么。
  1. 复读：10-17 23:56 / 10-18 00:32 / 06:00 三条回复同一句式骨架
     （「又丢这种…链接过来……本喵才不…，要看你自己看去喵」）。
  2. 自相矛盾：10-18 00:32「才不帮你看什么脑洞学生会」→ 06:32 议程跟进
     「勉为其难帮你瞅了一眼」。
  3. `observe_group_bot_reply` 只写 15 分钟 TTL 短时缓冲，跨小时即查不到。

修法：`ChatMemory.recent_bot_lines(group_id)` 从**持久化**的 (群,用户) 桶里
聚合 bot 自己的裸正文；`cognition` 阶段（被动回复）与 `autonomy._speak_locked`
（议程跟进）各消费一次。
"""
from __future__ import annotations

import inspect
import time

import pytest

from anima.memory.chat_memory import ChatMemory


def _mem(tmp_path) -> ChatMemory:
    return ChatMemory(persist_path=str(tmp_path / "chat_memory.json"))


def test_returns_bot_lines_only(tmp_path):
    mem = _mem(tmp_path)
    mem.add("group", 727631526, "user", "小明", "用户说的话", user_id=10001)
    mem.add("group", 727631526, "bot", "哈基米", "本喵才不帮你看呢", user_id=10001)
    lines = mem.recent_bot_lines(727631526)
    assert lines == ["本喵才不帮你看呢"], f"只应返回 bot 正文，实际 {lines}"


def test_newest_first(tmp_path):
    mem = _mem(tmp_path)
    mem.add("group", 727631526, "bot", "哈基米", "第一句", user_id=10001)
    mem.add("group", 727631526, "bot", "哈基米", "第二句", user_id=10001)
    assert mem.recent_bot_lines(727631526) == ["第二句", "第一句"]


def test_limit_is_respected(tmp_path):
    mem = _mem(tmp_path)
    for i in range(8):
        mem.add("group", 727631526, "bot", "哈基米", f"第{i}句", user_id=10001)
    assert len(mem.recent_bot_lines(727631526, limit=3)) == 3


def test_dedupes_identical_lines(tmp_path):
    mem = _mem(tmp_path)
    mem.add("group", 727631526, "bot", "哈基米", "同一句", user_id=10001)
    mem.add("group", 727631526, "bot", "哈基米", "同一句", user_id=10001)
    assert mem.recent_bot_lines(727631526) == ["同一句"]


def test_group_isolation(tmp_path):
    mem = _mem(tmp_path)
    mem.add("group", 111, "bot", "哈基米", "甲群的话", user_id=10001)
    mem.add("group", 222, "bot", "哈基米", "乙群的话", user_id=10001)
    assert mem.recent_bot_lines(111) == ["甲群的话"]
    assert mem.recent_bot_lines(222) == ["乙群的话"]


def test_old_lines_are_dropped(tmp_path):
    """超过 `max_age_sec` 的不再喂回 prompt（防止拿一周前的话当刚说的）。"""
    mem = _mem(tmp_path)
    mem.add("group", 727631526, "bot", "哈基米", "很旧的话", user_id=10001)
    for od in mem._store.values():
        for entries in od.values():
            for entry in entries:
                entry["time"] = time.time() - 3 * 86400
    assert mem.recent_bot_lines(727631526, max_age_sec=86400) == []


def test_missing_group_returns_empty(tmp_path):
    assert _mem(tmp_path).recent_bot_lines(999999) == []




# ── 撤回守卫（2026-10-18）：这两处**不得再往 prompt 喂原句** ──────────────────
# 部署后首个真实样本证明「别用这几句 + 4 份原句」反而给了模板（ratio 0.690 最高）。
# 保留取数口（供骨架检测/度量），但**禁止**把它拼进 system / user_request。

def test_cognition_no_longer_injects_self_lines():
    """🔴 用 AST 取**调用实参**，不 grep 源码 —— 10-18 已被自己的注释绊过一次
    （注释里写着 `self_recent_lines` 却并不注入 ⇒ 字符串匹配误报失败）。"""
    import ast

    from anima.pipeline.stages import cognition as c

    tree = ast.parse(inspect.getsource(c))
    keys = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = getattr(fn, "id", None) or getattr(fn, "attr", None)
        if name != "add_system_fragment":
            continue
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                keys.add(arg.value)
                break
    assert "self_recent_lines" not in keys, "cognition 不应再注入自身原句（已撤回）"


def test_autonomy_followup_no_longer_injects_prior_lines():
    from anima.core import autonomy as a

    src = inspect.getsource(a.Heartbeat._speak_locked)
    assert "recent_bot_lines(" not in src, "议程跟进不应再喂原句（已撤回）"
    assert "_prior_block" not in src


def test_guardrail_consumes_self_lines_for_detection_only():
    """取数口必须有活消费者，但只能用于**检测**（写日志），不得改写正文。"""
    from anima.pipeline.stages import guardrail as g

    src = inspect.getsource(g)
    assert "recent_bot_lines(" in src, "骨架检测需要取数口"
    assert "skeleton_duplicates(" in src
    # 只记日志：断言检测块里没有给 ctx.reply 赋值
    i = src.index("skeleton_duplicates(ctx.reply")
    j = src.index("骨架复读检测失败", i)
    block = src[i:j]
    assert "ctx.reply =" not in block, "骨架检测不得改写正文（本轮只度量）"
