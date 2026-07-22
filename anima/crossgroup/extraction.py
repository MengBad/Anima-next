"""每条用户消息的 LLM 结构化事实抽取。

复用 ``LLMClient.chat_structured``（OpenAI 兼容工具调用，与 ReAct 同款），
让模型把"关于该用户的持久个人事实"以 ``record_user_facts`` 工具调用返回。

设计要点：
- 仅当 ``len(text) >= min_len`` 才调 LLM，过滤 "666"/emoji/单字等无信息消息；
- ``temperature=0``、``max_tokens=256``，单次抽取 token 开销极小；
- 任何异常/空返回都静默降级为 ``[]``，**绝不中断 Pipeline 或拖慢回复**
  （调用方用 ``asyncio.create_task`` 后台跑，本函数失败不影响回复）。
"""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger("anima.crossgroup.extraction")
from anima.llm.usage import scoped_llm


EXTRACT_SYSTEM = (
    "你是用户事实抽取器。从下面给出的用户消息中，抽取关于该用户的【持久个人事实】——"
    "例如兴趣、身份、正在学习/做的事、长期偏好、重要背景。\n"
    "不要抽取：闲聊、瞬时情绪、问候、对机器人的提问、与该用户本人无关的内容、隐私/敏感信息。\n"
    "若消息不含任何持久个人事实，调用 record_user_facts 并传入 facts=[]。\n"
    "每条事实用简洁陈述句，主语默认为「该用户」（如「正在学习 Rust」）。"
)


# OpenAI 兼容工具 schema：让模型以结构化方式返回事实列表
EXTRACT_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "record_user_facts",
            "description": "从用户消息中抽取关于该用户的持久个人事实；无则返回空列表。",
            "parameters": {
                "type": "object",
                "properties": {
                    "facts": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "content": {
                                    "type": "string",
                                    "description": "事实内容，简洁陈述，如「正在学习 Rust」",
                                },
                                "tags": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "标签，如 [\"编程\",\"rust\"]",
                                },
                                "confidence": {
                                    "type": "number",
                                    "description": "置信度 0-1",
                                },
                            },
                            "required": ["content"],
                        },
                    }
                },
                "required": ["facts"],
            },
        },
    }
]


@scoped_llm("crossgroup", "extract_facts")
async def extract_user_facts(
    llm,
    text: str,
    user_name: str = "",
    min_len: int = 6,
) -> list[dict[str, Any]]:
    """从一条用户消息抽取持久个人事实。

    返回 ``[{"content": str, "tags": list[str], "confidence": float}, ...]``。
    任何失败均返回 ``[]``（绝不抛出）。
    """
    text = (text or "").strip()
    if len(text) < min_len:
        return []

    messages = [
        {"role": "system", "content": EXTRACT_SYSTEM},
        {"role": "user", "content": f"用户名：{user_name or '未知'}\n消息：{text}"},
    ]

    try:
        resp = await llm.chat_structured(
            messages, EXTRACT_TOOLS, temperature=0.0, max_tokens=256
        )
    except Exception:
        logger.exception("extract_user_facts 请求异常")
        return []

    if not isinstance(resp, dict):
        return []

    # 1) 优先解析工具调用
    tool_calls = resp.get("tool_calls", []) or []
    for tc in tool_calls:
        fn = tc.get("function", {}) if isinstance(tc, dict) else {}
        if fn.get("name") != "record_user_facts":
            continue
        args = fn.get("arguments", "{}")
        try:
            data = json.loads(args) if isinstance(args, str) else args
        except Exception:
            logger.warning("record_user_facts arguments 非法 JSON（length=%d）", len(args or ""))
            continue
        facts = data.get("facts", []) if isinstance(data, dict) else []
        return _normalize_facts(facts)

    # 2) 兜底：尝试从 content 里解析 JSON（部分网关不回 tool_calls）
    content = (resp.get("content") or "").strip()
    if content:
        parsed = _try_parse_json_facts(content)
        if parsed is not None:
            return parsed

    return []


def _normalize_facts(facts: list) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for f in facts:
        if not isinstance(f, dict):
            continue
        content = (f.get("content") or "").strip()
        if not content:
            continue
        try:
            conf = float(f.get("confidence") or 0.5)
        except (TypeError, ValueError):
            conf = 0.5
        out.append({
            "content": content,
            "tags": f.get("tags") or [],
            "confidence": conf,
        })
    return out


def _try_parse_json_facts(content: str) -> list[dict[str, Any]] | None:
    """尝试从文本中提取 JSON 并解析为 facts；失败返回 None。"""
    s = content.strip()
    # 去掉可能的 ```json ... ``` 包裹
    if s.startswith("```"):
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:]
        s = s.strip()
    try:
        data = json.loads(s)
    except Exception:
        return None
    if isinstance(data, dict):
        facts = data.get("facts", [])
    elif isinstance(data, list):
        facts = data
    else:
        return None
    return _normalize_facts(facts)
