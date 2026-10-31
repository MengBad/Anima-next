"""ask_user 全链路端到端：规则注入 -> 模型选择 -> 兜底 -> 挂起 -> 恢复。

为什么写这个文件：这个问题我修了 4 次，每次打补丁就以为好了，
从没在真实链路上验证：
  1. 加 ask_user 工具           — 工具在，模型不用
  2. 加系统提示硬规则           — 规则被上下文预算裁掉（priority=3）
  3. 加反问兜底                 — tool_calls>0 就放行，盲搜后反问漏网
  4. 修否决 + 补 attach_pending — 挂起发生在出站之后，下一句已进来

本文件把整条链路拆成 5 个环节逐一钉死，任何一环退化都会红。

生产实证（2026-10-31 真实对话）：
  17:53:58 你：我去推特上找一个画师的联系方式
  17:54:01 Context system budget: dropped=ask_user_rule   <- 规则被裁
  17:54:06 它：把画师名字交出来吧                          <- 文字反问
  17:54:15 你：suzume
  17:54:41 [反问兜底] 已挂起 task=4db2d3a6a5               <- 太晚
"""
from __future__ import annotations

import pathlib
import re
from types import SimpleNamespace

import pytest

from anima.agent import ask_user as au
from anima.agent.ask_backstop import build_pending_question, is_ask_back

ROOT = pathlib.Path(__file__).resolve().parent.parent
COGNITION = ROOT / "anima" / "pipeline" / "stages" / "cognition.py"
GUARDRAIL = ROOT / "anima" / "pipeline" / "stages" / "guardrail.py"
HANDLER = ROOT / "anima" / "llm" / "handler.py"

REAL_ASK_BACK = (
    "哼，又来使唤本喵……\n\n行啦行啦，把那个画师的名字或者主页链接交出来吧，"
    "本喵这就去 X 上帮你瞅瞅能不能把联系方式翻出来喵~"
)
REAL_ASK_BACK_AFTER_SEARCH = (
    "哼，叫 suzu 的画师在 X 上还挺多……本喵刚才去翻了一下。"
    "你要找的是不是这个呀？把具体全名或者主页链接再发给本喵看看喵~"
)


# ── 环节 1：规则优先级 ────────────────────────────────────────────────
def test_rule_priority_high_enough():
    """compose_system 按 (priority, 注入序) 升序删除，数字越小越先被砍。"""
    src = COGNITION.read_text(encoding="utf-8")
    m = re.search(r'ask_user_rule",\s*_ask_rule,\s*(\d+)', src)
    assert m, "找不到 ask_user_rule 注入点"
    prio = int(m.group(1))
    assert prio >= 90, f"优先级 {prio} 太低，会被预算裁掉"


def test_rule_only_in_private():
    """规则只在私聊注入。"""
    src = COGNITION.read_text(encoding="utf-8")
    idx = src.find("ask_user_rule")
    assert idx > 0
    window = src[max(0, idx - 900):idx]
    assert "private" in window, "ask_user_rule 没有私聊隔离"


# ── 环节 2：规则内容 ──────────────────────────────────────────────────
def test_rule_text_forces_tool():
    src = COGNITION.read_text(encoding="utf-8")
    idx = src.find("_ask_rule = (")
    assert idx > 0
    body = src[idx:idx + 900]
    assert "ask_user" in body, "规则里没点名工具"
    assert "不许用文字反问" in body or "必须调用" in body


# ── 环节 3：兜底识别真实样本 ──────────────────────────────────────────
@pytest.mark.parametrize("text", [REAL_ASK_BACK, REAL_ASK_BACK_AFTER_SEARCH])
def test_backstop_catches_real_samples(text):
    assert is_ask_back(text, detail_type="private", tool_calls=0) is not None


def test_backstop_catches_after_search():
    """盲搜之后才反问也必须兜 —— 17:54 漏网的原因。"""
    assert is_ask_back(REAL_ASK_BACK_AFTER_SEARCH,
                       detail_type="private", tool_calls=1) is not None


def test_backstop_ignores_group():
    assert is_ask_back(REAL_ASK_BACK, detail_type="group", tool_calls=0) is None


def test_backstop_ignores_casual_question():
    for t in ("你是猫吗？", "今天天气怎么样？", "在吗？"):
        assert is_ask_back(t, detail_type="private", tool_calls=0) is None, t


# ── 环节 4：真的调了 attach_pending ───────────────────────────────────
def test_guardrail_calls_attach_pending():
    """只判不挂等于没修（attach_pending 曾全仓零调用）。"""
    src = GUARDRAIL.read_text(encoding="utf-8")
    assert "attach_pending" in src
    assert "clear_stale" in src


# ── 环节 5：挂起 -> 回答 -> 恢复 ──────────────────────────────────────
class _Store:
    def __init__(self):
        self.items = {}
        self.saves = 0

    def save(self, task):
        self.items[getattr(task, "id", "")] = task
        self.saves += 1


def _core():
    c = SimpleNamespace(agent=SimpleNamespace(store=_Store()))
    c._active_agent_task = None
    return c


def test_full_roundtrip_with_real_sample():
    core = _core()
    task = au.attach_pending(
        core, build_pending_question(REAL_ASK_BACK), user_id=1562290139)
    assert core._active_agent_task is task
    assert task.status == "waiting_user"
    assert core.agent.store.saves == 1, "pending 没落盘"

    out = au.resolve_reply(core, "suzume")
    assert out is not None, "owner 的回答没有被消费"
    assert "suzume" in out
    assert task.status == "executing"
    assert task.pending_question == {}


def test_resolve_is_idempotent():
    core = _core()
    au.attach_pending(core, build_pending_question(REAL_ASK_BACK))
    assert au.resolve_reply(core, "suzume") is not None
    assert au.resolve_reply(core, "随便说点什么") is None


def test_handler_consumes_before_agent():
    """resolve_reply 必须排在 agent.submit 之前，否则回答被当新任务。"""
    src = HANDLER.read_text(encoding="utf-8")
    i_ask = src.find("_au.resolve_reply")
    i_agent = src.find("self.core.agent.submit(")
    assert i_ask > 0, "handler 里没有调 resolve_reply"
    assert i_agent > 0
    assert i_ask < i_agent, "resolve_reply 排在 agent.submit 之后"


def test_pending_ttl_eventually_clears():
    """挂起不能永久存在，否则会吞掉 owner 后续正常聊天。"""
    core = _core()
    task = au.attach_pending(core, build_pending_question(REAL_ASK_BACK))
    task.pending_question["_attached_at"] = 0.0
    assert au.clear_stale(core, now=au.PENDING_TTL_SEC + 1) is True
    assert core._active_agent_task is None
