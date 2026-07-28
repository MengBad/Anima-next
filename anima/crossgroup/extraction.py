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
    "你是用户事实抽取器。从下面给出的用户消息中，抽取关于【该用户】（即「用户名」字段给出的人）的"
    "【持久个人事实】——例如兴趣、身份、正在学习/做的事、长期偏好、重要背景。\n"
    "另外，如果消息里出现了【别人对该用户的称呼/外号】（如「大家都叫他企鹅仔」「胖虎来了」），"
    "提取到 nicknames 里；如果消息里提到了【该用户与另一个人的关系】（如「我和李四是室友」"
    "「她和王五是死对头」，主语必须是该用户本人），提取到 relations 里"
    "（target 写对方在消息中的称呼，relation 写关系类型）。\n"
    "【关键约束】关系抽取只针对「该用户本人」参与的关系。如果消息描述的是两个都【不是该用户】的"
    "第三方之间的关系（例如该用户说「张三和李四是室友」，而该用户既不是张三也不是李四），"
    "则【不要】抽取这条关系——那不是关于该用户本人的事实。\n"
    "不要抽取：闲聊、瞬时情绪、问候、对机器人的提问、与该用户本人无关的内容、隐私/敏感信息。\n"
    "若消息不含任何持久个人事实/外号/关系，调用 record_user_facts 并传入 facts=[]。\n"
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
                        },
                        "nicknames": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "别人对该用户的称呼/外号，如 [\"企鹅仔\",\"胖虎\"]",
                        },
                        "relations": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "target": {
                                        "type": "string",
                                        "description": "关系对方的称呼（消息中出现的形式），如「李四」",
                                    },
                                    "relation": {
                                        "type": "string",
                                        "description": "关系类型，如「室友」「情侣」「死对头」「同事」",
                                    },
                                    "note": {
                                        "type": "string",
                                        "description": "补充说明（可选）",
                                    },
                                },
                                "required": ["target", "relation"],
                            },
                        },
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
) -> dict[str, Any]:
    """从一条用户消息抽取持久个人事实、外号、人际关系。

    返回 ``{"facts": [...], "nicknames": [...], "relations": [...]}``。
    任何失败均返回全空字典（绝不抛出）。
    """
    empty = {"facts": [], "nicknames": [], "relations": []}
    text = (text or "").strip()
    if len(text) < min_len:
        return empty
    # P5: 轻量预筛——纯表情/标点/空白消息不含任何实质信息，跳过 LLM 调用。
    if not _looks_informative(text):
        return empty

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
        return empty

    if not isinstance(resp, dict):
        return empty

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
        if not isinstance(data, dict):
            continue
        return {
            "facts": _normalize_facts(data.get("facts", [])),
            "nicknames": _normalize_nicknames(data.get("nicknames", [])),
            "relations": _normalize_relations(data.get("relations", [])),
        }

    # 2) 兜底：尝试从 content 里解析 JSON（部分网关不回 tool_calls）
    content = (resp.get("content") or "").strip()
    if content:
        parsed = _try_parse_json_facts(content)
        if parsed is not None:
            return parsed

    return empty


def _looks_informative(text: str) -> bool:
    """判断文本是否可能含可抽取信息：去掉 emoji/标点/空白后是否残留实质字符。

    纯表情（😂😂）、纯标点（？？？）、纯空白等均视为无信息。
    """
    kept = []
    for ch in text:
        o = ord(ch)
        # 保留 ASCII 字母数字、CJK 统一表意文字、平假名/片假名、谚文
        if (
            (0x30 <= o <= 0x39 or 0x41 <= o <= 0x5A or 0x61 <= o <= 0x7A)
            or (0x4E00 <= o <= 0x9FFF)
            or (0x3040 <= o <= 0x30FF)
            or (0xAC00 <= o <= 0xD7AF)
        ):
            kept.append(ch)
    return len(kept) >= 2


def _normalize_nicknames(nicknames: list) -> list[str]:
    out: list[str] = []
    for nk in nicknames or []:
        if not isinstance(nk, str):
            continue
        nk = nk.strip()
        if nk and nk not in out:
            out.append(nk)
    return out


def _normalize_relations(relations: list) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in relations or []:
        if not isinstance(r, dict):
            continue
        target = (r.get("target") or "").strip()
        relation = (r.get("relation") or "").strip()
        if not target or not relation:
            continue
        out.append({
            "target": target,
            "relation": relation,
            "note": (r.get("note") or "").strip(),
        })
    return out


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


def _try_parse_json_facts(content: str) -> dict[str, Any] | None:
    """尝试从文本中提取 JSON 并解析为 facts/nicknames/relations；失败返回 None。"""
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
        return {
            "facts": _normalize_facts(data.get("facts", [])),
            "nicknames": _normalize_nicknames(data.get("nicknames", [])),
            "relations": _normalize_relations(data.get("relations", [])),
        }
    if isinstance(data, list):
        return {
            "facts": _normalize_facts(data),
            "nicknames": [],
            "relations": [],
        }
    return None
