"""ask_user —— 让 agent 在缺信息时**结构化地问 owner**，而不是瞎猜或空转。

## 为什么需要它

生产实证（2026-10-30）：
  「帮我做个查天气的工具」→ 规划器自己假设了实现方式 → 跑偏 → 回「需要你手动提供 API Key」
  「帮我做塔罗牌工具」    → 同上 → 回「释义还在收集中」

两次都不是做不到，是**缺一个「问一句」的出口**。模型只能靠聊天文本暗示，
而文本暗示会进入正常回复流、被当成聊天内容，agent 循环并不知道「在等人回答」。

## 设计（对齐 Memoh 的 ask_user）

- **暂停执行**：任务状态置 `waiting_user`，把问题存进 `task.pending_question`。
- **结构化**：`single_select` / `multi_select` / `text` 三种，带选项。
- **允许自由输入**：选项只是快捷方式，owner 可以随便说。
- **不计入失败**：这不是任务失败，是等待人。

## 边界

- `private_only=True`：群里绝不暴露（群里问「用哪个 API」会暴露它不是人）。
- 同一任务同时只能挂一个问题，避免连珠炮。
"""

from __future__ import annotations

import logging
from typing import Any, Literal, Optional

logger = logging.getLogger("anima.agent.ask_user")

QuestionKind = Literal["single_select", "multi_select", "text"]

MAX_QUESTIONS = 4
MAX_OPTIONS = 8


class AskUserError(ValueError):
    """问题结构不合法。"""


def _norm_option(raw: Any) -> dict:
    if isinstance(raw, str):
        return {"label": raw.strip(), "description": ""}
    if isinstance(raw, dict):
        return {
            "label": str(raw.get("label") or "").strip(),
            "description": str(raw.get("description") or "").strip(),
        }
    raise AskUserError(f"选项格式不支持: {type(raw).__name__}")


def normalize_questions(raw: Any) -> list[dict]:
    """校验并规范化提问结构；不合法直接抛，不做猜测性修复。"""
    if not isinstance(raw, list) or not raw:
        raise AskUserError("questions 必须是非空数组")
    if len(raw) > MAX_QUESTIONS:
        raise AskUserError(f"一次最多 {MAX_QUESTIONS} 个问题")

    out: list[dict] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise AskUserError(f"第 {i + 1} 个问题格式不对")
        text = str(item.get("text") or "").strip()
        if not text:
            raise AskUserError(f"第 {i + 1} 个问题缺少 text")
        kind = str(item.get("kind") or "text").strip()
        if kind not in ("single_select", "multi_select", "text"):
            raise AskUserError(f"第 {i + 1} 个问题 kind 非法: {kind}")

        options = item.get("options") or []
        if kind != "text":
            if not isinstance(options, list) or not options:
                raise AskUserError(f"第 {i + 1} 个问题是选择题但没有 options")
            if len(options) > MAX_OPTIONS:
                raise AskUserError(f"第 {i + 1} 个问题选项过多（上限 {MAX_OPTIONS}）")
            options = [_norm_option(o) for o in options]
            if any(not o["label"] for o in options):
                raise AskUserError(f"第 {i + 1} 个问题有空选项")
        else:
            options = []

        out.append({
            "text": text,
            "kind": kind,
            "options": options,
            "allow_custom": bool(item.get("allow_custom", True)),
        })
    return out


def render_qq(questions: list[dict]) -> str:
    """把问题渲染成 QQ 里能读的一段话。

    编号从 1 开始，方便 owner 直接回「1」。选择题标注可否多选。
    """
    lines: list[str] = ["想请你定一下喵："]
    for qi, q in enumerate(questions, 1):
        if len(questions) > 1:
            lines.append(f"\n【问题 {qi}】")
        lines.append(q["text"])
        if q["kind"] == "multi_select":
            lines.append("（可以选多个）")
        for oi, opt in enumerate(q["options"], 1):
            desc = f" —— {opt['description']}" if opt["description"] else ""
            lines.append(f"  {oi}. {opt['label']}{desc}")
        if q["kind"] != "text" and q["allow_custom"]:
            lines.append("  （也可以直接说别的）")
    if len(questions) == 1 and questions[0]["kind"] != "text":
        lines.append("\n回个数字就行。")
    return "\n".join(lines)


def parse_answer(text: str, question: dict) -> Optional[dict]:
    """把 owner 的回复解析成结构化答案；无法判定返回 None。

    - 纯数字 → 对应选项
    - 命中选项 label → 该选项
    - 其余 → 当自由文本（allow_custom 时）
    """
    t = (text or "").strip()
    if not t:
        return None

    options = question.get("options") or []
    # 纯数字
    if t.isdigit() and options:
        idx = int(t)
        if 1 <= idx <= len(options):
            return {"kind": "option", "index": idx, "label": options[idx - 1]["label"]}

    # 多选：逗号/顿号/空格分隔的数字
    if question.get("kind") == "multi_select" and options:
        parts = [p for p in t.replace("，", ",").replace("、", ",").split(",") if p.strip()]
        picked = []
        for p in parts:
            p = p.strip()
            if p.isdigit() and 1 <= int(p) <= len(options):
                picked.append(options[int(p) - 1]["label"])
        if picked:
            return {"kind": "multi", "labels": picked}

    # 命中 label（完全匹配或包含）
    for opt in options:
        if opt["label"] and opt["label"] in t:
            return {"kind": "option", "label": opt["label"]}

    if question.get("allow_custom", True):
        return {"kind": "text", "text": t}
    return None


def resolve_reply(core: Any, text: str) -> Optional[str]:
    """owner 私聊里的一句话是否是「回答 pending question」。

    返回 None 表示与本模块无关（照常聊天）；返回字符串表示已消费并给出回执。
    这是 P1 的闭环关键：没有人来解析回复，ask_user 就只是「问了但收不到」。
    """
    current = getattr(core, "_active_agent_task", None)
    if current is None:
        return None
    pending = getattr(current, "pending_question", None) or {}
    questions = pending.get("questions") or []
    if not questions:
        return None

    q = questions[0]
    answer = parse_answer(text, q)
    if answer is None:
        return None

    current.pending_question = {}
    current.status = "executing"
    try:
        store = getattr(getattr(core, "agent", None), "store", None)
        if store is not None:
            store.save(current)
    except Exception:
        logger.debug("恢复任务状态失败（内存仍生效）", exc_info=True)

    logger.info("[ask_user] task=%s 收到回答 kind=%s",
                getattr(current, "id", "?"), answer.get("kind"))
    got = answer.get("label") or answer.get("text") or ",".join(answer.get("labels") or [])
    return f"好喵，按「{got}」来。"


def make_ask_user_tool(core: Any):
    """构造 ask_user 工具 handler。

    调用即：置任务 `waiting_user`、存问题、把渲染好的文字回给调用方。
    真正的中断与恢复由 executor 处理（它知道当前 task）。
    """
    async def _ask_user(questions: Any) -> str:
        norm = normalize_questions(questions)
        current = getattr(core, "_active_agent_task", None)
        if current is None:
            return "错误: 当前没有正在执行的任务，ask_user 无处挂载。"

        if getattr(current, "pending_question", None):
            return "错误: 上一个问题还没回答，先处理那个。"

        current.pending_question = {"questions": norm}
        current.status = "waiting_user"
        store = getattr(core, "agent", None)
        try:
            if store is not None and hasattr(store, "store"):
                store.store.save(current)
        except Exception:
            logger.debug("ask_user 持久化失败（内存仍生效）", exc_info=True)

        logger.info("[ask_user] task=%s 挂起等待回答，问题数=%d",
                    getattr(current, "id", "?"), len(norm))
        return render_qq(norm)

    return _ask_user
