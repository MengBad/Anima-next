"""P1-C · 反问兜底：模型用文字反问时，自动转成结构化等待。

## 为什么需要它

A/B 实测（真实模型 + 60 个工具 schema）：
  原始提示   ask_user 命中 2/4
  加硬规则   ask_user 命中 3/4

即使加了硬规则，每 4 次仍有 1 次模型用文字反问。而任务丢失对 owner
是**完全无感**的 —— 它看起来就像正常聊天，回复也通顺，只是那条任务没了：

  你：帮我写个脚本监控服务器状态
  它：主人~ 这个简单！给你写了一个用 Python 写的轻量级服务...（代码在正文里，没存）
  ⇒ 你以为它在做，其实它只是说了一段话

## 判据（全部满足才转换）

1. 私聊（群里问「是谁」会暴露它不是人）
2. 本轮**零工具调用**（调了工具说明它在干活，不是干等）
3. 文本里有明确的**待答追问句式**（不是所有问号都算）
4. 句子里同时含**动作诉求**（找/查/做/写/爬/帮），否则只是闲聊

第 4 条是关键：没有它，「你是猫吗？」也会被误判成任务。
"""

from __future__ import annotations

import logging
import re
from typing import Optional

logger = logging.getLogger("anima.agent.ask_backstop")

#: 明确的待答追问句式。刻意保守 —— 宁可漏判，不要误把闲聊转成挂起。
_ASK_BACK_RE = re.compile(
    r"(告诉我|发我|给我个|说下|说一声|你是要|你要找|需要你|麻烦你给)"
    r"|(哪位|什么名字|哪个|哪一个|叫什么)"
    r"|(是吗|对吧|好不好|行吗)[？?]?"
    r"|([？?]$)"
)

#: 动作诉求。没有它就不是任务，只是聊天。
#:
#: 2026-10-31 首版太窄，实测两条真实样本都没命中：
#:   「主人是要找哪位画师呀？」        —— 意图词是「是要找」，不在原表里
#:   「给你写了一个服务，发我地址吧」  —— 意图词是「给你写」「发我」
#: 补上这些口语形态；仍保留「必须同时命中追问句式」这道联合判据，
#: 所以放宽意图词不会把闲聊误判成任务。
_TASK_INTENT_RE = re.compile(
    r"(帮我|给我|替你|替你|麻烦|去找|去查|去做|去搜|去瞅|去翻"
    r"|写个|写一|做个|做一|爬|搜|整理|监控|查一|找一"
    r"|是要找|是要查|要找|要查|给你|替你|帮本喵|要我去)"
)

#: 出现这些说明它其实已经把活干完了（贴了代码/结论），不该转挂起。
_DONE_MARKERS = (
    "```", "代码如下", "已经写好", "写好了", "结果是", "查到了", "已经查到",
)


def is_ask_back(
    text: str,
    *,
    detail_type: str,
    tool_calls: int,
) -> Optional[str]:
    """判断这条回复是不是「用文字反问、在等主人回答」。

    返回命中的理由串（供日志/审计），不命中返回 None。
    """
    if str(detail_type or "") != "private":
        return None
    if int(tool_calls or 0) > 0:
        return None          # 调了工具 = 在干活，不是干等

    t = (text or "").strip()
    if not t or len(t) > 500:
        return None          # 太长多半是完整回复，不是一句追问

    if any(m in t for m in _DONE_MARKERS):
        return None          # 已经给出产物/结论

    if not _TASK_INTENT_RE.search(t):
        return None          # 没有动作诉求 = 闲聊

    m = _ASK_BACK_RE.search(t)
    if not m:
        return None

    return f"ask_back:{m.group(0)[:10]}"


def build_pending_question(reply_text: str) -> dict:
    """把模型的文字反问转成一个 ask_user 问题结构。

    刻意用 kind=text 而不是选择题：模型原本就是在问开放问题，
    硬套选项会扭曲语义。allow_custom=True 保证主人随便说都行。
    """
    return {
        "questions": [{
            "text": (reply_text or "").strip()[:300],
            "kind": "text",
            "options": [],
            "allow_custom": True,
        }],
        "source": "ask_backstop",
    }
