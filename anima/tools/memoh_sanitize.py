"""清洗 Memoh 回复里的内部协议标记。

实测（2026-11-07）：Memoh 会把工具调用的 DSML 标记写进 assistant 消息，
例如 `<｜DSML｜｜ invoke name="exec">`。这些是内部协议，绝不该出现在：
  - QQ 私聊（用户可见）
  - Anima 的工具返回值（模型可见 ⇒ 可能原样出站）

这里用一处纯函数统一清洗，两边都调用它。
"""

from __future__ import annotations

import re

#: DSML 工具调用标签：`<｜DSML｜｜ invoke name="...">` / `<｜DSML｜｜ parameter ...>`
#: 只吃到闭合的 `>`，绝不吃掉标签后面的正文。
_DSML_TAG = re.compile(r"<｜DSML｜｜[^>\n]*>")

#: 半截标签（换行截断后没有闭合 `>`）：只吃这一小段，不吃整行。
_DSML_OPEN = re.compile(r"<｜DSML｜｜[^>\n]*")

#: 其它不该外泄的内部标记。
_NOISE = ("<tool_call>", "</tool_call>")


def sanitize(text: str) -> str:
    """把 Memoh 回复清成可以给用户看的人话。空串表示「没有可交付内容」。"""
    body = str(text or "")
    if not body.strip():
        return ""
    body = _DSML_TAG.sub("", body)
    body = _DSML_OPEN.sub("", body)
    for token in _NOISE:
        body = body.replace(token, "")
    lines = [ln.rstrip() for ln in body.splitlines() if ln.strip()]
    out = "\n".join(lines).strip()
    out = re.sub(r"[ \t]{2,}", " ", out).strip()
    if "DSML" in out or "<｜" in out:
        return ""
    return out


def is_clean(text: str) -> bool:
    """是否是一句能直接转述的人话（用于「等完整回复」的判据）。"""
    return bool(sanitize(text))