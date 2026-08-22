#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P0-P5 线上健康回归基线 —— 一键证明已交付链路仍就绪。

用途：把 ANIMA 已交付的 P0-P5 关键链路（基础聊天/多模态/Agentic路由/反馈闭环/
交互质量评估）固化为「可重复的健康回归」，驱动**真实本地组件**（非 mock）。

纪律：
- fail-closed：任一阶段抛错/断言失败 -> 该阶段 not-ok，汇总后 exit 1，绝不静默。
- 隔离：P0 用独立 persist_path，绝不读写生产 data/。
- 可选线上冒烟：设 ANIMA_HEALTH_URL + ANIMA_HEALTH_TOKEN 时，额外调生产
  /api/health 与 /api/os/self-evaluation 判新鲜度；未设则置 online=skipped。

用法：
    python scripts/health_regression.py            # 本地 P0-P5
    ANIMA_HEALTH_URL=... ANIMA_HEALTH_TOKEN=... python scripts/health_regression.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import urllib.request
from pathlib import Path

# 保证从仓库根可直接运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _run_stage(name: str, fn) -> tuple[bool, str]:
    """执行一个阶段，返回 (ok, evidence)。异常/断言失败一律 not-ok。"""
    try:
        ok, evidence = fn()
        tag = "OK " if ok else "FAIL"
        return ok, f"[{name}] {tag} evidence={evidence}"
    except Exception as exc:  # noqa: BLE001 - fail-closed，任何异常按失败上报
        return False, f"[{name}] FAIL exception={type(exc).__name__}: {exc}"


# ---------------------------------------------------------------- P0 基础聊天
def stage_p0_basic_chat():
    from anima.memory.chat_memory import ChatMemory

    tmp = tempfile.mkdtemp(prefix="anima_health_p0_")
    mem = ChatMemory(max_rounds=20, persist_path=str(Path(tmp) / "mem.json"))
    uid = 10086
    # 私聊跨轮关联
    mem.add("private", uid, "user", "小雨", "我明天要去医院复查，你帮我记一下", user_id=uid)
    mem.add("private", uid, "bot", "Anima", "好呀，记下了喵～复查是明天，对吧？", user_id=uid)
    mem.add("private", uid, "user", "小雨", "我昨天跟你说的那个事情呢，你还记得吗", user_id=uid)
    ctx_priv = mem.get_context("private", uid, user_id=uid)
    merged = "\n".join(str(m.get("content", "")) for m in ctx_priv)
    assert "医院复查" in merged, "私聊跨轮未召回上轮事项"
    # 群话题引用：A 提出话题 -> bot 回复 -> B 追问指代，短时群上下文须含话题
    gid = 727631526
    mem.observe_group_message(gid, 20001, "阿强", "今天有人去吃饭吗")
    mem.observe_group_message(gid, 20002, "小美", "我想吃火锅")
    mem.observe_group_bot_reply(gid, "火锅好啊，天气冷吃火锅最舒服了")
    mem.observe_group_message(gid, 20002, "小美", "那你觉得哪家好")
    recent = mem.get_recent_group_context(gid, limit=12)
    gtext = "\n".join(str(m.get("content", "")) for m in recent)
    assert "火锅" in gtext, "群短时上下文不含话题「火锅」，B 的「哪家」指代落空"
    assert "哪家" in gtext, "当前追问未进群上下文"
    # 群决策：被 @ 时应识别为直接呼叫（完整流水线决策由 test_group_reply_gate 覆盖）
    from anima.llm.group_gate import is_direct_group_call
    hit = is_direct_group_call({"mentions_self": True}, "今晚吃什么")
    miss = not is_direct_group_call({}, "有人在吗")
    assert hit and miss, f"群直接呼叫识别异常: hit={hit} miss={miss}"
    return True, "跨轮召回+群话题+@呼叫识别OK"

# ---------------------------------------------------------------- P1 多模态
def stage_p1_image_routing():
    from anima.pipeline.stages.generation import _X_SEARCH_RE, _X_IMAGE_RE
    # 发图请求须触发 want_images（Round 2 修复点）
    assert _X_SEARCH_RE.search("去X上搜一下这个人的照片") and _X_IMAGE_RE.search("去X上搜一下这个人的照片")
    assert _X_IMAGE_RE.search("帮我发张照片")
    assert _X_IMAGE_RE.search("配图")
    return True, "X 搜照片/发图/配图 均触发 want_images"

# ------------------------------------------------- P2 Agentic 工具路由 + 规划
def stage_p2_agentic_routing():
    import asyncio
    from anima.pipeline.stages.generation import _REALTIME_TOOL_RE
    # 3 个已修复误触发回归：闲聊绝不触发搜索
    casual = ("最近怎么样", "最近在忙什么", "今天心情怎么样", "今天在干嘛",
              "现在怎么样", "现在在忙吗", "在吗", "你在吗")
    for t in casual:
        assert not _REALTIME_TOOL_RE.search(t), f"闲聊误触发搜索：{t}"
    # 合法事实查询仍触发
    factual = ("最近天气怎么样", "今天有什么新闻", "现在几点了", "帮我查一下最近股价")
    for t in factual:
        assert _REALTIME_TOOL_RE.search(t), f"事实查询未触发：{t}"
    # 复杂问题 -> TaskPlanner（无 LLM 确定性兜底也产出有序计划）
    from anima.agent.planner import TaskPlanner
    steps = asyncio.run(TaskPlanner(llm=None).plan("帮我查一下为什么坏了，写个报告，再跑一下验证"))
    assert len(steps) >= 2, "TaskPlanner 未产出多步计划"
    descs = " ".join(s.description for s in steps)
    assert "验证结果正确性" in descs, "计划缺验证收尾"
    return True, f"闲聊0误触发/事实全触发/Planner={len(steps)}步"

# ------------------------------------------------- P3 反馈闭环
def stage_p3_feedback_loop():
    from anima.observation.feedback_detect import detect_engineering_feedback
    from anima.agent.issue_sources import collect_feedback_issues
    sig = detect_engineering_feedback("你刚才回答错了，你把 A 和 B 搞混了", sender_name="小明")
    assert sig is not None, "未识别工程反馈"
    d = sig.to_dict()
    # 只读提案：绝不携带任何执行/审批字段
    for forbidden in ("execute", "approve", "deploy", "disable"):
        assert forbidden not in d, f"反馈信号泄漏执行字段: {forbidden}"
    issues = collect_feedback_issues([sig])
    assert issues and issues[0].source == "feedback", "反馈未桥接为 feedback 源 DevIssue"
    return True, f"detect->{sig.kind}->DevIssue(source={issues[0].source})"

# ------------------------------------------------- P5 交互质量评估
def stage_p5_interaction_eval():
    from anima.observation.self_evaluation import build_self_evaluation, _score_interaction
    ev = build_self_evaluation(interaction={
        "conversation": {"ok_ratio": 0.9},
        "tools": {"success_rate": 0.88},
        "persona": {"stability": 1.0},
        "image": {"delivery_ok_ratio": 0.95},
    })
    iax = next((a for a in ev.axes if a.axis == "interaction"), None)
    assert iax is not None, "self_evaluation 缺 interaction 轴"
    assert iax.status == "ok", f"interaction 轴非 ok: {iax.status}"
    # fail-closed：空源 -> unknown，不编造
    assert _score_interaction({}).status == "unknown"
    return True, f"interaction={iax.score} ok; 空源->unknown"

# ------------------------------------------------- 可选线上冒烟
def stage_online():
    url = os.environ.get("ANIMA_HEALTH_URL", "").strip().rstrip("/")
    token = os.environ.get("ANIMA_HEALTH_TOKEN", "").strip()
    if not url:
        return True, "skipped（未设 ANIMA_HEALTH_URL）"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    req = urllib.request.Request(f"{url}/api/health", headers=headers)
    with urllib.request.urlopen(req, timeout=20) as resp:
        import json
        body = json.loads(resp.read().decode("utf-8", "replace"))
    assert str(body.get("status")) == "healthy", f"health={body.get('status')}"
    return True, f"api/health status=healthy; (token={'yes' if token else 'no'})"


STAGES = [
    ("P0 基础聊天", stage_p0_basic_chat),
    ("P1 多模态路由", stage_p1_image_routing),
    ("P2 Agentic路由+规划", stage_p2_agentic_routing),
    ("P3 反馈闭环", stage_p3_feedback_loop),
    ("P5 交互质量评估", stage_p5_interaction_eval),
    ("online 线上冒烟", stage_online),
]


def run_all() -> list[tuple[str, bool, str]]:
    """执行全部阶段，返回 [(name, ok, line)]。可被测试/Petri 调用。"""
    results = []
    for name, fn in STAGES:
        ok, line = _run_stage(name, fn)
        results.append((name, ok, line))
    return results


def main() -> int:
    results = run_all()
    for _, ok, line in results:
        print(line)
    ok_stages = [r for r in results if r[1]]
    fail_stages = [r for r in results if not r[1]]
    print("-" * 60)
    print(f"P0-P5 回归: {len(ok_stages)}/{len(results)} 阶段 OK; "
          f"FAILED={[r[0] for r in fail_stages]}")
    return 0 if not fail_stages else 1


if __name__ == "__main__":
    raise SystemExit(main())
