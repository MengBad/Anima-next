# -*- coding: utf-8 -*-
"""Conversation Harness —— 行为级对话验收仪器（Goal：自然对话 + 上下文）。

这不是组件级单测，而是把 Goal 的 Phase B/C/D 验收场景写成「多轮真实对话脚本」，
驱动真实的 ChatMemory 上下文记忆，断言**行为级**结果：

- 私聊跨轮关联：用户追问「我昨天跟你说的那个事情呢」，上一轮内容必须仍可召回。
- 群聊 topic 引用：A 说「吃火锅」，B 追问「那你觉得哪家好」，bot 的群上下文必须含
  「火锅」这一话题线索（即「哪家」有指代落点）。
- 跨用户 / 跨群隔离：A 群/用户 A 的记忆不得泄漏进用户 B 或 B 群。
- 图片 → 上下文：发图 + 追问，视觉描述必须能让后续轮次忆起「那张图」。

设计纪律：
- 只读驱动真实组件（anima.memory.chat_memory.ChatMemory），不改生产行为。
- 每个场景返回 (passed, failures) 统计，便于 §24「95%+ 通过率」直接计数。
"""
from __future__ import annotations

from anima.memory.chat_memory import ChatMemory


def _texts(ctx: list[dict]) -> str:
    return "\n".join(str(m.get("content", "")) for m in ctx)


def _harness_private_cross_turn(mem: ChatMemory) -> list[str]:
    """Phase B：私聊跨轮上下文关联。"""
    failures: list[str] = []
    uid = 10086
    # 第 1 轮：用户提出一个具体事项
    mem.add("private", uid, "user", "小雨", "我明天要去医院复查，你帮我记一下", user_id=uid)
    mem.add("private", uid, "bot", "Anima", "好呀，记下了喵～复查是明天，对吧？", user_id=uid)
    # 第 2 轮：用户用指代「那个事情」追问
    mem.add("private", uid, "user", "小雨", "我昨天跟你说的那个事情呢，你还记得吗", user_id=uid)
    ctx = mem.get_context("private", uid, user_id=uid)
    merged = _texts(ctx)
    if "医院复查" not in merged:
        failures.append("私聊跨轮：上一轮「医院复查」内容未出现在当前上下文")
    if "那个事情" not in merged:
        failures.append("私聊跨轮：当前追问未出现在上下文")
    return failures


def _harness_group_topic_reference(mem: ChatMemory) -> list[str]:
    """Phase C/D：群聊 topic 引用（A 吃火锅 → B 那你觉得哪家好）。"""
    failures: list[str] = []
    gid = 727631526
    a_uid, b_uid = 20001, 20002
    mem.observe_group_message(gid, a_uid, "阿强", "今天有人去吃饭吗")
    mem.observe_group_message(gid, b_uid, "小美", "我想吃火锅")
    mem.observe_group_bot_reply(gid, "火锅好啊，天气冷吃火锅最舒服了")
    # B 的追问「哪家」必须能接上「火锅」话题
    mem.observe_group_message(gid, b_uid, "小美", "那你觉得哪家好")
    recent = mem.get_recent_group_context(gid, limit=12)
    merged = _texts(recent)
    if "火锅" not in merged:
        failures.append("群聊 topic：短时群上下文不含「火锅」，bot 无法理解「哪家」指代")
    if "哪家" not in merged:
        failures.append("群聊 topic：当前追问「哪家」未出现在短时群上下文")
    return failures


def _harness_cross_user_isolation(mem: ChatMemory) -> list[str]:
    """Phase C/D：用户 A 的个人记忆不得注入用户 B（防串台）。"""
    failures: list[str] = []
    gid = 727631526
    a_uid, b_uid = 30001, 30002
    # A 在群里说了一句私人信息
    mem.observe_group_message(gid, a_uid, "阿强", "我银行卡尾号是 8888，别告诉别人")
    # B 的 (群,用户) 个人桶不应包含 A 的那句私人内容
    b_ctx = mem.get_context("group", gid, recent_rounds=10, user_id=b_uid)
    b_merged = _texts(b_ctx)
    if "8888" in b_merged:
        failures.append("跨用户隔离：用户 A 的私人内容泄漏进了用户 B 的个人上下文")
    # 但群级短时上下文（bot 理解群聊用）可以看见这句（这是设计，不是泄漏）
    recent = _texts(mem.get_recent_group_context(gid, limit=20))
    if "8888" not in recent:
        failures.append("群级短时上下文应可见 A 的发言（供 bot 理解群聊），却缺失")
    return failures


def _harness_cross_group_isolation(mem: ChatMemory) -> list[str]:
    """Phase C：A 群信息不得泄漏到 B 群。"""
    failures: list[str] = []
    g1, g2 = 111111, 222222
    mem.observe_group_message(g1, 40001, "阿强", "我们群的项目代号是凤凰计划")
    g2_recent = _texts(mem.get_recent_group_context(g2, limit=20))
    if "凤凰计划" in g2_recent:
        failures.append("跨群隔离：A 群「凤凰计划」泄漏进了 B 群短时上下文")
    g2_ctx = _texts(mem.get_group_context(g2, recent_rounds=6))
    if "凤凰计划" in g2_ctx:
        failures.append("跨群隔离：A 群内容出现在 B 群聚合上下文")
    return failures


def _harness_image_then_question(mem: ChatMemory) -> list[str]:
    """Phase E：图片 → 视觉描述 → 后续轮次能忆起「那张图」。"""
    failures: list[str] = []
    uid = 50001
    mem.add("private", uid, "user", "小雨", "[用户发送了图片]", user_id=uid)
    mem.set_vision_desc("private", uid, uid, "一只趴在窗台上的橘猫")
    # 后续轮次追问「这是什么」
    mem.add("private", uid, "user", "小雨", "这是什么", user_id=uid)
    ctx = mem.get_context("private", uid, user_id=uid)
    merged = _texts(ctx)
    if "橘猫" not in merged:
        failures.append("图片上下文：视觉描述「橘猫」未注入后续上下文")
    if "这是什么" not in merged:
        failures.append("图片上下文：追问「这是什么」未出现在上下文")
    return failures


# 场景注册表：名字 -> 执行函数（按 Goal 的 Phase 顺序）。
SCENARIOS = {
    "private_cross_turn": _harness_private_cross_turn,
    "group_topic_reference": _harness_group_topic_reference,
    "cross_user_isolation": _harness_cross_user_isolation,
    "cross_group_isolation": _harness_cross_group_isolation,
    "image_then_question": _harness_image_then_question,
}


def run_conversation_harness(mem: ChatMemory | None = None) -> dict:
    """跑全部场景，返回 {name: [failures...]} 与通过率。"""
    mem = mem or ChatMemory()
    results: dict = {}
    for name, fn in SCENARIOS.items():
        results[name] = fn(mem)
    passed = sum(1 for f in results.values() if not f)
    results["_pass_rate"] = passed / len(SCENARIOS) if SCENARIOS else 1.0
    results["_passed"] = passed
    results["_total"] = len(SCENARIOS)
    return results


# ----------------------------------------------------------------------
# pytest 用例：驱动真实组件，断言行为级结果
# ----------------------------------------------------------------------

def test_harness_private_cross_turn_associates():
    failures = _harness_private_cross_turn(ChatMemory())
    assert failures == [], failures


def test_harness_group_topic_reference_resolves_anaphora():
    failures = _harness_group_topic_reference(ChatMemory())
    assert failures == [], failures


def test_harness_cross_user_isolation_no_leak():
    failures = _harness_cross_user_isolation(ChatMemory())
    assert failures == [], failures


def test_harness_cross_group_isolation_no_leak():
    failures = _harness_cross_group_isolation(ChatMemory())
    assert failures == [], failures


def test_harness_image_then_question_recalls_vision():
    failures = _harness_image_then_question(ChatMemory())
    assert failures == [], failures


def test_harness_overall_pass_rate_is_full():
    results = run_conversation_harness(ChatMemory())
    assert results["_pass_rate"] == 1.0, results
    assert results["_passed"] == results["_total"], results
